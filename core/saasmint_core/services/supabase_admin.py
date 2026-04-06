"""Supabase Admin API — user and storage management via service_role key."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


def _service_role_headers(service_role_key: str) -> dict[str, str]:
    """Build the auth headers required by Supabase Admin / Storage APIs."""
    return {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
    }


async def _delete_resource(url: str, headers: dict[str, str], *, resource_label: str) -> None:
    """Send a DELETE request and handle the standard success/404/error responses."""
    async with httpx.AsyncClient() as client:
        resp = await client.delete(url, headers=headers)

    if resp.status_code in (200, 204):
        logger.info("Deleted %s", resource_label)
    elif resp.status_code == 404:
        logger.info("Already deleted: %s", resource_label)
    else:
        logger.error(
            "Failed to delete %s: %s %s",
            resource_label,
            resp.status_code,
            resp.text,
        )
        resp.raise_for_status()


async def delete_supabase_user(
    *, supabase_url: str, service_role_key: str, supabase_uid: str
) -> None:
    """Delete a user from Supabase Auth via the Admin API.

    Requires the service_role key (never expose to the client).
    Silently succeeds if the user has already been deleted.
    """
    if not service_role_key:
        logger.warning(
            "Supabase service role key not configured — skipping Supabase user deletion for %s",
            supabase_uid,
        )
        return

    url = f"{supabase_url}/auth/v1/admin/users/{supabase_uid}"
    await _delete_resource(
        url, _service_role_headers(service_role_key), resource_label=f"Supabase user {supabase_uid}"
    )


def _extract_storage_path(avatar_url: str, supabase_url: str) -> str | None:
    """Extract the object path from a Supabase Storage public URL.

    Expected format: {supabase_url}/storage/v1/object/public/{bucket}/{path}
    Returns the full ``{bucket}/{path}`` portion, or ``None`` if the URL
    doesn't belong to this Supabase project's storage.
    """
    parsed = urlparse(avatar_url)
    supabase_host = urlparse(supabase_url).hostname
    if parsed.hostname != supabase_host:
        return None
    prefix = "/storage/v1/object/public/"
    if not parsed.path.startswith(prefix):
        return None
    return parsed.path[len(prefix) :]


async def delete_supabase_avatar(
    *, supabase_url: str, service_role_key: str, avatar_url: str | None
) -> None:
    """Delete an avatar file from Supabase Storage.

    Silently succeeds if the avatar is ``None``, hosted externally, or
    already deleted.
    """
    if not avatar_url or not service_role_key:
        return

    object_path = _extract_storage_path(avatar_url, supabase_url)
    if object_path is None:
        logger.info("Avatar URL is external, skipping storage deletion: %s", avatar_url)
        return

    # The Supabase Storage Admin API deletes objects via POST with a list of paths.
    # Endpoint: /storage/v1/object/{bucket}  — body: {"prefixes": ["path"]}
    # But the simpler per-object delete is: DELETE /storage/v1/object/{bucket}/{path}
    bucket, _, path = object_path.partition("/")
    if not path:
        logger.warning("Could not parse bucket/path from avatar URL: %s", avatar_url)
        return

    url = f"{supabase_url}/storage/v1/object/{bucket}/{path}"
    await _delete_resource(
        url, _service_role_headers(service_role_key), resource_label=f"avatar {object_path}"
    )
