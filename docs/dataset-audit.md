# Enterprise-Bench 数据与工具审计（M0）

调研日期：2026-11-05。范围：官方仓库、全部 14 个任务问题、10 份任务评分标准、三个压缩包的静态检查及必要的只读数据计算。未运行 Harbor、Docker、MCP 服务或 LLM judge。

## 来源、版本与下载规模

官方仓库：[devrev/enterprise-bench](https://github.com/devrev/enterprise-bench)，默认分支 main；本次固定提交 **6220f818b0cf678471168d2c626a12fcce24ccf6**，项目包声明版本 1.0.0、Python >=3.12。目录树 API 返回 `truncated=false`。来源：[pyproject](https://github.com/devrev/enterprise-bench/blob/6220f818b0cf678471168d2c626a12fcce24ccf6/pyproject.toml)、[固定版本目录](https://github.com/devrev/enterprise-bench/tree/6220f818b0cf678471168d2c626a12fcce24ccf6)。

| 官方压缩包 | 下载字节 | 解压内容字节 | 本次用途 |
|---|---:|---:|---|
| [data.zip](https://github.com/devrev/enterprise-bench/blob/6220f818b0cf678471168d2c626a12fcce24ccf6/artifacts/data.zip) | 3,084,199 | 33,417,412 | 内存读取 JSON 与代表性文本，核验关系和计算口径 |
| [mcp-servers.zip](https://github.com/devrev/enterprise-bench/blob/6220f818b0cf678471168d2c626a12fcce24ccf6/artifacts/mcp-servers.zip) | 31,995 | 104,120 | 静态读取 REST/MCP 工具与查询解析源码 |
| [base-image.zip](https://github.com/devrev/enterprise-bench/blob/6220f818b0cf678471168d2c626a12fcce24ccf6/artifacts/base-image.zip) | 12,022 | 30,067 | 静态读取评分辅助代码；不是 Docker 镜像下载 |

下载前已说明来源、大小和必要性；未克隆仓库或下载镜像。三包 SHA-256 均与 [dataset.toml](https://github.com/devrev/enterprise-bench/blob/6220f818b0cf678471168d2c626a12fcce24ccf6/dataset.toml) 一致：

```text
data.zip        24d6d134067ffc763c953fab8ec28022c98bb4da11aa0e4456798d7f9bb656bc
mcp-servers.zip 828662a879b7d2702dbde80ae83570b5a7cb59c48c3577a04a26c44bb9deff27
base-image.zip  22f4c985c3efc8b780dcc8665c70f4f9ecee4ef05b7f52f58d844057a3dacae4
```

本机原始包、选取的上游源码和计算输出暂存在 `C:\Users\Administrator\AppData\Local\Temp\EnterpriseIQ-M0-6220f818`；工作区仅交付本文等五份文档。临时目录不是项目运行依赖。

## 仓库结构与许可证

| 路径 | 内容 |
|---|---|
| `artifacts/` | 数据、服务源码和评分运行辅助包 |
| `tasks/<task>/` | `instruction.md`、`task.toml`、环境 Dockerfile、`tests/criteria.yaml`、`trajectory.json`、`test.sh` |
| `docs/` | Schema 导览、任务编写、结果提交说明；Schema 导览不是完整字段约束 |
| `mcp.json`、`Makefile` | 服务入口和基准运行流程 |
| `leaderboard/`、`scripts/` | 结果记录、仓库/榜单校验 |

仓库 [LICENSE](https://github.com/devrev/enterprise-bench/blob/6220f818b0cf678471168d2c626a12fcce24ccf6/LICENSE) 为 Apache-2.0；保留上游许可和来源。数据明确为 Maple Payments 合成环境；官方 [CANARY](https://github.com/devrev/enterprise-bench/blob/6220f818b0cf678471168d2c626a12fcce24ccf6/CANARY.md) 要求避免进入训练语料。建议用于演示/评测，不做训练；发布衍生数据时保留 canary，检查所分发内容的许可与归属。

## 实际数据和关键 Schema

下表均来自解码后的原始 JSON 清单，不是 README 推算。对象存在可选字段；以下只列调查相关字段。

| 文件（data.zip 内） | 实测条数 | 主键及调查字段 |
|---|---:|---|
| `crm_json_data/accounts.json` | 42 | `account_id`；`account_name, arr, owner_id, tier`；合同/续约/`sla_tier` 仅各 6 条具备 |
| `crm_json_data/tickets.json` | 32,768 | `ticket_id`；`account_id, priority, status, components[], created_at`；`conversation_id` 仅 8 条具备 |
| `crm_json_data/opportunities.json` | 8,704 | `opportunity_id`；`account_id, stage, acv, currency, owner_id, target_close_date`；`probability` 31 条、`next_step` 27 条具备 |
| `crm_json_data/users.json` | 289 | `user_id`；姓名、角色、可选 `manager_id`；62 条具备 `account_id` |
| `pm_json_data/issues.json` | 8,448 | `issue_id`；`title, priority, status, components[], assignee_id, created_at, updated_at`；15 条具备 `conversation_id` |
| `pm_json_data/maple_parts.json` | 40 | `part_id`；`title, type, parent_id` 产品层级 |
| `pm_json_data/users.json` | 5 | `user_id`；工程人员，ID 均不在 CRM users 中 |
| `pm_json_data/conversations.json` / `comments.json` | 23 / 26 | 会话 `parent_type,parent_id`；评论 `conversation_id,author_id,body,visibility,created_at` |
| `maple_kb/articles.json` | 55 | `article_id,status,audience,content_file`，正文 55 个 Markdown 文件 |
| `transcripts/transcripts.json` | 3 | `transcript_id,account_id,opportunity_id,date,organizer_id,participants[],content_file` |
| `internal_docs/msa_and_compliance.json` | 8 | `document_id,status,audience,modified_at,content_file`；其中 1 份 draft |

状态/优先级实测：工单 `open=51, solved=32,717`，开放工单中 `p1=31,p2=13,p3=7`，全量无 p0；工程 `in_progress=3,362,done=5,086`，其中开放 `high/highest=20`；机会未关闭 30 条。工程 `highest` 是题目中的 P0/Critical 对应概念，不能把工单 p1 与工程 high 的原始枚举混用。

README 的知识文章数为 22，包内实测为 55；采用实际包并记录差异。原始记录中没有独立 Contact 或 Sprint 数据文件，不能从 README 的对象概述推断这些实体完整可用。来源：[README](https://github.com/devrev/enterprise-bench/blob/6220f818b0cf678471168d2c626a12fcce24ccf6/README.md)、上述 data.zip。

## 跨域关系及检查结果

```text
Account.account_id ← Ticket.account_id
Account.account_id ← Opportunity.account_id
Ticket.components[] → Part.part_id ← Issue.components[]
Part.parent_id → Part.part_id
Transcript.account_id / opportunity_id → Account / Opportunity
Transcript.content_file → 通话正文
Comment.conversation_id → Conversation → parent_type + parent_id
Account.owner_id / Opportunity.owner_id → CRM User
Issue.assignee_id → PM User（不可只查 CRM User）
```

**已验证：** Account、Ticket、Opportunity、Issue、Part 各自没有重复主键；全量 Ticket/Opportunity 的客户外键无悬空；Ticket/Issue 的组件均存在，且每条恰好一个组件；Part 父键无悬空、无环；所有非空会话引用和评论会话引用存在。3 条 Transcript 的客户/机会外键存在且客户一致。以上是指定关系的检查，不代表全字段或全部业务约束正确。

**关系语义：** 共享组件是潜在影响路径，不是 Ticket→Issue 的直接外键，也不证明根因。正文明确提及 issue ID 时应保存为文本证据关系，和组件候选关系分开。Opportunity 没有结构化组件键，经 Account 的开放工单建立潜在影响路径。

**代表性链已查到：** Vantara `ACC-013` → 开放 P1 `TKT-119` → `PART-010` ← 开放 high `ISS-017`；机会 `OPP-006` 通过客户关联，description/next_step 明确提及 ISS-017；`TRN-001` 清单同时指向 ACC-013/OPP-006。机会 ACV 为 USD 280,000，客户 ARR 为 432,000，机会概率为 85%；正文包含工程书面更新承诺及董事会条件，issue 状态说明无承诺 ETA。不能将金额相加或把概率当成健康结论。

## 官方工具接口与真实边界

以下是 mcp-servers.zip 中 `*/mcp_server.py` 和 `*/server.py` 的静态源码结论；网络运行尚未验证。[mcp.json](https://github.com/devrev/enterprise-bench/blob/6220f818b0cf678471168d2c626a12fcce24ccf6/mcp.json) 指向 PM/CRM/file 的 Streamable HTTP MCP 端口 8011/8012/8013。

| 服务 | MCP 工具 | 重要约束 |
|---|---|---|
| CRM | `crm_query(soql)`、`crm_get_record(object_type,record_id,fields)`、`crm_describe`、`crm_list_objects`、`crm_search(query,object_type,max_results)` | 对象仅 Account/Opportunity/Case/User；SOQL 子集，非完整 Salesforce；跨域 join/去重在调用方完成 |
| PM | `pm_search_issues(jql,start_at,max_results,expand,query)`、`pm_get_issue`、`pm_get_comments`、`pm_get_user`、`pm_list_components` | 查询页默认 20、最大 100，返回 total/startAt；支持部分 JQL，不能假定 OR/历史查询可用 |
| Wiki（PM 内） | `wiki_search(query,max_results)`、`wiki_get_page(page_id)`、`wiki_list_pages(title,max_results)` | KB 由 Markdown 加载，page_id 为文件名哈希派生数值，不是 ART-ID；列表截断无游标参数 |
| 文件 | `search_files(query,page_size)`、`get_file_metadata`、`read_file_content`、`list_recent_files` | 仅内文档和通话正文；标题/全文等子集搜索，非完整 Drive；搜索截断标记不能继续翻页 |

关键字段映射：CRM `Case.Id→ticket_id, AccountId→account_id, Components__c→components, Priority→priority`；`Account.AnnualRecurringRevenue__c→arr, Tier__c→tier`；`Opportunity.Amount→acv`。PM 描述转换为文档结构，组件为 `{id,name}`。原始 JSON 字段与协议字段需由 adapter 明确转换。

已识别的接口陷阱：

- CRM `LIMIT/OFFSET` 切片后仍 `done=true`，但 `totalSize` 是切片前总数。不能只按 done 判断取全；`crm_search.total_matches` 也是截断后的数量。
- 文件 `search_files` 截断后给 `nextPageToken="truncated_at_page_size"`，却没有 token 入参。必须缩小查询或显式报告覆盖不足。
- 隔离运行上游 `_parse_jql` 函数验证：`status WAS done` 被解析为空条件；`status = in_progress AND sprint IS EMPTY` 只保留 status。正常的 status + priority IN 条件可解析。未加载服务，也未安装依赖。adapter 应拒绝不支持的表达式，避免静默扩大查询。
- 文件加载器未关联通话清单元数据，且跳过 `transcripts.json`；文件时间来自文件系统。PM wiki 时间同样来自文件系统。不能据此判断真实业务活动时间。
- PM 评论入口接收 issue key，不能据原始包中有 ticket 会话就认定 MCP 可读取全部支持对话。CRM User 映射没有 `account_id`，原始用户关联也不是全都能经协议查询。
- 本包暴露的是只读工具。REST 有共享 bearer 检查；MCP 源码未配置同等鉴权、身份授权或逐条 ACL。数据中的 `internal/public` 标签不是权限执行证明。

## 必要计算的结果

| 验证 | 方法 | 结果与边界 |
|---|---|---|
| 产品区域 ARR | 开放 p0/p1 工单按直接 components 分组；每组 account 去重后求 arr，不向父组件汇总 | 31 个 P1、12 个区域；PART-010 最高 732,000 = Vantara 432,000 + TechFlow 180,000 + GlobalCommerce 120,000；与 eng-l2-a 的核心标准一致 |
| 工程影响候选 | 开放 high/highest issue 的组件与开放工单组件交集 | 14 个区域；8 个开放工单客户的全局去重 ARR 为 1,041,000；不能将逐区域或逐 issue 金额再相加 |
| SLA 题目复算 | 参考 `2026-04-13T00:00:00Z`；Enterprise 0.5h、Growth 2h；排除 Starter 和参考日期后的工单 | 31 个 P1 → 排除 Starter 2、未来创建 2 → 27 条票龄代理超时（Enterprise 16、Growth 11） |
| 时间完整性 | 对比 Transcript.date 与对应 Opportunity.created_at | 三份通话日期均早于对应机会创建；可作为后补建档，不能单独视为错误，也不能当成完整历史快照 |

ARR 计算对照：[eng-l2-a 标准](https://github.com/devrev/enterprise-bench/blob/6220f818b0cf678471168d2c626a12fcce24ccf6/tasks/eng-l2-a/tests/criteria.yaml)。SLA 对照：[support-l2-a 标准](https://github.com/devrev/enterprise-bench/blob/6220f818b0cf678471168d2c626a12fcce24ccf6/tasks/support-l2-a/tests/criteria.yaml)；阈值也已读取 Enterprise/Growth MSA 正文核对。

SLA 复算仅复现题目口径：全量工单没有 `first_response_at`；Growth 文档还规定工作时间/时区，票龄减固定小时没有实现真实服务日历。tier 字段与另一个稀疏的 sla_tier 字段不可混用。数据还同时有草案、标准模板和通话口头说法，不能自动视为客户已签署条款。

## 14 个任务对 EnterpriseIQ 的映射

每项均阅读了 `instruction.md` 的业务问题；10 项另读 criteria：eng-l1-a、eng-l2-a/b、sales-l2-a/b/c/d、support-l1-b/c、support-l2-a。未逐项复跑评分。

| 官方任务 | 产品用途 | 采用建议 |
|---|---|---|
| eng-l1-a | P1 工单→组件→工程→客户/ARR | 首选，确定性关联基线 |
| eng-l1-b | 客户工单量/开放量 | Risk Center 基础视图，不必调用 Agent |
| eng-l1-c | 高优先级工程事项的客户影响矩阵 | 首选，候选影响关系 |
| eng-l2-a | 直接产品区域的 ARR 敞口 | 首选，去重与层级语义基线 |
| eng-l2-b | 工程优先级与收入影响排序 | 首选；先排除普通技术债，再明确金额口径 |
| sales-l1-a | 指定客户负责人组合的工单量 | 作为客户范围过滤基线 |
| sales-l2-a | 客户组合健康综述 | 第二批；需三份通话独有信号 |
| sales-l2-b | Vantara 交易阻塞与推进条件 | 首选 Agent 综合案例，不能虚构 ETA |
| sales-l2-c | 未完成客户承诺 | 第二批；未见完成证据不等于确定未完成 |
| sales-l2-d | 销售组织周报 | 暂缓；验证组织层级/时间过滤，不把稀疏记录当完整活动 |
| support-l1-a | 退款主题客户筛查 | 简单风险信号；关键词只是筛选条件 |
| support-l1-b | P0 工程事项对机会 ACV 的潜在影响 | 后续；工程 highest、工单 p0 两套概念需分开 |
| support-l1-c | 多工单客户的票龄中位数 | 可做确定性信号；必须带参考日期 |
| support-l2-a | 按客户层级计算 SLA 票龄代理 | 受限演示，禁止标成真实首响违约 |

## 官方评测可复用与不可推断之处

base-image.zip 的 `judge/judge_helper.py` 与 `test_helpers.py` 表明：将参考问答、实际最终回答、criteria 交给 LLM judge；required 全过得到二元通过，weighted 仅诊断。当前源码 judge 模型字符串为 `openai/gpt-5`，本次未调用。评分主要检查语义结果；代码没有对每次工具轨迹、ACL 或证据路径进行独立执行核验。回答声称走了正确路径，不等于实际轨迹已被证明。

因此复用任务与业务标准，额外设计结构化数值校验、引用可解析性、真实工具调用日志和人工复核。评测器答案/criteria/本审计中的标准结果不能进入被测 Agent 的可检索知识库；评测代码保留可见范围与官方任务兼容性说明。合成静态数据和 14 道公开题不足以证明真实企业可靠性。

## 复查方法与尚未完成的验证

复查固定提交的三个包及 SHA-256，以 Python 标准库 `zipfile/json/collections/datetime/ast` 读取，不修改原数据。核心计算口径：`status=open`；工单优先级 `p0/p1`；按 components 直接标签分组；每组以 account_id 去重；SLA 使用上述固定 UTC 午夜。数据结果暂存 `audit-results.json`，本文件记录了必要统计与方法，复查不依赖临时结果。

未做：服务端到端调用、Harbor 完整复跑、模型调查、LangGraph 断电恢复、导入数据库后的查询一致性、真实多租户验证。上述内容作为后续最小验证，见 [Roadmap](roadmap.md)。
