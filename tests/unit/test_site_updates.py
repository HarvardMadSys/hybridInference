"""Unit tests for the site-updates (homepage announcements) schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_create_request_defaults():
    from serving.schemas_admin import CreateSiteUpdateRequest

    req = CreateSiteUpdateRequest(title="Hello")
    assert req.placement == "feed"
    assert req.published is True
    assert req.body == ""
    assert req.link_url is None
    assert req.link_label is None


def test_create_request_requires_title():
    from serving.schemas_admin import CreateSiteUpdateRequest

    with pytest.raises(ValidationError):
        CreateSiteUpdateRequest(title="")


def test_create_request_rejects_unknown_placement():
    from serving.schemas_admin import CreateSiteUpdateRequest

    with pytest.raises(ValidationError):
        CreateSiteUpdateRequest(title="x", placement="sidebar")


def test_update_request_exclude_unset_only_sets_supplied_fields():
    from serving.schemas_admin import UpdateSiteUpdateRequest

    # Only ``published`` was supplied — the partial-update path relies on
    # exclude_unset so untouched columns are left alone.
    req = UpdateSiteUpdateRequest(published=False)
    fields = req.model_dump(exclude_unset=True)
    assert fields == {"published": False}


def test_update_request_empty_is_empty():
    from serving.schemas_admin import UpdateSiteUpdateRequest

    assert UpdateSiteUpdateRequest().model_dump(exclude_unset=True) == {}


def test_create_request_rejects_explicit_null_body_via_update():
    # On the partial-update model, an explicit null for a NOT NULL column is a
    # 422, not a silent NULL write.
    from serving.schemas_admin import UpdateSiteUpdateRequest

    with pytest.raises(ValidationError):
        UpdateSiteUpdateRequest(body=None)
    with pytest.raises(ValidationError):
        UpdateSiteUpdateRequest(title=None)


@pytest.mark.parametrize("scheme_url", ["javascript:alert(1)", "data:text/html,x", "ftp://h/x"])
def test_link_url_rejects_non_http_schemes(scheme_url):
    from serving.schemas_admin import CreateSiteUpdateRequest

    with pytest.raises(ValidationError):
        CreateSiteUpdateRequest(title="x", link_url=scheme_url)


def test_link_url_accepts_http_and_https_and_normalizes_blank():
    from serving.schemas_admin import CreateSiteUpdateRequest

    assert CreateSiteUpdateRequest(title="x", link_url="https://a.com").link_url == "https://a.com"
    assert CreateSiteUpdateRequest(title="x", link_url="  ").link_url is None


def test_public_response_allows_null_banner():
    from serving.schemas_admin import PublicSiteUpdatesResponse

    resp = PublicSiteUpdatesResponse(banner=None, updates=[])
    assert resp.banner is None
    assert resp.updates == []
