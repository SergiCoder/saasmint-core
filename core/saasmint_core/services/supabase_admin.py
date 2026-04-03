"""Supabase Admin API — user and storage management via service_role key."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


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
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.delete(url, headers=headers)

    if resp.status_code in (200, 204):
        logger.info("Deleted Supabase user %s", supabase_uid)
    elif resp.status_code == 404:
        logger.info("Supabase user %s already deleted", supabase_uid)
    else:
        logger.error(
            "Failed to delete Supabase user %s: %s %s",
            supabase_uid,
            resp.status_code,
            resp.text,
        )
        resp.raise_for_status()


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
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.delete(url, headers=headers)

    if resp.status_code in (200, 204):
        logger.info("Deleted avatar from storage: %s", object_path)
    elif resp.status_code == 404:
        logger.info("Avatar already deleted from storage: %s", object_path)
    else:
        logger.error(
            "Failed to delete avatar %s: %s %s",
            object_path,
            resp.status_code,
            resp.text,
        )
        resp.raise_for_status()
