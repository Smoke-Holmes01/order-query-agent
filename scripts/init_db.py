"""Create only the explicit simulation schema and dedicated SELECT account.

MySQL DDL implicitly commits: existing objects are validated before any DDL,
and partial creation is left intact for a safe rerun. No reset operation exists.
"""

import json
import re
import sys
from pathlib import Path

import pymysql
from pydantic import ValidationError
from pymysql.cursors import DictCursor

from app.errors import AppError
from app.repository import check_grants
from scripts.admin_config import connect_admin, load_admin_config


TABLE_NAMES = ("products", "orders", "order_items")
TABLE_COMMENT = "order-query-agent simulated data v1"
EXPECTED_COLUMNS = {
    "products": [("id", "bigint unsigned", "NO"), ("sku", "varchar(32)", "NO"), ("name", "varchar(128)", "NO"), ("category", "varchar(64)", "NO")],
    "orders": [("id", "bigint unsigned", "NO"), ("order_no", "varchar(32)", "NO"), ("status", "varchar(16)", "NO"), ("paid_at", "datetime(6)", "YES")],
    "order_items": [("id", "bigint unsigned", "NO"), ("order_id", "bigint unsigned", "NO"), ("product_id", "bigint unsigned", "NO"), ("quantity", "int", "NO"), ("unit_price", "decimal(12,2)", "NO")],
}
EXPECTED_INDEXES = {
    "products": {("PRIMARY", 0, ("id",)), ("uq_products_sku", 0, ("sku",))},
    "orders": {("PRIMARY", 0, ("id",)), ("uq_orders_order_no", 0, ("order_no",)), ("idx_orders_status_paid_at_id", 1, ("status", "paid_at", "id"))},
    "order_items": {("PRIMARY", 0, ("id",)), ("idx_items_order", 1, ("order_id",)), ("idx_items_product", 1, ("product_id",))},
}
EXPECTED_CHECKS = {
    "products": {},
    "orders": {"chk_orders_status": "statusin'paid','unpaid','cancelled'", "chk_orders_paid_at": "status<>'paid'orpaid_atisnotnull"},
    "order_items": {"chk_items_quantity": "quantity>0", "chk_items_unit_price": "unit_price>=0"},
}


def _normalize_check(clause):
    # INFORMATION_SCHEMA on MySQL 8.4 may expose quotes as backslash-escaped.
    # Preserve every character inside literals; only normalize outside syntax.
    first_quote = clause.find("'")
    if first_quote > 0 and clause[first_quote - 1] == "\\":
        clause = clause.replace("\\'", "'")
    parts = re.split(r"('(?:''|\\.|[^'\\])*')", clause)
    normalized = []
    for index, part in enumerate(parts):
        if index % 2:
            normalized.append(part)
        else:
            if "'" in part:
                raise RuntimeError("Existing CHECK expression has unsupported quoting; no changes performed")
            syntax = re.sub(r"[\s`()]+", "", part.lower())
            normalized.append(re.sub(r"_utf8mb4$", "", syntax))
    return "".join(normalized)


def validate_schema(connection, database: str, *, require_all=False):
    """Inspect known objects without changing unknown schemas or data."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT DEFAULT_CHARACTER_SET_NAME AS charset, DEFAULT_COLLATION_NAME AS collation_name FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (database,))
        schema = cursor.fetchone()
        if schema is None:
            if require_all:
                raise RuntimeError("Demo database is missing; run scripts.init_db")
            return
        if schema["charset"] != "utf8mb4" or schema["collation_name"] != "utf8mb4_0900_ai_ci":
            raise RuntimeError("Existing demo database charset/collation conflicts; no objects changed")
        cursor.execute("SELECT TABLE_NAME AS table_name, TABLE_TYPE AS table_type, ENGINE AS engine, TABLE_COLLATION AS collation_name, TABLE_COMMENT AS table_comment FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s", (database,))
        tables = cursor.fetchall()
        if any(row["table_name"] not in TABLE_NAMES for row in tables):
            raise RuntimeError("Unknown objects exist in demo database; preserved without modification")
        if require_all and {row["table_name"] for row in tables} != set(TABLE_NAMES):
            raise RuntimeError("Demo schema is incomplete; run scripts.init_db")
        for lookup, field in (("TRIGGERS", "TRIGGER_SCHEMA"), ("ROUTINES", "ROUTINE_SCHEMA"), ("EVENTS", "EVENT_SCHEMA")):
            # Both identifiers are developer-owned constants, never user input.
            cursor.execute(f"SELECT COUNT(*) AS n FROM information_schema.{lookup} WHERE {field} = %s", (database,))
            if cursor.fetchone()["n"]:
                raise RuntimeError("Unknown triggers/routines/events exist; preserved without modification")
        for table in tables:
            name = table["table_name"]
            if (table["table_type"], table["engine"], table["collation_name"], table["table_comment"]) != ("BASE TABLE", "InnoDB", "utf8mb4_0900_ai_ci", TABLE_COMMENT):
                raise RuntimeError(f"Existing {name} object is not the managed simulation schema")
            cursor.execute("SELECT COLUMN_NAME AS name, COLUMN_TYPE AS type_name, IS_NULLABLE AS nullable, COLUMN_DEFAULT AS default_value, EXTRA AS extra, COLLATION_NAME AS collation_name FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (database, name))
            columns = cursor.fetchall()
            actual = [(column["name"], column["type_name"], column["nullable"]) for column in columns]
            if actual != EXPECTED_COLUMNS[name] or any(column["default_value"] is not None or column["extra"] or column["collation_name"] not in {None, "utf8mb4_0900_ai_ci"} for column in columns):
                raise RuntimeError(f"Existing {name} columns conflict; no migration is performed")
            cursor.execute("SELECT INDEX_NAME AS name, NON_UNIQUE AS non_unique, COLUMN_NAME AS column_name, SUB_PART AS sub_part, IS_VISIBLE AS visible FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY INDEX_NAME, SEQ_IN_INDEX", (database, name))
            indexes = {}
            for row in cursor.fetchall():
                if row["sub_part"] is not None or row["visible"] != "YES":
                    raise RuntimeError(f"Existing {name} index differs")
                indexes.setdefault((row["name"], row["non_unique"]), []).append(row["column_name"])
            if {(key[0], key[1], tuple(columns)) for key, columns in indexes.items()} != EXPECTED_INDEXES[name]:
                raise RuntimeError(f"Existing {name} indexes conflict")
            cursor.execute("SELECT tc.CONSTRAINT_NAME AS name, cc.CHECK_CLAUSE AS clause, tc.ENFORCED AS enforced FROM information_schema.TABLE_CONSTRAINTS AS tc JOIN information_schema.CHECK_CONSTRAINTS AS cc ON cc.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME=tc.CONSTRAINT_NAME WHERE tc.TABLE_SCHEMA=%s AND tc.TABLE_NAME=%s AND tc.CONSTRAINT_TYPE='CHECK'", (database, name))
            checks = cursor.fetchall()
            if any(row["enforced"] != "YES" for row in checks) or {row["name"]: _normalize_check(row["clause"]) for row in checks} != EXPECTED_CHECKS[name]:
                raise RuntimeError(f"Existing {name} checks conflict")
            cursor.execute("SELECT k.CONSTRAINT_NAME AS name, k.COLUMN_NAME AS column_name, k.REFERENCED_TABLE_SCHEMA AS target_schema, k.REFERENCED_TABLE_NAME AS target_table, k.REFERENCED_COLUMN_NAME AS target_column, r.UPDATE_RULE AS update_rule, r.DELETE_RULE AS delete_rule FROM information_schema.KEY_COLUMN_USAGE AS k JOIN information_schema.REFERENTIAL_CONSTRAINTS AS r ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME WHERE k.TABLE_SCHEMA=%s AND k.TABLE_NAME=%s AND k.REFERENCED_TABLE_NAME IS NOT NULL", (database, name))
            actual_fk = {(row["name"], row["column_name"], row["target_schema"], row["target_table"], row["target_column"], row["update_rule"], row["delete_rule"]) for row in cursor.fetchall()}
            expected_fk = {( "fk_items_order", "order_id", database, "orders", "id", "NO ACTION", "NO ACTION"), ("fk_items_product", "product_id", database, "products", "id", "NO ACTION", "NO ACTION")} if name == "order_items" else set()
            if actual_fk != expected_fk:
                raise RuntimeError(f"Existing {name} foreign keys conflict")


def reader_attribute(database: str) -> dict:
    """Ownership is set atomically with CREATE USER, before any separate GRANT."""
    return {"order_query_agent": {"owner": "order-query-agent", "database": database, "version": 1}}


def _existing_reader(cursor, config) -> set[str] | None:
    cursor.execute("SELECT Host AS host FROM mysql.user WHERE User=%s", (config.reader_user,))
    accounts = cursor.fetchall()
    if not accounts:
        return None
    if len(accounts) != 1 or accounts[0]["host"] != config.reader_host:
        raise RuntimeError("Conflicting reader accounts already exist; no account is changed")
    cursor.execute("SHOW GRANTS FOR %s@%s", (config.reader_user, config.reader_host))
    granted = check_grants([next(iter(row.values())) for row in cursor.fetchall()], config.database, config.reader_user, require_all=False)
    cursor.execute("SELECT ATTRIBUTE AS `attributes` FROM information_schema.USER_ATTRIBUTES WHERE USER=%s AND HOST=%s", (config.reader_user, config.reader_host))
    row = cursor.fetchone()
    raw_attributes = row["attributes"] if row else None
    try:
        attributes = json.loads(raw_attributes) if isinstance(raw_attributes, str) else (raw_attributes or {})
    except (TypeError, ValueError):
        raise RuntimeError("Existing reader attributes cannot be verified; no account changes performed") from None
    if not isinstance(attributes, dict):
        raise RuntimeError("Existing reader attributes are not a JSON object; no account changes performed")
    marker = attributes.get("order_query_agent")
    expected_marker = reader_attribute(config.database)["order_query_agent"]
    marker_matches = isinstance(marker, dict) and marker == expected_marker and type(marker.get("version")) is int
    if "order_query_agent" in attributes and not marker_matches:
        raise RuntimeError("Existing reader ownership/database/version marker conflicts; no account changes performed")
    if granted != set(TABLE_NAMES) and not marker_matches:
        raise RuntimeError("Unmarked reader has incomplete grants; preserved without modification")
    return granted


def _verify_reader_password(config, expected_tables: set[str]):
    """Authenticate existing account and reject implicit/mandatory role access too."""
    probe = pymysql.connect(
        host=config.host, port=config.port, user=config.reader_user,
        password=config.reader_password.get_secret_value(), cursorclass=DictCursor,
        connect_timeout=3, read_timeout=5, write_timeout=3,
    )
    try:
        with probe.cursor() as cursor:
            cursor.execute("SELECT CURRENT_USER() AS `account_name`, CURRENT_ROLE() AS `active_roles`")
            identity = cursor.fetchone()
            if identity["account_name"] != f"{config.reader_user}@{config.reader_host}" or identity["active_roles"] != "NONE":
                raise RuntimeError("Existing reader authentication identity/roles conflict; no account changes performed")
            cursor.execute("SHOW GRANTS")
            actual_tables = check_grants([next(iter(row.values())) for row in cursor.fetchall()], config.database, config.reader_user, require_all=False)
            if actual_tables != expected_tables:
                raise RuntimeError("Reader grants changed during preflight; no account changes performed")
    finally:
        probe.close()


def initialize(config):
    connection = connect_admin(config)
    try:
        validate_schema(connection, config.database)
        with connection.cursor() as cursor:
            granted_tables = _existing_reader(cursor, config)
        if granted_tables is not None:
            # Verify provided password; never ALTER an unknown account to make it fit.
            _verify_reader_password(config, granted_tables)
        with connection.cursor() as cursor:
            # Database identifier comes only from the two-name whitelist.
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{config.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
            cursor.execute(f"USE `{config.database}`")
            schema = Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
            sql = "\n".join(line for line in schema.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("--"))
            for statement in sql.split(";"):
                if statement.strip():
                    cursor.execute(statement)
        validate_schema(connection, config.database, require_all=True)
        with connection.cursor() as cursor:
            if granted_tables is None:
                cursor.execute("CREATE USER %s@%s IDENTIFIED BY %s ATTRIBUTE %s", (config.reader_user, config.reader_host, config.reader_password.get_secret_value(), json.dumps(reader_attribute(config.database), separators=(",", ":"))))
                granted_tables = set()
            # Only marked, password-verified partial accounts can reach this branch.
            # CREATE USER/GRANT implicitly commit; reruns finish only missing grants.
            for table in TABLE_NAMES:
                if table not in granted_tables:
                    cursor.execute(f"GRANT SELECT ON `{config.database}`.`{table}` TO %s@%s", (config.reader_user, config.reader_host))
        with connection.cursor() as cursor:
            if _existing_reader(cursor, config) != set(TABLE_NAMES):
                raise RuntimeError("Reader grants remain incomplete; rerun initialization after investigating the error")
        connection.commit()
    finally:
        connection.close()


def main():
    try:
        initialize(load_admin_config())
    except ValidationError:
        print("Initialization failed: invalid/missing administrator or runtime configuration; secrets omitted.", file=sys.stderr)
        return 1
    except AppError as exc:
        print(f"Initialization failed: {exc.message}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"Initialization failed: {exc}", file=sys.stderr)
        return 1
    except pymysql.MySQLError as exc:
        code = exc.args[0] if exc.args else "unknown"
        print(f"Initialization failed: MySQL error {code}; original message omitted. Existing objects preserved.", file=sys.stderr)
        return 1
    print("MySQL 8.4 simulation schema and dedicated SELECT account verified; no seed data written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
