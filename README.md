# EnterpriseIQ · Investigation Engine Alpha

当前开发 M1；M0 产品与架构基线见 `docs/`。目前提供固定快照导入、跨系统关联检查及 8 个受约束业务工具，尚无 Agent 调查运行结果。

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

模型供应方已确定为 DeepSeek；在本地 `.env` 配置 `DEEPSEEK_API_KEY`，默认模型 `deepseek-flash`、Base URL `https://api.deepseek.com`。密钥不得写进命令行或 Git。当前仅完成最小 API 连通验证；动态调查路径和 checkpoint 恢复将在后续交付中验证。

## 验证

```powershell
uv run pytest tests/test_dataset.py
# 在 .env 配置独立的 TEST_DATABASE_URL，指向预先创建的测试数据库后：
uv run --env-file .env pytest
```

集成测试逐项回滚，不修改开发数据库；未配置独立测试库或未提供数据包时会明确跳过。测试覆盖数据导入与关联、独立原始数据计算基线、分页、金额精度、缺失 ARR、非法输入、查询注入、文档行引用、幂等和事务回滚。
