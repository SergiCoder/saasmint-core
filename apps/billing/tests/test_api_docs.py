"""Tests for OpenAPI schema and documentation endpoints (Swagger, ReDoc)."""

from __future__ import annotations

import pytest
from django.test import Client


@pytest.fixture
def schema_content(db):
    client = Client()
    resp = client.get("/api/schema/", HTTP_ACCEPT="application/json")
    assert resp.status_code == 200
    return resp.content.decode()


@pytest.mark.django_db
class TestOpenAPIEndpoints:
    def test_schema_endpoint_returns_json(self):
        client = Client()
        resp = client.get("/api/schema/")
        assert resp.status_code == 200

    def test_swagger_ui_returns_html(self):
        client = Client()
        resp = client.get("/api/docs/")
        assert resp.status_code == 200
        assert b"swagger" in resp.content.lower() or b"openapi" in resp.content.lower()

    def test_redoc_returns_html(self):
        client = Client()
        resp = client.get("/api/redoc/")
        assert resp.status_code == 200

    def test_schema_excludes_admin_paths(self, schema_content):
        assert "/admin/" not in schema_content

    def test_schema_excludes_webhook_paths(self, schema_content):
        assert "/api/v1/webhooks/" not in schema_content

    def test_schema_excludes_dashboard_paths(self, schema_content):
        assert "/dashboard/" not in schema_content

    def test_schema_excludes_hijack_paths(self, schema_content):
        assert "/hijack/" not in schema_content

    def test_schema_includes_billing_paths(self, schema_content):
        assert "/api/v1/billing/" in schema_content

    def test_schema_includes_account_paths(self, schema_content):
        assert "/api/v1/account/" in schema_content

    def test_schema_includes_orgs_paths(self, schema_content):
        assert "/api/v1/orgs/" in schema_content
