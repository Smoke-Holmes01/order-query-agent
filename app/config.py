"""Runtime configuration only. Never reads administrator configuration."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

import os


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    app_env: Literal["development", "test", "production"] = "development"
    app_timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    model_mode: Literal["mock", "live"] = "mock"
    model_base_url: str = ""
    model_name: str = ""
    model_api_key: SecretStr = SecretStr("")
    model_json_mode: Literal["json_object", "json_schema"] = "json_object"
    model_total_timeout: float = Field(default=15, ge=0.1, le=60)
    model_connect_timeout: float = Field(default=3, ge=0.1, le=30)
    model_read_timeout: float = Field(default=10, ge=0.1, le=60)
    model_write_timeout: float = Field(default=3, ge=0.1, le=30)
    model_pool_timeout: float = Field(default=2, ge=0.1, le=30)
    db_host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    db_port: int = Field(default=3307, ge=1, le=65535)
    db_name: Literal["order_query_demo", "order_query_demo_test"] = "order_query_demo"
    db_user: str = Field(default="order_query_reader", min_length=1, max_length=32)
    db_password: SecretStr
    db_connect_timeout: int = Field(default=3, ge=1, le=30)
    db_read_timeout: int = Field(default=5, ge=1, le=60)
    db_write_timeout: int = Field(default=3, ge=1, le=30)
    db_max_execution_time: int = Field(default=2000, ge=1, le=5000)
    db_max_concurrency: int = Field(default=4, ge=1, le=4)
    db_acquire_timeout: float = Field(default=2, ge=0.1, le=10)

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.db_user.casefold() in {"root", "admin", "administrator", "mysql", "mysql.sys"}:
            raise ValueError("DB_USER must be the dedicated read-only account")
        if not self.db_password.get_secret_value():
            raise ValueError("DB_PASSWORD is required")
        if self.db_read_timeout * 1000 <= self.db_max_execution_time:
            raise ValueError("DB_READ_TIMEOUT must exceed DB_MAX_EXECUTION_TIME")
        try:
            ZoneInfo(self.app_timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("Timezone data missing; install tzdata") from None
        if self.model_mode == "live":
            if not self.model_name.strip() or not self.model_api_key.get_secret_value().strip():
                raise ValueError("live requires MODEL_NAME and MODEL_API_KEY")
            url = urlsplit(self.model_base_url)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("live requires a valid MODEL_BASE_URL without credentials/query/fragment")
            if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("remote model endpoint must use HTTPS")
        return self


def load_settings(model_mode: str | None = None, *, env_file: str | Path = ".env") -> Settings:
    """Explicit runtime allowlist; environment takes precedence over .env."""
    values = dotenv_values(env_file, interpolate=False) if Path(env_file).is_file() else {}
    fields = Settings.model_fields
    data = {}
    for field in fields:
        key = field.upper()
        value = os.environ.get(key, values.get(key))
        if value is not None:
            data[field] = value
    if model_mode is not None:
        data["model_mode"] = model_mode
    return Settings.model_validate(data)
