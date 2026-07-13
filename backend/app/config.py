from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment / .env — no secrets in code."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Kyivstar OAuth2 (client credentials) ---
    ks_token_url: str = "https://api-gateway.kyivstar.ua/idp/oauth2/token"
    ks_client_id: str = ""
    ks_client_secret: str = ""

    # --- Kyivstar SMS OTP Verification API ---
    # Kyivstar GENERATES and sends the OTP; we correlate by `cid` from the send
    # response and confirm the user's code against the verify endpoint.
    # Base: mock .../mock/rest/v1 — prod .../rest/v1
    ks_api_base: str = "https://api-gateway.kyivstar.ua/mock/rest/v1"
    ks_template_id: int = 1
    # Verify endpoint (confirmed from portal). Body: subscriberId (the phone the OTP
    # was generated for) + validationCode (the code the user entered).
    # Response 200: { reqId, cid, resource: { status: "VALID" | "INVALID" } }.
    ks_verify_path: str = "/verification/sms/check"
    ks_verify_subscriber_field: str = "subscriberId"
    ks_verify_code_field: str = "validationCode"

    # --- Telegram Gateway (second OTP channel; empty token = disabled -> SMS only) ---
    # Access token from https://gateway.telegram.org. When set, OTP delivery tries
    # Telegram first (checkSendAbility -> sendVerificationMessage) and falls back to
    # Kyivstar SMS when the number is not reachable on Telegram.
    tg_gateway_token: str = ""
    tg_gateway_base: str = "https://gatewayapi.telegram.org"
    tg_code_length: int = 6                 # Telegram generates a code of this length (4–8)

    # --- Google Sheets (private, service account) ---
    sheet_id: str = "1HqqepslB5VUAm24cpubUDtt015DqQsmXy6YoXEKMCIU"
    sheet_range: str = "A2:N"  # skip header row; columns A..N cover name/address/debt/phone
    google_credentials_file: str = "/run/secrets/google_sa.json"

    # --- Session / challenge security ---
    session_secret: str = "change-me-in-env"
    session_ttl_seconds: int = 900          # signed session lifetime after successful OTP
    challenge_ttl_seconds: int = 300        # how long an unverified OTP challenge lives
    max_code_attempts: int = 5              # wrong-code attempts before a challenge is burned

    # --- Rate limiting (anti SMS-bomb / enumeration) ---
    rl_max_requests_per_phone: int = 3      # OTP sends per phone per window
    rl_max_requests_per_ip: int = 10        # OTP sends per IP per window
    rl_window_seconds: int = 3600

    # Comma-separated origins allowed by CORS (empty = same-origin only, via nginx)
    cors_origins: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
