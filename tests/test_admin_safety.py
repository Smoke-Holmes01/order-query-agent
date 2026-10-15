import json
import re

import pymysql
from pydantic import ValidationError
import pytest

from app.errors import AppError
from scripts.admin_config import AdminConfig
from scripts import init_db
from scripts.init_db import TABLE_NAMES, _normalize_check, reader_attribute


def config(**updates):
    values = dict(host="127.0.0.1", port=3307, user="root", password="admin-unit-only", allowed_hosts="127.0.0.1,localhost,db", database="order_query_demo", reader_user="order_query_reader", reader_password="reader-unit-only")
    return AdminConfig(**(values | updates))


@pytest.mark.parametrize("clause", [
    "(`status` in (_utf8mb4'paid',_utf8mb4'unpaid',_utf8mb4'cancelled'))",
    r"(`status` in (_utf8mb4\'paid\',_utf8mb4\'unpaid\',_utf8mb4\'cancelled\'))",
])
def test_actual_mysql_84_check_clause_quoting(clause):
    assert _normalize_check(clause) == "statusin'paid','unpaid','cancelled'"
    assert _normalize_check(r"((`status` <> _utf8mb4\'paid\') or (`paid_at` is not null))") == "status<>'paid'orpaid_atisnotnull"


@pytest.mark.parametrize("updates", [
    {"database": "production"}, {"host": "production.example.com"},
    {"reader_user": "root"}, {"reader_user": "x` SQL"},
    {"reader_password": "admin-unit-only"}, {"password": ""},
])
def test_administrator_target_guard_rejects_unsafe_inputs(updates):
    with pytest.raises(ValidationError):
        config(**updates)


def test_administrator_validation_error_hides_raw_inputs():
    with pytest.raises(ValidationError) as caught:
        config(password="unit-secret-must-stay-hidden", reader_user="root")
    assert "unit-secret-must-stay-hidden" not in str(caught.value)


@pytest.mark.parametrize("literal", ["pa id", "pa(id)", "PAID", " paid", "paid ", "pa`id"])
def test_check_normalization_preserves_literal_content(literal):
    original = _normalize_check("status IN ('paid','unpaid','cancelled')")
    conflicting = _normalize_check(f"status IN ('{literal}','unpaid','cancelled')")
    assert original != conflicting


def test_check_normalization_only_ignores_external_syntax():
    assert _normalize_check("((`STATUS` <> _utf8mb4'paid') OR (`paid_at` IS NOT NULL))") == "status<>'paid'orpaid_atisnotnull"
    assert _normalize_check(r"status <> 'pa\'id' OR paid_at IS NOT NULL") != "status<>'paid'orpaid_atisnotnull"


class AccountDatabase:
    """Small persistent account fake: CREATE USER/GRANT survive connection close."""
    def __init__(self, *, exists=False, tables=(), attributes=None, fail_after=None):
        self.exists = exists
        self.tables = set(tables)
        self.attributes = attributes or {}
        self.password = "reader-unit-only"
        self.fail_after = fail_after
        self.calls = []
        self.unsafe_grants = []

    def connect(self, _config):
        return AccountConnection(self)

    def verify_password(self, current, expected):
        self.calls.append(("VERIFY PASSWORD", None))
        if current.reader_password.get_secret_value() != self.password:
            raise pymysql.OperationalError(1045, "unit authentication denied")
        assert expected == self.tables


class AccountConnection:
    def __init__(self, database):
        self.database = database

    def cursor(self):
        return AccountCursor(self.database)

    def close(self):
        pass

    def commit(self):
        pass


class AccountCursor:
    def __init__(self, database):
        self.database = database
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.database.calls.append((sql, params))
        if sql.startswith("CREATE USER"):
            assert not self.database.exists
            assert "ATTRIBUTE %s" in sql
            self.database.exists = True
            self.database.password = params[2]
            self.database.attributes = json.loads(params[3])
        if sql.startswith("GRANT SELECT"):
            if self.database.fail_after is not None and len(self.database.tables) == self.database.fail_after:
                self.database.fail_after = None
                raise pymysql.OperationalError(2013, "unit interruption after account DDL committed")
            table = re.search(r"\.`(\w+)` TO", sql).group(1)
            self.database.tables.add(table)

    def fetchall(self):
        if self.sql.startswith("SELECT Host"):
            return [{"host": "%"}] if self.database.exists else []
        if self.sql.startswith("SHOW GRANTS"):
            grants = ["GRANT USAGE ON *.* TO `order_query_reader`@`%`"]
            grants += [f"GRANT SELECT ON `order_query_demo`.`{table}` TO `order_query_reader`@`%`" for table in sorted(self.database.tables)]
            return [{"grant": grant} for grant in grants + self.database.unsafe_grants]
        raise AssertionError(f"Unexpected account inspection: {self.sql}")

    def fetchone(self):
        assert self.sql.startswith("SELECT ATTRIBUTE")
        return {"attributes": json.dumps(self.database.attributes)}


def use_account_database(monkeypatch, database):
    monkeypatch.setattr(init_db, "connect_admin", database.connect)
    monkeypatch.setattr(init_db, "validate_schema", lambda *args, **kwargs: None)
    monkeypatch.setattr(init_db, "_verify_reader_password", database.verify_password)


@pytest.mark.parametrize("interrupted_after", [0, 1, 2])
def test_marked_account_recovers_only_missing_grants_after_interruption(monkeypatch, interrupted_after):
    database = AccountDatabase(fail_after=interrupted_after)
    use_account_database(monkeypatch, database)
    with pytest.raises(pymysql.OperationalError):
        init_db.initialize(config())
    assert database.attributes == reader_attribute("order_query_demo")
    assert len(database.tables) == interrupted_after
    database.calls.clear()
    init_db.initialize(config())
    grants = [sql for sql, _ in database.calls if sql.startswith("GRANT SELECT")]
    assert len(grants) == 3 - interrupted_after
    assert database.tables == set(TABLE_NAMES)
    assert not any(sql.startswith("CREATE USER") or sql.startswith("ALTER USER") for sql, _ in database.calls)
    verify_index = next(i for i, (sql, _) in enumerate(database.calls) if sql == "VERIFY PASSWORD")
    assert verify_index < next(i for i, (sql, _) in enumerate(database.calls) if sql.startswith("GRANT SELECT"))


@pytest.mark.parametrize("attributes", [
    {},
    {"order_query_agent": {"owner": "another-project", "database": "order_query_demo", "version": 1}},
    reader_attribute("order_query_demo_test"),
    {"order_query_agent": {"owner": "order-query-agent", "database": "order_query_demo", "version": True}},
    {"order_query_agent": {"owner": "order-query-agent", "database": "order_query_demo", "version": 2}},
])
def test_unknown_or_conflicting_partial_account_is_not_modified(monkeypatch, attributes):
    database = AccountDatabase(exists=True, tables=("products",), attributes=attributes)
    use_account_database(monkeypatch, database)
    with pytest.raises(RuntimeError):
        init_db.initialize(config())
    assert database.tables == {"products"}
    assert not any(sql.startswith(("CREATE ", "ALTER ", "GRANT ")) for sql, _ in database.calls)


def test_recovery_rejects_wrong_password_before_any_modification(monkeypatch):
    database = AccountDatabase(exists=True, attributes=reader_attribute("order_query_demo"))
    use_account_database(monkeypatch, database)
    with pytest.raises(pymysql.OperationalError):
        init_db.initialize(config(reader_password="wrong-unit-password"))
    assert not database.tables
    assert not any(sql.startswith(("CREATE ", "ALTER ", "GRANT ")) for sql, _ in database.calls)


@pytest.mark.parametrize("unsafe", [
    "GRANT UPDATE ON `order_query_demo`.`orders` TO `order_query_reader`@`%`",
    "GRANT SELECT ON *.* TO `order_query_reader`@`%`",
    "GRANT `role`@`%` TO `order_query_reader`@`%`",
])
def test_owned_marker_never_allows_repairing_unsafe_grants(monkeypatch, unsafe):
    database = AccountDatabase(exists=True, attributes=reader_attribute("order_query_demo"))
    database.unsafe_grants.append(unsafe)
    use_account_database(monkeypatch, database)
    with pytest.raises(AppError):
        init_db.initialize(config())
    assert not any(sql.startswith(("CREATE ", "ALTER ", "GRANT ")) for sql, _ in database.calls)


def test_complete_unmarked_legacy_account_is_accepted_without_relabeling(monkeypatch):
    database = AccountDatabase(exists=True, tables=TABLE_NAMES)
    use_account_database(monkeypatch, database)
    init_db.initialize(config())
    assert database.attributes == {}
    assert not any(sql.startswith(("CREATE USER", "ALTER USER", "GRANT ")) for sql, _ in database.calls)
