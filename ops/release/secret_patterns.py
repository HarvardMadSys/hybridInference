"""Credential shapes the public-export audit refuses to publish.

These lived in the agent sandbox's patch gate, which read every diff an agent
proposed before it could become a pull request. That gate moved to the cloud
agent's own repository (task H4), and ``public_export.py`` loaded this tuple
**from it by file path** — so deleting the gate would have quietly left the
release audit with no patterns at all. A leak scanner that silently stops
scanning is worse than no scanner, because the green check keeps arriving.

The two repositories now each hold a copy, which is the honest cost of the
split rather than a mistake to fix: they scan different things (a proposed
patch there, a published tree here) and neither can import the other. **A
credential shape added in either belongs in both.**

``public_export.py`` loads this module by path, outside any package, so
**nothing here may import beyond the standard library**. ``re`` and
``__future__`` only; ``test_public_export_audit.py`` enforces it.

Deliberately narrow. Each pattern targets a credential with a recognizable
prefix or structure so ordinary code and prose do not trip it: a false positive
blocks a release, and an operator who learns to expect noise stops reading the
output — which is how a real key gets waved through.
"""

from __future__ import annotations

import re

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}")),
    ("gateway_key", re.compile(r"\bhyi-[A-Za-z0-9\-_]{20,}")),
    # The cloud agent's inference grant. Kept although this gateway no longer
    # runs agents: it *mints* these, so one can still reach a log, a fixture or
    # a commit here.
    ("agent_grant_token", re.compile(r"\bagr\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}")),
    # The per-attempt worker token this gateway stopped issuing at H4. Retained
    # anyway: a scanner is for what might be in the tree, not for what the
    # current code can produce, and dropping a pattern is how a scanner gets
    # quietly narrower than the thing it guards.
    ("agent_worker_token", re.compile(r"\bajt\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
)
