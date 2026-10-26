"""The attribution contract of an audit row (``serving.audit``).

What a writer may say about who acted and on what, and how that becomes the
row's columns. Legacy arguments are stored raw and never re-typed; new
attribution cannot be contradicted by a legacy argument passed beside it.
"""

from __future__ import annotations

import dataclasses

import pytest

from serving.audit import AuditActor, AuditTarget, audit_columns


class TestAuditActor:
    @pytest.mark.parametrize("kind", ["admin", "user"])
    def test_an_account_actor_needs_its_stable_id(self, kind):
        with pytest.raises(ValueError, match="user id"):
            AuditActor(kind, None, "192.0.2.1")

    @pytest.mark.parametrize("kind", ["admin_token", "anonymous", "bootstrap", "system"])
    def test_a_non_account_actor_cannot_claim_an_account(self, kind):
        """The shared token, a registration or setup has no account to name."""
        with pytest.raises(ValueError, match="no account id"):
            AuditActor(kind, "u-1", "192.0.2.1")

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError, match="unknown audit actor type"):
            AuditActor("root", None)  # type: ignore[arg-type]

    @pytest.mark.parametrize("unresolved", ["unknown", "", None])
    def test_an_untrusted_source_is_no_address(self, unresolved):
        assert AuditActor.admin("u-1", unresolved).source_ip is None

    def test_actors_are_immutable(self):
        actor = AuditActor.user("u-1", "192.0.2.1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            actor.id = "u-2"  # type: ignore[misc]


class TestAuditTarget:
    def test_only_a_user_target_is_an_affected_user(self):
        assert AuditTarget.user("u-1").user_id == "u-1"
        assert AuditTarget.model("glm-4.6").user_id is None
        assert AuditTarget.provider("openrouter").user_id is None

    def test_a_target_needs_a_known_type_and_an_id(self):
        with pytest.raises(ValueError):
            AuditTarget("widget", "w-1")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            AuditTarget.model("")


class TestAuditColumns:
    def test_an_actor_supplies_the_source_and_admin_ip_mirrors_it(self):
        columns = audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=AuditActor.admin("u-admin", "192.0.2.7"),
            target=AuditTarget.model("glm-4.6"),
        )
        assert (columns.actor_type, columns.actor_id) == ("admin", "u-admin")
        assert columns.source_ip == columns.admin_ip == "192.0.2.7"
        assert (columns.target_type, columns.target_id, columns.target_user_id) == (
            "model",
            "glm-4.6",
            None,
        )

    def test_an_actor_without_an_address_stores_null_and_the_honest_word(self):
        columns = audit_columns(
            admin_ip=None, target_user_id=None, actor=AuditActor.admin_token(None), target=None
        )
        assert columns.source_ip is None
        assert columns.admin_ip == "unknown"

    def test_a_user_target_also_fills_target_user_id(self):
        columns = audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=AuditActor.anonymous(None),
            target=AuditTarget.user("u-new"),
        )
        assert columns.target_user_id == "u-new"

    def test_a_legacy_call_is_stored_raw_and_not_retyped(self):
        """No actor, no source, and a target_user_id that is not assumed a user."""
        columns = audit_columns(
            admin_ip="ops@example.test", target_user_id="openrouter", actor=None, target=None
        )
        assert columns.admin_ip == "ops@example.test"
        assert columns.target_user_id == "openrouter"
        assert columns.actor_type is columns.actor_id is columns.source_ip is None
        assert columns.target_type is columns.target_id is None

    def test_a_legacy_argument_cannot_contradict_new_attribution(self):
        actor = AuditActor.admin("u-admin", "192.0.2.7")
        with pytest.raises(ValueError):
            audit_columns(
                admin_ip="ops@example.test", target_user_id=None, actor=actor, target=None
            )
        with pytest.raises(ValueError):
            audit_columns(
                admin_ip=None,
                target_user_id="openrouter",
                actor=actor,
                target=AuditTarget.provider("openrouter"),
            )
