"""Read-only MySQL repository; never imports or reads administrator settings."""

import re
from contextlib import contextmanager
from decimal import Decimal
from threading import BoundedSemaphore

import pymysql
from pymysql.cursors import DictCursor

from app.errors import AppError
from app.query_templates import select_template
from app.schemas import ValidatedPlan


TABLES = ("products", "orders", "order_items")


def check_grants(grants: list[str], database: str, username: str, *, require_all: bool = True) -> set[str]:
    """Fail closed: roles, global privileges, proxy and grant option are forbidden."""
    account = rf"`{re.escape(username)}`@`[^`]+`"
    seen = set()
    for grant in grants:
        if re.fullmatch(rf"GRANT USAGE ON \*\.\* TO {account}", grant, re.IGNORECASE):
            continue
        match = re.fullmatch(
            rf"GRANT SELECT ON `{re.escape(database)}`\.`(products|orders|order_items)` TO {account}",
            grant,
            re.IGNORECASE,
        )
        if not match:
            raise AppError("DB_UNAVAILABLE", "运行账号的授权不符合专用只读要求。", 503, "unsafe_permissions")
        seen.add(match.group(1))
    if require_all and seen != set(TABLES):
        raise AppError("DB_UNAVAILABLE", "运行账号缺少三个演示表的明确 SELECT 授权。", 503, "unsafe_permissions")
    return seen


class Repository:
    def __init__(self, settings):
        self.settings = settings
        self._semaphore = BoundedSemaphore(settings.db_max_concurrency)

    def _connect(self):
        return pymysql.connect(
            host=self.settings.db_host,
            port=self.settings.db_port,
            user=self.settings.db_user,
            password=self.settings.db_password.get_secret_value(),
            database=self.settings.db_name,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=self.settings.db_connect_timeout,
            read_timeout=self.settings.db_read_timeout,
            write_timeout=self.settings.db_write_timeout,
            # Multi-statements are intentionally not enabled.
        )

    def _check_access(self, connection) -> str:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION() AS server_version, CURRENT_USER() AS `current_user`, CURRENT_ROLE() AS `current_role`")
            identity = cursor.fetchone()
            version = identity["server_version"]
            if not re.match(r"^8\.4\.\d+(?:[-.]|$)", version):
                raise AppError("DB_UNAVAILABLE", "需要真实 MySQL 8.4；当前数据库版本不受支持。", 503, "unsupported_database")
            if identity["current_user"].rsplit("@", 1)[0] != self.settings.db_user or identity["current_role"] != "NONE":
                raise AppError("DB_UNAVAILABLE", "运行账号身份或角色不符合专用只读要求。", 503, "unsafe_permissions")
            cursor.execute("SHOW GRANTS")
            check_grants([next(iter(row.values())) for row in cursor.fetchall()], self.settings.db_name, self.settings.db_user)
        return version

    @contextmanager
    def _connection(self):
        if not self._semaphore.acquire(timeout=self.settings.db_acquire_timeout):
            raise AppError("DB_UNAVAILABLE", "数据库查询资源暂不可用，请稍后重试。", 503, "concurrency_limit")
        connection = None
        try:
            try:
                connection = self._connect()
            except pymysql.MySQLError:
                raise AppError("DB_UNAVAILABLE", "无法连接只读 MySQL 数据库。", 503, "connection") from None
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION time_zone = %s", ("+08:00",))
                cursor.execute("SET SESSION max_execution_time = %s", (self.settings.db_max_execution_time,))
                cursor.execute("START TRANSACTION READ ONLY")
            self._check_access(connection)
            yield connection
        except pymysql.MySQLError as exc:
            code = exc.args[0] if exc.args else None
            if code == 3024:
                raise AppError("QUERY_TIMEOUT", "MySQL 服务端查询执行超过时间限制。", 504, "server_timeout") from None
            if code in {1040, 1042, 1043, 1044, 1045, 1049, 1129, 1130, 1203, 2002, 2003, 2006, 2013, 2055}:
                raise AppError("DB_UNAVAILABLE", "只读 MySQL 连接暂不可用。", 503, "connection") from None
            raise AppError("QUERY_FAILED", "固定查询执行失败，请使用 request_id 联系维护者。", 500, "database") from None
        finally:
            try:
                if connection is not None:
                    try:
                        connection.rollback()
                    except pymysql.MySQLError:
                        pass
                    connection.close()
            finally:
                self._semaphore.release()

    def health(self) -> dict:
        with self._connection() as connection:
            version = self._check_access(connection)
            with connection.cursor() as cursor:
                cursor.execute("SELECT p.id FROM products AS p JOIN order_items AS i ON i.product_id = p.id JOIN orders AS o ON o.id = i.order_id LIMIT %s", (1,))
                cursor.fetchall()
        return {"database": "mysql", "database_ready": True, "server_version": version}

    def execute(self, plan: ValidatedPlan) -> dict:
        template_id, sql, params = select_template(plan)
        query = {"template_id": template_id, "sql_template": sql, "query_params": params}
        attempted = False
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    attempted = True
                    cursor.execute(sql, params)
                    rows = list(cursor.fetchall())
        except AppError as exc:
            if attempted:
                exc.query = query
            raise
        if plan.query_type == "category_sales" and len(rows) > 100:
            error = AppError("RESULT_LIMIT_EXCEEDED", "品类超过 100 个，请缩小日期区间，并将原问题与补充条件一起重新提交。", 200, "needs_clarification")
            error.query = query
            raise error
        for row in rows:
            if not isinstance(row.get("sales_amount"), Decimal):
                error = AppError("QUERY_FAILED", "数据库金额没有按精确 DECIMAL 返回。", 500, "result_type")
                error.query = query
                raise error
            quantity = row["sales_quantity"]
            if isinstance(quantity, bool) or not isinstance(quantity, (int, Decimal)) or quantity != int(quantity):
                error = AppError("QUERY_FAILED", "数据库销量没有按整数返回。", 500, "result_type")
                error.query = query
                raise error
            row["sales_quantity"] = int(quantity)
        return {"template_id": template_id, "sql_template": sql, "query_params": params, "rows": rows}
