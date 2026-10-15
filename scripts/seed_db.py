"""Insert only missing fixed simulation rows, with conflict checks in one transaction."""

import sys
from datetime import datetime
from decimal import Decimal

import pymysql
from pydantic import ValidationError

from scripts.admin_config import connect_admin, load_admin_config
from scripts.init_db import validate_schema


PRODUCTS = (
    (1, "P1", "键盘", "数码"),
    (2, "P2", "鼠标", "数码"),
    (3, "P3", "杯子", "家居"),
)
ORDERS = (
    (1001, "O1001", "paid", datetime(2026, 10, 1)),
    (1002, "O1002", "paid", datetime(2026, 10, 1, 23, 59, 59, 999999)),
    (1003, "O1003", "paid", datetime(2026, 10, 2)),
    (1004, "O1004", "paid", datetime(2026, 10, 4)),
    (1005, "O1005", "paid", datetime(2026, 9, 30, 23, 59, 59, 999999)),
    (1006, "O1006", "unpaid", None),
    (1007, "O1007", "cancelled", datetime(2026, 10, 2, 12)),
)
ITEMS = (
    (1, 1001, 1, 2, Decimal("100.00")),
    (2, 1001, 2, 1, Decimal("50.00")),
    (3, 1002, 3, 3, Decimal("30.00")),
    (4, 1003, 1, 1, Decimal("100.00")),
    (5, 1003, 3, 2, Decimal("30.00")),
    (6, 1004, 2, 4, Decimal("50.00")),
    (7, 1005, 1, 1, Decimal("100.00")),
    (8, 1006, 1, 10, Decimal("100.00")),
    (9, 1007, 2, 10, Decimal("50.00")),
)
SEED_TABLES = (
    ("products", ("id", "sku", "name", "category"), PRODUCTS),
    ("orders", ("id", "order_no", "status", "paid_at"), ORDERS),
    ("order_items", ("id", "order_id", "product_id", "quantity", "unit_price"), ITEMS),
)


def seed(config):
    connection = connect_admin(config, select_database=True)
    inserted = 0
    try:
        validate_schema(connection, config.database, require_all=True)
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute("START TRANSACTION")
            pending = []
            for table, columns, expected_rows in SEED_TABLES:
                # Constant SQL identifiers; only script-owned seed values are bound.
                cursor.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY id FOR UPDATE")
                actual_rows = [tuple(row[column] for column in columns) for row in cursor.fetchall()]
                expected = {row[0]: row for row in expected_rows}
                if any(row[0] not in expected for row in actual_rows):
                    raise RuntimeError(f"Unknown {table} records exist; no data changed. Use a separate allowed empty demonstration database.")
                for row in actual_rows:
                    if expected[row[0]] != row:
                        raise RuntimeError(f"Existing {table} seed record conflicts; transaction rolled back without overwriting.")
                actual_ids = {row[0] for row in actual_rows}
                pending.append((table, columns, [row for row in expected_rows if row[0] not in actual_ids]))
            for table, columns, missing in pending:
                for row in missing:
                    cursor.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})", row)
                    inserted += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return inserted


def main():
    try:
        inserted = seed(load_admin_config())
    except ValidationError:
        print("Seed failed: invalid/missing administrator or runtime configuration; secrets omitted.", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"Seed failed: {exc}", file=sys.stderr)
        return 1
    except pymysql.MySQLError as exc:
        code = exc.args[0] if exc.args else "unknown"
        print(f"Seed failed: MySQL error {code}; original message omitted; transaction rolled back.", file=sys.stderr)
        return 1
    print(f"Fixed SIMULATED data verified; inserted {inserted} missing rows (products=3, orders=7, order_items=9).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
