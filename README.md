# EnterpriseIQ · Investigation Engine Alpha

当前开发 M1；M0 产品与架构基线见 `docs/`。已提供快照导入、8 个业务工具及 DeepSeek 驱动的 LangGraph 调查 Agent。报告是待独立验证的草稿。

## 本地运行（PowerShell）

需要 Python 3.12+、uv、Docker Desktop。复制 `.env.example` 为 `.env`，替换两个位置的本地数据库密码；`.env` 已被 Git 忽略。数据库端口只绑定本机。

```powershell
uv sync --locked
docker compose up -d --wait
uv run --env-file .env enterpriseiq import-data data/enterprise-bench-data.zip
uv run --env-file .env enterpriseiq data-status
uv run --env-file .env enterpriseiq account-links ACC-013
uv run --env-file .env enterpriseiq tool calculate_arr_exposure
uv run --env-file .env enterpriseiq tool list_open_tickets --arguments '{"account_id":"ACC-013"}'
```

输入包来自 Apache-2.0 的 [devrev/enterprise-bench](https://github.com/devrev/enterprise-bench)，固定提交 `6220f818b0cf678471168d2c626a12fcce24ccf6` 的 [data.zip](https://github.com/devrev/enterprise-bench/raw/6220f818b0cf678471168d2c626a12fcce24ccf6/artifacts/data.zip)。将其保存到已忽略的 `data/enterprise-bench-data.zip`；导入器强制验证 M0 记录的 SHA-256。仅用于合成业务数据演示/评测，不用于训练；不分发原始数据。保留上游许可、归属和 canary 要求，见 `docs/dataset-audit.md`。

导入在单个事务中完成，相同快照重复导入不覆盖数据。源 JSON 与正文保留内容哈希；记录保留来源路径、JSON Pointer、原始字段及规范化记录哈希。所有关系绑定快照与源系统命名空间；组件数组保留多值；共享组件查询只输出候选关联。`transcript.date` 等业务日期保留在原始字段中，导入时间不替代业务时间。

只加载显式列出的业务清单及其引用正文；M0 文档、任务答案、评分标准不进入数据库。当前单一合成数据域，未实现多租户权限体系。数据库 schema 目前仅用于 Alpha 的新建本地实例。

业务工具包括客户搜索、记录读取、开放工单查询、组件工程候选、机会查询、文档检索、正文读取和 ARR 计算。输入由 Pydantic 校验，不接受任意 SQL/SOQL/JQL。查询包含快照、原始来源引用、总匹配数与分页位置；只有单次结果覆盖全部匹配时 `complete=true`，最后一页不代表前面的记录已被读取。调用方需自行累计各页。

金额使用 PostgreSQL numeric 和 Python Decimal，输出十进制字符串。每个直接组件按客户去重，全局再独立去重，不累加区域总额。未提供账户币种时标记 `UNSPECIFIED_BENCHMARK`；ARR 与机会 ACV 不合并；缺少 ARR 时不输出部分总额冒充完整结果。

模型供应方为 DeepSeek；在本地 `.env` 配置 `DEEPSEEK_API_KEY`，默认模型 `deepseek-flash`、Base URL `https://api.deepseek.com`。密钥不得写进命令行或 Git。

## 调查 Agent

```powershell
uv run --env-file .env enterpriseiq investigate 'Vantara 的扩展交易被什么阻塞，是否有承诺 ETA？请查阅原始证据。'
uv run --env-file .env enterpriseiq investigate '开放 p0/p1 工单按直接产品组件的去重 ARR 前三名是什么？'
# 最小恢复验证：首个工具执行完后持久化并退出；用上条输出的 run_id 开启新进程继续。
uv run --env-file .env enterpriseiq investigate '调查 Vantara 的交易阻塞与下一步行动' --pause-after-tool
uv run --env-file .env enterpriseiq resume <run_id>
```

两个问题走同一个 `StateGraph`：`agent → tools → agent` 循环，每次只执行模型实际请求的一个工具；批量工具请求会逐项执行、逐项 checkpoint。条件路由依据待执行工具、回答、错误与预算，不按问题关键词选择预设流程。使用 DeepSeek 原生 function calling；图状态保存工具消息、证据字典、Trace、错误与累计用量，不保存隐藏思维链。汇总结果附展示名称以减少无意义重复读取。

Checkpoint 使用独立的 `investigation_checkpoint` schema、独立 autocommit 连接和 `durability="sync"`；`resume` 从同一 run 的未完成节点继续，已结束的 run 不重新调用模型。初始化锁只保护 schema/migration，不提供后台调度。只读工具在节点中途崩溃时可能重读；不承诺副作用恰好一次或并发恢复安全。

默认每次调查最多 20 次工具调用、20 次模型调用、180 秒；`--max-tool-calls` 和 `--max-seconds` 可在启动时设置。恢复保留已有预算与绝对截止时间，人工暂停时间也计入；此版本不支持长暂停延期。预算耗尽输出 partial/failed 与已有证据，不标记为完整调查。API 失败暂不自动重试；有限错误修正由模型通过工具结果自行决定。

本地 `outputs/<run_id>.json` 保存完整工具消息与 Trace；终端输出摘要。LangSmith 外部 tracing 显式禁用。运行状态 completed 仅表示图执行结束，`report_validation=pending_delivery_4` 明确表示结论尚未独立校验。引用 ID 存在不等于主张被证据支持，歧义问题仍可能导致错误口径，属于后续报告校验范围。

## 验证

```powershell
uv run pytest tests/test_dataset.py
# 在 .env 配置独立的 TEST_DATABASE_URL，指向预先创建的测试数据库后：
uv run --env-file .env pytest
```

业务集成测试逐项回滚，不修改开发数据库；未配置独立测试库或未提供数据包时会明确跳过。测试覆盖数据与业务工具，以及 Agent 预算路由、错误状态、PostgreSQL 重建连接恢复和已完成 run 的重复 resume。新增初始化竞争回归测试仅在专用 `enterpriseiq_test` 库重建 checkpoint schema，不作用于开发库。自动测试不调用付费模型；真实 DeepSeek 验证记录见 `docs/PROJECT_STATUS.md`。
