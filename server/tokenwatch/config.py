import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    database: Path = Path("tokenwatch.db")
    bearer_token: SecretStr = Field(min_length=1)
    signing_key: SecretStr = Field(min_length=1)
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
    allow_pricing_id_checks: bool = False

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
