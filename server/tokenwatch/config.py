import os
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)

    database: Path = Path("tokenwatch.db")
    bearer_token: SecretStr = Field(min_length=1)
    signing_key: SecretStr = Field(min_length=1)
    binance_proxy_url: SecretStr | None = None
    upstream_factory: str = ""
    browser_config: Path = Path("browser.local.toml")
    request_interval: float = Field(default=30, gt=0)
    request_jitter: float = Field(default=10, ge=0)
    backoff_initial: float = Field(default=60, gt=0)
    backoff_max: float = Field(default=3600, gt=0)
    transfer_min_interval: float = Field(default=60, gt=0)
    transfer_max_interval: float = Field(default=1800, gt=0)
    transfer_retention_days: int = Field(default=30, gt=0)
    snapshot_retention_days: int = Field(default=30, gt=0)
    account_status_ttl: float = Field(default=600, gt=0)
    novnc_url: str = ""
    vnc_password: SecretStr | None = None
    login_enabled: bool = True
    login_timeout: float = Field(default=900, gt=0)
    shutdown_grace_seconds: float = Field(default=30, ge=0)

    @field_validator("binance_proxy_url")
    @classmethod
    def validate_binance_proxy(cls, value):
        if value is not None:
            try:
                parts = urlsplit(value.get_secret_value())
                if parts.scheme not in ("socks5", "socks5h", "http", "https") or not parts.hostname or not parts.port:
                    raise ValueError
            except ValueError:
                raise ValueError("Invalid Binance proxy URL: expected a supported scheme, host and port") from None
        return value

    @model_validator(mode="after")
    def validate_ranges(self):
        if self.backoff_max < self.backoff_initial:
            raise ValueError("backoff_max must be at least backoff_initial")
        if self.transfer_max_interval < self.transfer_min_interval:
            raise ValueError("transfer_max_interval must be at least transfer_min_interval")
        return self


def load_settings() -> Settings:
    path = Path(os.environ.get("TOKENWATCH_CONFIG", "config.local.toml"))
    values = tomllib.loads(path.read_text()) if path.exists() else {}
    for field in Settings.model_fields:
        env = f"TOKENWATCH_{field.upper()}"
        if env in os.environ:
            values[field] = os.environ[env]
    return Settings.model_validate(values)
