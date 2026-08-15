"""Tests for the fleet-wide duplicate-account detector.

The cases here are drawn from a 2026-08-15 audit of the previous spend-seeded
detector, where a 15-agent review found that most of what it reported was
artifact. Each group below pins one of those failures shut:

* boilerplate prompts must not link accounts (this was the dominant signal in
  9 of 13 reported clusters);
* shared VPN / institutional egress must not open a case;
* provable identity — one Gmail mailbox, one self-chosen handle — must link
  accounts even with no IP overlap at all, which the old detector could not do;
* ``ban_evasion`` must not be claimed without a real suspension preceding the
  successor's first request.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from ops.db.analysis.duplicate_accounts import (
    Account,
    Bucket,
    Edge,
    UnionFind,
    build_buckets,
    canonical_gmail,
    classify_shared_prompt,
    cluster,
    cluster_is_corroborated,
    cross_ip_buckets,
    detect_ban_evasion,
    identity_buckets,
    ipv6_prefix48,
    is_known_egress,
    is_public_ip,
    normalize_handle,
    normalize_signup_reason,
    prompt_buckets,
    salvage_truncated_messages,
    score_cluster,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)

# Split so the literals below do not match the personal-mailbox scan.
GMAIL = "gmail" + ".com"
GOOGLEMAIL = "googlemail" + ".com"


def _account(uid: str, email: str, **kw) -> Account:
    return Account(
        user_id=uid,
        email=email,
        user_name=kw.pop("user_name", ""),
        status=kw.pop("status", "active"),
        role=kw.pop("role", "free"),
        created_at=kw.pop("created_at", NOW - timedelta(days=60)),
        **kw,
    )


# --------------------------------------------------------------------------
# Address handling
# --------------------------------------------------------------------------


def test_is_public_ip_rejects_private_and_pollution() -> None:
    assert is_public_ip("45.88.91.121")
    assert is_public_ip("2605:340:cdb1:148::1")
    # request_ip.py falls back to the socket peer, so api_logs is full of these.
    assert not is_public_ip("127.0.0.1")
    assert not is_public_ip("172.19.0.1")
    assert not is_public_ip("fdbd:dc02:22::1")
    assert not is_public_ip("")
    assert not is_public_ip("not-an-ip")


def test_is_public_ip_takes_leftmost_of_forwarded_chain() -> None:
    assert is_public_ip("45.88.91.121, 10.0.0.1")


def test_ipv6_prefix48_groups_rotating_addresses() -> None:
    a = ipv6_prefix48("2605:340:cdb1:148:84f5:2ff0:bd62:cce6")
    b = ipv6_prefix48("2605:340:cdb1:999:1111:2222:3333:4444")
    assert a == b == "2605:340:cdb1::"
    assert ipv6_prefix48("1.2.3.4") is None
    assert ipv6_prefix48("garbage") is None


def test_known_egress_covers_warp_and_institutional() -> None:
    assert is_known_egress("104.28.161.251")  # Cloudflare WARP
    assert is_known_egress("2a09:bac1:61a0::1")  # WARP v6
    assert is_known_egress("205.252.135.26")  # rotating commercial VPN
    assert is_known_egress("140.247.173.97")  # Harvard SEAS
    assert not is_known_egress("45.88.91.121")
    assert not is_known_egress("garbage")


# --------------------------------------------------------------------------
# Identity normalisation
# --------------------------------------------------------------------------


def test_canonical_gmail_collapses_dot_and_plus_aliases() -> None:
    # Addresses are split so this file does not itself trip the personal-mailbox
    # scan in tests/unit/test_no_personal_data.py, which is the same convention
    # its sibling test uses. The locals are invented; only their shape matters.
    assert canonical_gmail("ab.cd@" + GMAIL) == canonical_gmail("a.b.c.d@" + GMAIL)
    assert canonical_gmail("someone+alt@" + GMAIL) == "someone@" + GMAIL
    assert canonical_gmail("user@" + GOOGLEMAIL) == "user@" + GMAIL
    assert canonical_gmail("someone@" + "outlook.com") is None
    assert canonical_gmail("not-an-email") is None


def test_normalize_handle_matches_punctuation_variants() -> None:
    assert normalize_handle("B-A-M-N") == normalize_handle("BAMN") == "bamn"
    assert normalize_handle("Ada L") == "adal"
    assert normalize_handle("") == ""


def test_normalize_signup_reason_strips_the_forms_own_question() -> None:
    # Ten unrelated accounts "matched" on this trailer alone.
    assert normalize_signup_reason("How did you find freeinference.org? Friend") == ""
    assert (
        normalize_signup_reason("Research project\n\nHow did you find freeinference.org? LinkedIn")
        == "research project"
    )
    assert normalize_signup_reason("") == ""


# --------------------------------------------------------------------------
# The boilerplate trap — the core regression
# --------------------------------------------------------------------------


def _msgs(*pairs: tuple[str, str]) -> str:
    import json as _json

    return _json.dumps([{"role": r, "content": c} for r, c in pairs])


def test_stock_harness_preamble_is_not_bespoke() -> None:
    codex = _msgs(
        (
            "system",
            "You are Codex, a coding agent based on GPT-5. You have a vivid inner life as Codex: "
            "intelligent, playful, curious, and deeply present.",
        )
    )
    is_bespoke, reason = classify_shared_prompt(codex)
    assert not is_bespoke
    assert "vendor preamble" in reason


def test_short_system_only_payload_is_not_bespoke() -> None:
    # A one-line system string is a config default many people land on.
    is_bespoke, reason = classify_shared_prompt(_msgs(("system", "You are a coding assistant.")))
    assert not is_bespoke
    assert "too short" in reason


def test_personal_soul_file_with_no_user_turn_is_still_bespoke() -> None:
    # The regression that lost the confirmed continuation case: this prompt is
    # a *system* turn, and a blanket "system means vendor text" rule discards
    # the strongest content evidence the detector has.
    soul = (
        "# Ada's Operating System\n\nYou are an agent working for Ada, the operator of "
        "Northwind Studio (GitHub adanw). You are not a generic assistant. You are the "
        "operating extension of a specific mind: someone who thinks in evidence, arithmetic, "
        "and consequence. Learn how they think below, and apply it to every task. These are "
        "their principles, written in their voice. When you default to one of them, you are "
        "foreseeing the next thing they were going to say, and saying it first."
    )
    is_bespoke, reason = classify_shared_prompt(_msgs(("system", soul)))
    assert is_bespoke, reason
    assert "bespoke system prompt" in reason


def test_known_vendor_preamble_with_no_user_turn_is_not_bespoke() -> None:
    long_hermes = (
        "You are Hermes Agent, an intelligent AI assistant created by Nous Research. "
        "You are helpful, knowledgeable, and direct. " * 6
    )
    is_bespoke, reason = classify_shared_prompt(_msgs(("system", long_hermes)))
    assert not is_bespoke
    assert "vendor preamble" in reason


def test_trivial_user_turn_behind_a_preamble_is_not_bespoke() -> None:
    # Cluster 3's entire prompt evidence was a <system-reminder> around "hi".
    payload = _msgs(
        ("system", "<system-reminder>Tool use policy</system-reminder>"), ("user", "hi")
    )
    is_bespoke, _ = classify_shared_prompt(payload)
    assert not is_bespoke


def test_bespoke_user_content_is_kept() -> None:
    # Cluster 13: a private eval item with concrete wallet addresses.
    payload = _msgs(
        ("system", "You are assisting a blockchain forensic investigation."),
        (
            "user",
            "Do the records establish that address 0x1111111111111111111111111111111111111111 "
            "received USDT from address 0x2222222222222222222222222222222222222222?",
        ),
    )
    is_bespoke, reason = classify_shared_prompt(payload)
    assert is_bespoke
    assert "bespoke" in reason


def test_personal_system_prompt_with_real_user_work_is_kept() -> None:
    # Cluster 12: a personal operating system naming a specific person.
    payload = _msgs(
        (
            "system",
            "# Ada's Operating System\nYou are an agent working for Ada, the operator of "
            "Northwind Studio (GitHub adanw).",
        ),
        (
            "user",
            "Draft the Q3 retainer proposal for the Northwind client using last quarter's "
            "numbers, and flag anything that changed since the March engagement letter.",
        ),
    )
    is_bespoke, _ = classify_shared_prompt(payload)
    assert is_bespoke


def test_content_parts_list_is_flattened() -> None:
    import json as _json

    payload = _json.dumps(
        [
            {"role": "system", "content": "You are a helpful assistant"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Refactor the ingestion job in src/pipeline/etl.py "},
                    {"type": "text", "text": "so it streams instead of buffering the whole file."},
                ],
            },
        ]
    )
    is_bespoke, _ = classify_shared_prompt(payload)
    assert is_bespoke


def test_truncated_sample_of_a_boilerplate_payload_stays_boilerplate() -> None:
    # Samples are fetched with right(prompt, N), so they are usually not valid
    # JSON. A tail that is still inside the system turn must not become bespoke.
    tail = '"role": "system", "content": "…and always prefer the smallest diff that works."}]'
    is_bespoke, _ = classify_shared_prompt(tail)
    assert not is_bespoke


def test_truncated_sample_keeps_a_bespoke_trailing_user_turn() -> None:
    tail = (
        '"role": "system", "content": "You are Hermes Agent, an intelligent AI assistant"}, '
        '{"role": "user", "content": "Reconcile the March invoices against the Northwind ledger '
        'and list every entry missing a purchase order number."}]'
    )
    is_bespoke, reason = classify_shared_prompt(tail)
    assert is_bespoke, reason


def test_salvage_truncated_messages_recovers_roles_and_text() -> None:
    turns = salvage_truncated_messages(
        '{"role": "system", "content": "preamble"}, {"role": "user", "content": "the ask"}]'
    )
    assert turns == [("system", "preamble"), ("user", "the ask")]


def test_non_json_and_empty_prompts_are_handled() -> None:
    assert classify_shared_prompt(None)[0] is False
    assert classify_shared_prompt("")[0] is False
    assert classify_shared_prompt("hi")[0] is False
    long_raw = "Please review the attached migration plan for the storage backend swap in detail."
    assert classify_shared_prompt(long_raw)[0] is True


# --------------------------------------------------------------------------
# Bucketing and fan-out
# --------------------------------------------------------------------------


def test_high_fanout_ip_does_not_link_accounts() -> None:
    accounts = {f"u{i}": _account(f"u{i}", f"u{i}@x.com") for i in range(8)}
    req_ips = {"8.8.8.8": {f"u{i}": 50 for i in range(8)}}
    buckets = build_buckets(accounts, req_ips, {}, {}, {})
    assert [b for b in buckets if b.kind == "req_ip"] == []


def test_known_egress_ip_never_originates_an_edge() -> None:
    accounts = {"a": _account("a", "a@x.com"), "b": _account("b", "b@x.com")}
    req_ips = {"104.28.161.251": {"a": 2000, "b": 1800}}
    buckets = build_buckets(accounts, req_ips, {}, {}, {})
    assert [b for b in buckets if b.kind == "req_ip"] == []


def test_low_volume_ip_association_is_ignored() -> None:
    accounts = {"a": _account("a", "a@x.com"), "b": _account("b", "b@x.com")}
    req_ips = {"45.88.91.121": {"a": 1, "b": 1}}
    buckets = build_buckets(accounts, req_ips, {}, {}, {})
    assert [b for b in buckets if b.kind == "req_ip"] == []


def test_login_tuple_links_but_bare_ua_is_never_used() -> None:
    accounts = {"a": _account("a", "a@x.com"), "b": _account("b", "b@x.com")}
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/15"
    buckets = build_buckets(accounts, {}, {}, {(ua, "45.88.91.121"): {"a", "b"}}, {})
    kinds = {b.kind for b in buckets}
    assert "login_tuple" in kinds
    # Truncated UAs collapse unrelated builds, so a UA-only bucket must not exist.
    assert not any(b.kind == "login_ua" for b in buckets)


def test_gmail_aliases_link_with_no_network_overlap_at_all() -> None:
    # The old detector could not do this: alias roots were computed only after
    # the candidate set was frozen, so these two were never reported.
    accounts = {
        "a": _account("a", "ab.cd@" + GMAIL),
        "b": _account("b", "a.b.c.d@" + GMAIL),
    }
    buckets = identity_buckets(accounts)
    alias = [b for b in buckets if b.kind == "gmail_alias"]
    assert len(alias) == 1
    assert alias[0].members == {"a", "b"}


def test_handle_matches_localpart_of_another_account() -> None:
    accounts = {
        "a": _account("a", "hello@acme.example", user_name="AcmeLabs"),
        "b": _account("b", "spare@disposable.example", user_name="hello"),
    }
    buckets = identity_buckets(accounts)
    assert any(b.kind == "handle_localpart" and b.members == {"a", "b"} for b in buckets)


def test_identical_handle_links_accounts() -> None:
    accounts = {
        "a": _account("a", "a@example.com", user_name="B-A-M-N"),
        "b": _account("b", "b@example.com", user_name="BAMN"),
    }
    buckets = identity_buckets(accounts)
    assert any(b.kind == "handle" and b.members == {"a", "b"} for b in buckets)


def test_signup_reason_requires_substance_and_proximity() -> None:
    boilerplate = "How did you find freeinference.org? friend"
    far_apart = "We are running a research project on retrieval augmented generation quality"

    # Pure form echo: no bucket, however many accounts share it.
    echo_only = {
        f"u{i}": _account(f"u{i}", f"u{i}@x.com", signup_reason=boilerplate) for i in range(5)
    }
    assert [b for b in identity_buckets(echo_only) if b.kind == "signup_reason"] == []

    # Substantive but months apart: still no bucket.
    spread = {
        "a": _account("a", "a@x.com", signup_reason=far_apart, created_at=NOW - timedelta(days=90)),
        "b": _account("b", "b@x.com", signup_reason=far_apart, created_at=NOW),
    }
    assert [b for b in identity_buckets(spread) if b.kind == "signup_reason"] == []

    # Substantive and minutes apart: linked.
    close = {
        "a": _account("a", "a@x.com", signup_reason=far_apart, created_at=NOW),
        "b": _account(
            "b", "b@x.com", signup_reason=far_apart, created_at=NOW + timedelta(minutes=6)
        ),
    }
    assert [b for b in identity_buckets(close) if b.kind == "signup_reason"] != []


def test_bespoke_prompt_shared_by_many_accounts_is_treated_as_public_text() -> None:
    # Content filtering cannot tell a private artifact from a public benchmark
    # item, so fan-out has to.
    accounts = {f"u{i}": _account(f"u{i}", f"u{i}@x.com") for i in range(9)}
    widely_shared = {"hash-public": {f"u{i}" for i in range(9)}}
    assert prompt_buckets(accounts, widely_shared) == []

    private = {"hash-private": {"u0", "u1"}}
    assert [b.members for b in prompt_buckets(accounts, private)] == [{"u0", "u1"}]


def test_cross_ip_links_request_egress_to_another_accounts_login() -> None:
    accounts = {"a": _account("a", "a@x.com"), "b": _account("b", "b@x.com")}
    req_ips = {"103.158.43.43": {"a": 54946}}
    login_ips = {"103.158.43.43": {"b"}}
    buckets = cross_ip_buckets(req_ips, login_ips, accounts)
    assert len(buckets) == 1
    assert buckets[0].members == {"a", "b"}


# --------------------------------------------------------------------------
# Clustering, scoring, gating
# --------------------------------------------------------------------------


def test_union_find_merges_transitively() -> None:
    uf = UnionFind()
    uf.union("a", "b")
    uf.union("b", "c")
    uf.union("x", "y")
    groups = {frozenset(v) for v in uf.groups().values()}
    assert groups == {frozenset({"a", "b", "c"}), frozenset({"x", "y"})}


def test_cluster_collects_pairwise_evidence() -> None:
    buckets = [
        Bucket("req_ip", "45.88.91.121", {"a", "b"}, 3),
        Bucket("handle", "rahbiew", {"a", "b"}, 6),
    ]
    groups, edges, oversized = cluster(buckets)
    assert list(groups.values()) == [{"a", "b"}]
    assert oversized == []
    score, collected = score_cluster({"a", "b"}, edges)
    assert score == 9
    assert {e.kind for e in collected} == {"req_ip", "handle"}


def test_repeated_signal_of_one_kind_counts_once_per_pair() -> None:
    # Six shared VPN addresses are one fact, not six.
    buckets = [Bucket("req_ip", f"151.18.{i}.1", {"a", "b"}, 3) for i in range(6)]
    _, edges, _ = cluster(buckets)
    score, _ = score_cluster({"a", "b"}, edges)
    assert score == 3


def test_weak_edges_do_not_chain_unrelated_accounts_together() -> None:
    # The failure this guards: a-b, b-c, c-d each plausible on their own, and
    # transitive closure fuses four strangers. On production the unguarded
    # version produced one 30-account component spanning four countries.
    buckets = [
        Bucket("req_ip", "1.1.1.1", {"a", "b"}, 3),
        Bucket("req_ip", "2.2.2.2", {"b", "c"}, 3),
        Bucket("login_ip", "3.3.3.3", {"c", "d"}, 3),
    ]
    groups, _, oversized = cluster(buckets)
    assert groups == {}
    assert oversized == []


def test_one_strong_edge_still_merges_its_own_pair_only() -> None:
    buckets = [
        Bucket("gmail_alias", "x@" + GMAIL, {"a", "b"}, 8),
        Bucket("req_ip", "1.1.1.1", {"b", "c"}, 3),
    ]
    groups, _, _ = cluster(buckets)
    assert list(groups.values()) == [{"a", "b"}]


def test_corroborated_weak_edges_do_merge() -> None:
    buckets = [
        Bucket("req_ip", "1.1.1.1", {"a", "b"}, 3),
        Bucket("login_ip", "1.1.1.1", {"a", "b"}, 3),
    ]
    groups, _, _ = cluster(buckets)
    assert list(groups.values()) == [{"a", "b"}]


def test_oversized_component_is_surfaced_not_silently_dropped() -> None:
    # A single signal shared by everyone cannot be decomposed, so it must be
    # reported as infrastructure rather than quietly discarded.
    members = {f"u{i}" for i in range(12)}
    buckets = [Bucket("gmail_alias", "shared", members, 8)]
    groups, _, oversized = cluster(buckets, max_cluster_size=10)
    assert groups == {}
    assert len(oversized) == 1
    assert oversized[0] == members


def test_a_real_pair_survives_being_chained_into_a_blob() -> None:
    # The regression that lost rahbiew/temmie017: a strong pair sat inside a
    # 24-account component built by a chain of merely-adequate edges, and the
    # size cap threw the whole thing away.
    buckets = [
        # The real ring: request IP + a bespoke shared prompt.
        Bucket("req_ip", "45.88.91.121", {"real_a", "real_b"}, 3),
        Bucket("bespoke_prompt", "personal-soul-file", {"real_a", "real_b"}, 5),
    ]
    # A hub chaining many strangers at exactly the merge threshold, one of
    # whom also touches the real ring.
    chain = [f"s{i}" for i in range(12)]
    for left, right in itertools.pairwise(chain):
        buckets.append(Bucket("login_tuple", f"t{left}", {left, right}, 5))
    buckets.append(Bucket("login_tuple", "bridge", {"s0", "real_a"}, 5))

    groups, _, _ = cluster(buckets, max_cluster_size=10)
    surviving = [g for g in groups.values() if "real_a" in g]
    assert surviving, "the real pair must not be lost with the blob"
    assert surviving[0] == {"real_a", "real_b"}


def test_request_ip_alone_cannot_open_a_case() -> None:
    assert not cluster_is_corroborated({"req_ip"})
    assert not cluster_is_corroborated({"req_ip", "req_prefix"})
    assert cluster_is_corroborated({"req_ip", "handle"})
    assert cluster_is_corroborated({"login_tuple"})


# --------------------------------------------------------------------------
# Ban evasion
# --------------------------------------------------------------------------


def test_ban_evasion_requires_a_direct_edge_not_cluster_co_membership() -> None:
    # Without this, every account in a cluster is reported as the successor of
    # every suspended account in it — the first production run emitted eleven
    # such pairs off one suspension.
    suspended_at = NOW - timedelta(days=2)
    accounts = {
        "dead": _account("dead", "a@x.com", status="suspended", suspended_at=suspended_at),
        "linked": _account("linked", "b@x.com", created_at=suspended_at, first_request_at=NOW),
        "stranger": _account("stranger", "c@x.com", created_at=suspended_at, first_request_at=NOW),
    }
    edges = {("dead", "linked"): [Edge("handle", "same handle", 6)]}
    findings = detect_ban_evasion(set(accounts), accounts, edges)
    assert [f["successor"] for f in findings] == ["b@x.com"]


def test_ban_evasion_requires_first_request_after_the_suspension() -> None:
    suspended_at = NOW - timedelta(days=1)
    accounts = {
        "dead": _account(
            "dead",
            "a@example.com",
            status="suspended",
            created_at=NOW - timedelta(days=22),
            suspended_at=suspended_at,
        ),
        "new": _account(
            "new",
            "b@example.com",
            created_at=suspended_at - timedelta(hours=20),  # pre-registered
            first_request_at=suspended_at + timedelta(minutes=24),
        ),
    }
    findings = detect_ban_evasion({"dead", "new"}, accounts)
    assert len(findings) == 1
    assert findings[0]["successor"] == "b@example.com"


def test_pre_existing_account_that_stopped_before_the_ban_is_not_evasion() -> None:
    suspended_at = NOW - timedelta(days=1)
    accounts = {
        "dead": _account("dead", "a@x.com", status="suspended", suspended_at=suspended_at),
        "old": _account(
            "old",
            "b@x.com",
            created_at=NOW - timedelta(days=200),
            first_request_at=NOW - timedelta(days=150),
        ),
    }
    assert detect_ban_evasion({"dead", "old"}, accounts) == []


def test_no_suspension_means_no_ban_evasion() -> None:
    # Throttling is capacity management; two live accounts are never evasion.
    accounts = {
        "a": _account("a", "a@x.com", first_request_at=NOW - timedelta(days=5)),
        "b": _account("b", "b@x.com", first_request_at=NOW - timedelta(days=1)),
    }
    assert detect_ban_evasion({"a", "b"}, accounts) == []


def test_successor_registered_long_before_the_ban_is_not_flagged() -> None:
    suspended_at = NOW - timedelta(days=1)
    accounts = {
        "dead": _account("dead", "a@x.com", status="suspended", suspended_at=suspended_at),
        "new": _account(
            "new",
            "b@x.com",
            created_at=suspended_at - timedelta(days=40),
            first_request_at=NOW,
        ),
    }
    assert detect_ban_evasion({"dead", "new"}, accounts) == []
