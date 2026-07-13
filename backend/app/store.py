"""In-memory challenge + rate-limit store.

Single-instance only. For multiple backend replicas, swap this for Redis
(same interface). TTLs keep it from growing unbounded.
"""
import secrets
import threading
import time
from dataclasses import dataclass

from .config import get_settings


@dataclass
class Challenge:
    cid: str                  # provider operation id: Kyivstar cid / Telegram request_id
    phone: str                # the number the OTP was sent to (== the account id)
    channel: str              # "sms" (Kyivstar) or "tg" (Telegram Gateway)
    created_at: float
    attempts: int = 0


@dataclass
class _Window:
    count: int = 0
    reset_at: float = 0.0


class Store:
    def __init__(self, *, monotonic=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._monotonic = monotonic
        self._challenges: dict[str, Challenge] = {}
        self._rl: dict[str, _Window] = {}

    # --- rate limiting ---------------------------------------------------
    def hit_rate_limit(self, key: str, limit: int, window: int) -> bool:
        """Register one event for `key`. Returns True if the limit is now exceeded."""
        now = self._monotonic()
        with self._lock:
            w = self._rl.get(key)
            if w is None or now >= w.reset_at:
                w = _Window(count=0, reset_at=now + window)
                self._rl[key] = w
            w.count += 1
            return w.count > limit

    # --- challenges ------------------------------------------------------
    def create_challenge(self, cid: str, phone: str, channel: str = "sms") -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._challenges[token] = Challenge(
                cid=cid,
                phone=phone,
                channel=channel,
                created_at=self._monotonic(),
            )
        return token

    def get_challenge(self, token: str) -> Challenge | None:
        s = get_settings()
        now = self._monotonic()
        with self._lock:
            c = self._challenges.get(token)
            if c is None:
                return None
            if now - c.created_at > s.challenge_ttl_seconds:
                del self._challenges[token]
                return None
            return c

    def register_attempt(self, token: str) -> int:
        """Increment attempt count; burn the challenge past the max. Returns attempts left."""
        s = get_settings()
        with self._lock:
            c = self._challenges.get(token)
            if c is None:
                return 0
            c.attempts += 1
            left = s.max_code_attempts - c.attempts
            if left <= 0:
                del self._challenges[token]
            return max(0, left)

    def consume_challenge(self, token: str) -> None:
        with self._lock:
            self._challenges.pop(token, None)

    def sweep(self) -> None:
        """Drop expired challenges and rate-limit windows (call periodically)."""
        s = get_settings()
        now = self._monotonic()
        with self._lock:
            for tok in [t for t, c in self._challenges.items()
                        if now - c.created_at > s.challenge_ttl_seconds]:
                del self._challenges[tok]
            for k in [k for k, w in self._rl.items() if now >= w.reset_at]:
                del self._rl[k]


store = Store()
