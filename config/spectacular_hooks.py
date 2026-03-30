"""drf-spectacular preprocessing hooks."""

from __future__ import annotations

from typing import Any


def preprocess_exclude_spectacular_views(endpoints: list[Any], **kwargs: object) -> list[Any]:
    """Exclude drf-spectacular's own UI views from schema generation.

    SpectacularSwaggerView, SpectacularRedocView, and SpectacularAPIView do not
    use drf-spectacular's AutoSchema, so the schema generator asserts on them.
    They are internal UI views and should never appear in the API schema.
    """
    return [
        (path, path_regex, method, callback)
        for path, path_regex, method, callback in endpoints
        if not getattr(callback.cls, "__module__", "").startswith("drf_spectacular")
    ]
