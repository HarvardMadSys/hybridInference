"""Ownership declarations reject cross-extension claims in either registration order."""

from __future__ import annotations

import pytest

from serving.config import app_config_registry as registry


@pytest.mark.parametrize("migration_first", [False, True])
def test_entry_and_migration_owners_cannot_disagree(extension_registry, migration_first):
    entry = registry.ConfigEntry("SAMPLE_REGION", "email", "A sample delivery region.")
    claim = registry.ConfigOwnershipMigration(
        id="sample-claim-v1", owner="extension:first", keys=(entry.key,)
    )
    if migration_first:
        registry.register_config_ownership_migration(claim)
        with pytest.raises(ValueError):
            registry.register_config_entries(
                [registry.ConfigEntry("SAMPLE_OTHER", "email", "Unrelated entry."), entry],
                owner="extension:second",
            )
        assert registry.static_entry("SAMPLE_OTHER") is None
        assert registry.static_entry(entry.key) is None
        assert registry.ownership_migrations() == (claim,)
    else:
        registry.register_config_entries([entry], owner="extension:second")
        with pytest.raises(ValueError):
            registry.register_config_ownership_migration(claim)
        assert registry.static_entry(entry.key).owner == "extension:second"
        assert registry.ownership_migrations() == ()


def test_overlapping_migration_claims_require_the_same_owner(extension_registry):
    first = registry.ConfigOwnershipMigration(
        id="first-v1", owner="extension:first", keys=("SAMPLE_REGION",)
    )
    registry.register_config_ownership_migration(first)
    with pytest.raises(ValueError):
        registry.register_config_ownership_migration(
            registry.ConfigOwnershipMigration(
                id="second-v1", owner="extension:second", keys=("SAMPLE_REGION", "SAMPLE_OTHER")
            )
        )
    assert registry.ownership_migrations() == (first,)
    followup = registry.ConfigOwnershipMigration(
        id="first-v2",
        owner="extension:first",
        keys=("SAMPLE_REGION",),
        secret_keys=("SAMPLE_REGION",),
    )
    registry.register_config_ownership_migration(followup)
    assert registry.ownership_migrations() == (first, followup)
