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


def live_settings(**changes):
    values = dict(db_password="unit-test-placeholder", model_mode="live",
                  model_base_url="http://model.example/v1", model_name="unit-test-model",
                  model_api_key="unit-test-placeholder")
    return Settings(**(values | changes))


def test_remote_http_requires_explicit_opt_in():
    with pytest.raises(ValidationError):
        live_settings()
    assert live_settings(model_allow_insecure_http=True).app_env == "development"


@pytest.mark.parametrize("environment", ["test", "production"])
def test_insecure_http_opt_in_is_development_only(environment):
    with pytest.raises(ValidationError):
        live_settings(model_allow_insecure_http=True, app_env=environment)


def test_https_needs_no_insecure_opt_in():
    assert not live_settings(model_base_url="https://model.example/v1").model_allow_insecure_http


def test_env_can_explicitly_enable_development_http(tmp_path):
    runtime = tmp_path / ".env"
    runtime.write_text("DB_PASSWORD=unit-test-placeholder\nMODEL_MODE=live\n"
                       "MODEL_BASE_URL=http://model.example/v1\nMODEL_NAME=unit-test-model\n"
                       "MODEL_API_KEY=unit-test-placeholder\nMODEL_ALLOW_INSECURE_HTTP=true\n")
    assert load_settings(env_file=runtime).model_allow_insecure_http
