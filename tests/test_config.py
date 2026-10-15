import pytest
from pydantic import ValidationError

from app.config import Settings, load_settings


def test_runtime_never_reads_admin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.admin").write_text("DB_ADMIN_PASSWORD=secret\nDB_PASSWORD=wrong\n")
    with pytest.raises(ValidationError):
        load_settings()


def test_environment_precedes_runtime_file(tmp_path, monkeypatch):
    runtime = tmp_path / ".env"
    runtime.write_text("DB_PASSWORD=local\nDB_PORT=3307\n")
    monkeypatch.setenv("DB_PORT", "3308")
    assert load_settings(env_file=runtime).db_port == 3308


@pytest.mark.parametrize("changes", [
    {"db_user": "root"}, {"db_name": "production"}, {"db_port": 0},
    {"db_max_concurrency": 5}, {"db_read_timeout": 1},
    {"model_mode": "live"}, {"model_total_timeout": 0},
])
def test_invalid_settings(changes):
    with pytest.raises(ValidationError):
        Settings(db_password="unit-test-placeholder", **changes)


def test_live_configuration_does_not_fall_back():
    with pytest.raises(ValidationError):
        Settings(db_password="unit-test-placeholder", model_mode="live", model_name="configured")


def test_secret_is_redacted():
    settings = Settings(db_password="unit-test-secret")
    assert "unit-test-secret" not in repr(settings)
