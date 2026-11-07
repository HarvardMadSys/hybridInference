"""Admin endpoints for the database-backed application configuration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from serving.audit import AuditActor, AuditTarget
from serving.config import app_config, live, live_control
from serving.config.revisions import SourceVersion
from serving.schemas_config import (
    ConfigResponse,
    LiveConfigStatusResponse,
    UpdateConfigRequest,
)
from serving.servers import restart
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_admin_audit_actor, get_operational_store, verify_admin_access
from serving.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin")


def _response(response: Response, saved: SourceVersion | None = None) -> ConfigResponse:
    body = ConfigResponse(
        **app_config.describe(offered_only=True),
        restart_supported=restart.restart_supported(),
        saved_version=saved.as_dict() if saved is not None else None,
    )
    version = app_config.source_version()
    if version is not None:
        response.headers["ETag"] = version.etag
    return body


def _expected(if_match: str | None) -> SourceVersion | None:
    """Parse ``If-Match``; absent (an older client) keeps last-write-wins."""
    if if_match is None or not if_match.strip() or if_match.strip() == "*":
        return None
    try:
        return SourceVersion.parse(if_match)
    except ValueError:
        raise HTTPException(400, "If-Match must be the ETag GET /admin/config returned") from None


def _refused(exc: app_config.ConfigUpdateError) -> HTTPException:
    headers = None
    if exc.current_version is not None:
        headers = {"ETag": exc.current_version.etag}
    return HTTPException(status_code=exc.status_code, detail=exc.detail, headers=headers)


@router.get("/config", response_model=ConfigResponse)
async def get_config(
    response: Response, _admin_id: str = Depends(verify_admin_access)
) -> ConfigResponse:
    """List every setting the console offers, with its effective value; secrets carry none.

    The ETag names the saved configuration's version; send it back as
    ``If-Match`` to make a change conditional on nothing having changed since.
    The table is re-read first: a save made through another worker would
    otherwise make this worker's view, and its version, up to one refresh
    interval old, and every conditional save from it a conflict.
    """
    try:
        await app_config.refresh()
    except Exception as exc:
        # The cached view is still correct to show; it may be a little older.
        logger.warning("Configuration refresh before listing failed (%s)", type(exc).__name__)
    return _response(response)


@router.patch("/config", response_model=ConfigResponse)
async def update_config(
    payload: UpdateConfigRequest,
    response: Response,
    admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
    if_match: str | None = Header(default=None),
) -> ConfigResponse:
    """Validate a batch of values together, store it in one transaction, and apply it.

    ``secrets`` marks keys secret, one-way. A key it names that ``values`` does
    not keeps its stored value and gains only the flag (400 when it has no
    stored row yet). The audit log records a secret's change, never its value.
    With ``If-Match``, nothing is stored (409) once the configuration has
    changed since that version was read.
    """
    try:
        changes = await app_config.update(
            payload.values,
            payload.secrets,
            updated_by=admin_id,
            expected_version=_expected(if_match),
        )
    except app_config.ConfigUpdateError as exc:
        raise _refused(exc) from None
    for change in changes:
        await log_admin_action(
            op_store,
            action="config.update",
            details=change.audit_details(),
            actor=actor,
            target=AuditTarget("config", change.key),
        )
    return _response(response, changes[0].version if changes else None)


@router.delete("/config/{key}", response_model=ConfigResponse)
async def reset_config(
    key: str,
    response: Response,
    admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
    if_match: str | None = Header(default=None),
) -> ConfigResponse:
    """Remove a stored value, so the setting falls back to the environment or its default."""
    try:
        change = await app_config.delete(
            key, updated_by=admin_id, expected_version=_expected(if_match)
        )
    except app_config.ConfigUpdateError as exc:
        raise _refused(exc) from None
    await log_admin_action(
        op_store,
        action="config.reset",
        details=change.audit_details(),
        actor=actor,
        target=AuditTarget("config", change.key),
    )
    return _response(response, change.version)


@router.get("/config/live", response_model=LiveConfigStatusResponse)
async def live_config_status(
    _admin_id: str = Depends(verify_admin_access),
) -> LiveConfigStatusResponse:
    """Show how the saved configuration has been applied: per group, per registered process.

    Applying a configuration is reported apart from delivering a message: an
    applied alert channel says nothing about whether the channel accepts posts.
    """
    local = live.COORDINATOR.local_status()
    store = app_config.attached_store()
    if store is None:
        return LiveConfigStatusResponse(database=False, read_ok=local["read_ok"])
    try:
        # The preview's saved targets and the counts must describe one version.
        status, entries = await live_control.consistent_read(
            store,
            refresh=app_config.refresh,
            entries=lambda: app_config.describe()["entries"],
            current_version=app_config.source_version,
        )
    except live_control.ControlError as exc:
        raise HTTPException(503, str(exc)) from None
    except Exception as exc:
        logger.warning("Live configuration status read failed (%s)", type(exc).__name__)
        raise HTTPException(503, "Could not read the configuration status; retry") from None
    status["this_instance"] = restart.INSTANCE_ID
    for row in (row for group in status["groups"] for row in group["instances"]):
        row["this_process"] = row["instance_id"] == restart.INSTANCE_ID
    local = live.COORDINATOR.local_status()
    for group in status["groups"]:
        group["local"] = local["groups"].get(group["name"])
        group["preview"] = live_control.preview(status, entries, group["name"])
    return LiveConfigStatusResponse(database=True, read_ok=local["read_ok"], **status)
