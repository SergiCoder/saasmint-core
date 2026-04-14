"""Tests for drf-spectacular preprocessing hooks."""

from __future__ import annotations

from unittest.mock import MagicMock

from config.spectacular_hooks import preprocess_exclude_spectacular_views


def _make_endpoint(
    module: str,
    path: str = "/api/v1/test/",
    method: str = "GET",
) -> tuple[str, str, str, MagicMock]:
    callback = MagicMock()
    callback.cls.__module__ = module
    return (path, r"^api/v1/test/$", method, callback)


class TestPreprocessExcludeSpectacularViews:
    def test_removes_spectacular_swagger_view(self) -> None:
        endpoints = [
            _make_endpoint("drf_spectacular.views"),
            _make_endpoint("apps.billing.views", "/api/v1/billing/plans/"),
        ]
        result = preprocess_exclude_spectacular_views(endpoints)
        assert len(result) == 1
        assert result[0][0] == "/api/v1/billing/plans/"

    def test_removes_spectacular_submodule_views(self) -> None:
        endpoints = [
            _make_endpoint("drf_spectacular.contrib.something"),
            _make_endpoint("apps.users.views"),
        ]
        result = preprocess_exclude_spectacular_views(endpoints)
        assert len(result) == 1

    def test_keeps_non_spectacular_views(self) -> None:
        endpoints = [
            _make_endpoint("apps.billing.views", "/api/v1/billing/plans/"),
            _make_endpoint("apps.users.views", "/api/v1/account/"),
            _make_endpoint("apps.orgs.views", "/api/v1/orgs/"),
        ]
        result = preprocess_exclude_spectacular_views(endpoints)
        assert len(result) == 3

    def test_empty_endpoints_returns_empty(self) -> None:
        result = preprocess_exclude_spectacular_views([])
        assert result == []

    def test_all_spectacular_returns_empty(self) -> None:
        endpoints = [
            _make_endpoint("drf_spectacular.views"),
            _make_endpoint("drf_spectacular.openapi"),
        ]
        result = preprocess_exclude_spectacular_views(endpoints)
        assert result == []

    def test_preserves_endpoint_tuple_structure(self) -> None:
        endpoint = _make_endpoint("apps.billing.views", "/api/v1/billing/plans/", "POST")
        result = preprocess_exclude_spectacular_views([endpoint])
        assert len(result) == 1
        path, _path_regex, method, _callback = result[0]
        assert path == "/api/v1/billing/plans/"
        assert method == "POST"
