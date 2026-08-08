"""Typed application settings.

Everything is read from the environment. Nothing secret is ever defaulted to a
usable value: the production validators below refuse to start the service with
a placeholder secret, a wildcard CORS origin, or debug mode left on. A service
that will not boot is a much better failure mode than one that boots insecurely.
"""

from __future__ import annotations

import base64
import secrets
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any, Self

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import AnyHttpUrl, BeforeValidator, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

MIN_SECRET_LENGTH = 32


def _hkdf(root: bytes, *, info: str, length: int = 32) -> bytes:
    """Derive an independent subkey from the master secret.

    HKDF's `info` label is what separates purposes: two labels produce two keys
    with no computable relationship, so a subkey that leaks cannot be walked
    back to the master or across to a sibling. This is why the service needs
    only one configured secret rather than one per purpose.
    """
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=None,
        info=info.encode("utf-8"),
    ).derive(root)


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"

    @property
    def is_production_like(self) -> bool:
        return self in (Environment.PRODUCTION, Environment.STAGING)


def _split_csv(value: Any) -> Any:
    """Accept both `a,b` and a real list, so env files and code agree."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


#: A list that reads from the environment as `a,b,c`.
#:
#: ``NoDecode`` is essential. For any complex field type, pydantic-settings
#: tries ``json.loads`` on the raw environment value *before* validators run,
#: so a plain `list[str]` would reject `http://a,http://b` with a JSON parse
#: error and only accept `["http://a","http://b"]`. NoDecode hands the raw
#: string through so the BeforeValidator below can split it.
CommaSeparated = Annotated[list[str], NoDecode, BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- Identity ----------------------------------------------------------
    project_name: str = "Basivo Orch Api"
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False

    public_base_url: AnyHttpUrl = Field(default="http://localhost:8000")  # type: ignore[assignment]
    """Where this API is reachable. Used to build links inside emails."""

    frontend_base_url: AnyHttpUrl = Field(default="http://localhost:3000")  # type: ignore[assignment]
    """Where the user-facing app lives. Reset/verify links point here."""

    cors_origins: CommaSeparated = Field(default_factory=lambda: ["http://localhost:3000"])

    # -- Secret ------------------------------------------------------------
    # Exactly one. No default: a missing secret must be a startup error, not a
    # silent fallback to a value an attacker could read in the source.
    #
    # Every other key the service needs — JWT signing, CSRF signing, password
    # reset and verification tokens, OAuth state, TOTP seed encryption — is
    # derived from this one with HKDF under a distinct label (see
    # `subkey` below). Derivation gives the same separation that separate
    # environment variables would: the subkeys are independent, and recovering
    # one tells an attacker nothing about the others. It just does not make
    # that the operator's problem.
    #
    # Rotating this key rotates everything at once: sessions end, and
    # outstanding reset and verification links stop working. That is the
    # intended behaviour of a master-key rotation.
    secret_key: SecretStr

    # -- Storage -----------------------------------------------------------
    # No database_url in embedded mode: the host application owns the engine,
    # the connection pool and the URL. Declaring one here would create a second
    # source of truth that could quietly point auth at a different database.
    redis_url: str = "redis://localhost:6379/0"

    # -- Token policy ------------------------------------------------------
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_seconds: int = Field(default=2592000, ge=3600)
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "basivo-orch-api"
    jwt_audience: str = "basivo-orch-api:api"

    reset_password_token_ttl_seconds: int = Field(default=3600, ge=300, le=86_400)
    verify_email_token_ttl_seconds: int = Field(default=86_400, ge=300)

    # -- Cookies -----------------------------------------------------------
    cookie_name: str = "basivo_orch_api_session"
    refresh_cookie_name: str = "basivo_orch_api_refresh"
    cookie_domain: str | None = None
    cookie_secure: bool = True
    cookie_samesite: str = "lax"
    """'lax' allows top-level navigation from an IdP redirect. Use 'strict' only
    if you have no OAuth/SSO redirect flows."""

    # -- Password policy ---------------------------------------------------
    password_min_length: int = Field(default=12, ge=8)
    password_max_length: int = Field(default=128, le=1024)
    """Bounded because Argon2 hashing time grows with input; unbounded input is a DoS."""

    password_check_breaches: bool = True
    """Check candidate passwords against Have I Been Pwned using k-anonymity:
    only the first 5 characters of the SHA-1 hash ever leave this service."""

    password_breach_fail_open: bool = True
    """If HIBP is unreachable, accept the password rather than block registration.
    Set false to fail closed if your threat model prefers availability loss."""

    # -- Abuse control -----------------------------------------------------
    rate_limit_enabled: bool = True
    login_rate_limit: str = "5/minute"
    register_rate_limit: str = "3/hour"
    forgot_password_rate_limit: str = "3/hour"
    otp_send_rate_limit: str = "3/15minutes"
    otp_length: int = Field(default=6, ge=6, le=10)
    otp_ttl_seconds: int = Field(default=300, ge=60, le=900)
    otp_max_attempts: int = Field(default=5, ge=1, le=10)

    lockout_threshold: int = Field(default=5, ge=3)
    lockout_base_seconds: int = Field(default=60, ge=10)
    lockout_max_seconds: int = Field(default=3600, ge=60)

    trusted_proxy_count: int = Field(default=0, ge=0, le=8)
    """How many reverse proxies you control sit in front of this service.

    0 means X-Forwarded-For is ignored entirely and the socket address is used.
    Set this to the real number — guessing high lets a client forge its own
    address by prepending entries, which defeats every IP-keyed control here."""

    # -- Email -------------------------------------------------------------
    email_provider: str = "smtp"
    email_from: str = "no-reply@basivo-orch-api.local"
    email_from_name: str = "Basivo Orch Api"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_tls: bool = False

    # -- TOTP --------------------------------------------------------------
    totp_issuer: str = "Basivo Orch Api"
    totp_window: int = Field(default=1, ge=0, le=2)
    """Accepted drift in 30s steps. 1 tolerates ~30s of clock skew each way."""
    totp_recovery_code_count: int = Field(default=10, ge=5, le=20)

    # -- SSO ---------------------------------------------------------------
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    github_client_id: str = ""
    github_client_secret: SecretStr = SecretStr("")
    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")

    sso_allowed_redirect_urls: CommaSeparated = Field(
        default_factory=lambda: ["http://localhost:3000/auth/callback"]
    )
    """Exact-match allowlist. An open redirect here is an account takeover."""

    sso_auto_link_verified_emails: bool = False
    """Linking an OAuth identity to an existing local account by email address is
    an account-takeover vector when the IdP does not verify emails. Off by default:
    the user must confirm the link while authenticated."""

    # -- Authorization -----------------------------------------------------
    superuser_bypasses_org_permissions: bool = False
    """Whether `is_superuser` grants owner-level access to every organisation.

    Off by default. A global flag that silently grants access to every tenant is
    the kind of authority that gets granted once and forgotten, and it defeats
    the isolation the rest of this module exists to provide. Turn it on only if
    you need platform-staff break-glass, and watch the `superuser_org_access`
    log line — every use is recorded."""

    # -- Observability -----------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True
    audit_log_enabled: bool = True

    # ---------------------------------------------------------------------
    # Derived keys
    # ---------------------------------------------------------------------

    def subkey(self, purpose: str, *, length: int = 32) -> bytes:
        """An independent key for `purpose`, derived from `secret_key`.

        The issuer is folded into the label so that two services sharing a
        secret by accident still do not share subkeys.
        """
        return _hkdf(
            self.secret_key.get_secret_value().encode("utf-8"),
            info=f"{self.jwt_issuer}:{purpose}",
            length=length,
        )

    def subkey_str(self, purpose: str) -> str:
        """`subkey` as text, for libraries that want a string secret."""
        return base64.urlsafe_b64encode(self.subkey(purpose)).decode("ascii")

    # ---------------------------------------------------------------------
    # Validation
    # ---------------------------------------------------------------------

    @model_validator(mode="after")
    def _enforce_secret_strength(self) -> Self:
        weak = {"", "changeme", "secret", "supersecret", "dev", "test", "password"}
        raw = self.secret_key.get_secret_value()
        if raw.strip().lower() in weak:
            raise ValueError(
                "SECRET_KEY is a placeholder. Generate one with `openssl rand -base64 48`."
            )
        if len(raw) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"SECRET_KEY is {len(raw)} characters; needs at least {MIN_SECRET_LENGTH}. "
                "It is the root of every other key in the service, so its strength "
                "is the strength of all of them."
            )
        return self

    @model_validator(mode="after")
    def _enforce_production_posture(self) -> Self:
        if not self.environment.is_production_like:
            return self

        if self.debug:
            raise ValueError("DEBUG must be false in staging/production.")

        if "*" in self.cors_origins:
            raise ValueError(
                "CORS_ORIGINS cannot be '*' when credentials are allowed. "
                "List the exact frontend origins."
            )
        for origin in self.cors_origins:
            if origin.startswith("http://") and "localhost" not in origin:
                raise ValueError(f"CORS origin {origin!r} is plaintext HTTP in production.")
        if not self.cookie_secure:
            raise ValueError("COOKIE_SECURE must be true in staging/production.")
        if self.cookie_samesite not in {"lax", "strict"}:
            raise ValueError(
                "COOKIE_SAMESITE must be 'lax' or 'strict' in production; "
                "'none' exposes the session to cross-site requests."
            )

        if str(self.public_base_url).startswith("http://"):
            raise ValueError("PUBLIC_BASE_URL must be https in staging/production.")
        for url in self.sso_allowed_redirect_urls:
            if url.startswith("http://") and "localhost" not in url:
                raise ValueError(f"SSO redirect {url!r} is plaintext HTTP in production.")
        return self

    @property
    def is_test(self) -> bool:
        return self.environment is Environment.TEST


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Override in tests with `get_settings.cache_clear()`."""
    return Settings()


def generate_secret() -> str:
    """Helper: `python -c "from basivo_orch.auth.settings import generate_secret as g; print(g())"`."""
    return secrets.token_urlsafe(64)
