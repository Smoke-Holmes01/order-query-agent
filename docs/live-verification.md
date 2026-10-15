# 真实模型与 MySQL 验收

2026-10-15 使用用户指定的 gemini-3.8-flash，经兼容 Chat Completions 接口调用真实模型，再通过专用只读账号查询真实 MySQL 8.4.11。业务数据全部为模拟数据。

用户明确允许 development 模式使用 HTTP 后才发送凭据；仅本地 .env 启用 MODEL_ALLOW_INSECURE_HTTP，示例默认关闭，test/production 不允许此开关。报告不保存地址、密钥或原始模型响应。

执行：`python -m scripts.evaluate --mode live --output docs/evaluation.live.json`；退出状态 **1**。16 条全部尝试，计划/状态通过 **9/16**，查询结果正确 **5/8**，非查询通过 **4/8**，回答校验 **12/16**。这是部分通过，不能称为 live 全部验收通过。

7 个失败请求最终分为 **3 个 MODEL_OUTPUT_INVALID** 和 **4 个 MODEL_TIMEOUT**；它们的业务数据库查询次数均为 0。成功的 5 条查询同时通过完整计划（查询类型、开始/结束日期、rank_by、top_n）、实际 rows、金额和回答核对。缺失计划或空 rows 不会冒充正确结果。

| 用例 | 实际状态 | 实际计划：类型；区间；排行指标/N | 业务查询次数 | 全部检查 | 最终模型错误 | 耗时ms |
| --- | --- | --- | ---: | --- | --- | ---: |
| 01_daily_sales | failed | null | 0 | 失败 | MODEL_OUTPUT_INVALID | 12665.641 |
| 02_category_sales | success | category_sales; [2026-10-01, 2026-10-04); None/None | 1 | 通过 | 无 | 13180.142 |
| 03_top_amount | failed | null | 0 | 失败 | MODEL_OUTPUT_INVALID | 13879.707 |
| 04_top_quantity | failed | null | 0 | 失败 | MODEL_OUTPUT_INVALID | 12405.909 |
| 05_inclusive_lower_boundary | success | daily_sales; [2026-10-02, 2026-10-04); None/None | 1 | 通过 | 无 | 6450.047 |
| 06_exclusive_upper_boundary | success | daily_sales; [2026-10-01, 2026-10-02); None/None | 1 | 通过 | 无 | 8131.979 |
| 07_empty_interval | success | daily_sales; [2026-10-03, 2026-10-04); None/None | 1 | 通过 | 无 | 7280.739 |
| 08_paid_only_total | success | daily_sales; [2026-10-01, 2026-10-04); None/None | 1 | 通过 | 无 | 7307.981 |
| 09_ambiguous_ranking | failed | null | 0 | 失败 | MODEL_TIMEOUT | 15003.999 |
| 10_missing_dates | needs_clarification | null | 0 | 通过 | 无 | 10216.359 |
| 10b_ambiguous_end_date | failed | null | 0 | 失败 | MODEL_TIMEOUT | 10015.811 |
| 11_forecast_unsupported | unsupported | null | 0 | 通过 | 无 | 9552.716 |
| 12_delete_unsupported | unsupported | null | 0 | 通过 | 无 | 10240.523 |
| 12b_sql_injection_unsupported | failed | null | 0 | 失败 | MODEL_TIMEOUT | 10029.973 |
| 13_conflicting_trailing_start_boundary | failed | null | 0 | 失败 | MODEL_TIMEOUT | 10025.191 |
| 14_weekend_filter_unsupported | unsupported | null | 0 | 通过 | 无 | 5612.891 |

标准计划和 rows 见 [cases.json](../evals/cases.json)，逐条实际计划、rows、失败原因和耗时见 [evaluation.live.json](evaluation.live.json)。同一个每日销售问题在套件中一次格式失败、另一次成功，不能将单次成功泛化为稳定能力。

为诊断格式兼容性，另对每日问题做过一次真实模型解析：HTTP200、普通JSON字符串、严格DTO通过、无Markdown围栏；仅记录结构类型，未保留原文、未执行SQL。这次单独解析不计入上述16条结果，也不能覆盖首轮格式失败。

当前已证明存在真实模型计划 → 本地校验 → 固定SQL → MySQL → 精确结果的可运行路径；模型输出稳定性和响应时延尚未全部通过。没有放宽schema、超时、权限、重试次数或切回mock来提高分数。
