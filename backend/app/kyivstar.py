"""Kyivstar SMS OTP Verification client: OAuth token + send + verify.

Secrets come from settings (.env). The access token is valid for 8 hours per the
portal, so we cache it and refresh a minute early.
"""
import time

import httpx

from .config import get_settings


class KyivstarError(Exception):
    """Upstream Kyivstar API failure (network or non-2xx)."""


class _TokenCache:
    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get(self, *, monotonic=time.monotonic) -> str | None:
        if self._token and monotonic() < self._expires_at:
            return self._token
        return None

    def set(self, token: str, ttl_seconds: int, *, monotonic=time.monotonic) -> None:
        self._token = token
        self._expires_at = monotonic() + max(0, ttl_seconds - 60)  # refresh 1 min early


_token_cache = _TokenCache()


async def _get_token(client: httpx.AsyncClient) -> str:
    cached = _token_cache.get()
    if cached:
        return cached

    s = get_settings()
    if not s.ks_client_id or not s.ks_client_secret:
        raise KyivstarError("Kyivstar client credentials are not configured")

    try:
        resp = await client.post(
            s.ks_token_url,
            data={"grant_type": "client_credentials"},
            auth=(s.ks_client_id, s.ks_client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        raise KyivstarError(f"token request failed: {exc}") from exc

    token = payload.get("access_token")
    if not token:
        raise KyivstarError("token response missing access_token")
    _token_cache.set(token, int(payload.get("expires_in", 8 * 3600)))
    return token


async def send_otp(phone: str) -> str:
    """Ask Kyivstar to generate + send an OTP SMS to `phone` (format 380XXXXXXXXX).

    Returns the operation id (`cid`) used to correlate the later verify call.
    Send response schema (confirmed from portal):
      { cid, reqId, resource: { status: "SUCCESS", messageId } }
    """
    s = get_settings()
    async with httpx.AsyncClient(timeout=15.0) as client:
        token = await _get_token(client)
        try:
            resp = await client.post(
                f"{s.ks_api_base}/verification/sms",
                json={"to": phone, "templateId": s.ks_template_id},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise KyivstarError(f"send_otp failed: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise KyivstarError(f"send_otp failed: {exc}") from exc

    data = _safe_json(resp)
    cid = data.get("cid") or data.get("reqId")
    if not cid:
        raise KyivstarError("send response missing cid")
    return str(cid)


async def verify_otp(phone: str, code: str) -> bool:
    """Confirm the user's `code` for `phone` via POST /verification/sms/check.

    Body: { subscriberId: phone, validationCode: code }.
    Success is resource.status == "VALID" (wrong code -> "INVALID").
    """
    s = get_settings()
    async with httpx.AsyncClient(timeout=15.0) as client:
        token = await _get_token(client)
        try:
            resp = await client.post(
                f"{s.ks_api_base}{s.ks_verify_path}",
                json={s.ks_verify_subscriber_field: phone, s.ks_verify_code_field: code},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise KyivstarError(f"verify_otp failed: {exc}") from exc

    if resp.status_code == 200:
        data = _safe_json(resp)
        resource = data.get("resource") if isinstance(data.get("resource"), dict) else {}
        return str(resource.get("status", "")).upper() == "VALID"
    if resp.status_code in (400, 401, 422):
        return False  # malformed / expired
    raise KyivstarError(f"verify_otp unexpected status: HTTP {resp.status_code}")


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
