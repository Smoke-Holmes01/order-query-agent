# 实际验收记录

验收日期：2026-10-15（Asia/Shanghai）。所有业务数据均为模拟数据。

本项目的数据库验收使用真实 MySQL，未使用 SQLite；模型验收分别记录 mock 和 live，未用 mock 替代真实模型。

## 环境与秘密边界

| 项目 | 实测值 |
| --- | --- |
| 宿主系统 | Windows、PowerShell |
| 宿主 Python | 3.11.16，项目独立 `.venv` |
| 容器 Python | 3.11.17（同属要求的 3.11 系列） |
| MySQL | 8.4.11，Docker Official Image，独立命名卷 |
| Docker / Compose | 29.8.1 / v5.5.1 |
| FastAPI / Pydantic | 0.143.1 / 2.14.1 |
| LangGraph / PyMySQL / httpx | 1.2.15 / 1.2.3 / 0.28.1 |
| 模型模式 | mock和真实live分别评测；live使用用户指定的gemini-3.8-flash |

完整依赖固定在 requirements.txt；MySQL 与 Python 基础镜像固定实际拉取的 digest。管理员和运行密码分别使用随机值，仅保存于被忽略的本地文件，未打印或提交。未查找电脑上的既有数据库或模型凭据，未改动既有 MySQL84 服务。

## 初始化与独立 SQL 核对

实际执行 `python -m scripts.init_db`、`python -m scripts.seed_db`，均退出 0。第一次 seed 插入 19 条固定记录（products=3、orders=7、order_items=9）；重复执行 init/seed 均退出 0，第二次 seed 插入 0 条。

独立控制 SQL 不从被测模板导入，结果保存于 [control-sql.json](control-sql.json)：

| 检查 | 真实 MySQL 结果 |
| --- | --- |
| 每日 `[2026-10-01,2026-10-04)` | 340.00 / 6件；160.00 / 3件 |
| 品类 | 数码350.00 / 4件；家居150.00 / 5件 |
| 商品金额 | P1 300.00；P3 150.00；P2 50.00 |
| 商品销量 | P3 5；P1 3；P2 1 |
| 下边界 `[2026-10-02,2026-10-04)` | 160.00 |
| 上边界 `[2026-10-01,2026-10-02)` | 340.00 |
| 空区间 `[2026-10-03,2026-10-04)` | 空行集合，不补零 |
| 全区间独立汇总 | 500.00，多明细正确且 unpaid/cancelled 排除 |

金额保留 Decimal，O1002 的支付时间为 `23:59:59.999999`，unpaid 的支付时间为 NULL；二者均在真实集成测试中核对。

## 测试、权限和超时

本轮修复后完整运行 **339 passed in 4.31s**，退出0，无失败、无跳过项，其中5项真实MySQL集成检查已明确启用。启用命令为：

```powershell
$env:RUN_MYSQL_TESTS = "1"
python -m pytest -q
```

真实 MySQL 检查使用运行账号，先核对 `CURRENT_USER()`、`CURRENT_ROLE()`、`SHOW GRANTS`；仅接受 USAGE 和三个指定表的 SELECT，无角色、写入、DDL、全局权限或 GRANT OPTION。所有授权检查通过后才运行无数据影响的探针：

```sql
INSERT INTO products (id,sku,name,category)
SELECT id,sku,name,category FROM products WHERE 1=0;
UPDATE products SET name=name WHERE 1=0;
DELETE FROM products WHERE 1=0;
```

三条均收到 MySQL 1142 权限拒绝；DDL 权限依据完整授权核查，没有执行 DDL 探针。检查前后全部 seed 行及字段相同，计数保持3/7/9。

会话实测 `time_zone='+08:00'`、`max_execution_time=2000`。表查询中的受控 `SLEEP(6)` 被服务器以3024中止，随后同一连接 `SELECT 1` 成功，seed 快照仍一致。客户端 read timeout 为5秒；3024来自服务器，不是将客户端停止等待误记为中止查询。

单元/接口测试覆盖严格整数、额外字段和state注入、日期和N范围、最多两次格式解析、供应商分类及总等待预算、数据库异常、连接关闭与名额释放、并发上限和请求状态隔离。新增19项fake-live ready反例证明：即使模型给出合法完整计划，缺失或冲突的排行条件、矛盾边界及具体时分秒仍被本地拒绝查询；这类适配器测试不算真实模型验收。

另在白名单 `order_query_demo_test` 中新建专用测试账号，实际验证CREATE USER的项目ATTRIBUTE标记。仅对该新账号分别保留0、1、2项SELECT授权，然后重跑初始化，三次都恢复到指定三表SELECT，seed每次新增0条。主demo库和原运行账号未改动。证据见 [init-recovery.json](init-recovery.json)；未知或未标记的部分授权账号仍拒绝自动修复。CHECK字面值内的空格、括号和大小写也有拒绝冲突的回归测试。

## 语义评测与 HTTP 演示

`python -m scripts.evaluate --mode mock --output docs/evaluation.mock.json` 实际使用 mock 计划解析加真实 MySQL，退出0。报告见 [evaluation.mock.json](evaluation.mock.json)：16条总用例，计划/状态16/16，查询结果8/8，澄清/拒绝8/8，回答检查16/16；8条非查询用例的业务查询次数全部为0。该结果只衡量固定用例，不能泛化为通用自然语言查询准确率。

用户提供真实模型配置并明确同意development模式明文HTTP后，实际执行 `python -m scripts.evaluate --mode live --output docs/evaluation.live.json`，16条全部尝试，退出1。计划/状态9/16、查询结果5/8、非查询4/8、回答12/16；7个最终失败分为3次MODEL_OUTPUT_INVALID和4次MODEL_TIMEOUT，业务查询次数全为0。报告见 [evaluation.live.json](evaluation.live.json)，逐条计划核对及补充诊断说明见 [live-verification.md](live-verification.md)。**真实模型链路已验证可运行，稳定性尚未全部验收通过**。此前缺配置的失败记录不再作为当前live报告。

本机实际启动 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8002`，`/health`、`/openapi.json` 返回200。四种真实HTTP响应已写入 [demo.md](demo.md) 和 [demo.local.json](demo.local.json)：金额排行/每日查询成功，口径不明澄清，删除请求拒绝。响应展示真实 plan、data、query 和 trace。

## Compose 复现与外部缺口

已实际执行 `docker compose --env-file .env.admin up --build -d`，退出0。db健康、bootstrap退出0且seed新增0条、API运行；宿主映射仅为 `127.0.0.1:3307` 和 `127.0.0.1:8002`。API容器实测UID=10001，不存在DB_ADMIN环境变量、管理员脚本目录或 `.env.admin` 文件。命名卷始终保留。

修复后的容器API（mock模式）的 `/health`、`/docs` 返回200，16条真实HTTP语义请求全部与已核对计划及MySQL rows一致；客户端state注入返回422。用户原句“不含开始日期”返回needs_clarification，“排除周末”返回unsupported，query=null、data=[]且trace无execute_query。证据见 [compose-verification.json](compose-verification.json)。目前8002端口由Compose API提供，真实live通过评测CLI独立验证。

Docker 29.8.1 的 internal 网络未发布宿主端口，因此使用项目独立普通bridge。`MYSQL_ROOT_HOST=%` 仅为宿主初始化与bootstrap开发访问，配合回环宿主映射和独立网络；这不是生产部署安全方案。

验收中实际发现并修复 MySQL CHECK元数据转义形式及身份SQL保留字别名兼容问题，均以真实MySQL复验；没有放宽结构、授权或超时要求。

本轮确认并修复：原句末尾的开始边界遗漏，以及live候选计划丢失附加条件仍被放行。修复后两种模式共享保守语法覆盖检查；未知条件澄清、周末/节假日筛选拒绝，不能因为模型返回ready而绕过。本轮新增126项测试（原213项，现339项），包含模拟live-ready零查询回归，不将替身模型计作真实live成绩。

当前缺口：live格式稳定性和超时尚未全部通过；生产部署和性能基准未验证。已有本机MySQL路径共享相同入口，但本轮数据库验收来自Docker MySQL8.4.11，不宣称已验收电脑上既有MySQL服务或所有操作系统。HTTP开发开关默认关闭，只有development可以显式启用，实际评测前已获用户明确授权；原始凭据只在忽略的本地配置中，报告不记录模型地址或密钥。
