"""Minimal async HTTP client for MAX Bot API.

Auth: header `Authorization: <token>` (no "Bearer" prefix).
Base: https://botapi.max.ru, overridable via MAX_API_BASE.

MAX is migrating clients to platform-api2.max.ru, which serves a chain rooted
at the Minitsifry CA that is absent from the default trust stores. Point
MAX_CA_BUNDLE at a PEM holding that root + sub CA to trust it for this client
only, instead of installing the root system-wide.

NOTE: the Minitsifry sub CA expires 2027-03-06. When MAX reissues it, a pinned
bundle goes stale and every request starts failing TLS — refresh the PEM before
that date.
"""
import logging
import os
import httpx

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("MAX_API_BASE", "https://botapi.max.ru").rstrip("/")
CA_BUNDLE = os.getenv("MAX_CA_BUNDLE", "").strip()


class MaxAPIError(Exception):
    pass


def _verify_option():
    """TLS verification setting: a pinned CA bundle, or the system default.

    A configured-but-missing bundle is fatal on purpose — silently falling back
    to the system store would look fine until the day it isn't.
    """
    if not CA_BUNDLE:
        return True
    if not os.path.isfile(CA_BUNDLE):
        raise MaxAPIError(f"MAX_CA_BUNDLE points at a missing file: {CA_BUNDLE}")
    logger.info(f"MAX API: pinned CA bundle {CA_BUNDLE}")
    return CA_BUNDLE


class MaxAPI:
    def __init__(self, token: str):
        self.token = token
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(70.0, connect=10.0),
            verify=_verify_option(),
            headers={
                "Authorization": token,
                "User-Agent": "maxos-bot/1.0",
            },
        )

    async def aclose(self):
        await self._client.aclose()

    async def _request(self, method: str, path: str, params: dict | None = None,
                       json_body: dict | None = None, timeout: float | None = None) -> dict:
        url = f"{BASE_URL}{path}"
        try:
            r = await self._client.request(
                method, url, params=params, json=json_body,
                timeout=timeout if timeout is not None else self._client.timeout,
            )
        except httpx.HTTPError as e:
            raise MaxAPIError(f"HTTP error {method} {path}: {e}") from e
        if r.status_code >= 400:
            raise MaxAPIError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        if not r.text:
            return {}
        try:
            return r.json()
        except ValueError as e:
            # MAX occasionally answers 2xx with an HTML error page from its edge;
            # surface it as a MaxAPIError so callers see the body, not a bare
            # "Expecting value: line 1 column 1".
            raise MaxAPIError(
                f"{method} {path} -> {r.status_code}, non-JSON body: {r.text[:500]}"
            ) from e

    async def get_me(self) -> dict:
        return await self._request("GET", "/me")

    async def get_updates(self, marker: int | None = None, timeout: int = 30,
                          limit: int = 100, types: list[str] | None = None) -> dict:
        params: dict = {"limit": limit, "timeout": timeout}
        if marker is not None:
            params["marker"] = marker
        if types:
            params["types"] = ",".join(types)
        return await self._request(
            "GET", "/updates", params=params,
            timeout=timeout + 15,
        )

    async def send_message(self, chat_id: int | None = None, user_id: int | None = None,
                           text: str = "", notify: bool = True,
                           attachments: list[dict] | None = None) -> dict:
        if chat_id is None and user_id is None:
            raise ValueError("chat_id or user_id required")
        params: dict = {}
        if chat_id is not None:
            params["chat_id"] = chat_id
        if user_id is not None:
            params["user_id"] = user_id
        body: dict = {"text": text, "notify": notify}
        if attachments:
            body["attachments"] = attachments
        return await self._request("POST", "/messages", params=params, json_body=body)

    async def upload_attachment(self, kind: str, filename: str, data: bytes) -> dict:
        """Upload media and return an attachment dict for send_message.

        kind: "image" | "video" | "audio" | "file".
        Flow: POST /uploads?type=<kind> -> upload URL, then multipart upload.
        Image uploads return {"photos": {...}}, others return {"token": ...}.
        """
        upload = await self._request("POST", "/uploads", params={"type": kind})
        url = upload.get("url")
        if not url:
            raise MaxAPIError(f"/uploads returned no url: {upload}")
        try:
            r = await self._client.post(
                url, files={"data": (filename, data)},
                timeout=httpx.Timeout(120.0, connect=10.0),
            )
        except httpx.HTTPError as e:
            raise MaxAPIError(f"upload failed for {kind}: {e}") from e
        if r.status_code >= 400:
            raise MaxAPIError(f"upload {kind} -> {r.status_code}: {r.text[:300]}")
        result = r.json() if r.text else {}
        if kind == "image":
            photos = result.get("photos")
            if not photos:
                raise MaxAPIError(f"image upload returned no photos: {result}")
            return {"type": "image", "payload": {"photos": photos}}
        # video/audio uploads may return the token in the /uploads response
        token = result.get("token") or upload.get("token")
        if not token:
            raise MaxAPIError(f"{kind} upload returned no token: {result}")
        return {"type": kind, "payload": {"token": token}}

    async def send_action(self, chat_id: int, action: str = "typing_on") -> dict:
        try:
            return await self._request(
                "POST", f"/chats/{chat_id}/actions",
                json_body={"action": action},
            )
        except MaxAPIError as e:
            logger.debug(f"send_action failed (non-critical): {e}")
            return {}
