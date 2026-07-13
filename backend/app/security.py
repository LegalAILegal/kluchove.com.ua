"""Signed, expiring session tokens bound to a verified phone (the account id)."""
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import get_settings

SESSION_COOKIE = "kl_session"
_SALT = "kluchove-otp-session"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt=_SALT)


def issue_session(phone: str) -> str:
    return _serializer().dumps({"p": phone})


def read_session(token: str | None) -> str | None:
    """Return the phone the session is bound to, or None if absent/invalid/expired."""
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=get_settings().session_ttl_seconds)
    except (BadSignature, SignatureExpired):
        return None
    phone = data.get("p") if isinstance(data, dict) else None
    return phone or None
