"""Find accounts that may belong to one person: duplicate (sockpuppet) accounts.

A deployment that gives every account a free quota wants to know when one
person holds several. This module groups the accounts whose recent activity
says so, and explains every link it draws. It never changes an account: the
result is a report for an administrator to read before deciding anything.

**Evidence.** Two accounts are linked by what they share in the scan window:

* an address -- an IPv4 address, or an IPv6 /64, inside which a host's
  temporary privacy addresses rotate
  (:func:`serving.utils.client_addresses.address_key`) -- that both
  registered from (``signup_ip``), signed in from (``login_ip``) or sent API
  requests from (``request_ip``), or one's API traffic leaving from where the
  other registered or signs in (``cross_ip``);
* time: registering from one address within an hour of each other
  (``signup_burst``), or sending requests from one address in the same clock
  hours (``co_active``);
* identity: being signed in together on one browser of the console
  (``same_browser``), or email addresses that reach one mailbox
  (``email_alias``);
* behaviour, which only strengthens a link drawn another way: the same weekly
  rhythm of activity (``activity_profile``) and the same uncommon client
  (``same_client``).

**From links to groups.** Each kind of evidence has a weight
(:data:`SIGNAL_WEIGHTS`), counted once per pair however many addresses carry
it. Two accounts are merged only when the evidence between that one pair
reaches :data:`MERGE_THRESHOLD`, so a chain of weak links cannot fuse
strangers: no weak link merges anything on its own. A group's score is the sum
of the weights of every linked pair in it.

**Shared infrastructure.** An office, a campus, a VPN exit or a carrier NAT
puts strangers on one address. An address seen on more than
:data:`ADDRESS_FANOUT_MAX` accounts in the window links nobody. A group that
still grows past :data:`MAX_GROUP_SIZE` is split at a stricter threshold, and
whatever stays that large is reported apart as oversized rather than as one
person -- unless identity evidence alone holds it together, which is one
person with many accounts. Administrator, internal and service accounts are
never linked: they share the operator's own addresses by design. They still
count towards an address's fan-out, so an address the operator's own staff
use is treated as shared.

The pure part (:class:`DuplicateAccountScan`) needs no database and is unit
tested; :func:`scan_duplicate_accounts` reads the tables over an asyncpg
connection and runs it, for the admin endpoint.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import TYPE_CHECKING, Any

from serving.utils.client_addresses import address_key

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    import asyncpg

# --- tuning ------------------------------------------------------------------

#: The scan window, in days: the default, and the most the endpoint accepts.
DEFAULT_DAYS = 14
MAX_DAYS = 90

#: The weight of each kind of evidence, strongest first; a link lists its
#: evidence in this order. Each kind counts once per pair of accounts.
SIGNAL_WEIGHTS: dict[str, int] = {
    "same_browser": 8,
    "email_alias": 6,
    "signup_burst": 3,
    "signup_ip": 3,
    "login_ip": 3,
    "cross_ip": 3,
    "co_active": 3,
    "request_ip": 2,
    "activity_profile": 2,
    "same_client": 2,
}
#: The weight of a pair that shares every kind of evidence.
MAX_PAIR_WEIGHT = sum(SIGNAL_WEIGHTS.values())

#: The kinds drawn on an address; their evidence names the addresses.
ADDRESS_KINDS = frozenset(
    {"signup_ip", "signup_burst", "login_ip", "request_ip", "cross_ip", "co_active"}
)
#: The address kinds that say only where two accounts were, not when. A group
#: with no other evidence is as often one household or office as one person,
#: and the report marks it ``address_only``.
PRESENCE_KINDS = ADDRESS_KINDS - {"signup_burst", "co_active"}
#: The kinds that prove one person on their own; a group they hold together is
#: never set aside as shared infrastructure, however large.
IDENTITY_KINDS = frozenset({"same_browser", "email_alias"})

#: The evidence between two accounts that merges them into one group. Every
#: single address kind is below it, so an address alone links nobody: it
#: takes a second kind, such as registering close together or being active in
#: the same hours.
MERGE_THRESHOLD = 5

#: A group larger than this is split at a stricter threshold (see the module
#: docstring).
MAX_GROUP_SIZE = 10

#: An address seen on more accounts than this in the window is shared
#: infrastructure and links nobody.
ADDRESS_FANOUT_MAX = 5
#: A browser holding more accounts than this is a shared computer.
BROWSER_FANOUT_MAX = 10
#: A User-Agent more accounts than this used in the window is not uncommon.
CLIENT_FANOUT_MAX = 3

#: Requests an account must have sent from an address, or with a client,
#: before that address or client can link it.
MIN_SHARED_REQUESTS = 2

#: Registrations from one address at most this far apart are a burst.
SIGNUP_BURST_MINUTES = 60

#: Clock hours with requests from one address by both accounts that make them
#: active together.
CO_ACTIVE_MIN_HOURS = 2

#: Active hours each account needs before its weekly rhythm is compared, and
#: the cosine similarity of the two rhythms that counts as the same.
PROFILE_MIN_ACTIVE_HOURS = 10
PROFILE_MIN_SIMILARITY = 0.9

#: Roles whose accounts are never linked: the operator's own.
EXCLUDED_ROLES = frozenset({"admin", "internal"})

#: How many groups a report lists by default, and at most.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

#: The time one whole scan may take, in seconds, below the general pool's
#: per-statement timeout: past it the database cancels the statement running.
SCAN_BUDGET_SEC = 50.0

_HOURS_PER_WEEK = 7 * 24


# --- accounts and addresses --------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountRecord:
    """The fields of a ``users`` row the scan reads and reports."""

    user_id: str
    email: str | None = None
    login_name: str | None = None
    phone: str | None = None
    user_name: str | None = None
    role: str = "free"
    status: str = "active"
    created_at: datetime | None = None
    service_account: bool = False

    @property
    def linkable(self) -> bool:
        """Whether the scan may link this account: it is not one of the operator's own."""
        return not self.service_account and self.role not in EXCLUDED_ROLES


@dataclass(slots=True)
class _Presence:
    """What one account did from one address in the window."""

    signed_up_at: datetime | None = None
    logins: int = 0
    requests: int = 0

    @property
    def interactive(self) -> bool:
        return self.signed_up_at is not None or self.logins > 0


@dataclass(slots=True)
class _Clue:
    """One kind of evidence between two accounts, over every key that carries it.

    ``keys`` are the addresses, mailboxes or clients it was seen on. ``value``
    is the kind's measurement: the closest gap in minutes (``signup_burst``),
    the hours together (``co_active``), the similarity (``activity_profile``)
    or the earliest moment the two shared a browser, as a POSIX timestamp
    (``same_browser``).
    """

    kind: str
    keys: set[str] = field(default_factory=set)
    value: float | None = None


class _UnionFind:
    """Disjoint sets over account ids."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        """The representative of *item*'s set, adding it if unseen."""
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        """Merge the sets holding *a* and *b*."""
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_a] = root_b

    def groups(self) -> list[set[str]]:
        """Every set of more than one member."""
        sets: dict[str, set[str]] = defaultdict(set)
        for item in list(self._parent):
            sets[self.find(item)].add(item)
        return [members for members in sets.values() if len(members) > 1]


Pair = tuple[str, str]


def _pair(a: str, b: str) -> Pair:
    return (a, b) if a < b else (b, a)


def _components(pairs: Iterable[Pair], keep: Callable[[Pair], bool]) -> list[set[str]]:
    """The connected components over the pairs *keep* accepts."""
    sets = _UnionFind()
    for pair in pairs:
        if keep(pair):
            sets.union(*pair)
    return sets.groups()


def _utc_hour(value: datetime) -> datetime:
    """*value* as a naive UTC datetime: what ``timestamp AT TIME ZONE 'UTC'`` gives."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(minute=0, second=0, microsecond=0)


def weekly_profile(hours: Iterable[datetime]) -> list[int]:
    """Active hours per hour of the week (Monday 00:00 UTC first), from UTC hours."""
    profile = [0] * _HOURS_PER_WEEK
    for hour in hours:
        hour = _utc_hour(hour)
        profile[hour.weekday() * 24 + hour.hour] += 1
    return profile


def cosine_similarity(a: Iterable[float], b: Iterable[float]) -> float:
    """The cosine of the angle between two vectors; 0 when either is all zeros."""
    left, right = list(a), list(b)
    dot = sum(x * y for x, y in zip(left, right, strict=True))
    norm = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(y * y for y in right))
    return dot / norm if norm else 0.0


# --- the scan ----------------------------------------------------------------


class DuplicateAccountScan:
    """The links between accounts, the groups they form, and the report.

    Built in two steps, because the second needs the first. The constructor
    draws the links from addresses and identities over every account;
    :meth:`add_activity` then adds the time and behaviour evidence for the
    accounts those links reach (:attr:`candidates`), which are the only
    accounts the gather reads hour-by-hour activity for.

    Rows are plain tuples, in the order the gather's queries select them:

    * ``signups``: ``(user_id, address, registered_at)``;
    * ``logins``: ``(user_id, address, sign_ins)``, successful ones;
    * ``requests``: ``(user_id, address, requests)``, the address may be None;
    * ``browsers``: ``(browser_id, user_id, added_at)``;
    * ``mailboxes``: ``(mailbox, user_ids)``, groups of two or more.

    Signups, sign-ins and requests are the scan window's. A browser or a
    mailbox is not bound to the window, but it links accounts only when one
    of them was active in it.
    """

    def __init__(
        self,
        accounts: Mapping[str, AccountRecord],
        *,
        signups: Iterable[tuple[str, str | None, datetime]] = (),
        logins: Iterable[tuple[str, str | None, int]] = (),
        requests: Iterable[tuple[str, str | None, int]] = (),
        browsers: Iterable[tuple[str, str, datetime]] = (),
        mailboxes: Iterable[tuple[str, Iterable[str]]] = (),
    ) -> None:
        self.accounts = dict(accounts)
        self.pairs: dict[Pair, dict[str, _Clue]] = defaultdict(dict)
        #: Requests each account sent in the window, from any address.
        self.requests_by_account: dict[str, int] = defaultdict(int)
        #: Addresses and browsers set aside as shared by too many accounts.
        self.shared_addresses = 0
        self.shared_browsers = 0
        #: The linkable accounts active in the window: registered, signed in
        #: or sent a request.
        self.seen: set[str] = set()

        presences: dict[str, dict[str, _Presence]] = defaultdict(dict)

        def presence(user_id: str, raw: str | None) -> _Presence | None:
            self._saw(user_id)
            key = address_key(raw)
            if key is None:
                return None
            return presences[key].setdefault(user_id, _Presence())

        for user_id, raw, registered_at in signups:
            found = presence(user_id, raw)
            if found is not None and (
                found.signed_up_at is None or registered_at < found.signed_up_at
            ):
                found.signed_up_at = registered_at
        for user_id, raw, count in logins:
            found = presence(user_id, raw)
            if found is not None:
                found.logins += int(count)
        for user_id, raw, count in requests:
            self.requests_by_account[user_id] += int(count)
            found = presence(user_id, raw)
            if found is not None:
                found.requests += int(count)

        for key, members in presences.items():
            self._link_address(key, members)
        self._link_browsers(browsers)
        self._link_mailboxes(mailboxes)

    # -- drawing links -------------------------------------------------------

    def _saw(self, user_id: str) -> None:
        if self._linkable(user_id):
            self.seen.add(user_id)

    def _linkable(self, user_id: str) -> bool:
        account = self.accounts.get(user_id)
        return account is not None and account.linkable

    def _add(
        self, a: str, b: str, kind: str, key: str | None = None, value: float | None = None
    ) -> _Clue:
        clues = self.pairs[_pair(a, b)]
        clue = clues.get(kind)
        if clue is None:
            clue = clues[kind] = _Clue(kind)
        if key is not None:
            clue.keys.add(key)
        if value is not None:
            clue.value = value if clue.value is None else min(clue.value, value)
        return clue

    def _link_address(self, key: str, members: Mapping[str, _Presence]) -> None:
        """Link the accounts one address saw, unless too many accounts share it."""
        if len(members) > ADDRESS_FANOUT_MAX:
            self.shared_addresses += 1
            return
        for a, b in combinations(sorted(u for u in members if self._linkable(u)), 2):
            first, second = members[a], members[b]
            if first.signed_up_at is not None and second.signed_up_at is not None:
                self._add(a, b, "signup_ip", key)
                gap = abs(first.signed_up_at - second.signed_up_at) / timedelta(minutes=1)
                if gap <= SIGNUP_BURST_MINUTES:
                    self._add(a, b, "signup_burst", key, gap)
            if first.logins and second.logins:
                self._add(a, b, "login_ip", key)
            first_api = first.requests >= MIN_SHARED_REQUESTS
            second_api = second.requests >= MIN_SHARED_REQUESTS
            if first_api and second_api:
                self._add(a, b, "request_ip", key)
            elif (first_api and second.interactive) or (second_api and first.interactive):
                self._add(a, b, "cross_ip", key)

    def _link_browsers(self, browsers: Iterable[tuple[str, str, datetime]]) -> None:
        """Link the accounts signed in together on one browser."""
        by_browser: dict[str, dict[str, datetime]] = defaultdict(dict)
        for browser_id, user_id, added_at in browsers:
            by_browser[browser_id][user_id] = added_at
        for members in by_browser.values():
            if len(members) < 2:
                continue
            if len(members) > BROWSER_FANOUT_MAX:
                self.shared_browsers += 1
                continue
            if self.seen.isdisjoint(members):
                continue
            for a, b in combinations(sorted(u for u in members if self._linkable(u)), 2):
                together_since = max(members[a], members[b])
                self._add(a, b, "same_browser", value=together_since.timestamp())

    def _link_mailboxes(self, mailboxes: Iterable[tuple[str, Iterable[str]]]) -> None:
        """Link the accounts whose email addresses reach one mailbox."""
        for mailbox, user_ids in mailboxes:
            members = sorted({u for u in user_ids if self._linkable(u)})
            if len(members) < 2 or self.seen.isdisjoint(members):
                continue
            for a, b in combinations(members, 2):
                self._add(a, b, "email_alias", mailbox)

    @property
    def candidates(self) -> set[str]:
        """The accounts some link reaches: those the time and behaviour evidence reads."""
        return {user_id for pair in self.pairs for user_id in pair}

    def add_activity(
        self,
        *,
        hours: Iterable[tuple[str, str | None, datetime, int]] = (),
        clients: Iterable[tuple[str, str | None, int]] = (),
    ) -> None:
        """Add the time and behaviour evidence to the pairs already linked.

        *hours* is ``(user_id, address, utc_hour, requests)`` for the
        candidates; rows of other accounts are ignored. *clients* is
        ``(user_id, user_agent, requests)`` for every account active in the
        window, since whether a client is uncommon depends on all of them.
        """
        candidates = self.candidates
        by_address: dict[str, dict[str, set[datetime]]] = defaultdict(lambda: defaultdict(set))
        active_hours: dict[str, set[datetime]] = defaultdict(set)
        for user_id, raw, hour, _count in hours:
            if user_id not in candidates:
                continue
            hour = _utc_hour(hour)
            active_hours[user_id].add(hour)
            key = address_key(raw)
            if key is not None:
                by_address[user_id][key].add(hour)
        profiles = {
            user_id: weekly_profile(seen)
            for user_id, seen in active_hours.items()
            if len(seen) >= PROFILE_MIN_ACTIVE_HOURS
        }

        users_by_client: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for user_id, client, count in clients:
            text = (client or "").strip()
            if text and text != "-":
                users_by_client[text][user_id] += int(count)
        uncommon: dict[str, set[str]] = defaultdict(set)
        for text, users in users_by_client.items():
            if len(users) > CLIENT_FANOUT_MAX:
                continue
            for user_id, count in users.items():
                if count >= MIN_SHARED_REQUESTS and user_id in candidates:
                    uncommon[user_id].add(text)

        for (a, b), clues in list(self.pairs.items()):
            shared_api = clues.get("request_ip")
            if shared_api is not None:
                together: set[datetime] = set()
                where: set[str] = set()
                for key in shared_api.keys:
                    both = by_address[a].get(key, set()) & by_address[b].get(key, set())
                    if both:
                        together |= both
                        where.add(key)
                if len(together) >= CO_ACTIVE_MIN_HOURS:
                    clue = self._add(a, b, "co_active")
                    clue.keys |= where
                    clue.value = float(len(together))
            if a in profiles and b in profiles:
                similarity = cosine_similarity(profiles[a], profiles[b])
                if similarity >= PROFILE_MIN_SIMILARITY:
                    clue = self._add(a, b, "activity_profile")
                    clue.value = round(similarity, 3)
            common = uncommon.get(a, set()) & uncommon.get(b, set())
            if common:
                clue = self._add(a, b, "same_client")
                clue.keys |= common

    # -- grouping and the report ---------------------------------------------

    def _weights(self) -> dict[Pair, int]:
        return {
            pair: sum(SIGNAL_WEIGHTS[kind] for kind in clues) for pair, clues in self.pairs.items()
        }

    @staticmethod
    def _plausible(part: set[str], identity: set[Pair]) -> bool:
        """Whether *part* can be one person's: small enough, or held by identity alone."""
        if len(part) <= MAX_GROUP_SIZE:
            return True
        inner = [pair for pair in identity if pair[0] in part and pair[1] in part]
        return any(component == part for component in _components(inner, lambda _: True))

    def _split(
        self, members: set[str], weights: Mapping[Pair, int]
    ) -> tuple[list[set[str]], list[set[str]]]:
        """Split an oversized component: ``(plausible parts, parts still oversized)``.

        The bar rises until every part is plausible. That peels the weak
        chaining links off first, and what survives is each part's dense core,
        which is what one person's accounts look like; identity links are never
        peeled. A component that dissolves without leaving a core is returned
        whole as oversized, so a cap never reads as "nothing found".
        """
        inner = [pair for pair in self.pairs if pair[0] in members and pair[1] in members]
        identity = {pair for pair in inner if not IDENTITY_KINDS.isdisjoint(self.pairs[pair])}
        parts: list[set[str]] = []
        for threshold in range(MERGE_THRESHOLD + 1, MAX_PAIR_WEIGHT + 1):
            parts = _components(
                inner, lambda pair, bar=threshold: pair in identity or weights[pair] >= bar
            )
            if not parts:
                break
            if all(self._plausible(part, identity) for part in parts):
                return parts, []
        kept = [part for part in parts if self._plausible(part, identity)]
        loose = [part for part in parts if not self._plausible(part, identity)]
        return kept, loose or [members]

    def groups(self) -> tuple[list[set[str]], list[set[str]]]:
        """The groups of accounts, and the oversized components set aside.

        Two accounts are in one group when a chain of pairs, each with
        evidence of at least :data:`MERGE_THRESHOLD`, joins them.
        """
        weights = self._weights()
        groups: list[set[str]] = []
        oversized: list[set[str]] = []
        for members in _components(self.pairs, lambda pair: weights[pair] >= MERGE_THRESHOLD):
            if len(members) <= MAX_GROUP_SIZE:
                groups.append(members)
                continue
            kept, loose = self._split(members, weights)
            groups.extend(kept)
            oversized.extend(loose)
        return groups, oversized

    def report(
        self,
        *,
        days: int,
        since: datetime,
        min_score: int = MERGE_THRESHOLD,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """The groups worth an administrator's attention, highest score first.

        A group's score is the sum of its linked pairs' weights. Groups scoring
        below *min_score* are left out; at most *limit* are listed, and
        ``total_groups`` says how many there were.
        """
        weights = self._weights()
        groups, oversized = self.groups()
        listed: list[dict[str, Any]] = []
        for members in groups:
            inner = sorted(
                (pair for pair in self.pairs if pair[0] in members and pair[1] in members),
                key=lambda pair: (-weights[pair], pair),
            )
            score = sum(weights[pair] for pair in inner)
            if score < min_score:
                continue
            kinds = {kind for pair in inner for kind in self.pairs[pair]}
            listed.append(
                {
                    "score": score,
                    "user_ids": sorted(members),
                    "signals": [kind for kind in SIGNAL_WEIGHTS if kind in kinds],
                    # Shared addresses and nothing else, not even timing: a
                    # household or an office looks like this as often as one
                    # person does.
                    "address_only": kinds <= PRESENCE_KINDS,
                    "members": [self._member(user_id) for user_id in self._ordered(members)],
                    "links": [self._link(pair, weights[pair]) for pair in inner],
                }
            )
        listed.sort(key=lambda group: (-group["score"], -len(group["user_ids"]), group["user_ids"]))
        return {
            "days": days,
            "since": since,
            "accounts_scanned": len(self.seen),
            "shared_addresses": self.shared_addresses,
            "shared_browsers": self.shared_browsers,
            "total_groups": len(listed),
            "groups": listed[:limit],
            "oversized": [
                {"user_ids": sorted(part)}
                for part in sorted(oversized, key=lambda part: (-len(part), sorted(part)))
            ],
        }

    def _ordered(self, members: Iterable[str]) -> list[str]:
        """Oldest account first: the one the others most likely copy."""
        far_future = datetime.max.replace(tzinfo=UTC)

        def created(user_id: str) -> datetime:
            account = self.accounts.get(user_id)
            stamp = account.created_at if account is not None else None
            if stamp is None:
                return far_future
            return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)

        return sorted(members, key=lambda user_id: (created(user_id), user_id))

    def _member(self, user_id: str) -> dict[str, Any]:
        account = self.accounts.get(user_id) or AccountRecord(user_id)
        return {
            "user_id": user_id,
            "email": account.email,
            "login_name": account.login_name,
            "phone": account.phone,
            "user_name": account.user_name,
            "role": account.role,
            "status": account.status,
            "created_at": account.created_at,
            "requests": self.requests_by_account.get(user_id, 0),
        }

    def _link(self, pair: Pair, weight: int) -> dict[str, Any]:
        clues = self.pairs[pair]
        return {
            "user_ids": list(pair),
            "weight": weight,
            "evidence": [
                {
                    "kind": kind,
                    "weight": SIGNAL_WEIGHTS[kind],
                    "addresses": sorted(clues[kind].keys) if kind in ADDRESS_KINDS else [],
                    "detail": describe(clues[kind]),
                }
                for kind in SIGNAL_WEIGHTS
                if kind in clues
            ],
        }


# --- wording -----------------------------------------------------------------


def _listing(keys: Iterable[str], shown: int = 3) -> str:
    """``a``, ``a and b``, ``a, b and c``, or ``a, b, c and 2 more``."""
    items = sorted(keys)
    if len(items) > shown:
        return f"{', '.join(items[:shown])} and {len(items) - shown} more"
    if len(items) > 1:
        return f"{', '.join(items[:-1])} and {items[-1]}"
    return items[0] if items else ""


def _times(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def describe(clue: _Clue) -> str:
    """One sentence on what a piece of evidence shows, for the admin console."""
    where = _listing(clue.keys)
    value = clue.value
    if clue.kind == "same_browser":
        if value is None:
            return "Signed in together on one browser"
        since = datetime.fromtimestamp(value, UTC).date().isoformat()
        return f"Signed in together on one browser since {since}"
    if clue.kind == "email_alias":
        return f"Their email addresses reach one mailbox, {where}"
    if clue.kind == "signup_burst":
        minutes = round(value or 0)
        apart = (
            "less than a minute apart"
            if minutes < 1
            else f"{_times(minutes, 'minute', 'minutes')} apart"
        )
        return f"Registered {apart} from {where}"
    if clue.kind == "signup_ip":
        return f"Both registered from {where}"
    if clue.kind == "login_ip":
        return f"Both signed in from {where}"
    if clue.kind == "cross_ip":
        return f"One sent API requests from {where}, where the other registered or signed in"
    if clue.kind == "co_active":
        hours = int(value or 0)
        return (
            f"Sent requests from {where} in the same clock hour, {_times(hours, 'time', 'times')}"
        )
    if clue.kind == "request_ip":
        return f"Both sent API requests from {where}"
    if clue.kind == "activity_profile":
        return f"Active at the same times of the week (similarity {value or 0:.2f})"
    if clue.kind == "same_client":
        clients = sorted(clue.keys)
        if not clients:
            return "Used the same uncommon client"
        first = clients[0] if len(clients[0]) <= 80 else clients[0][:79] + "…"
        more = f" and {len(clients) - 1} more" if len(clients) > 1 else ""
        return f"Used the same uncommon client, {first}{more}"
    return clue.kind


# --- reading the database ----------------------------------------------------

#: Self-registrations in the window: email, phone and single sign-on signups
#: all audit ``create_user`` with an anonymous actor and its trusted address.
_SIGNUPS_SQL = """
    SELECT target_user_id AS user_id, source_ip AS ip, timestamp AS registered_at
    FROM admin_audit_log
    WHERE action = 'create_user'
      AND actor_type = 'anonymous'
      AND COALESCE(success, TRUE)
      AND target_user_id IS NOT NULL
      AND source_ip IS NOT NULL
      AND timestamp >= $1
"""

_LOGINS_SQL = """
    SELECT user_id, ip, count(*)::bigint AS sign_ins
    FROM login_events
    WHERE outcome = 'success'
      AND user_id IS NOT NULL
      AND ip IS NOT NULL
      AND created_at >= $1
    GROUP BY user_id, ip
"""

#: The rows a person's own traffic made: batch and cloud-agent requests leave
#: from the gateway's or the sandbox's address, and probes from the monitor's.
_OWN_TRAFFIC = """
      AND batch_job_id IS NULL
      AND agent_job_id IS NULL
      AND COALESCE(metadata->>'synthetic_probe', 'false') <> 'true'
"""

#: One pass over the window's requests, counted per account and address and
#: per account and client. ``by_client`` is 1 on the per-client rows.
_REQUESTS_SQL = f"""
    SELECT user_id,
           metadata->>'ip' AS ip,
           left(metadata->>'user_agent', 200) AS client,
           count(*)::bigint AS requests,
           GROUPING(metadata->>'ip') AS by_client
    FROM api_logs
    WHERE timestamp >= $1
      AND user_id IS NOT NULL
      {_OWN_TRAFFIC}
    GROUP BY GROUPING SETS (
        (user_id, metadata->>'ip'),
        (user_id, left(metadata->>'user_agent', 200))
    )
"""

#: The browsers holding more than one account, one of them active in the window
#: (``$1``). Not bound to the window otherwise: a row exists while the account
#: is signed in there.
_BROWSERS_SQL = """
    SELECT browser_session_id, user_id, added_at
    FROM browser_session_accounts
    WHERE browser_session_id IN (
        SELECT browser_session_id
        FROM browser_session_accounts
        GROUP BY browser_session_id
        HAVING count(*) > 1 AND bool_or(user_id = ANY($1::text[]))
    )
"""

#: Accounts whose addresses reach one mailbox, one of them active in the window
#: (``$1``): everything after a ``+`` in the local part is a tag for every
#: provider, and Gmail also ignores dots.
_MAILBOXES_SQL = """
    WITH addresses AS (
        SELECT id,
               split_part(split_part(lower(email), '@', 1), '+', 1) AS local,
               split_part(lower(email), '@', 2) AS domain
        FROM users
        WHERE email LIKE '%_@_%'
    ), mailboxes AS (
        SELECT id,
               CASE WHEN domain IN ('gmail.com', 'googlemail.com')
                    THEN replace(local, '.', '') || '@gmail.com'
                    ELSE local || '@' || domain
               END AS mailbox
        FROM addresses
        WHERE local <> '' AND domain <> ''
    )
    SELECT mailbox, array_agg(id ORDER BY id) AS user_ids
    FROM mailboxes
    GROUP BY mailbox
    HAVING count(*) > 1 AND bool_or(id = ANY($1::text[]))
"""

_USERS_SQL = """
    SELECT id, email, login_name, phone, user_name, role, status, created_at,
           is_service_account
    FROM users
    WHERE id = ANY($1::text[])
"""

#: Each candidate's requests per address and UTC clock hour; bounded by
#: ``user_id``, so it rides ``idx_api_logs_user``.
_HOURS_SQL = f"""
    SELECT user_id,
           metadata->>'ip' AS ip,
           date_trunc('hour', timestamp AT TIME ZONE 'UTC') AS hour,
           count(*)::bigint AS requests
    FROM api_logs
    WHERE user_id = ANY($1::text[])
      AND timestamp >= $2
      {_OWN_TRAFFIC}
    GROUP BY 1, 2, 3
"""


async def scan_duplicate_accounts(
    conn: asyncpg.Connection,
    *,
    days: int = DEFAULT_DAYS,
    min_score: int = MERGE_THRESHOLD,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
    budget_sec: float = SCAN_BUDGET_SEC,
) -> dict[str, Any]:
    """Scan the last *days* days for accounts that may belong to one person.

    Read-only. Reads ``admin_audit_log`` (registrations), ``login_events``,
    ``api_logs``, ``browser_session_accounts`` and ``users``, and returns
    :meth:`DuplicateAccountScan.report`. The window's requests are read in one
    pass; only the accounts a link reaches have their requests read again,
    hour by hour.

    Every statement runs in one read-only, repeatable-read transaction, so the
    scan sees one snapshot, and the whole scan is held to *budget_sec*: each
    statement may take only what is left of it. Past it, the database cancels
    the statement and ``asyncpg.QueryCanceledError`` is raised.
    """
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        fetch = _budgeted(conn, budget_sec)
        return await _gather_and_scan(
            fetch, since=since, days=days, min_score=min_score, limit=limit
        )


def _budgeted(conn: asyncpg.Connection, budget_sec: float) -> Callable[..., Any]:
    """A ``fetch`` that holds every statement to what is left of *budget_sec*.

    ``SET LOCAL`` lasts until the transaction ends, so each statement is given
    the remainder just before it runs. A statement started with less than a
    second left gets that second, and the database cancels it if it needs more.
    """
    deadline = time.monotonic() + budget_sec

    async def fetch(sql: str, *args: Any) -> list[Any]:
        remaining_ms = max(1000, int((deadline - time.monotonic()) * 1000))
        await conn.execute(f"SET LOCAL statement_timeout = {remaining_ms}")
        return await conn.fetch(sql, *args)

    return fetch


async def _gather_and_scan(
    fetch: Callable[..., Any], *, since: datetime, days: int, min_score: int, limit: int
) -> dict[str, Any]:
    signups = await fetch(_SIGNUPS_SQL, since)
    logins = await fetch(_LOGINS_SQL, since)
    requests = await fetch(_REQUESTS_SQL, since)
    # A browser or a mailbox links accounts only when one of them was active
    # in the window, so only those groups are read, and only their members'
    # rows are looked up below.
    active = sorted({row["user_id"] for row in (*signups, *logins, *requests)})
    browsers = await fetch(_BROWSERS_SQL, active)
    mailboxes = await fetch(_MAILBOXES_SQL, active)

    by_address = [row for row in requests if not row["by_client"]]
    by_client = [row for row in requests if row["by_client"]]
    user_ids = set(active).union(row["user_id"] for row in browsers)
    user_ids.update(user_id for row in mailboxes for user_id in row["user_ids"])
    rows = await fetch(_USERS_SQL, sorted(user_ids)) if user_ids else []
    accounts = {
        row["id"]: AccountRecord(
            user_id=row["id"],
            email=row["email"],
            login_name=row["login_name"],
            phone=row["phone"],
            user_name=row["user_name"],
            role=row["role"] or "free",
            status=row["status"] or "active",
            created_at=row["created_at"],
            service_account=bool(row["is_service_account"]),
        )
        for row in rows
    }

    scan = DuplicateAccountScan(
        accounts,
        signups=[(r["user_id"], r["ip"], r["registered_at"]) for r in signups],
        logins=[(r["user_id"], r["ip"], r["sign_ins"]) for r in logins],
        requests=[(r["user_id"], r["ip"], r["requests"]) for r in by_address],
        browsers=[(r["browser_session_id"], r["user_id"], r["added_at"]) for r in browsers],
        mailboxes=[(r["mailbox"], list(r["user_ids"])) for r in mailboxes],
    )
    candidates = sorted(scan.candidates)
    hours = await fetch(_HOURS_SQL, candidates, since) if candidates else []
    scan.add_activity(
        hours=[(r["user_id"], r["ip"], r["hour"], r["requests"]) for r in hours],
        clients=[(r["user_id"], r["client"], r["requests"]) for r in by_client],
    )
    return scan.report(days=days, since=since, min_score=min_score, limit=limit)
