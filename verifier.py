# File: verifier.py
import re
from dataclasses import dataclass
from typing import Optional
import httpx

USERNAME_REGEX = re.compile(r"^[A-Za-z0-9_]{1,15}$")


@dataclass
class XLookupResult:
    discoverable: bool
    status_label: str
    username: str
    x_user_id: Optional[str] = None
    x_name: Optional[str] = None
    x_created_at: Optional[str] = None
    protected: Optional[bool] = None
    detail: str = ""
    api_error: bool = False


class XReadOnlyVerifier:
    BASE_URL = "https://api.x.com/2/users/by/username"

    def __init__(self, bearer_token: str) -> None:
        self.bearer_token = bearer_token

    async def verify_username(self, raw_username: str) -> XLookupResult:
        clean_username = raw_username.lstrip("@").strip()
        if not USERNAME_REGEX.match(clean_username):
            return XLookupResult(
                discoverable=False,
                status_label="INVALID_FORMAT",
                username=clean_username,
                detail="Username must be 1-15 characters (letters, numbers, or underscore).",
                api_error=True,
            )

        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Accept": "application/json",
            "User-Agent": "HoloweenSequenceVerifierBot/2.0",
        }
        params = {"user.fields": "id,name,username,created_at,protected"}
        url = f"{self.BASE_URL}/{clean_username}"

        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.get(url, headers=headers, params=params)

            if resp.status_code == 400:
                return XLookupResult(
                    discoverable=False,
                    status_label="API_BAD_REQUEST",
                    username=clean_username,
                    detail="X API returned 400 Bad Request. Invalid query syntax.",
                    api_error=True,
                )
            if resp.status_code == 401:
                return XLookupResult(
                    discoverable=False,
                    status_label="API_UNAUTHORIZED",
                    username=clean_username,
                    detail="X API Bearer Token unauthorized (HTTP 401). Check X_API_BEARER_TOKEN.",
                    api_error=True,
                )
            if resp.status_code == 403:
                return XLookupResult(
                    discoverable=False,
                    status_label="API_FORBIDDEN",
                    username=clean_username,
                    detail="X API access forbidden (HTTP 403). Check developer portal app permissions.",
                    api_error=True,
                )
            if resp.status_code == 404:
                return XLookupResult(
                    discoverable=False,
                    status_label="NOT_FOUND_OR_AVAILABLE",
                    username=clean_username,
                    detail="Username not found on X (HTTP 404). Available for registration.",
                )
            if resp.status_code == 429:
                reset_epoch = resp.headers.get("x-rate-limit-reset", "unknown")
                return XLookupResult(
                    discoverable=False,
                    status_label="RATE_LIMITED",
                    username=clean_username,
                    detail=f"X API rate limit reached (HTTP 429). Reset epoch: {reset_epoch}.",
                    api_error=True,
                )
            if resp.status_code >= 500:
                return XLookupResult(
                    discoverable=False,
                    status_label="X_SERVER_ERROR",
                    username=clean_username,
                    detail=f"X API server error (HTTP {resp.status_code}). Try again later.",
                    api_error=True,
                )

            try:
                payload = resp.json()
            except Exception:
                return XLookupResult(
                    discoverable=False,
                    status_label="INVALID_JSON",
                    username=clean_username,
                    detail="Received invalid JSON response from X API.",
                    api_error=True,
                )

            if "data" in payload and isinstance(payload["data"], dict):
                data = payload["data"]
                return XLookupResult(
                    discoverable=True,
                    status_label="PUBLICLY_DISCOVERABLE",
                    username=data.get("username", clean_username),
                    x_user_id=str(data.get("id", "")),
                    x_name=str(data.get("name", "")),
                    x_created_at=data.get("created_at"),
                    protected=bool(data.get("protected", False)),
                    detail="Profile verified as publicly discoverable via official X API v2.",
                )

            if "errors" in payload and isinstance(payload["errors"], list):
                first_err = payload["errors"][0]
                detail_msg = first_err.get("detail", "User not found.")
                if "suspended" in detail_msg.lower():
                    return XLookupResult(
                        discoverable=False,
                        status_label="SUSPENDED_ON_X",
                        username=clean_username,
                        detail=detail_msg,
                    )
                return XLookupResult(
                    discoverable=False,
                    status_label="NOT_FOUND_OR_AVAILABLE",
                    username=clean_username,
                    detail=detail_msg,
                )

            return XLookupResult(
                discoverable=False,
                status_label="UNRECOGNIZED_RESPONSE",
                username=clean_username,
                detail="Unrecognized response structure from X API.",
                api_error=True,
            )

        except httpx.TimeoutException:
            return XLookupResult(
                discoverable=False,
                status_label="NETWORK_TIMEOUT",
                username=clean_username,
                detail="Connection timed out while communicating with api.x.com.",
                api_error=True,
            )
        except httpx.HTTPError as exc:
            return XLookupResult(
                discoverable=False,
                status_label="NETWORK_ERROR",
                username=clean_username,
                detail=f"Network error during X API lookup: {exc}",
                api_error=True,
            )
