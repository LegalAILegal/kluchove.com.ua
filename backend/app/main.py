"""kluchove.com.ua — phone-first SMS OTP authorization backend.

Flow (WayForPay-style single field: "Вхід | Реєстрація"):
  POST /api/auth/request {phone}          -> send OTP to the entered number,
                                             return masked phone + challenge token
  POST /api/auth/verify {challenge, code} -> confirm with Kyivstar, upsert account,
                                             auto-link every sheet row whose col-N
                                             phone matches, set session, return data
  GET  /api/me                            -> account's linked objects (+ debt) and
                                             any pending manual-claim status
  POST /api/objects/claim {name, address} -> file a claim for a row with no phone
                                             on file; owner approves it in the sheet
  POST /api/auth/logout                   -> clear session

Identity = the verified phone. Which property objects belong to an account is a
separate binding stored in the sheet (see accounts.py).
"""
import logging

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .accounts import repo as accounts
from .config import get_settings
from . import telegram as tg
from .kyivstar import KyivstarError, send_otp, verify_otp
from .matching import name_matches, normalize_address, normalize_phone
from .telegram import TelegramError
from .security import SESSION_COOKIE, issue_session, read_session
from .sheets import Tenant, repo
from .store import store

logger = logging.getLogger("kluchove.auth")

app = FastAPI(title="kluchove.com.ua auth", docs_url=None, redoc_url=None, openapi_url=None)

_settings = get_settings()
if _settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _settings.cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["POST", "GET"],
        allow_headers=["Content-Type"],
    )


# --- request models -----------------------------------------------------
class RequestBody(BaseModel):
    phone: str = Field(min_length=5, max_length=20)


class VerifyBody(BaseModel):
    challenge: str = Field(min_length=8, max_length=128)
    code: str = Field(min_length=3, max_length=12)


class ClaimBody(BaseModel):
    name: str = Field(min_length=3, max_length=200)
    address: str = Field(default="", max_length=300)


# --- helpers ------------------------------------------------------------
def _client_ip(request: Request) -> str:
    # nginx sets X-Forwarded-For; take the first hop.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _mask_phone(phone: str) -> str:
    # 380671234567 -> +38067••••67
    if len(phone) < 6:
        return "•••"
    return f"+{phone[:5]}••••{phone[-2:]}"


def _object_payload(t: Tenant) -> dict:
    name = t.name.split("/")[0].strip()
    addr = normalize_address(t.address)
    purpose = ", ".join(p for p in (addr, name) if p)
    return {
        "key": t.key,
        "name": t.name,
        "address": t.address,
        "debt": t.debt,
        "payment_purpose": purpose,
    }


def _suggest(name: str, address: str) -> Tenant | None:
    """Best fuzzy match for a manual claim (mirrors the front-end matching)."""
    candidates = [t for t in repo.all() if name_matches(name, t.name)]
    na = normalize_address(address).lower() if address else ""
    if na and candidates:
        exact = [t for t in candidates if normalize_address(t.address).lower() == na]
        partial = [t for t in candidates if na in normalize_address(t.address).lower()]
        candidates = exact or partial or candidates
    return candidates[0] if candidates else None


def _resolve(text: str) -> Tenant | None:
    """Resolve an owner-approved value to a row: a normalized object_key, or free
    text (a name or address the owner typed into `resolved_key`)."""
    if not text:
        return None
    t = repo.find_by_key(text)
    if t:
        return t
    for cand in repo.all():
        if name_matches(text, cand.name):
            return cand
    na = normalize_address(text).lower()
    if na:
        for cand in repo.all():
            if na in normalize_address(cand.address).lower():
                return cand
    return None


def _me_payload(phone: str) -> dict:
    """Sync the account's bindings from the sheet, then return its objects.

    The binding IS column N: an object belongs to an account exactly when its
    col-N phone matches. So there is nothing to auto-link — we only materialize
    owner-approved manual claims (which write the phone into col N), then read
    every row that now carries this phone.
    """
    accounts.ensure_account(phone)

    def _bind(text: str) -> bool:
        t = _resolve(text)
        return t is not None and repo.set_phone(t.key, phone) is not None

    accounts.materialize_approved(phone, _bind)

    objects = [_object_payload(t) for t in repo.find_by_phone(phone)]

    pending = [
        {"entered_name": r["entered_name"], "entered_address": r["entered_address"]}
        for r in accounts.requests_for_phone(phone)
        if r["status"] in ("pending", "approved")
    ]
    return {"phone": _mask_phone(phone), "objects": objects, "pending": pending}


def _set_session(response: Response, phone: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(phone),
        max_age=get_settings().session_ttl_seconds,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )


# --- routes -------------------------------------------------------------
@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/auth/request")
async def auth_request(body: RequestBody, request: Request, response: Response) -> dict:
    s = get_settings()
    ip = _client_ip(request)

    if store.hit_rate_limit(f"ip:{ip}", s.rl_max_requests_per_ip, s.rl_window_seconds):
        response.status_code = 429
        return {"status": "rate_limited"}

    phone = normalize_phone(body.phone)
    if not phone:
        response.status_code = 422
        return {"status": "invalid_phone"}

    if store.hit_rate_limit(f"phone:{phone}", s.rl_max_requests_per_phone, s.rl_window_seconds):
        response.status_code = 429
        return {"status": "rate_limited"}

    # Route the OTP: Telegram first (cheap, if the number is reachable there),
    # otherwise Kyivstar SMS. SMS also catches any Telegram send failure.
    op_id: str | None = None
    channel = "sms"
    if tg.enabled():
        try:
            if await tg.can_send(phone):
                op_id = await tg.send_otp(phone)
                channel = "tg"
        except TelegramError as exc:
            logger.warning("telegram send failed, falling back to SMS: %s", exc)
            op_id = None

    if op_id is None:
        try:
            op_id = await send_otp(phone)
        except KyivstarError as exc:
            logger.error("send_otp failed: %s", exc)
            response.status_code = 502
            return {"status": "sms_failed"}
        channel = "sms"

    challenge = store.create_challenge(op_id, phone, channel)
    return {"status": "sent", "channel": channel,
            "masked_phone": _mask_phone(phone), "challenge": challenge}


@app.post("/api/auth/verify")
async def auth_verify(body: VerifyBody, response: Response) -> dict:
    challenge = store.get_challenge(body.challenge)
    if challenge is None:
        response.status_code = 410
        return {"status": "expired"}

    try:
        if challenge.channel == "tg":
            ok = await tg.verify_otp(challenge.cid, body.code)
        else:
            ok = await verify_otp(challenge.phone, body.code)
    except (KyivstarError, TelegramError) as exc:
        logger.error("verify_otp failed: %s", exc)
        response.status_code = 502
        return {"status": "verify_failed"}

    if not ok:
        left = store.register_attempt(body.challenge)
        response.status_code = 401
        return {"status": "invalid_code", "attempts_left": left}

    phone = challenge.phone
    store.consume_challenge(body.challenge)

    _set_session(response, phone)
    return {"status": "ok", "data": _me_payload(phone)}


@app.get("/api/me")
async def me(request: Request, response: Response) -> dict:
    phone = read_session(request.cookies.get(SESSION_COOKIE))
    if not phone:
        response.status_code = 401
        return {"status": "unauthorized"}
    return {"status": "ok", "data": _me_payload(phone)}


@app.post("/api/objects/claim")
async def claim_object(body: ClaimBody, request: Request, response: Response) -> dict:
    phone = read_session(request.cookies.get(SESSION_COOKIE))
    if not phone:
        response.status_code = 401
        return {"status": "unauthorized"}

    suggestion = _suggest(body.name, body.address)
    if suggestion is not None and normalize_phone(suggestion.phone) == phone:
        return {"status": "already_linked"}

    accounts.request_add(
        phone,
        body.name.strip(),
        body.address.strip(),
        suggestion.label if suggestion else "",
        suggestion.key if suggestion else "",
    )
    return {"status": "submitted"}


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}
