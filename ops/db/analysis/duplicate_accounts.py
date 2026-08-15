"""Fleet-wide duplicate / sockpuppet account detection.

This is the many-account counterpart to ``compare_users.py``: instead of
assessing a pair you already suspect, it sweeps the whole user table and
reports the clusters worth a human's attention.

Why this exists in the form it does — an audit of the previous spend-seeded
detector (2026-08-15) found three structural problems, and each one is a
design constraint here:

1. **Boilerplate dominated the content signal.** ``api_logs.last_user_msg_hash``
   is computed over a payload that usually *begins* with the agent harness's
   system preamble, so "identical prompt" overwhelmingly meant "same tool",
   not "same person". Stock Codex / Hermes / OpenHands / pi preambles are
   byte-identical for every user of that tool worldwide. In 9 of 13 clusters
   every cross-account prompt edge was one of these. Here, a shared hash must
   survive :func:`classify_shared_prompt` before it can link anything.

2. **Seeding from top-N daily spenders created a large blind spot.** The
   expensive part — the platform-wide scan of request IPs — already ran every
   night; only the *anchor* set was narrow, so a low-spend or dormant alt was
   invisible no matter how strong its evidence, and the report churned day to
   day as the spend ranking moved. Seeding here defaults to every account with
   activity in the window.

3. **Only IP signals could grow a cluster.** Prompt hashes and Gmail-alias
   roots were computed *after* the candidate set was frozen, so they could
   annotate an edge but never pull an account in — two accounts provably
   sharing one Gmail mailbox went unreported for weeks. Every signal here is
   an expansion signal, with per-signal weights.

Read-only: this script never writes to the database or changes account state.

Usage:
    python ops/db/analysis/duplicate_accounts.py
    python ops/db/analysis/duplicate_accounts.py --days 60 --min-score 5
    python ops/db/analysis/duplicate_accounts.py --json --out report.json
    python ops/db/analysis/duplicate_accounts.py --ban-evasion-only
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncpg
import dotenv

if TYPE_CHECKING:
    from collections.abc import Iterable

# --------------------------------------------------------------------------
# Tuning
# --------------------------------------------------------------------------

SIGNAL_DAYS = 30
"""Trailing window for request/login signals."""

IP_FANOUT_MAX = 5
"""An exact IP used by more than this many accounts is shared infrastructure.

Above this it is a CGNAT block, an office, or a VPN exit, and linking its
members produces strangers.
"""

PREFIX_FANOUT_MAX = 3
"""IPv6 /48s are coarser than exact addresses, so they need a tighter cap."""

TUPLE_FANOUT_MAX = 4
"""Cap for a login ``(user_agent, IP)`` tuple.

Measured on production (2026-08-15): 1,235 distinct tuples, of which only 94
are shared by 2-4 accounts. The tuple is the highest-precision network signal
available, which is why it gets its own bucket rather than being folded into
the login-IP one.
"""

HASH_FANOUT_MAX = 4
"""Cap for a shared prompt hash.

Boilerplate is filtered by content (:func:`classify_shared_prompt`), but a
genuinely bespoke-looking prompt can still be a public benchmark item or a
snippet circulating on social media. Anything above this cap is treated as
public text rather than a private artifact.
"""

MIN_SHARED_REQ = 2
"""Ignore an exact-IP association backed by fewer requests than this."""

REGISTER_SLACK_DAYS = 14
"""How far *before* a suspension a successor account may have been registered.

Operators frequently pre-register the replacement while the first account is
still running, so requiring registration strictly after the suspension misses
the common case.
"""

MIN_CLUSTER_SCORE = 5
"""Report threshold for a whole cluster."""

MERGE_THRESHOLD = 5
"""Pairwise evidence weight required before two accounts are merged.

This is the brake on transitive chaining. Below it, a lone shared request IP
or login IP cannot pull two accounts together, so a chain of individually
plausible weak edges cannot fuse unrelated people into one component.
"""

MAX_CLUSTER_SIZE = 10
"""Above this, a component is shared infrastructure, not a ring.

Reported separately rather than dropped — a silent cap reads as "we looked and
found nothing".
"""

# Signal weights. Identity evidence outranks network evidence because a
# network can be shared innocently (household, lab, VPN) while a self-chosen
# handle appearing on two accounts cannot.
W_REQ_IP = 3
W_REQ_PREFIX = 2
W_LOGIN_IP = 3
W_LOGIN_TUPLE = 5  # measured high-precision: 94 of 1,235 tuples are shared at all
W_CROSS_IP = 4
W_GMAIL_ALIAS = 8
W_HANDLE = 6
W_BESPOKE_PROMPT = 5
W_SIGNUP_REASON = 3

CORROBORATION_REQUIRED = {"req_ip", "req_prefix"}
"""Signals that may not open a case on their own.

Request-IP overlap alone manufactured three of thirteen clusters in the audit
(Cloudflare WARP, a rotating commercial VPN pool, and a university egress).
A cluster whose every edge is drawn from this set is dropped.
"""

# Egress ranges that must never *originate* an edge. These are shared exits:
# two accounts appearing on one of them have shown you nothing. They are still
# reported as corroboration when some other signal already links the accounts.
KNOWN_EGRESS_CIDRS = (
    # Cloudflare WARP
    "104.28.0.0/16",
    "2a09:bac0::/29",
    # Commercial VPN exits seen linking unrelated accounts in the 2026-08 audit
    "217.138.0.0/16",
    "193.108.118.0/24",
    "31.171.100.0/22",
    "205.252.135.0/24",
    "104.167.26.0/23",
    # Institutional egress (operator + collaborators)
    "140.247.0.0/16",
    "2607:fb60::/32",
    "128.2.0.0/16",
)

OPERATOR_ROLES = frozenset({"admin", "internal"})
"""Roles excluded from clustering.

The operator's own fleet shares an office IP and an in-house harness preamble,
so it self-clusters every sweep and drags in any customer who happens to touch
the same probe prompt.
"""

# Harness preambles observed carrying cross-account edges. This list is a
# fallback: `classify_shared_prompt` is structural first, and only consults
# these when the structure is ambiguous.
KNOWN_PREAMBLE_MARKERS = (
    "you are codex",
    "you are a coding agent running in the codex cli",
    "you are hermes agent, an intelligent ai assistant created by nous research",
    "<soul>",
    "you are openhands agent",
    "you are an expert coding assistant operating inside pi",
    "you are a title generator",
    "you are a powerful assistant mastering all kinds of tasks operating in traework",
    "<system-reminder>",
    "you are agent '",
    "you are a helpful assistant",
)

SIGNUP_FORM_ECHO = re.compile(r"how did you find\s+\S+\s*\?.*$", re.IGNORECASE | re.DOTALL)
"""The signup form echoes its own referral question into ``signup_reason``.

Left in, it makes every applicant who picked "friend" look like the same
person — the largest apparent match on production is ten unrelated accounts
sharing exactly that trailer and nothing else.
"""

MIN_SIGNUP_REASON_CHARS = 25
MAX_SIGNUP_REASON_GAP = timedelta(hours=6)

MIN_BESPOKE_SYSTEM_CHARS = 400
"""Length below which a system-only prompt is treated as a generic instruction.

Personal "soul files" are long — the one that identified the 2026-08-15
continuation case runs to several kilobytes and names its owner. A one-line
system string is a config default that many people will land on independently.
"""


# --------------------------------------------------------------------------
# Pure helpers (no database access — these are what the unit tests drive)
# --------------------------------------------------------------------------


def is_public_ip(ip: str) -> bool:
    """True for a globally routable address."""
    if not ip:
        return False
    ip = ip.split(",")[0].strip()
    try:
        obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return obj.is_global and not obj.is_private


def ipv6_prefix48(ip: str) -> str | None:
    """Return the /48 of an IPv6 address, or None for IPv4 / unparseable input.

    Rotating RFC-4941 privacy addresses change the low bits every few hours, so
    the exact address stops matching while the /48 keeps identifying the site.
    """
    try:
        obj = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if obj.version != 6:
        return None
    return str(ipaddress.ip_network(f"{obj}/48", strict=False).network_address)


_EGRESS_NETS = tuple(ipaddress.ip_network(c) for c in KNOWN_EGRESS_CIDRS)


def is_known_egress(ip: str) -> bool:
    """True if the address sits in a shared exit that must not originate an edge."""
    try:
        obj = ipaddress.ip_address(ip.split(",")[0].strip())
    except ValueError:
        return False
    return any(obj.version == net.version and obj in net for net in _EGRESS_NETS)


def canonical_gmail(email: str) -> str | None:
    """Canonical Gmail mailbox for an address, or None if not Gmail.

    Gmail ignores dots and everything after a ``+`` in the local part, so
    ``sisa.tmp@`` and ``si.sa.tmp@`` are provably one mailbox rather than two
    accounts that merely look alike.
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    local, _, domain = email.partition("@")
    if domain not in ("gmail.com", "googlemail.com"):
        return None
    local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@gmail.com" if local else None


def normalize_handle(value: str) -> str:
    """Case-fold and strip non-alphanumerics, so ``B-A-M-N`` == ``BAMN``."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def email_localpart(email: str) -> str:
    """Normalized local part of an address."""
    return normalize_handle((email or "").split("@", 1)[0])


def normalize_signup_reason(reason: str) -> str:
    """Strip the form's echoed referral question and collapse whitespace.

    Returns an empty string when nothing substantive remains, which is the
    common case and must not be treated as a match.
    """
    text = SIGNUP_FORM_ECHO.sub("", reason or "")
    return re.sub(r"\s+", " ", text).strip().lower()


def _iter_message_dicts(payload: Any) -> Iterable[dict]:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
    elif isinstance(payload, dict):
        for key in ("messages", "input"):
            if isinstance(payload.get(key), list):
                for item in payload[key]:
                    if isinstance(item, dict):
                        yield item


def _content_text(content: Any) -> str:
    """Flatten a message content field, which may be a string or a part list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


_ROLE_RE = re.compile(r'"role"\s*:\s*"(?P<role>[a-zA-Z_]+)"')
_CONTENT_RE = re.compile(r'"(?:text|content)"\s*:\s*"(?P<text>(?:[^"\\]|\\.){0,4000})"')


def salvage_truncated_messages(text: str) -> list[tuple[str, str]]:
    """Extract ``(role, text)`` pairs from a possibly truncated JSON message array.

    Prompt samples are fetched with ``substr``/``right`` to keep the TOAST read
    partial — agent payloads run to hundreds of kilobytes — so the sample is
    usually not valid JSON. Classification only needs to know whether a
    substantive non-system turn exists, which survives a sloppy parse.
    """
    out: list[tuple[str, str]] = []
    marks = list(_ROLE_RE.finditer(text))
    for index, match in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        segment = text[match.end() : end]
        body = " ".join(m.group("text") for m in _CONTENT_RE.finditer(segment))
        out.append((match.group("role").lower(), body))
    return out


def classify_shared_prompt(prompt: str | None) -> tuple[bool, str]:
    """Decide whether a shared prompt is bespoke content or harness boilerplate.

    Returns ``(is_bespoke, reason)``. Only bespoke prompts may link accounts.

    Two kinds of prompt are bespoke, and the second is easy to lose:

    * a substantive **user** turn — someone's actual work;
    * a personal **system** prompt. A hand-written "soul file" naming its owner
      ("# Rabiu's Operating System … the operator of Knife Agency") is the
      single strongest content signal there is, and treating every system turn
      as vendor text discards it. Telling a personal system prompt from a
      vendor preamble is a fan-out question, not a structural one — a vendor
      preamble is shared by every user of that tool, so :data:`HASH_FANOUT_MAX`
      is what separates them, backed by the marker list for the common tools.
    """
    if not prompt:
        return False, "empty prompt"

    turns: list[tuple[str, str]] = []
    try:
        payload = json.loads(prompt)
    except (TypeError, ValueError):
        payload = None

    if payload is not None:
        for message in _iter_message_dicts(payload):
            turns.append(
                (str(message.get("role", "")).lower(), _content_text(message.get("content")))
            )
    elif '"role"' in prompt:
        # A truncated sample of a JSON message array.
        turns = salvage_truncated_messages(prompt)
    else:
        # Not JSON at all — treat the raw text as a single user turn.
        text = prompt.strip()
        if len(text) < 40:
            return False, "raw prompt too short to be distinguishing"
        if any(m in text.lower() for m in KNOWN_PREAMBLE_MARKERS):
            return False, "raw prompt matches a known harness preamble"
        return True, "raw prompt with substantive text"

    user_text = [text for role, text in turns if role not in ("system", "developer") and text]
    system_text = [text for role, text in turns if role in ("system", "developer") and text]

    joined_user = re.sub(r"\s+", " ", " ".join(user_text)).strip()
    joined_system = re.sub(r"\s+", " ", " ".join(system_text)).strip()

    # A recognised vendor preamble is boilerplate no matter what follows it,
    # unless the user turn itself carries real work.
    vendor = any(marker in joined_system.lower() for marker in KNOWN_PREAMBLE_MARKERS)

    if len(joined_user) >= 40:
        return True, f"bespoke user content ({len(joined_user)} chars)"

    if joined_user:
        return False, f"user turn too short to distinguish ({joined_user[:40]!r})"

    # System-only payload. This is either a vendor preamble shared by everyone
    # using that tool, or a personal soul file shared by one operator's
    # accounts. The marker list catches the tools we know; for the rest, the
    # caller's fan-out cap is what decides.
    if vendor:
        return False, "known vendor preamble with no user turn"
    if len(joined_system) < MIN_BESPOKE_SYSTEM_CHARS:
        return (
            False,
            f"system-only prompt too short to be a personal preamble ({joined_system[:40]!r})",
        )
    return True, f"bespoke system prompt ({len(joined_system)} chars, no known vendor marker)"


class UnionFind:
    """Disjoint-set over account ids."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        """Return the representative of ``item``'s set, adding it if unseen."""
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:  # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        """Merge the sets containing ``a`` and ``b``."""
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def groups(self) -> dict[str, set[str]]:
        """Return representative -> members, for sets with more than one member."""
        out: dict[str, set[str]] = defaultdict(set)
        for item in list(self.parent):
            out[self.find(item)].add(item)
        return {k: v for k, v in out.items() if len(v) > 1}


@dataclass
class Edge:
    """One piece of evidence linking two accounts."""

    kind: str
    detail: str
    weight: int


@dataclass
class Bucket:
    """A shared key and the accounts that touched it."""

    kind: str
    key: str
    members: set[str]
    weight: int
    detail: str = ""


@dataclass
class Account:
    """The account fields clustering and reporting need."""

    user_id: str
    email: str
    user_name: str
    status: str
    role: str
    created_at: datetime
    signup_reason: str = ""
    spend: float = 0.0
    reqs: int = 0
    suspended_at: datetime | None = None
    first_request_at: datetime | None = None


def build_buckets(
    accounts: dict[str, Account],
    req_ips: dict[str, dict[str, int]],
    login_ips: dict[str, set[str]],
    login_tuples: dict[tuple[str, str], set[str]],
    prompt_hashes: dict[str, set[str]],
) -> list[Bucket]:
    """Turn raw signal maps into low-fan-out buckets that may link accounts.

    Every bucket here is an *expansion* signal. Fan-out caps are what separate
    a real association from shared infrastructure, and a known shared-egress
    address is dropped outright rather than merely down-weighted.
    """
    buckets: list[Bucket] = []

    for ip, members in req_ips.items():
        eligible = {u for u, n in members.items() if n >= MIN_SHARED_REQ and u in accounts}
        if not 2 <= len(eligible) <= IP_FANOUT_MAX or is_known_egress(ip):
            continue
        buckets.append(Bucket("req_ip", ip, eligible, W_REQ_IP, f"{len(eligible)} accounts"))

    prefixes: dict[str, set[str]] = defaultdict(set)
    for ip, members in req_ips.items():
        prefix = ipv6_prefix48(ip)
        if prefix and not is_known_egress(ip):
            prefixes[prefix] |= {u for u in members if u in accounts}
    for prefix, members in prefixes.items():
        if 2 <= len(members) <= PREFIX_FANOUT_MAX:
            buckets.append(Bucket("req_prefix", prefix, members, W_REQ_PREFIX, "IPv6 /48"))

    for ip, members in login_ips.items():
        members = {u for u in members if u in accounts}
        if 2 <= len(members) <= IP_FANOUT_MAX and not is_known_egress(ip):
            buckets.append(Bucket("login_ip", ip, members, W_LOGIN_IP))

    for (agent, ip), members in login_tuples.items():
        members = {u for u in members if u in accounts}
        if 2 <= len(members) <= TUPLE_FANOUT_MAX and not is_known_egress(ip):
            buckets.append(Bucket("login_tuple", f"{ip} :: {agent[:60]}", members, W_LOGIN_TUPLE))

    buckets.extend(prompt_buckets(accounts, prompt_hashes))
    buckets.extend(identity_buckets(accounts))
    return buckets


def prompt_buckets(
    accounts: dict[str, Account], prompt_hashes: dict[str, set[str]]
) -> list[Bucket]:
    """Buckets from shared prompt hashes that already passed the bespoke filter."""
    buckets = []
    for digest, members in prompt_hashes.items():
        shared = {u for u in members if u in accounts}
        # Even bespoke-looking text shared by many accounts is a public
        # benchmark or a circulated snippet, not a private artifact.
        if 2 <= len(shared) <= HASH_FANOUT_MAX:
            buckets.append(
                Bucket("bespoke_prompt", digest, shared, W_BESPOKE_PROMPT, "non-boilerplate")
            )
    return buckets


def identity_buckets(accounts: dict[str, Account]) -> list[Bucket]:
    """Buckets from self-chosen identity: Gmail root, handle, and signup text.

    Handle matching deliberately compares each ``user_name`` against *other*
    accounts' email local parts. Self-matches (``shivv`` inside ``shivansg``)
    are noise, and fuzzy-matching two different given names across accounts
    produced the worst overreach in the audit, so neither is done.
    """
    buckets: list[Bucket] = []

    by_gmail: dict[str, set[str]] = defaultdict(set)
    for account in accounts.values():
        root = canonical_gmail(account.email)
        if root:
            by_gmail[root].add(account.user_id)
    for root, members in by_gmail.items():
        if len(members) >= 2:
            buckets.append(
                Bucket("gmail_alias", root, members, W_GMAIL_ALIAS, "same Gmail mailbox")
            )

    by_handle: dict[str, set[str]] = defaultdict(set)
    for account in accounts.values():
        handle = normalize_handle(account.user_name)
        if len(handle) >= 4:
            by_handle[handle].add(account.user_id)
    for handle, members in by_handle.items():
        if len(members) >= 2:
            buckets.append(Bucket("handle", handle, members, W_HANDLE, f"user_name {handle!r}"))

    # user_name of one account == email local part of another.
    for account in accounts.values():
        handle = normalize_handle(account.user_name)
        if len(handle) < 5:
            continue
        for other in accounts.values():
            if other.user_id == account.user_id:
                continue
            if handle == email_localpart(other.email):
                buckets.append(
                    Bucket(
                        "handle_localpart",
                        f"{handle}:{other.user_id}",
                        {account.user_id, other.user_id},
                        W_HANDLE,
                        f"user_name {handle!r} is the local part of {other.email}",
                    )
                )

    by_reason: dict[str, list[Account]] = defaultdict(list)
    for account in accounts.values():
        reason = normalize_signup_reason(account.signup_reason)
        if len(reason) >= MIN_SIGNUP_REASON_CHARS:
            by_reason[reason].append(account)
    for reason, group in by_reason.items():
        if len(group) < 2:
            continue
        # Identical wording only counts when the registrations are close
        # together; the same sentence months apart is a common applicant
        # phrasing, not one hand.
        group.sort(key=lambda a: a.created_at)
        span = group[-1].created_at - group[0].created_at
        if span <= MAX_SIGNUP_REASON_GAP:
            buckets.append(
                Bucket(
                    "signup_reason",
                    reason[:60],
                    {a.user_id for a in group},
                    W_SIGNUP_REASON,
                    f"identical wording, registered within {span}",
                )
            )
    return buckets


def pair_weight(edges: list[Edge]) -> int:
    """Weight of one pair's evidence, counting each signal kind at most once.

    Six addresses in one ISP's dynamic pool are one fact about that pool, not
    six independent facts about the accounts.
    """
    seen: set[str] = set()
    total = 0
    for edge in edges:
        if edge.kind not in seen:
            seen.add(edge.kind)
            total += edge.weight
    return total


def cluster(
    buckets: list[Bucket],
    merge_threshold: int | None = None,
    max_cluster_size: int | None = None,
) -> tuple[dict[str, set[str]], dict[tuple[str, str], list[Edge]], list[set[str]]]:
    """Group accounts, returning ``(clusters, pairwise evidence, oversized blobs)``.

    Two accounts are merged only when the evidence *between that pair* clears
    ``merge_threshold``. Transitively closing over every individual bucket —
    which is what the previous detector did — lets one weak edge chain into the
    next until unrelated people fuse: on production this produced a single
    30-account component spanning four countries, held together by a chain of
    individually-plausible IP edges.

    A component larger than ``max_cluster_size`` is still shared infrastructure
    that slipped the per-bucket caps, so it is returned separately rather than
    reported as a ring — and never silently dropped.
    """
    merge_threshold = MERGE_THRESHOLD if merge_threshold is None else merge_threshold
    max_cluster_size = MAX_CLUSTER_SIZE if max_cluster_size is None else max_cluster_size

    edges: dict[tuple[str, str], list[Edge]] = defaultdict(list)
    for bucket in buckets:
        members = sorted(bucket.members)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                edges[(members[i], members[j])].append(
                    Edge(bucket.kind, bucket.detail or bucket.key, bucket.weight)
                )

    groups: dict[str, set[str]] = {}
    oversized: list[set[str]] = []
    for members in _components(edges, edges.keys(), merge_threshold):
        if len(members) <= max_cluster_size:
            groups[min(members)] = members
        else:
            # Do not throw the component away: a genuine two-account ring is
            # often inside it, chained to strangers through one hub. Tighten
            # the bar on this component alone until the parts are plausible.
            kept, blob = _split_oversized(members, edges, merge_threshold, max_cluster_size)
            for part in kept:
                groups[min(part)] = part
            oversized.extend(blob)
    return groups, edges, oversized


def _components(
    edges: dict[tuple[str, str], list[Edge]],
    pairs: Iterable[tuple[str, str]],
    threshold: int,
) -> list[set[str]]:
    """Connected components over the pairs whose evidence clears ``threshold``."""
    uf = UnionFind()
    for pair in pairs:
        if pair_weight(edges[pair]) >= threshold:
            uf.union(*pair)
    return list(uf.groups().values())


def _split_oversized(
    members: set[str],
    edges: dict[tuple[str, str], list[Edge]],
    threshold: int,
    max_cluster_size: int,
    ceiling: int = 20,
) -> tuple[list[set[str]], list[set[str]]]:
    """Raise the bar within one component until its parts are plausible sizes.

    Returns ``(usable parts, still-oversized parts)``. Raising the threshold
    peels the weak chaining edges away first, so what survives is the dense
    core — which is what a real ring looks like.
    """
    inner = [p for p in edges if p[0] in members and p[1] in members]
    for tighter in range(threshold + 1, ceiling + 1):
        parts = _components(edges, inner, tighter)
        if not parts:
            break
        if all(len(p) <= max_cluster_size for p in parts):
            return parts, []
    parts = _components(edges, inner, ceiling)
    return (
        [p for p in parts if len(p) <= max_cluster_size],
        [p for p in parts if len(p) > max_cluster_size] or [members],
    )


def score_cluster(
    members: set[str], edges: dict[tuple[str, str], list[Edge]]
) -> tuple[int, list[Edge]]:
    """Total evidence weight for a cluster, counting each signal kind once per pair."""
    total, collected = 0, []
    ordered = sorted(members)
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            pair_edges = edges.get((ordered[i], ordered[j]), [])
            collected.extend(pair_edges)
            total += pair_weight(pair_edges)
    return total, collected


def cluster_is_corroborated(edge_kinds: set[str]) -> bool:
    """False when every edge comes from a signal that may not open a case."""
    return bool(edge_kinds - CORROBORATION_REQUIRED)


def detect_ban_evasion(
    members: set[str],
    accounts: dict[str, Account],
    edges: dict[tuple[str, str], list[Edge]] | None = None,
) -> list[dict[str, str]]:
    """Find suspended -> active continuations inside a cluster.

    The label carries a hard precondition, because the previous audit applied
    it to two clusters that did not meet one: there must be a real suspension,
    and the successor's **first request** — not its registration — must fall
    after that suspension. Operators routinely pre-register a replacement, and
    throttling is capacity management, not enforcement.

    When ``edges`` is supplied the pair must also be *directly* linked. Cluster
    co-membership is transitive, so without this every account in a cluster is
    reported as the successor of every suspended account in it.
    """
    findings = []
    suspended = [a for a in (accounts[m] for m in members) if a.suspended_at]
    for dead in suspended:
        for candidate in (accounts[m] for m in members):
            if candidate.user_id == dead.user_id or candidate.status != "active":
                continue
            if edges is not None:
                key = tuple(sorted((dead.user_id, candidate.user_id)))
                if pair_weight(edges.get(key, [])) < MERGE_THRESHOLD:
                    continue
            if candidate.created_at < dead.suspended_at - timedelta(days=REGISTER_SLACK_DAYS):
                continue
            if candidate.first_request_at is None:
                continue
            if candidate.first_request_at <= dead.suspended_at:
                continue
            findings.append(
                {
                    "suspended": dead.email,
                    "suspended_at": dead.suspended_at.isoformat(),
                    "successor": candidate.email,
                    "successor_first_request": candidate.first_request_at.isoformat(),
                }
            )
    return findings


# --------------------------------------------------------------------------
# Database access
# --------------------------------------------------------------------------


def _load_env(env_path: str | None = None) -> None:
    candidates = [
        env_path,
        os.environ.get("ENV_FILE"),
        "/srv/hybridInference/.env",
        str(Path(__file__).resolve().parents[3] / ".env"),
    ]
    for path in candidates:
        if path and Path(path).is_file():
            dotenv.load_dotenv(path, override=False)
            return


def _dsn() -> str:
    user = os.environ.get("DB_USER")
    if not user:
        raise SystemExit(
            "ERROR: DB_USER is not set. Load the .env file or set the environment variable."
        )
    return (
        f"postgresql://{user}:{os.environ.get('DB_PASSWORD', '')}"
        f"@{os.environ.get('DB_HOST', 'localhost')}:{os.environ.get('DB_PORT', '5432')}"
        f"/{os.environ.get('DB_NAME', 'freeinference_db')}"
    )


async def fetch_accounts(conn: asyncpg.Connection, days: int) -> dict[str, Account]:
    """Load candidate accounts, excluding the operator's own fleet."""
    rows = await conn.fetch(
        """
        SELECT u.id, u.email, coalesce(u.user_name,'') AS user_name, u.status, u.role,
               u.created_at, coalesce(u.signup_reason,'') AS signup_reason
        FROM users u
        WHERE u.status IN ('active','suspended') AND u.role <> ALL($1::text[])
        """,
        list(OPERATOR_ROLES),
    )
    accounts = {
        r["id"]: Account(
            user_id=r["id"],
            email=r["email"],
            user_name=r["user_name"],
            status=r["status"],
            role=r["role"],
            created_at=r["created_at"],
            signup_reason=r["signup_reason"],
        )
        for r in rows
    }

    for row in await conn.fetch(
        """
        SELECT user_id, round(coalesce(sum(cost_usd),0),2)::float8 AS spend,
               coalesce(sum(requests),0)::bigint AS reqs
        FROM user_daily_cost
        WHERE day::date >= current_date - $1::int
        GROUP BY 1
        """,
        days,
    ):
        if row["user_id"] in accounts:
            accounts[row["user_id"]].spend = row["spend"]
            accounts[row["user_id"]].reqs = row["reqs"]

    # Suspension timestamps come from the audit log; `users` has no such column.
    for row in await conn.fetch(
        """
        SELECT DISTINCT ON (target_user_id) target_user_id AS uid, timestamp AS ts
        FROM admin_audit_log
        WHERE action = 'update_user' AND success
          AND details->'values'->>'status' = 'suspended'
        ORDER BY target_user_id, timestamp DESC
        """
    ):
        account = accounts.get(row["uid"])
        if account and account.status == "suspended":
            account.suspended_at = row["ts"]
    return accounts


async def fetch_network_signals(
    conn: asyncpg.Connection, days: int
) -> tuple[dict[str, dict[str, int]], dict[str, set[str]], dict[tuple[str, str], set[str]]]:
    """Load request IPs, login IPs and login (UA, IP) tuples for the window."""
    req_ips: dict[str, dict[str, int]] = defaultdict(dict)
    for row in await conn.fetch(
        """
        SELECT user_id, metadata->>'ip' AS ip, count(*)::bigint AS n
        FROM api_logs
        WHERE timestamp >= now() - make_interval(days => $1::int)
          AND user_id IS NOT NULL
          AND metadata->>'ip' IS NOT NULL AND metadata->>'ip' <> ''
        GROUP BY 1, 2
        """,
        days,
    ):
        ip = (row["ip"] or "").split(",")[0].strip()
        if is_public_ip(ip):
            req_ips[ip][row["user_id"]] = req_ips[ip].get(row["user_id"], 0) + row["n"]

    login_ips: dict[str, set[str]] = defaultdict(set)
    login_tuples: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in await conn.fetch(
        """
        SELECT user_id, ip, coalesce(user_agent,'') AS ua
        FROM login_events
        WHERE outcome = 'success' AND ip IS NOT NULL AND ip <> ''
          AND created_at >= now() - make_interval(days => $1::int)
        GROUP BY 1, 2, 3
        """,
        days,
    ):
        ip = (row["ip"] or "").strip()
        if not is_public_ip(ip):
            continue
        login_ips[ip].add(row["user_id"])
        # Login UAs are stored truncated near 100 chars, so the string alone
        # collapses every Windows Chrome build together. Only the tuple is used.
        if row["ua"]:
            login_tuples[(row["ua"], ip)].add(row["user_id"])
    return req_ips, login_ips, login_tuples


def cross_ip_buckets(
    req_ips: dict[str, dict[str, int]],
    login_ips: dict[str, set[str]],
    accounts: dict[str, Account],
) -> list[Bucket]:
    """Link an account whose API traffic egresses an address another logs in from.

    The old detector compared login-IPs to login-IPs and request-IPs to
    request-IPs, never across. That is how the single most incriminating link
    in the 2026-08-15 cluster 13 stayed invisible: the pack reported
    ``shared login IPs: []`` while one account's entire API traffic left from
    an address the other signs in on.
    """
    buckets = []
    for ip, requesters in req_ips.items():
        owners = login_ips.get(ip, set())
        if not owners or is_known_egress(ip):
            continue
        members = {u for u in requesters if u in accounts} | {u for u in owners if u in accounts}
        if 2 <= len(members) <= IP_FANOUT_MAX:
            buckets.append(
                Bucket("cross_ip", ip, members, W_CROSS_IP, "request egress == another's login IP")
            )
    return buckets


async def fetch_bespoke_prompt_hashes(
    conn: asyncpg.Connection, candidates: set[str], days: int, max_hashes: int = 4000
) -> dict[str, set[str]]:
    """Shared prompt hashes among candidates, keeping only non-boilerplate ones.

    Bounded by ``user_id`` so it rides ``idx_api_logs_user``; there is no index
    on ``last_user_msg_hash``, so an unbounded scan would read the whole table.
    One sample prompt per shared hash is fetched and classified, and boilerplate
    hashes are discarded before they can link anything.

    Ordering matters more than it looks. Taking the highest-volume hashes first
    is exactly backwards: a personal soul file shared by one operator's two
    accounts appears a handful of times, while the boilerplate we are trying to
    discard appears tens of thousands of times. Fewest-accounts-first puts the
    discriminating hashes at the front, so the cap trims the least useful tail.
    """
    if not candidates:
        return {}

    ids = list(candidates)
    shared = await conn.fetch(
        """
        WITH per_user AS (
            SELECT last_user_msg_hash AS hh, user_id, count(*)::bigint AS n
            FROM api_logs
            WHERE user_id = ANY($1::text[])
              AND timestamp >= now() - make_interval(days => $2::int)
              AND last_user_msg_hash IS NOT NULL
            GROUP BY 1, 2
        )
        SELECT hh, array_agg(user_id) AS users, sum(n) AS total
        FROM per_user
        GROUP BY hh
        HAVING count(*) > 1 AND count(*) <= $3::int
        ORDER BY count(*) ASC, sum(n) ASC
        LIMIT $4::int
        """,
        ids,
        days,
        HASH_FANOUT_MAX,
        max_hashes + 1,
    )
    if not shared:
        return {}
    if len(shared) > max_hashes:
        # Never let a cap look like an empty result.
        shared = shared[:max_hashes]
        print(
            f"note: shared-prompt inspection capped at {max_hashes} hashes; "
            "raise --max-hashes if a known pair is missing",
            file=sys.stderr,
        )

    # One batched sample fetch, not one query per hash, and only the tail of
    # each prompt: the signal is `last_user_msg_hash`, so what decides
    # boilerplate-vs-bespoke is the final turn, and agent payloads run to
    # hundreds of kilobytes whose leading system preamble we do not need.
    by_hash = {row["hh"]: set(row["users"]) for row in shared}
    samples = await conn.fetch(
        """
        SELECT DISTINCT ON (last_user_msg_hash)
               last_user_msg_hash AS hh, right(prompt, 6000) AS tail
        FROM api_logs
        WHERE user_id = ANY($1::text[])
          AND last_user_msg_hash = ANY($2::bigint[])
          AND timestamp >= now() - make_interval(days => $3::int)
          AND prompt IS NOT NULL
        ORDER BY last_user_msg_hash, timestamp DESC
        """,
        ids,
        list(by_hash),
        days,
    )

    out: dict[str, set[str]] = {}
    for row in samples:
        is_bespoke, _reason = classify_shared_prompt(row["tail"])
        if is_bespoke:
            out[str(row["hh"])] = by_hash[row["hh"]]
    return out


async def fetch_first_requests(
    conn: asyncpg.Connection, candidates: set[str]
) -> dict[str, datetime]:
    """First request timestamp per candidate, for the ban-evasion precondition."""
    if not candidates:
        return {}
    rows = await conn.fetch(
        """
        SELECT user_id, min(timestamp) AS first_req
        FROM api_logs WHERE user_id = ANY($1::text[])
        GROUP BY 1
        """,
        list(candidates),
    )
    return {r["user_id"]: r["first_req"] for r in rows}


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


@dataclass
class ClusterReport:
    """A scored cluster ready for rendering."""

    members: list[Account]
    score: int
    edge_kinds: list[str]
    evidence: list[str]
    ban_evasion: list[dict[str, str]] = field(default_factory=list)

    @property
    def combined_spend(self) -> float:
        """Total 30-day cost across the cluster."""
        return sum(a.spend for a in self.members)


def render_markdown(
    reports: list[ClusterReport],
    generated: str,
    scanned: int,
    oversized: list[set[str]] | None = None,
) -> str:
    """Render the report, ranked by total cost.

    Ranking is by total cost, not by "free-tier exposure": there is no payment
    system, so ``role=pro`` is an admin-granted quota rather than revenue and
    every dollar here is money the platform spent.
    """
    lines = [
        f"# Duplicate-account scan — {generated}",
        "",
        f"Scanned {scanned} accounts over {SIGNAL_DAYS} days. Read-only; no account changes made.",
        "",
        f"**{len(reports)} cluster(s) above score {MIN_CLUSTER_SCORE}.**",
        "",
    ]
    evading = [r for r in reports if r.ban_evasion]
    if evading:
        lines += ["## ⚠ Continuation after suspension", ""]
        for report in evading:
            for finding in report.ban_evasion:
                lines.append(
                    f"- **{finding['successor']}** first requested "
                    f"{finding['successor_first_request'][:16]}, after "
                    f"**{finding['suspended']}** was suspended {finding['suspended_at'][:16]}"
                )
        lines.append("")

    for index, report in enumerate(reports, 1):
        lines += [
            f"## {index}. ${report.combined_spend:,.2f} — score {report.score}",
            "",
            "| account | status | role | spend (30d) | reqs |",
            "|---|---|---|---:|---:|",
        ]
        for account in sorted(report.members, key=lambda a: -a.spend):
            name = account.email
            if account.status == "suspended":
                name = f"~~{name}~~"
            lines.append(
                f"| {name} | {account.status} | {account.role} "
                f"| ${account.spend:,.2f} | {account.reqs:,} |"
            )
        lines += ["", f"- **Signals:** {', '.join(sorted(set(report.edge_kinds)))}"]
        for item in report.evidence[:8]:
            lines.append(f"- {item}")
        lines.append("")

    if oversized:
        lines += [
            "## Oversized components (not reported as rings)",
            "",
            f"{len(oversized)} component(s) exceeded {MAX_CLUSTER_SIZE} accounts, which means "
            "shared infrastructure slipped the per-bucket fan-out caps. Listed so the cap is "
            "visible rather than silent — investigate the linking IP, do not action the accounts.",
            "",
        ]
        for blob in oversized:
            lines.append(f"- {len(blob)} accounts")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> tuple[list[ClusterReport], list[set[str]]]:
    """Execute a scan, returning the scored clusters and any oversized blobs."""
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            accounts = await fetch_accounts(conn, args.days)
            req_ips, login_ips, login_tuples = await fetch_network_signals(conn, args.days)

            # Network + identity first, so the prompt query can be bounded by
            # the accounts those signals already implicate.
            buckets = build_buckets(accounts, req_ips, login_ips, login_tuples, {})
            buckets += cross_ip_buckets(req_ips, login_ips, accounts)
            # The prompt query has to be bounded by user_id, so it is fed every
            # account touching any bucket — NOT only those already merged.
            # Merging needs the prompt evidence, and fetching the prompt
            # evidence must not need the merge: the 2026-08-14 continuation
            # case scores 3 on network alone (one shared IP) and only clears
            # the bar once its bespoke prompts are counted.
            candidates = {m for bucket in buckets for m in bucket.members}

            prompt_hashes = await fetch_bespoke_prompt_hashes(
                conn, candidates, args.days, getattr(args, "max_hashes", 4000)
            )
            buckets += prompt_buckets(accounts, prompt_hashes)

            groups, edges, oversized = cluster(buckets)
            first_requests = await fetch_first_requests(
                conn, {m for members in groups.values() for m in members}
            )
            for uid, ts in first_requests.items():
                if uid in accounts:
                    accounts[uid].first_request_at = ts
    finally:
        await pool.close()

    reports = []
    for members in groups.values():
        score, collected = score_cluster(members, edges)
        kinds = {e.kind for e in collected}
        if score < args.min_score or not cluster_is_corroborated(kinds):
            continue
        evidence = sorted({f"`{e.kind}` — {e.detail}" for e in collected})
        reports.append(
            ClusterReport(
                members=[accounts[m] for m in sorted(members)],
                score=score,
                edge_kinds=sorted(kinds),
                evidence=evidence,
                ban_evasion=detect_ban_evasion(members, accounts, edges),
            )
        )

    if args.ban_evasion_only:
        reports = [r for r in reports if r.ban_evasion]
    reports.sort(key=lambda r: (-r.combined_spend, -r.score))
    return reports, oversized


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=SIGNAL_DAYS, help="signal window in days")
    parser.add_argument(
        "--min-score", type=int, default=MIN_CLUSTER_SCORE, help="minimum cluster score to report"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    parser.add_argument("--out", type=str, default=None, help="write the report to this path")
    parser.add_argument(
        "--ban-evasion-only",
        action="store_true",
        help="only report clusters with a confirmed continuation after suspension",
    )
    parser.add_argument(
        "--max-hashes",
        type=int,
        default=4000,
        help="cap on shared prompt hashes inspected for boilerplate",
    )
    parser.add_argument("--env-file", type=str, default=None, help="path to a .env file")
    args = parser.parse_args()

    _load_env(args.env_file)
    reports, oversized = asyncio.run(run(args))
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    if args.json:
        payload = [
            {
                "score": r.score,
                "combined_spend": round(r.combined_spend, 2),
                "signals": r.edge_kinds,
                "evidence": r.evidence,
                "ban_evasion": r.ban_evasion,
                "members": [
                    {
                        "email": a.email,
                        "user_id": a.user_id,
                        "user_name": a.user_name,
                        "status": a.status,
                        "role": a.role,
                        "spend": a.spend,
                        "reqs": a.reqs,
                    }
                    for a in r.members
                ],
            }
            for r in reports
        ]
        text = json.dumps(
            {"clusters": payload, "oversized_components": [sorted(b) for b in oversized]},
            indent=2,
        )
    else:
        text = render_markdown(reports, generated, sum(len(r.members) for r in reports), oversized)

    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
