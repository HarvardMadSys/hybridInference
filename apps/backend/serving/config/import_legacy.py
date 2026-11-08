"""Explicit, offline import of previous-release application YAML.

Normal startup never calls this module. Keep the old files until the operator
has checked the imported configuration and completed the rollback window.

It imports what the previous release had in force, judged the way that release
judged it:

- The manifest applied only with ``DISTRIBUTION_CONFIG_MODE=active``. A dark
  manifest (the default) was loaded and compared, never applied: its site
  identity, features, branding and documents are imported only when asked to
  (``include_dark``), since importing them would apply them now.
- A file the environment named (``MODELS_CONFIG_PATH``, ``ROUTING_CONFIG_PATH``,
  ``ALERTS_CONFIG_PATH``) won over the manifest's in either mode. The alert
  rules are the case that matters: the catalogs came from the database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from serving.config import app_config
from serving.config.documents import DOCUMENT_KEYS, encode, separate_secrets
from serving.utils.logging import get_logger

logger = get_logger(__name__)

#: The documents a manifest's ``paths`` (or ``site.branding``) could name.
_FILE_KINDS = ("models", "routing", "alerts", "branding")


def _read(source: Path) -> Any:
    """Parse a legacy YAML (or JSON) file.

    Raises:
        ValueError: The file cannot be read or parsed. The message names the
            file, never its contents.
    """
    try:
        text = source.read_text()
    except (OSError, UnicodeDecodeError):
        raise ValueError(f"Cannot read {source}") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError(f"{source} must contain valid YAML or JSON") from None


def _encode(document: dict[str, Any], source: Path) -> str:
    try:
        return encode(document)
    except (TypeError, ValueError):
        raise ValueError(f"{source} holds a value JSON cannot represent") from None


def _previous_mode() -> str:
    """Return how the previous release ran its manifest: ``active``, else ``dark``.

    From ``DISTRIBUTION_CONFIG_MODE``, as that release read it; an unset or
    unknown mode never applied the manifest.
    """
    from serving.config.distribution import _effective_mode

    try:
        return _effective_mode()
    except ValidationError:
        logger.warning(
            "The environment does not validate, so DISTRIBUTION_CONFIG_MODE cannot be read; "
            "treating the manifest as dark"
        )
        return "dark"


def _environment_file(kind: str) -> Path | None:
    """Return the file the environment named for *kind*, which won over the manifest's."""
    from serving.config.distribution import _env_override

    try:
        named = _env_override(kind)  # type: ignore[arg-type]
    except ValidationError:
        logger.warning(
            "The environment does not validate, so the %s file it names cannot be read", kind
        )
        return None
    return Path(named) if named else None


async def import_legacy(
    path: Path | None = None,
    *,
    alerts: Path | None = None,
    include_dark: bool = False,
) -> list[str]:
    """Import a legacy manifest and its documents atomically, without overwriting settings.

    Args:
        path: The previous release's manifest (``DISTRIBUTION_CONFIG_PATH``),
            if it had one.
        alerts: An alert rules file to import, ahead of every other source.
        include_dark: Import a manifest the previous release ran dark.

    Returns:
        The settings stored, sorted. A setting already stored (or reset) is
        left as it is.

    Raises:
        ValueError: A file cannot be read or parsed.
        app_config.ConfigUpdateError: A document is not valid; the reason
            names its field, never a value.

    Nothing is stored unless everything can be.
    """
    manifest: dict[str, Any] | None = None
    if path is not None:
        manifest = _read(path)
        if not isinstance(manifest, dict):
            raise ValueError("Legacy manifest must contain a mapping")
        mode = _previous_mode()
        if mode != "active" and not include_dark:
            logger.warning(
                "Not importing %s: the previous release ran it %s (DISTRIBUTION_CONFIG_MODE "
                "was not 'active' here), so its site identity, features, branding and "
                "documents were never in force. If it did run with DISTRIBUTION_CONFIG_MODE="
                "active, set that for this command; to apply the manifest now anyway, pass "
                "--include-dark.",
                path,
                mode,
            )
            manifest = None

    # The file each document came from, as the previous release resolved it:
    # an explicit file first, then the environment's, then the manifest's. Only
    # the explicit one must exist: the previous release read a missing file as
    # no document (built-in alert thresholds, say).
    sources: dict[str, tuple[Path, bool]] = {}
    if manifest is not None and path is not None:
        named = dict(manifest.pop("paths", {}) or {})
        site = manifest.get("site") or {}
        branding = site.pop("branding", "") if isinstance(site, dict) else ""
        if branding:
            named["branding"] = branding
        for kind in _FILE_KINDS:
            if named.get(kind):
                sources[kind] = (path.parent / named[kind], False)
    for kind in ("models", "routing", "alerts"):
        file = _environment_file(kind)
        if file is not None:
            sources[kind] = (file, False)
    if alerts is not None:
        sources["alerts"] = (alerts, True)

    existing = {item["key"] for item in await app_config.export_rows()}
    values: dict[str, str] = {}
    secrets: list[str] = []
    for kind in _FILE_KINDS:
        if DOCUMENT_KEYS[kind] in existing or kind not in sources:
            continue
        source, required = sources[kind]
        if not required and not source.exists():
            logger.warning("Not importing %s: %s does not exist", DOCUMENT_KEYS[kind], source)
            continue
        value = _read(source) or {}
        if not isinstance(value, dict):
            raise ValueError(f"Legacy {kind} document must contain a mapping")
        document, credentials = (value, {}) if kind == "branding" else separate_secrets(value, kind)
        values[DOCUMENT_KEYS[kind]] = _encode(document, source)
        values.update(credentials)
        secrets.extend(credentials)
    if manifest is not None and path is not None:
        values[DOCUMENT_KEYS["distribution"]] = _encode(manifest, path)
    # update() validates every document as Admin does, all or nothing.
    missing = {key: value for key, value in values.items() if key not in existing}
    if missing:
        await app_config.update(
            missing,
            {key: True for key in secrets if key in missing},
            updated_by="legacy-import",
            expected_version=app_config.source_version(),
        )
    return sorted(missing)
