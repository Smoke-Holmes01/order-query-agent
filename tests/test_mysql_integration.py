"""Explicit real MySQL 8.4 checks. Never loads administrator credentials."""

import os
from datetime import date, datetime
from decimal import Decimal

import pymysql
import pytest

from app.config import load_settings
from app.repository import Repository
from app.schemas import ValidatedPlan


pytestmark = [pytest.mark.mysql, pytest.mark.skipif(os.environ.get("RUN_MYSQL_TESTS") != "1", reason="Set RUN_MYSQL_TESTS=1 for real MySQL 8.4 checks")]


@pytest.fixture(scope="module")
def repository():
    current = Repository(load_settings())
    assert current.health()["server_version"].startswith("8.4.")
    return current


def plan(kind="daily_sales", start="2026-10-01", end="2026-10-04", rank_by=None, top_n=None):
    return ValidatedPlan(query_type=kind, start_date=date.fromisoformat(start), end_date=date.fromisoformat(end), rank_by=rank_by, top_n=top_n)


def snapshot(connection):
    values = []
    with connection.cursor() as cursor:
        for sql in ("SELECT id,sku,name,category FROM products ORDER BY id", "SELECT id,order_no,status,paid_at FROM orders ORDER BY id", "SELECT id,order_id,product_id,quantity,unit_price FROM order_items ORDER BY id"):
            cursor.execute(sql)
            values.append(cursor.fetchall())
    return values


def test_three_templates_exact_decimal_results_and_rank_order(repository):
    assert repository.execute(plan())["rows"] == [
        {"payment_date": date(2026, 10, 1), "sales_amount": Decimal("340.00"), "sales_quantity": 6},
        {"payment_date": date(2026, 10, 2), "sales_amount": Decimal("160.00"), "sales_quantity": 3},
    ]
    assert repository.execute(plan("category_sales"))["rows"] == [
        {"category": "数码", "sales_amount": Decimal("350.00"), "sales_quantity": 4},
        {"category": "家居", "sales_amount": Decimal("150.00"), "sales_quantity": 5},
    ]
    ranked = repository.execute(plan("top_products", rank_by="sales_amount", top_n=2))["rows"]
    assert [(row["sku"], row["sales_amount"]) for row in ranked] == [("P1", Decimal("300.00")), ("P3", Decimal("150.00"))]
    quantity = repository.execute(plan("top_products", rank_by="sales_quantity", top_n=1))["rows"]
    assert quantity[0]["sku"] == "P3" and quantity[0]["sales_quantity"] == 5


def test_lower_upper_microsecond_boundaries_and_empty_day(repository):
    assert repository.execute(plan(start="2026-10-02"))["rows"] == [{"payment_date": date(2026, 10, 2), "sales_amount": Decimal("160.00"), "sales_quantity": 3}]
    assert repository.execute(plan(end="2026-10-02"))["rows"] == [{"payment_date": date(2026, 10, 1), "sales_amount": Decimal("340.00"), "sales_quantity": 6}]
    assert repository.execute(plan(start="2026-10-03"))["rows"] == []


def test_independent_manual_sql_total_and_seed_data_types(repository):
    with repository._connection() as connection:
        with connection.cursor() as cursor:
            # Independently written control SQL, not imported from production templates.
            cursor.execute("SELECT SUM(order_items.unit_price * order_items.quantity) AS amount, SUM(order_items.quantity) AS units FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE status=%s AND paid_at >= %s AND paid_at < %s)", ("paid", datetime(2026, 10, 1), datetime(2026, 10, 4)))
            control = cursor.fetchone()
            assert control == {"amount": Decimal("500.00"), "units": Decimal(9)}
            cursor.execute("SELECT paid_at FROM orders WHERE id=%s", (1002,))
            assert cursor.fetchone()["paid_at"] == datetime(2026, 10, 1, 23, 59, 59, 999999)
            cursor.execute("SELECT paid_at FROM orders WHERE id=%s", (1006,))
            assert cursor.fetchone()["paid_at"] is None
    assert sum((row["sales_amount"] for row in repository.execute(plan())["rows"]), Decimal(0)) == control["amount"]


def test_reader_privileges_refuse_no_effect_writes_and_preserve_seed(repository):
    connection = repository._connect()
    try:
        repository._check_access(connection)  # Must pass BEFORE issuing any DML probe.
        before = snapshot(connection)
        statements = (
            "INSERT INTO products (id,sku,name,category) SELECT id,sku,name,category FROM products WHERE 1=0",
            "UPDATE products SET name=name WHERE 1=0",
            "DELETE FROM products WHERE 1=0",
        )
        for sql in statements:
            with connection.cursor() as cursor, pytest.raises(pymysql.OperationalError) as caught:
                cursor.execute(sql)
            assert caught.value.args[0] == 1142, "Must fail because of table privilege, not SQL/transaction errors"
        assert snapshot(connection) == before
        assert [len(rows) for rows in before] == [3, 7, 9]
    finally:
        connection.close()


def test_session_settings_and_server_interrupt_3024_not_socket_timeout(repository):
    connection = repository._connect()
    try:
        repository._check_access(connection)
        before = snapshot(connection)
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION time_zone=%s", ("+08:00",))
            cursor.execute("SET SESSION max_execution_time=%s", (repository.settings.db_max_execution_time,))
            cursor.execute("SELECT @@SESSION.max_execution_time AS time_limit, @@SESSION.time_zone AS zone")
            assert cursor.fetchone() == {"time_limit": repository.settings.db_max_execution_time, "zone": "+08:00"}
            cursor.execute("START TRANSACTION READ ONLY")
            # SLEEP must be part of a table query: standalone SLEEP returns 1 on interrupt.
            with pytest.raises(pymysql.OperationalError) as caught:
                cursor.execute("SELECT 1 FROM products WHERE id=%s AND SLEEP(%s)", (1, 6))
            assert caught.value.args[0] == 3024, "Client socket timeout is not proof of server execution cancellation"
            cursor.execute("SELECT 1 AS alive")
            assert cursor.fetchone() == {"alive": 1}
        connection.rollback()
        assert snapshot(connection) == before
    finally:
        connection.close()
