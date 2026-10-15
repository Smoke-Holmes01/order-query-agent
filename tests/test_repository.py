from datetime import date, datetime
from decimal import Decimal
from threading import Event, Thread

import pymysql
import pytest

from app.config import Settings
from app.errors import AppError
from app.query_templates import select_template
from app.repository import Repository, check_grants
from app.schemas import ValidatedPlan


def settings(**updates):
    return Settings(db_password="unit-test-only-value", **updates)


def plan(kind="daily_sales", **updates):
    return ValidatedPlan(query_type=kind, start_date=date(2026, 10, 1), end_date=date(2026, 10, 4), rank_by=updates.get("rank_by"), top_n=updates.get("top_n"))


def valid_grants():
    return ["GRANT USAGE ON *.* TO `order_query_reader`@`%`", *[f"GRANT SELECT ON `order_query_demo`.`{table}` TO `order_query_reader`@`%`" for table in ("products", "orders", "order_items")]]


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.connection.calls.append((sql, params))
        if sql.startswith("SELECT DATE") and self.connection.error is not None:
            raise self.connection.error

    def fetchone(self):
        return {"server_version": "8.4.7", "current_user": "order_query_reader@%", "current_role": "NONE"}

    def fetchall(self):
        if self.sql == "SHOW GRANTS":
            return [{"Grants": grant} for grant in valid_grants()]
        return self.connection.rows.copy()


class FakeConnection:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.calls = []
        self.closed = False
        self.rolled_back = False

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_fixed_templates_bind_dates_limit_and_choose_only_two_sort_branches():
    amount = select_template(plan("top_products", rank_by="sales_amount", top_n=2))
    quantity = select_template(plan("top_products", rank_by="sales_quantity", top_n=2))
    assert "ORDER BY sales_amount DESC, p.id ASC" in amount[1]
    assert "ORDER BY sales_quantity DESC, p.id ASC" in quantity[1]
    assert amount[2] == (datetime(2026, 10, 1), datetime(2026, 10, 4), 2)
    for current in (plan(), plan("category_sales"), plan("top_products", rank_by="sales_amount", top_n=10)):
        _, sql, params = select_template(current)
        assert sql.count("%s") == len(params) == 3
        assert "paid_at >= %s AND o.paid_at < %s" in sql
        assert "2026" not in sql


@pytest.mark.parametrize("extra", [
    "GRANT SELECT ON `order_query_demo`.* TO `order_query_reader`@`%`",
    "GRANT UPDATE ON `order_query_demo`.`orders` TO `order_query_reader`@`%`",
    "GRANT FILE ON *.* TO `order_query_reader`@`%`",
    "GRANT `reader_role`@`%` TO `order_query_reader`@`%`",
    "GRANT SELECT ON `order_query_demo`.`products` TO `order_query_reader`@`%` WITH GRANT OPTION",
    "GRANT SELECT ON `other`.`orders` TO `order_query_reader`@`%`",
])
def test_privilege_allowlist_fails_closed(extra):
    with pytest.raises(AppError) as caught:
        check_grants([*valid_grants(), extra], "order_query_demo", "order_query_reader")
    assert caught.value.category == "unsafe_permissions"


def test_exact_table_select_grants_pass_and_missing_table_fails():
    check_grants(valid_grants(), "order_query_demo", "order_query_reader")
    with pytest.raises(AppError):
        check_grants(valid_grants()[:-1], "order_query_demo", "order_query_reader")


def test_decimal_is_preserved_integer_normalized_and_connection_closed(monkeypatch):
    connection = FakeConnection([{"payment_date": date(2026, 10, 1), "sales_amount": Decimal("340.00"), "sales_quantity": Decimal(6)}])
    repository = Repository(settings())
    monkeypatch.setattr(repository, "_connect", lambda: connection)
    result = repository.execute(plan())
    assert result["rows"][0]["sales_amount"] == Decimal("340.00")
    assert type(result["rows"][0]["sales_quantity"]) is int
    assert ("SET SESSION max_execution_time = %s", (2000,)) in connection.calls
    assert ("SET SESSION time_zone = %s", ("+08:00",)) in connection.calls
    assert ("START TRANSACTION READ ONLY", None) in connection.calls
    assert connection.closed and connection.rolled_back


@pytest.mark.parametrize("database_error,code,http_status", [(3024, "QUERY_TIMEOUT", 504), (1064, "QUERY_FAILED", 500), (2013, "DB_UNAVAILABLE", 503)])
def test_database_errors_are_safe_closed_and_have_attempted_query(monkeypatch, database_error, code, http_status):
    connection = FakeConnection(error=pymysql.OperationalError(database_error, "secret internal raw database detail"))
    repository = Repository(settings())
    monkeypatch.setattr(repository, "_connect", lambda: connection)
    with pytest.raises(AppError) as caught:
        repository.execute(plan())
    assert caught.value.code == code and caught.value.http_status == http_status
    assert "secret" not in caught.value.message
    assert caught.value.query["template_id"] == "daily_sales_v1"
    assert connection.closed and connection.rolled_back


def test_connect_failure_does_not_claim_query_execution(monkeypatch):
    repository = Repository(settings())

    def fail():
        raise pymysql.OperationalError(1045, "secret credential details")

    monkeypatch.setattr(repository, "_connect", fail)
    with pytest.raises(AppError) as caught:
        repository.execute(plan())
    assert caught.value.code == "DB_UNAVAILABLE"
    assert not hasattr(caught.value, "query")
    assert repository._semaphore.acquire(blocking=False)
    repository._semaphore.release()


def test_category_limit_refuses_instead_of_truncating(monkeypatch):
    connection = FakeConnection([{"category": str(i), "sales_amount": Decimal("1.00"), "sales_quantity": 1} for i in range(101)])
    repository = Repository(settings())
    monkeypatch.setattr(repository, "_connect", lambda: connection)
    with pytest.raises(AppError) as caught:
        repository.execute(plan("category_sales"))
    assert caught.value.code == "RESULT_LIMIT_EXCEEDED"
    assert caught.value.category == "needs_clarification"
    assert caught.value.query["query_params"][-1] == 101
    assert connection.closed


@pytest.mark.parametrize("amount,quantity", [(340.0, 6), (Decimal("340.00"), Decimal("1.5")), (Decimal("340.00"), True)])
def test_repository_rejects_inexact_money_or_fractional_quantity(monkeypatch, amount, quantity):
    connection = FakeConnection([{"payment_date": date(2026, 10, 1), "sales_amount": amount, "sales_quantity": quantity}])
    repository = Repository(settings())
    monkeypatch.setattr(repository, "_connect", lambda: connection)
    with pytest.raises(AppError) as caught:
        repository.execute(plan())
    assert caught.value.code == "QUERY_FAILED"
    assert caught.value.query["template_id"] == "daily_sales_v1"
    assert connection.closed


def test_concurrent_connection_limit_and_release(monkeypatch):
    repository = Repository(settings(db_max_concurrency=1, db_acquire_timeout=0.1))
    monkeypatch.setattr(repository, "_connect", FakeConnection)
    entered, release = Event(), Event()

    def hold():
        with repository._connection():
            entered.set()
            release.wait(timeout=5)

    worker = Thread(target=hold)
    worker.start()
    try:
        assert entered.wait(timeout=2)
        with pytest.raises(AppError) as caught:
            repository.execute(plan())
        assert caught.value.code == "DB_UNAVAILABLE" and caught.value.category == "concurrency_limit"
    finally:
        release.set()
        worker.join(timeout=2)
    assert repository.execute(plan())["rows"] == []
