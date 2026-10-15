"""Administrator configuration, loaded only by init_db and seed_db entrypoints."""

import os
import re
from pathlib import Path

import pymysql
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pymysql.cursors import DictCursor


class AdminConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    host: str
    port: int = Field(ge=1, le=65535)
    user: str = Field(min_length=1, max_length=32)
    password: SecretStr
    allowed_hosts: str
    database: str
    reader_user: str
    reader_password: SecretStr
    reader_host: str = "%"

    @model_validator(mode="after")
    def validate_target(self):
        if self.database not in {"order_query_demo", "order_query_demo_test"}:
            raise ValueError("Only explicitly allowed demo databases may be initialized")
        if self.host not in {item.strip() for item in self.allowed_hosts.split(",")}:
            raise ValueError("Administrator host is not in DB_ADMIN_ALLOWED_HOSTS")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", self.reader_user):
            raise ValueError("Dedicated reader account name must be a simple identifier")
        if self.reader_user.casefold() in {"root", "admin", "administrator", "mysql", "mysql.sys"} or self.reader_user == self.user:
            raise ValueError("Runtime account must differ from administrator")
        if not self.password.get_secret_value() or not self.reader_password.get_secret_value():
            raise ValueError("Both administrator and runtime passwords are required")
        if self.password.get_secret_value() == self.reader_password.get_secret_value():
            raise ValueError("Administrator and runtime passwords must differ")
        return self


def load_admin_config() -> AdminConfig:
    admin = dotenv_values(".env.admin", interpolate=False) if Path(".env.admin").is_file() else {}
    runtime = dotenv_values(".env", interpolate=False) if Path(".env").is_file() else {}

    def value(key, source, default=None):
        return os.environ.get(key, source.get(key, default))

    return AdminConfig(
        host=value("DB_ADMIN_HOST", admin), port=value("DB_ADMIN_PORT", admin),
        user=value("DB_ADMIN_USER", admin), password=value("DB_ADMIN_PASSWORD", admin),
        allowed_hosts=value("DB_ADMIN_ALLOWED_HOSTS", admin, "127.0.0.1,localhost,db"),
        database=value("DB_NAME", runtime, "order_query_demo"),
        reader_user=value("DB_USER", runtime, "order_query_reader"),
        reader_password=value("DB_PASSWORD", runtime),
    )


def connect_admin(config: AdminConfig, *, select_database=False):
    connection = pymysql.connect(
        host=config.host, port=config.port, user=config.user,
        password=config.password.get_secret_value(),
        database=config.database if select_database else None,
        charset="utf8mb4", cursorclass=DictCursor, autocommit=False,
        connect_timeout=3, read_timeout=5, write_timeout=3,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION() AS version")
            if not re.match(r"^8\.4\.\d+(?:[-.]|$)", cursor.fetchone()["version"]):
                raise RuntimeError("Initialization requires MySQL 8.4")
            cursor.execute("SET SESSION time_zone = %s", ("+08:00",))
        return connection
    except Exception:
        connection.close()
        raise
