"""Telegram Gateway OTP client — a second delivery channel alongside Kyivstar SMS.

Telegram delivers the verification code to a phone number (E.164) for ~$0.01,
refunded if undelivered within the TTL. Identity stays the phone, so this slots
in behind the same phone-first flow; only the send/verify transport differs.

Flow mirrors kyivstar.py:
  can_send(phone)          -> checkSendAbility: is the number reachable on Telegram?
  send_otp(phone)          -> sendVerificationMessage (Telegram generates the code),
                              returns the request_id used to verify later
  verify_otp(request_id,c) -> checkVerificationStatus, True iff status == code_valid

All responses are {"ok": bool, "result"|"error": ...}. The token comes from
settings (gateway.telegram.org). If no token is configured the channel is simply
disabled and the caller falls back to SMS.
"""
import httpx

from .config import get_settings


class TelegramError(Exception):
    """Upstream Telegram Gateway failure (network or non-ok response)."""


def enabled() -> bool:
    return bool(get_settings().tg_gateway_token)


async def _post(client: httpx.AsyncClient, method: str, body: dict) -> dict:
    s = get_settings()
    try:
        resp = await client.post(
            f"{s.tg_gateway_base}/{method}",
            json=body,
            headers={
                "Authorization": f"Bearer {s.tg_gateway_token}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise TelegramError(f"{method} failed: {exc}") from exc
    except ValueError as exc:
        raise TelegramError(f"{method}: invalid JSON") from exc

    if not isinstance(data, dict) or not data.get("ok"):
        # Not an exception for our purposes when it just means "cannot send"
        raise TelegramError(f"{method}: {(data or {}).get('error', 'not ok')}")
    return data.get("result", {}) if isinstance(data.get("result"), dict) else {}


async def can_send(phone: str) -> bool:
    """checkSendAbility: True if a code can be delivered to `phone` via Telegram.

    Free pre-check used to route: reachable -> Telegram, otherwise -> SMS.
    Any error (number not on Telegram, etc.) means "not reachable" -> False.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            await _post(client, "checkSendAbility", {"phone_number": phone})
            return True
        except TelegramError:
            return False


async def send_otp(phone: str) -> str:
    """Send a Telegram-generated verification code; return its request_id."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=15.0) as client:
        result = await _post(client, "sendVerificationMessage", {
            "phone_number": phone,
            "code_length": s.tg_code_length,
            "ttl": s.challenge_ttl_seconds,
        })
    request_id = result.get("request_id")
    if not request_id:
        raise TelegramError("send response missing request_id")
    return str(request_id)


async def verify_otp(request_id: str, code: str) -> bool:
    """Confirm the user's `code` for a prior send via checkVerificationStatus."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        result = await _post(client, "checkVerificationStatus", {
            "request_id": request_id,
            "code": code,
        })
    status = ""
    vs = result.get("verification_status")
    if isinstance(vs, dict):
        status = str(vs.get("status", ""))
    return status == "code_valid"
