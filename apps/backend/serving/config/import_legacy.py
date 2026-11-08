"""Explicit, offline import of previous-release application YAML.

Normal startup never calls this module. Keep the old files until the operator
has checked the imported configuration and completed the rollback window.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from serving.config import app_config
from serving.config.documents import DOCUMENT_KEYS, encode, separate_secrets, validate

if TYPE_CHECKING:
    from pathlib import Path


async def import_legacy(path: Path) -> list[str]:
    """Import a legacy manifest and its documents atomically, without overwriting settings.

    Raises:
        ValueError: A file is invalid, or a destination already exists. Existing
            administrator settings are never silently overwritten.
    """

    def read(source: Path):
        try:
            return yaml.safe_load(source.read_text())
        except yaml.YAMLError:
            raise ValueError("Legacy configuration must contain valid YAML or JSON") from None

    raw = read(path)
    if not isinstance(raw, dict):
        raise ValueError("Legacy manifest must contain a mapping")
    values: dict[str, str] = {}
    secrets: list[str] = []
    sources = dict(raw.pop("paths", {}) or {})
    site = raw.get("site") or {}
    branding = site.pop("branding", "")
    if branding:
        sources["branding"] = branding
    existing = {item["key"] for item in await app_config.export_rows()}
    for kind in ("models", "routing", "alerts", "branding"):
        if DOCUMENT_KEYS[kind] in existing:
            continue
        source = sources.get(kind)
        if not source:
            continue
        value = read(path.parent / source) or {}
        if not isinstance(value, dict):
            raise ValueError(f"Legacy {kind} document must contain a mapping")
        document, credentials = (value, {}) if kind == "branding" else separate_secrets(value, kind)
        values[DOCUMENT_KEYS[kind]] = encode(document)
        values.update(credentials)
        secrets.extend(credentials)
    values[DOCUMENT_KEYS["distribution"]] = encode(raw)
    for key, value in values.items():
        if key in DOCUMENT_KEYS.values():
            validate(key, value)
    missing = {key: value for key, value in values.items() if key not in existing}
    if missing:
        await app_config.update(
            missing,
            {key: True for key in secrets if key in missing},
            updated_by="legacy-import",
            expected_version=app_config.source_version(),
        )
    return sorted(missing)
