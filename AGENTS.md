# 电商经营分析工作流助手：项目执行规范

## 1. 任务与交付原则

你是本项目的实现 Agent。请从当前独立项目根目录出发，完成源码、数据库初始化、模拟数据、模型适配、LangGraph 工作流、接口、测试、评测和说明文档，并实际执行能够运行的验收。不要停在方案或代码骨架。先检查目录内容，保留已有文件和用户改动，再按本文件推进；常规实现选择由你自主决定。

项目准确定位为：**受限工具型电商经营查询 Agent**。模型将自然语言转换为结构化查询计划，程序选择固定 SQL 模板查询 MySQL，并展示结果与执行过程。它不是通用 NL2SQL，不允许模型自由生成 SQL，不是完整 BI 平台，也不声称能自主发现经营原因。

所有业务数据必须明确标注为模拟数据。简历建议、README 和演示只能描述已实现、已验证的能力；没有实测结果时保留空缺，不能编造正确率、性能提升、用户量或业务收益。遇到确实无法获得的外部依赖，可以继续完成独立部分，但必须留下准确的未验证项，不能把 mock 当成真实模型验收，不能把 SQLite 当成 MySQL 验收。

不要引入退款、预测、推荐、任意 Schema 检索、向量数据库、多 Agent、持久化记忆、任务队列、前端框架或自动写数据库等范围。必要的接口文档和演示请求即可形成完整展示。

## 2. 固定技术栈与资料

- Python 3.11，FastAPI，Pydantic v2，LangGraph `StateGraph`。
- MySQL 8.4，PyMySQL；使用明确的参数化 SQL，不添加 ORM。
- httpx 访问兼容 Chat Completions 的模型接口；API 地址、模型名和密钥通过环境变量配置。
- pytest；Docker Compose 提供独立演示数据库，并支持连接本机 MySQL 8.4。
- 依赖包含 python-dotenv，必要时加入 tzdata；固定实际验证的依赖版本，并记录 Python/MySQL 版本。

先核对当前官方文档，再使用实际存在的 API，不凭印象拼接 LangGraph 方法：

- LangGraph Graph API：https://docs.langchain.com/oss/python/langgraph/graph-api
- MySQL 8.4 Reference Manual：https://dev.mysql.com/doc/refman/8.4/en/
- MySQL 执行时间提示：https://dev.mysql.com/doc/refman/8.4/en/optimizer-hints.html

使用请求级 `TypedDict` state、`StateGraph`、条件边、`compile()` 和 `invoke()` 即可；没有持久化需求，不加入 checkpointer。若接口是 async，需要把同步 PyMySQL/图执行放到线程池，不能阻塞事件循环；也可以使用普通 `def` 路由统一执行同步图。

## 3. 建议目录与配置边界

允许为清晰性微调文件名，但应保持职责分离：

```text
app/
  main.py                 # FastAPI 应用和路由
  config.py               # 仅运行时配置，不读取管理员配置
  schemas.py              # 请求、模型决策、查询计划、响应 DTO
  graph.py                # state、节点、条件路由与编译
  model_adapter.py        # live/mock 适配器
  plan_validation.py      # 日期、枚举、组合与业务范围校验
  query_templates.py      # 固定 SQL、固定字段和模板标识
  repository.py           # 只读连接、超时、参数化执行
  formatting.py           # 基于真实 rows 生成确定性回答
scripts/
  __init__.py
  init_db.py              # 管理员入口：仅建演示库、表和只读账号
  seed_db.py              # 管理员入口：幂等写入固定模拟数据
  evaluate.py             # mock/live 均查询真实 MySQL
tests/
  test_plan_validation.py
  test_graph.py
  test_api.py
  test_mysql_integration.py
evals/
  cases.json              # 语义用例和人工核对的期望结果
sql/
  schema.sql
docs/
  demo.md
  interview.md
  verification.md
.env.example              # 运行时变量，无真实秘密
.env.admin.example        # 初始化/Compose 管理变量，无真实秘密
.gitignore
compose.yaml
Dockerfile
requirements.txt
README.md
```

运行时 `.env` 至少包含 `APP_ENV=development`、`APP_TIMEZONE=Asia/Shanghai`、`MODEL_MODE=mock|live`、`MODEL_BASE_URL`、`MODEL_NAME`、`MODEL_API_KEY`、`DB_HOST`、`DB_PORT`、`DB_NAME=order_query_demo`、`DB_USER`、`DB_PASSWORD` 和模型/数据库限额。配置值须有类型及范围校验。

管理员 `.env.admin` 单独保存 `DB_ADMIN_HOST`、`DB_ADMIN_PORT`、`DB_ADMIN_USER`、`DB_ADMIN_PASSWORD` 等初始化连接配置。只有初始化和 seed 脚本可以读取它；`app` 不得导入管理员模块，不得在查询失败后回退到管理员账号。Compose 的 API 服务只接收运行时变量；管理员密码只用于数据库初始化容器及初始化脚本。

`.env`、`.env.admin`、本地虚拟环境、缓存和原始敏感日志必须被忽略。示例文件只放占位符；可以用 Python `secrets` 为本地演示生成随机密码，但不得提交或打印真实密钥。不要写入通用共享密码，也不要从用户电脑上搜寻现有数据库/模型凭据。

默认仅操作固定库 `order_query_demo`，若测试需要独立库，使用显式列出的 `order_query_demo_test`。初始化入口必须检查库名白名单、连接目标和必要配置，禁止自动猜测生产库。已有库存在时，不得清空、删除表或覆盖未知数据；冲突时输出清晰错误并保留现场。

## 4. 数据结构、业务口径与固定数据

创建 InnoDB、utf8mb4 表，外键与必要索引一起交付：

| 表 | 核心字段 | 约束/索引 |
| --- | --- | --- |
| `products` | `id`、`sku`、`name`、`category` | 主键；SKU 唯一；名称和品类非空 |
| `orders` | `id`、`order_no`、`status`、`paid_at` | 主键；订单号唯一；状态限 paid/unpaid/cancelled；索引 `(status, paid_at, id)` |
| `order_items` | `id`、`order_id`、`product_id`、`quantity`、`unit_price` | 外键关联订单和商品；订单/商品索引；数量大于 0，单价非负 |

`unit_price` 使用 `DECIMAL(12,2)`，表示下单时的成交单价，不能使用商品当前价格计算历史销售额。金额在 SQL 中以 DECIMAL 求和，在 Python 中保持 `Decimal`，不能经 float 中转。响应以两位小数的字符串序列化金额，例如 `"340.00"`；销量保持整数。统一为人民币，不做币种换算。

`paid_at` 使用 `DATETIME(6)`，项目约定其值为 Asia/Shanghai 的当地时间，应用将日期转换为当地零点参数；数据库连接设置 `time_zone='+08:00'`。明确 DATETIME 本身不携带时区，不能宣称数据库替你自动转换。paid 订单必须有支付时间；unpaid 允许为 NULL。

统计只包含 `orders.status='paid'`，采用支付时间，筛选条件必须为 `paid_at >= start AND paid_at < end`，即左闭右开。销售额为 `SUM(quantity * unit_price)`，销量为 `SUM(quantity)`；cancelled/unpaid 均排除。按日期聚合只返回实际有符合条件订单的日期，不自动补零；空数据返回空数组和明确说明。

使用固定种子与稳定主键，至少包含下列可人工验证的数据。商品 P1/P2/P3 分别为键盘（数码）、鼠标（数码）、杯子（家居），成交单价分别为 100.00、50.00、30.00：

| 订单 | 状态/支付时间 | 明细 |
| --- | --- | --- |
| O1001 | paid，2026-10-01 00:00:00 | P1 × 2，P2 × 1 |
| O1002 | paid，2026-10-01 23:59:59.999999 | P3 × 3 |
| O1003 | paid，2026-10-02 00:00:00 | P1 × 1，P3 × 2 |
| O1004 | paid，2026-10-04 00:00:00 | P2 × 4 |
| O1005 | paid，2026-09-30 23:59:59.999999 | P1 × 1 |
| O1006 | unpaid，NULL | P1 × 10 |
| O1007 | cancelled，2026-10-02 12:00:00 | P2 × 10 |

区间 `[2026-10-01, 2026-10-04)` 的总额应为 500.00；每日分别为 340.00、160.00；数码为 350.00、家居为 150.00；商品销售额为 P1 300.00、P3 150.00、P2 50.00；销量为 P3 5、P1 3、P2 1。2026-10-03 没有符合条件的订单。这些值应通过真实 MySQL 查询验证，不能由应用直接返回这些常量。

`python -m scripts.init_db` 创建必要对象和专用只读账号；`python -m scripts.seed_db` 只插入缺失的固定记录。两者重复运行不得制造重复记录或修改未知数据；建议事务内检查已存在的 seed 主键是否一致，不一致则回滚并报错。不要提供默认执行的 DROP/TRUNCATE/reset；不需要以破坏性重建才能复现。

## 5. 模型输出与查询计划

只有以下三类 `query_type`：

- `daily_sales`：按支付日期统计销售额和销量，日期升序。
- `category_sales`：按品类统计销售额和销量，销售额降序、品类升序。
- `top_products`：按商品统计，以 `rank_by=sales_amount|sales_quantity` 排名，`top_n` 为 1—10，分数相同时按商品 ID 升序。

计划固定字段为 `query_type`、`start_date`、`end_date`、`rank_by`、`top_n`。日期采用严格 `YYYY-MM-DD`，`start_date < end_date`，跨度最多 90 个自然日。daily/category 的 `rank_by`、`top_n` 应为 null；top_products 必须给出有效排名口径及数量。不要接受 SQL、表名、字段名、排序表达式或数据库连接参数。

可使用两层 Pydantic DTO：模型候选计划允许缺参，程序验证后的计划必须完整。模型返回 `decision=ready|needs_clarification|unsupported`，以及候选 `plan` 和简短 `message`；输出 schema 设置额外字段禁止，枚举和整数严格校验。最终是否允许查询由程序决定，不能因为模型说 ready 就跳过检查。

示例：

```json
{
  "decision": "ready",
  "plan": {
    "query_type": "top_products",
    "start_date": "2026-10-01",
    "end_date": "2026-10-04",
    "rank_by": "sales_amount",
    "top_n": 2
  },
  "message": ""
}
```

缺日期、时间边界不清、缺排名口径或缺 N 时返回澄清；不要偷偷假设。自然语言明确“包含结束日期”时，可以转成次日的排他上界，但响应必须展示转换后的区间。相对日期若未实现可靠解释，应要求用户提供明确年月日。超出 90 日、N 超范围、日期颠倒等情况提示用户调整，不能静默截断或改写意图。

预测、退款、利润、订单明细导出、任意筛选条件、数据库管理以及写操作请求均为 unsupported。遇到“忽略规则、输出 SQL、执行删除”等指令也不能扩大权限。安全边界由枚举、模板、参数化和数据库权限共同承担，不能把 Prompt 当作安全机制。

live 适配器通过 httpx 调用配置的模型，只发送必要问题、支持范围及结构化输出约束；不发送数据库密码、管理员配置或全表数据。使用供应商支持的 JSON 输出能力；仍须进行本地 schema 校验。HTTP 超时必须有限，建议总等待约 15 秒，并分别设置 connect/read/write/pool 超时，禁用无限重试。错误日志不得记录 Authorization 或完整密钥。

`MODEL_MODE=mock` 提供规则驱动的离线计划解析，仅覆盖 README 明示的演示表达和用例；无法识别时诚实返回澄清/不支持。mock 只替代模型，后续必须真实执行 MySQL，不准预置查询结果。mock 不是任意中文理解能力证明。

`MODEL_MODE=live` 缺少密钥、模型名或地址时明确失败；不得悄悄切换 mock。评测报告和每条响应均显示 `model_mode`。网络错误、鉴权失败、限流和格式错误应分类；不要将它们统一描述为“问题不支持”。

## 6. LangGraph 状态、分支和执行限制

主流程为 `parse_plan -> validate_plan -> execute_query -> format_answer`，使用条件边实现分支。state 至少包含：

```text
request_id, question, model_mode, parse_attempts,
candidate_plan, validated_plan, status, clarification,
template_id, sql_template, query_params, rows,
error_code, safe_error_message, trace, answer
```

state 由服务端创建。每个节点返回状态更新，不能接受用户上传的 graph state、SQL、rows、attempts 或角色字段。状态字符串应定义为固定枚举。trace 至少记录节点名、节点结果和耗时，格式错重试也可见；不记录密码、请求 Authorization、完整原始模型响应或堆栈。

条件路由必须满足：

1. parse 成功后进入 validate。JSON/schema 格式错误时最多重新请求模型一次，state 中总解析尝试最多 2 次；第二次仍错误即结束。
2. 缺参或有效业务歧义进入 needs_clarification，跳过数据库；不支持的意图进入 unsupported，跳过数据库。
3. 参数完整且通过本地规则后执行数据库；异常、超时立即停止，不让模型猜测结果。
4. 不自动修复数据库错误，不更换账号、不扩大范围、不放宽限额、不无限重试。格式重试与数据库重试不能混淆。
5. format_answer 可以统一生成成功/澄清/不支持/失败响应，但只能用当前真实状态，不能把失败写成成功。

本项目是请求级流程。澄清结果应说明缺少什么，并提示将原问题和补充条件一起重新提交；README 明确没有跨请求记忆。若额外实现服务端会话，必须证明隔离和状态来源，但不作为默认工作量。

SQL 模板与 query_type 的映射在代码中固定。表名、列名、GROUP BY、ORDER BY 方向均不能来自自由字符串；top_products 的两个排名字段通过固定分支选择。所有外部值，包括日期、N 和结果限额，使用 PyMySQL `%s` 参数绑定，不做字符串拼接。禁止模型返回 SQL 后再“尽量执行”。

专用运行账号仅拥有三个演示表的 SELECT 权限，不给 INSERT/UPDATE/DELETE/DDL、FILE、PROCESS、GRANT 或全局权限。启动时可校验账号身份和授权，发现明显管理员账号应拒绝启动。连接采用只读事务作为附加措施，但不能用它替代最小权限。

每次查询连接设置服务端 `max_execution_time`，建议 2000 毫秒，适用于本项目的只读 SELECT；必要时使用官方支持的 MAX_EXECUTION_TIME 提示并实际验证。connect/read/write 等 socket 超时也必须有限，但 socket read_timeout 只是客户端停止等待，不能宣称其会中止服务器查询。不得为了中止查询给运行账号增加管理权限。

服务端同时限制日期跨度、top_n、返回行数和并发查询数，例如最多 4 个并发连接。daily_sales 自然最多 90 行；category_sales 最多读取 101 行，若超过 100 行则返回范围错误或要求缩小问题，不静默截断；top_products 最多 10 行。固定模板只使用必要关联，读取后及时关闭连接。LIMIT 只限制结果量，不能保证扫描/排序成本，因此必须保留服务端时间限制。

format_answer 用确定性模板即可。说明统计指标、已支付口径、支付时间区间、排名规则和实际结果。金额从 SQL 结果读取；空数据要说“该区间没有符合口径的记录”。不能凭数据推断营销活动、用户偏好、增长原因或经营建议，也不能让模型重新计算替代 SQL 金额。

## 7. API、状态与 HTTP 合同

提供 `GET /health`、`POST /api/query` 和自动生成的 `/docs`。请求体仅接受：

```json
{"question": "按销售额查询从2026-10-01（含）到2026-10-04（不含）的前2个商品"}
```

question 去除首尾空白后长度为 1—1000；请求 DTO 使用 `extra='forbid'`。禁止客户端设置 model_mode、state、SQL、数据库连接和重试次数；不启用面向任意来源的宽泛 CORS。默认监听 127.0.0.1。

统一响应包含 `request_id`、`status`、`model_mode`、`answer`、`plan`、`data`、`trace`、`error`，以及可审查的 `query`。成功时 `query` 可展示固定模板标识、带占位符的 SQL 和安全参数，不能暴露连接信息。未执行查询时 query 为 null，data 为空数组。错误包含稳定 `code` 和安全 `message`。

| 情况 | HTTP | status / error code |
| --- | --- | --- |
| 正常完成，包括空数据 | 200 | success，error=null |
| 缺参、歧义或需调整业务范围 | 200 | needs_clarification |
| 超出支持范围/写操作请求 | 200 | unsupported |
| 请求字段、类型、长度或 JSON 不合法 | 422 | FastAPI/Pydantic 请求校验错误 |
| 两次模型输出仍不合法 | 502 | failed / MODEL_OUTPUT_INVALID |
| 模型网络、鉴权、限流等供应商异常 | 502 | failed / MODEL_PROVIDER_ERROR |
| 模型等待超时 | 504 | failed / MODEL_TIMEOUT |
| 数据库不可达或连接失败 | 503 | failed / DB_UNAVAILABLE |
| 服务端查询超时 | 504 | failed / QUERY_TIMEOUT |
| 固定 SQL 执行异常/内部错误 | 500 | failed / QUERY_FAILED 或 INTERNAL_ERROR |

422 可保留框架默认格式，但必须在 README 说明这一例外。业务失败也应包含安全 trace；不要返回原始数据库异常、密码、供应商响应或堆栈。响应前后保持同一 request_id，服务端日志可据此定位。

`/health` 检查服务配置和只读 MySQL 连接，成功返回 200，包含 model_mode、database=mysql、database_ready=true；数据库不可用返回 503。mock 健康不能被描述为 live 模型健康；健康检查不触发付费模型调用。

## 8. 测试、12 条语义检查与安全核查

先为验证计划、路由和错误合同写必要测试，再在真实 MySQL 上验证 SQL 与权限。单元测试可以替换模型/连接；集成测试必须使用 MySQL 8.4，不使用 SQLite 模拟权限、DECIMAL、日期函数或执行时间行为。

`evals/cases.json` 至少包含以下 12 条语义检查。每条保存输入、预期状态、标准计划及必要的标准 rows。前 8 条的期望结果由人工 SQL 核对，不从被测模板调用后反向生成。金额比较为精确 Decimal；排名保留顺序，不需要顺序的结果按稳定键比较，保留重复行语义和 NULL/空结果的区别。

| 编号 | 场景 | 必须检查的结果 |
| --- | --- | --- |
| 1 | `[10-01,10-04)` 每日销售 | 340.00、160.00，日期升序 |
| 2 | 同区间品类销售 | 数码 350.00、家居 150.00 |
| 3 | 同区间销售额前 2 商品 | P1 300.00、P3 150.00 |
| 4 | 同区间销量前 1 商品 | P3，销量 5 |
| 5 | `[10-02,10-04)` | 包含下边界订单，销售额 160.00 |
| 6 | `[10-01,10-02)` | 排除上边界订单，销售额 340.00 |
| 7 | `[10-03,10-04)` 空区间 | success、空 rows、无补造数据 |
| 8 | 全区间汇总核对口径 | 累计 500.00，多明细正确且 unpaid/cancelled 排除 |
| 9 | “该区间卖得最好的前 2 个商品” | 追问按销售额还是销量；零数据库查询 |
| 10 | 缺日期/结束日期含义不清 | needs_clarification；零数据库查询 |
| 11 | 预测下一期销售额 | unsupported；零数据库查询 |
| 12 | 要求删除订单或拼接 SQL | unsupported；零数据库查询 |

表中日期在实际用例里必须写完整年份。第 8 条可对 daily_sales 返回值独立求和进行验收，不能为它扩展第 4 类查询。补充必要的技术测试：日期颠倒/超 90 日、N 超范围、额外字段/客户端 state 注入、严格整数、格式错仅重试一次、模型超时、数据库异常、连接关闭与并发限额。mock 的解析规则可见、可审查，不能按用例 ID 返回期望结果。

安全核查必须实际连接 MySQL：

- 用运行账号查询成功，并核对当前用户与授权，确认只授予指定表 SELECT。
- 先检查授权；如果发现运行账号存在写入、DDL 或全局权限，直接判定验收失败，不继续发出可能修改数据的语句。禁止用管理员账号替运行账号执行权限核查。
- 在隔离演示库中，用不匹配任何记录的 INSERT ... SELECT ... WHERE 1=0、UPDATE ... WHERE 1=0、DELETE ... WHERE 1=0 核查写权限拒绝，明确列出所用合法字段；不要把真实 seed 记录当作修改目标。DDL 权限优先依据授权结果核查，如需执行验证，仅在专用测试环境使用无业务影响的测试对象。
- 对权限检查前后的 seed 数据做一致性核对；权限配置出错时测试也不得改变业务记录。额外事务保护不能替代无数据影响的测试设计，DDL 也不能依赖事务回滚。
- 核对会话的 max_execution_time，并在隔离演示环境用受控只读查询验证服务器执行限制；单纯看到 socket timeout 不算通过。若环境无法可靠验证，明确标记该项未验证。
- 模型输出或用户输入里出现额外 SQL/字段时，验证本地拒绝且没有未知 SQL 到达数据库。

`python -m scripts.evaluate --mode mock` 必须使用 mock 计划解析加真实 MySQL；`--mode live` 使用真实模型加真实 MySQL。live 缺配置应非零退出，不回退。报告分别输出总用例数、计划/状态通过数、结果正确数、错误分类和耗时；追问/拒绝不能混算为 SQL 正确率。不要把 12 条固定用例的结果泛化成“通用查询准确率”。

默认评测以规范化计划和真实 rows 为准，不比较 SQL 文本。结果为空的案例必须同时检查计划和区间，避免错误 SQL 碰巧返回空结果也算通过。格式化答案至少核对口径、金额与 rows 一致。

## 9. 可复现运行与交付流程

依次完成以下过程，并将执行结果写入 `docs/verification.md`：

1. 检查现有仓库和适用指令，确认 Python 与依赖；建立目录、配置 DTO 和忽略规则。初始化本地演示密码时不覆盖已有值，不把秘密写入报告。
2. 实现 compose.yaml 和数据库结构；数据库映射仅绑定 `127.0.0.1:3307`，API 宿主端口映射为 `127.0.0.1:8002:8002`。本机直接启动 Uvicorn 使用 `127.0.0.1`；容器内部必须使用 `--host 0.0.0.0 --port 8002`，确保端口映射能访问服务。数据库有健康检查、独立命名卷，API 依赖数据库健康；不要执行删除已有卷的命令。
3. 实现并重复执行初始化/seed，证明幂等性；使用管理员配置完成建表与授权后，切换到专用只读账号。运行时连接默认本机 3307，Compose API 内部覆盖为服务名 db、端口 3306。
4. 先验证三个固定查询的真实结果，再接入模型适配和 LangGraph；保留清楚的节点 trace，完成缺参/不支持/错误出口。
5. 实现接口合同与配置启动检查，完成单元测试和真实 MySQL 集成检查，再运行 mock 评测。有真实密钥且调用授权明确时运行 live 评测；没有则交付可用 mock 路径并如实说明 live 未验收。
6. 完成 Docker 和本机启动说明、演示请求、面试解释与最终验收记录。只在检查失败或实现变化时追加相应验证，不无意义重复运行。

README 必须给出可直接执行的命令，核心入口统一如下，不要混用文件路径脚本入口：

```powershell
python -m pip install -r requirements.txt
docker compose --env-file .env.admin up -d db
python -m scripts.init_db
python -m scripts.seed_db
python -m scripts.evaluate --mode mock
python -m pytest
python -m uvicorn app.main:app --host 127.0.0.1 --port 8002
```

真实模型验证使用：

```powershell
python -m scripts.evaluate --mode live
```

Compose 全服务复现使用 `docker compose --env-file .env.admin up --build -d`，并说明数据库初始化/seed 发生在何处。可以在 db 健康后通过一次性命令运行初始化脚本，不能把管理员凭据注入长期运行的 API 服务。`.env.admin` 的 Compose 插值只用于 db；为本地 Docker root 初始化配置受限的开发访问方式并记录原因，宿主端口始终绑定回环地址。

本机 MySQL 路径需要列出修改 `.env`/`.env.admin` 的变量，然后使用相同的 init、seed、evaluate、pytest、uvicorn 入口；不要要求安装 SQLite。对于可选集成测试环境开关，README 必须给出启用方式；最终验收真实 MySQL 时不能把所有集成测试都跳过后宣称通过。

Docker 不可用、镜像无法获取或没有本机 MySQL 时，不冒充数据库验收成功：保留源码、命令和单元测试结果，明确列出缺少的真实环境验证。真实模型同理。日志和报告记录模式、版本、用例计数、退出状态和实际错误，不记录秘密。

## 10. 文档、演示、面试说明与完成标准

README 应包含：项目定位与支持范围、固定数据/口径、三种查询、目录、环境变量表、模型 live/mock 区别、Docker/本机复现、API 请求响应、HTTP 状态、评测方法与实际结果、已知限制。架构图展示模型只产计划、程序选择 SQL、只读账号执行，而不是画成任意 SQL 生成器。

`docs/demo.md` 至少准备四段可复制请求：销售额商品排行、每日销售、排名口径澄清、写操作拒绝；展示实际响应中的 plan、rows、query 和 trace。注明先启动哪种模式、日期区间与期望重点。不使用空壳截图或虚构的响应。

`docs/interview.md` 解释：为什么使用固定模板、为什么仍用 LangGraph、为何追问而不猜、金额与时间边界、参数化与只读权限如何配合、客户端超时与服务器中止的区别、mock 能证明什么、项目还不支持什么。可提供三条基于实际完成情况的简历措辞，名称采用“电商经营分析工作流助手”，不能写成“通用 NL2SQL”。

只有满足下列条件才标记全部完成：

- 三个查询类型均可从自然语言/结构化计划流转到真实 MySQL 并返回正确结果；模拟数据边界和金额已核对。
- LangGraph 有可观测 state/trace，澄清、不支持、有限格式重试和数据库失败分支可演示。
- 运行账号只有指定表 SELECT，写操作核查确实被 MySQL 拒绝；应用不会读取管理员配置。
- SQL 模板固定且所有外部值参数化；日期、排名、行数、并发和模型/数据库超时均生效。
- 12 条语义检查和必要技术测试有实际结果，真实 MySQL 验收与真实模型验收分别标记；没有用 mock 或跳过的检查替代。
- 新目录按 README 可复现，初始化/seed 重复运行安全，不需要破坏性重建；接口与文档一致。
- README、演示、面试说明、验收记录齐全，没有真实秘密、虚构指标或未实现能力。

最终向用户报告：实现了什么、如何运行、测试/评测的实际模式与结果，以及仍未验证的外部依赖。遇到依赖阻塞时交付完整可审查成果并精确说明缺口，不以“代码已写完”替代真实验证。
