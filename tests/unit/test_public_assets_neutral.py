"""``apps/frontend/public/`` ships only the atlas data — nothing else.

The text guards cannot see this tree: the brand-residue sweep reads file
contents and skips anything that does not decode, and the personal-data scan
skips image suffixes outright. A team photo or a sponsor's logo checked in
here is invisible to both — which is exactly how three of them survived every
sweep until a manual audit found them (removed in the change that adds this
test; they ride in through the deployment overlay's console build now).

So the public tree gets a shape guard instead of a content guard: the neutral
upstream serves the atlas dataset and nothing else. A new file here is either
neutral (extend ``ALLOWED`` deliberately, with the reasoning) or it is
distribution content and belongs in an overlay.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

PUBLIC = "apps/frontend/public/"

# Directory prefixes (relative to PUBLIC) the neutral upstream may ship.
ALLOWED = (
    # Natural Earth-derived country boundaries + license for the admin geo
    # globe: a dataset, not an identity.
    "atlas/",
)


def test_public_tree_ships_only_the_atlas() -> None:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", PUBLIC],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout
    tracked = [n.decode() for n in out.split(b"\0") if n]
    assert tracked, f"{PUBLIC} lists nothing tracked — did the tree move?"
    offenders = [path for path in tracked if not path.removeprefix(PUBLIC).startswith(ALLOWED)]
    assert not offenders, (
        "these are served verbatim to every deployment; a neutral upstream "
        f"ships no distribution content here: {offenders}"
    )
