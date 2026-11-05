# EnterpriseIQ 项目进度

更新：2026-11-05（Asia/Shanghai）。

- M0：已完成，五份既有文档保留为参考基线。
- 当前：M1 — Investigation Engine Alpha；交付项 1、2 已确认；交付项 3 已实现并完成真实模型与最小恢复验证，报告独立校验待交付项 4。
- 分支：`codex/m1-investigation-engine-alpha`；基线提交：`edaee68`。
- 初始仓库：仅 `.gitignore` 和五份 M0 文档；无应用代码、依赖配置或项目内数据包；初始工作区干净。
- 交付项 1、2 经用户明确授权已创建阶段性 Commit `91cbc8a`（feat: import audited snapshot and add typed business tools），未合并 main；后续实现尚未提交。

## M1 交付项

| 交付项 | 状态 | 验收重点 |
|---|---|---|
| 版本化快照导入与跨系统关联 | 已完成 | 已通过全量导入、来源解析、关键外键、Vantara 链、幂等与事务回滚验证 |
| 受约束业务工具与精确计算 | 已完成 | 结构化关联与 ARR 均符合独立原始数据基线；分页、金额口径、输入约束和来源引用已验证；不代表 Agent 任务已通过 |
| 可运行 LangGraph Investigation Agent | 已完成 | 已有两个问题的真实 DeepSeek tool calling Trace；StateGraph 条件路由、预算与 PostgreSQL 最小恢复已验证；报告仍是未独立校验草稿 |
| 证据报告、基本验证与错误处理 | 待实施 | 重要引用可解析，数值可复核，未知 ETA/冲突不被编造；故障与预算耗尽有明确结果 |
| Alpha 端到端验收与交付 | 待实施 | 三个既定样本各重复运行 3 次，报告成功/失败及调用量、用量、成本可得性与延迟；提供复现入口 |

## 交付项 1 实证（2026-11-05）

- 固定归档哈希与 M0 一致，已复制到项目忽略目录 `data/`，不依赖 M0 临时目录。
- PostgreSQL 17.6 实际导入 50,411 条实体、78 个源文件、100,086 条关系和 66 份正文关联；12 类实体数量均与审计一致。
- 实测复现 `ACC-013 → TKT-119 → PART-010 ← ISS-017`，关系标为 `shared_component`；`OPP-006` 的 description/next_step 对 ISS-017 的引用单列为 `text_reference`；TRN-001 指向同一客户和机会。
- `uv run --env-file .env pytest -q`：12 passed（14.16s），含实际 PostgreSQL 集成测试，无跳过；逐条解析全量记录来源、核对所有源文件哈希，验证多组件、悬空键、层级环、缺正文、幂等与数据库 FK 失败整笔回滚。
- CLI 实际导入成功，重复导入返回 `imported: false`；`account-links ACC-013` 输出上述链。`.env` 和原始包经 `git check-ignore` 确认忽略；M0 文档未改。
- 技术问题已处理：Docker 引擎初始未启动；批量导入后主动 ANALYZE，避免新表缺少统计导致关联查询慢；测试使用独立库与外层回滚事务。
- 本地配置解析警告曾意外回显开发库密码，已修正格式并轮换密码；未提交秘密。

## 交付项 2 实证（2026-11-05）

- 新增 8 个 Pydantic 约束的只读业务工具，禁止任意查询语言；快照由执行上下文固定。返回来源引用、分页范围、完整性和结构化错误。
- 对照原始 JSON 独立算法验证开放 P1 工单集合、组件工程候选集合、各区域和全局 ARR；31 个开放 P1 工单、12 个区域，PART-010 为 732,000，全局 8 个客户去重 ARR 为 1,041,000。
- 金额使用 numeric/Decimal，保留未知账户币种口径；ACV 与 ARR 分开。小数金额精度及缺少 ARR 时不提供伪完整总额已测试。
- 业务工具实际查询 Vantara 得到 TKT-119、ISS-017、OPP-006、TRN-001；OPP-006 ACV 为 USD 280,000。文档返回可定位的原始行范围。
- 全套测试 `uv run --env-file .env pytest -q`：18 passed（29.19s），无跳过。`uv build` 已成功生成 sdist/wheel；CLI、忽略规则和差异格式检查通过。
- 这些验证属于确定性工具层，不声称完成了 eng-l1-a / eng-l2-a 的 Agent 调查或官方 MCP 榜单评测。

## 模型接入状态（2026-11-05）

- 用户选择 DeepSeek，Flash 或 Pro 均可；默认采用官方 API 的 `deepseek-flash`，Base URL 为 `https://api.deepseek.com`，不使用 OpenAI 凭据。
- 用户已在忽略的 `.env` 配置 DEEPSEEK_API_KEY；经用户授权的最小真实请求成功，返回 `OK`，prompt 9 / completion 1 / total 10 token，未发送业务数据。
- API 已用于真实动态工具调用和新进程 checkpoint 恢复；报告质量仍待独立验证。价格未硬编码，账单成本尚未测得。

## 交付项 3 实证（2026-11-05）

- 使用锁定版本 LangGraph 1.2.20、langgraph-checkpoint-postgres 3.2.0、httpx 0.28.1。统一 StateGraph 不按问题关键词路由：模型产生原生工具请求，工具执行器逐项执行，再由模型选择后续查询或完成。
- State 包含消息、待执行工具、按 ID 合并的证据/Trace、错误、累计调用/token 数、版本与截止时间；模型不接触凭据或 checkpoint 连接。DeepSeek 非 thinking 模式，不保存隐藏思维链；外部 LangSmith tracing 显式禁用。
- PostgreSQL Checkpoint 使用独立 schema 和连接，sync 持久化；CLI 支持 investigate/resume/单工具后暂停。只有初始化建表/迁移串行化，无作业队列、后台调度或完整人工审核。
- 两个实际运行共用同一图和同一工具集：
  - 组件 ARR（`f2edf7ec-2a2f-4e8e-a497-3024c7ad4707`）：2 次模型调用、1 次工具调用 `calculate_arr_exposure`；前三组件 PART-010 / PART-007 / PART-003，ARR 732,000 / 417,000 / 395,000，全局 1,041,000。prompt/completion token 为 13,683 / 1,113，模型请求累计 7.525 秒。
  - Vantara 阻塞（`594eae4b-76ed-41a8-8e5d-c0436573da70`）：7 次模型调用、15 次工具调用。客户搜索 → 机会/开放工单/通话检索 → 读取 TRN-001 与 ISS-017 → 组件候选及补充记录 → 沿 Plan Versioning 文本线索读取 ART-055、ISS-032 → ARR 汇总。prompt/completion token 为 89,232 / 3,006，模型请求累计 27.975 秒。
- Vantara 在第 1 次工具完成后退出进程；新 CLI 进程仅给 run_id 即继续到完成。历史 checkpoint 确认当时 model_calls=1、tool_calls=1、next=agent，最终 7/15；客户搜索只执行一次，证据与计数保留。累计模型请求耗时不含人为暂停或数据库时间。
- 两个草稿所引用的证据 ID 均存在于实际工具返回中；实际执行 call_id 均能匹配 DeepSeek 返回的请求。完整本地消息和 Trace 位于忽略目录 `outputs/<run_id>.json`，摘要校验位于 `outputs/m1-agent-verification.json`。
- 保留失败/限制样本：首轮 ARR 请求因逐个补读名称耗尽 20 次工具预算而返回 partial（`56ddbb01-5887-4cd7-93cc-862e889b5f24`）；工具已补齐展示名称、模型能看到剩余预算，未提高默认上限。
- 歧义样本（`d41a4494-a40c-40d0-aafa-d2de163e6bac`）把“区域”解释为地理区域，并自行计算额外金额。即使工具计算正确、图状态 completed、引用 ID 存在，也不代表报告合格。此样本明确不作为业务答案通过证据，留待交付项 4 的口径与主张校验。
- 已修复首次并发启动的 checkpoint DDL 竞争（初始化短轮询锁，避免阻塞事务与并发建索引死锁），并在全新独立测试 schema 并发启动两次复验；修复 Windows GBK 终端输出 Unicode 时的异常，JSON 文件始终使用 UTF-8。
- 新增 5 项 Agent 测试覆盖预算路由、模型失败、时间预算、重建连接恢复/重复 resume 和首次并发初始化；沿用已有 18 项业务测试，不重复建立相同业务逻辑测试。最终 `uv run --env-file .env pytest -q`：23 passed（31.95s），无跳过；`uv build` 已通过。
- `completed` 仅指图执行结束，所有报告显式标记 `report_validation=pending_delivery_4`；不宣称交付项 4、5 或 M1 全部完成。

## 实施边界与验收建议

- 沿用 Python 3.12+、LangGraph、Pydantic、PostgreSQL 候选方向，优先 CLI 调查闭环；模型通过可替换适配层接入，具体模型及数据外发边界在真实调用前落实。
- 首批样本：eng-l1-a、eng-l2-a、sales-l2-b（Vantara）。只验证合成快照接入，不宣称已接通真实企业系统或复现官方 MCP 榜单。
- 建议硬门槛：结构化实体/金额全部符合独立基线，重要引用全部可解析且人工抽查支持结论，无虚构 ETA；三场景各 3 次均满足对应核心要求，失败如实记录并修复或明确未通过。
- 初始单次预算建议：最多 20 次工具调用、180 秒总时限（含有限重试）；超限返回 partial 与缺口，不冒充 completed。实施时验证限额生效。
- 保留最小持久化与重启继续验证；暂不实现完整前端、多租户权限体系、复杂调度或完整人工审核系统。原 M0 路线图中相关广泛退出条件不作为本次 Alpha 全量承诺。
- 用户补充约束：PostgreSQL checkpoint 只做必要持久化和最小恢复；Agent 必须由不同任务的实际 Trace 证明动态工具选择与调查路径，不以固定 Workflow 冒充。
- 每项完成后补充实际运行/测试证据；里程碑最终验收前不提交变更、不进入 M2。

## 待解决

- 报告尚无独立的主张支持、金额口径和调查覆盖校验；已观察到歧义导致额外地理聚合，及 Vantara 的宽泛组件候选检索未取全，需在交付项 4 显示缺口并防止误报完整。
- 模型可能检索过多无关记录、累计输入用量偏大，暂不以当前少量运行宣称稳定成功率或成本达标；三场景各 3 次验收仍待交付项 5。
- 默认绝对 180 秒预算包含暂停时间；中途崩溃可能重读未完成的只读工具。未实现并发恢复控制、长暂停延期、跨图版本迁移和生产级容错，这些不是本次最小恢复承诺。
- API 暂未自动重试；工具错误、来源冲突和预算不足的报告行为仍需交付项 4 验证。仅发送所需合成业务记录，未外发 trace。
