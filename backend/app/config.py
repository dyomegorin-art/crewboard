from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_ENVS = {"local", "test"}
KNOWN_ENVS = {"local", "test", "staging", "production"}
PAYMENT_PROVIDERS = {"mock", "yookassa"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "local"
    log_level: str = "INFO"

    database_url: str
    redis_url: str

    jwt_secret: str
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 30

    public_url: str = "http://localhost:5173"
    api_internal_url: str = "http://api:8000"

    payment_provider: str = "mock"
    webhook_secret: str = ""
    mock_webhook_duplicate_rate: float = 0.2
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""

    github_client_id: str = ""
    github_client_secret: str = ""

    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    mail_from: str = "Crewboard <noreply@crewboard.local>"

    dev_endpoints: bool = False

    rate_limit_login_per_minute: int = 10
    rate_limit_register_per_minute: int = 10
    rate_limit_reset_per_minute: int = 5
    rate_limit_refresh_per_minute: int = 30
    rate_limit_demo_per_minute: int = 10
    rate_limit_webhook_per_minute: int = 300

    demo_ttl_hours: int = 24

    @property
    def is_local(self) -> bool:
        return self.env in LOCAL_ENVS

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

    def validate_startup(self) -> None:
        """Fail closed: небезопасные конфигурации не стартуют (как в repairdesk)."""
        if self.env not in KNOWN_ENVS:
            raise RuntimeError(f"Unknown ENV {self.env!r}")
        if self.dev_endpoints and not self.is_local:
            raise RuntimeError("DEV_ENDPOINTS must not be enabled outside local/test")
        if self.payment_provider not in PAYMENT_PROVIDERS:
            raise RuntimeError(f"Unknown PAYMENT_PROVIDER {self.payment_provider!r}")
        if self.payment_provider == "yookassa" and not (
            self.yookassa_shop_id and self.yookassa_secret_key
        ):
            raise RuntimeError("YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY are required")
        if not self.is_local and len(self.jwt_secret) < 32:
            raise RuntimeError("JWT_SECRET must be at least 32 characters outside local/test")
        if self.payment_provider == "mock" and not self.webhook_secret:
            raise RuntimeError("WEBHOOK_SECRET is required for the mock payment provider")


@lru_cache
def get_settings() -> Settings:
    return Settings()
