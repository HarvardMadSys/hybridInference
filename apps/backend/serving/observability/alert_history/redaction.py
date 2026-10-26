"""Value-level redaction shared by every way text enters alert history.

An allowlisted field *name* says nothing about its *value*: a circuit's
``reason`` or a monitor's ``note`` can carry whatever an upstream error said,
including a header it echoed back. So every string kept in history — facts,
titles, snapshot bodies, labels, source metadata — goes through
:func:`redact_text` first, at capture in the backend and again at the ingestion
boundary, whatever the sender claims about having redacted it already.

What is replaced: credential-carrying headers and ``Bearer``/``Basic`` values,
``name=value`` pairs whose name says secret, well-known token shapes (OpenAI /
Anthropic style ``sk-``, Slack ``xox*-``, GitHub ``gh*_``, AWS access keys,
JWTs, PEM blocks), any URL's path and query (webhook URLs are credentials),
e-mail addresses, IP addresses, and long unbroken high-entropy runs. The
placeholders say what was there, never the value.

This is a backstop, not a licence: producers still decide what to put in
facts, and fields with no safe story are omitted before they get here.
"""

from __future__ import annotations

import re

#: Field names that never carry a keepable value, whatever it looks like.
SENSITIVE_KEY = re.compile(
    r"(?i)(authori[sz]ation|bearer|token|secret|passw(or)?d|passwd|pwd|cookie|credential|"
    r"api[_-]?key|private[_-]?key|webhook|session|signature|e[_-]?mail|ip[_-]?addr|"
    r"remote[_-]?ip|client[_-]?ip|user[_-]?id|account|phone)"
)

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S
        ),
        "[redacted:private_key]",
    ),
    (
        re.compile(
            r"(?i)\b((?:proxy-)?authori[sz]ation|x-api-key|api-key|cookie|set-cookie)\s*[:=]\s*[^\r\n,;]*"
        ),
        r"\1: [redacted]",
    ),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=:-]{6,}"), r"\1 [redacted]"),
    (
        re.compile(
            r"(?i)\b([a-z0-9_-]*(?:api[_-]?key|token|secret|passw(?:or)?d|passwd|pwd|"
            r"access[_-]?key|private[_-]?key|signature|sig|auth|key))(\s*[=:]\s*)"
            r"(\"[^\"]*\"|'[^']*'|[^\s&,;\"']+)"
        ),
        r"\1\2[redacted]",
    ),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"), "[redacted:jwt]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"), "[redacted:token]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}"), "[redacted:token]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "[redacted:token]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[redacted:token]"),
    (
        re.compile(
            r"(?i)\b([a-z][a-z0-9+.-]{1,15})://(?:[^\s/@]+@)?([^\s/?#:]+)(?::\d+)?[^\s<>\"']*"
        ),
        r"\1://\2/[redacted]",
    ),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[redacted:email]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[redacted:ip]"),
    (re.compile(r"(?i)\b(?:[0-9a-f]{1,4}:){4,7}[0-9a-f]{1,4}\b"), "[redacted:ip]"),
    (re.compile(r"[A-Za-z0-9_+/=-]{40,}"), "[redacted:opaque]"),
)


def redact_text(text: str) -> tuple[str, bool]:
    """Return ``text`` with credential-shaped values replaced, and whether any were."""
    out = text
    for pattern, replacement in _RULES:
        out = pattern.sub(replacement, out)
    return out, out != text


def looks_sensitive(text: str) -> bool:
    """Whether :func:`redact_text` would change ``text``."""
    return redact_text(text)[1]


def sensitive_key(name: str) -> bool:
    """Whether a fact *name* marks its value as never keepable."""
    return bool(SENSITIVE_KEY.search(name))


_IDENTIFIER_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
    re.compile(r"(?:^|[^A-Za-z0-9])(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?i)[a-z][a-z0-9+.-]{1,15}://"),
    re.compile(r"(?i)(bearer|authori[sz]ation|password|passwd|secret|api[_-]?key)"),
)


def credential_shaped_identifier(value: str) -> bool:
    """Whether an *identifier* carries a recognizable credential or address.

    Narrower than :func:`redact_text`: long opaque runs are what identifiers
    are (hashes, UUIDs), so only shapes that are never a legitimate id count.
    An identifier cannot be partly redacted without breaking idempotency, so
    the ingestion boundary refuses it instead.
    """
    return any(pattern.search(value) for pattern in _IDENTIFIER_RULES)


#: Fact names whose string value is a *code*: a lowercase token such as
#: ``control_plane_rpc``, ``stale_result_not_applied`` or
#: ``status-monitor:cycle``. A value that is not shaped like one is dropped,
#: so these names cannot carry prose. Aligned with the status monitor's and
#: alert control plane's technical field inventory; extend with the contract.
INGEST_CODE_FACT_KEYS = frozenset(
    {
        "action_status",
        "action_status_after",
        "action_type",
        "basis",
        "check",
        "claim_mode",
        "commit",
        "commit_state",
        "control_plane_action",
        "cycle_owner",
        "db_kind",
        "default_owner",
        "delivery_owner",
        "end_trigger",
        "endpoint_id_omitted",
        "error_type",
        "gate",
        "handoff_outcome",
        "incident_key_form",
        "incident_state",
        "lifecycle_action",
        "lifecycle_state",
        "mode",
        "model_id_omitted",
        "notification",
        "observation_basis",
        "phase",
        "previous_state",
        "probe",
        "producer_source",
        "reason",
        "record_role",
        "recovery_notification",
        "region",
        "role",
        "rule_version",
        "source_event_id_form",
        "state",
        "status",
        "store",
        "task_name",
        "transition",
        "transport",
        "trigger",
        "window",
    }
)
#: Fact names whose string value is an *identifier* another system minted,
#: kept exactly as sent: the contract's identifier charset
#: ``[A-Za-z0-9._:@/-]``, at most :data:`INGEST_IDENTIFIER_FACT_MAX`
#: characters, mixed case allowed, and refused (dropped) when it carries a
#: credential or address shape (:func:`credential_shaped_identifier`). It is
#: never partly redacted: a changed identifier would no longer join anything.
INGEST_IDENTIFIER_FACT_KEYS = frozenset({"monitor_incident_key"})
INGEST_IDENTIFIER_FACT_MAX = 200
_IDENTIFIER_VALUE = re.compile(r"[A-Za-z0-9._:@/-]{1,200}")
#: Fact names whose string value is a short *label*: a model or provider
#: name, a timestamp, a rendered rate or money amount, a top-N summary of
#: labels. Printable, at most :data:`INGEST_TEXT_FACT_MAX` characters, redacted.
INGEST_LABEL_FACT_KEYS = frozenset(
    {
        "alert",
        "availability",
        "budget",
        "endpoint_id",
        "first_at",
        "first_lost_at",
        "hour",
        "hourly_spend",
        "last_at",
        "last_lost_at",
        "model",
        "over_by",
        "provider",
        "quota",
        "rate",
        "spend",
        "top_endpoints",
        "top_error_types",
        "top_providers",
        "top_status_codes",
    }
)
#: Every name an external string fact may use.
INGEST_TEXT_FACT_KEYS = INGEST_CODE_FACT_KEYS | INGEST_IDENTIFIER_FACT_KEYS | INGEST_LABEL_FACT_KEYS
#: Longest code / label an external fact may hold.
INGEST_CODE_FACT_MAX = 100
INGEST_TEXT_FACT_MAX = 200
_CODE_VALUE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,99}")


def sanitize_facts(
    facts: dict[str, object] | None,
    *,
    prefix: str = "",
) -> tuple[dict[str, object], list[str]]:
    """Keep typed facts and recognized codes and labels; drop everything else.

    Secret-named facts go whatever their type. Numbers, booleans and nulls are
    kept under any other well-formed name. A string is kept only under a
    recognized name: a code name keeps it only if it is a code token (and
    survives redaction unchanged); an identifier name keeps it exactly if it
    is a well-formed identifier with no credential or address in it; a label
    name keeps it printable, cut to
    :data:`INGEST_TEXT_FACT_MAX` and redacted. Drops and changes are reported
    as ``omitted:<name>``, ``truncated:<name>`` or ``redacted:<name>``.
    """
    out: dict[str, object] = {}
    notes: list[str] = []
    for key, value in (facts or {}).items():
        if sensitive_key(key):
            notes.append(f"omitted:{prefix}{key}")
            continue
        if isinstance(value, str):
            if key in INGEST_CODE_FACT_KEYS:
                if not _CODE_VALUE.fullmatch(value) or looks_sensitive(value):
                    notes.append(f"omitted:{prefix}{key}")
                    continue
            elif key in INGEST_IDENTIFIER_FACT_KEYS:
                if not _IDENTIFIER_VALUE.fullmatch(value) or credential_shaped_identifier(value):
                    notes.append(f"omitted:{prefix}{key}")
                    continue
            elif key in INGEST_LABEL_FACT_KEYS:
                if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                    notes.append(f"omitted:{prefix}{key}")
                    continue
                if len(value) > INGEST_TEXT_FACT_MAX:
                    value = value[:INGEST_TEXT_FACT_MAX]
                    notes.append(f"truncated:{prefix}{key}")
                value, changed = redact_text(value)
                if changed:
                    notes.append(f"redacted:{prefix}{key}")
            else:
                notes.append(f"omitted:{prefix}{key}")
                continue
        out[key] = value
    return out, notes
