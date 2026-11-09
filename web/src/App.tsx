import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  actionRequest,
  api,
  ApiError,
  pendingBody,
  pendingActions,
} from "./actions";

type User = { user_id: string; workspace: string; roles: string[] };
type Job = {
  job_id: string;
  case_id: string;
  run_id: string;
  status: string;
  error_code: string | null;
  updated_at: string;
};
type Ref = {
  evidence_id: string;
  source_id: string;
  entity_type: string;
  source_path: string;
  snapshot_id: string;
  json_pointer?: string;
  start_line?: number;
  end_line?: number;
  file_sha256?: string;
};
type Trace = {
  type: string;
  number: number;
  name?: string;
  status?: string;
  error?: string;
  action_summary?: string;
};
type Run = {
  run_id: string;
  question: string;
  execution_status: string;
  initiator: string | null;
  evidence: Record<string, Ref>;
  trace: Record<string, Trace>;
  gaps: unknown[];
};
type Claim = Record<string, unknown> & { kind: string };
type Draft = { scope: string; claims: Claim[]; open_questions: string[] };
type Report = {
  report_id: string;
  run_id: string;
  version: number;
  payload: {
    report_validation: string;
    draft: string;
    report: {
      formal_report: string | null;
      gaps?: unknown[];
      rejected_claims?: unknown[];
      open_questions_unverified?: string[];
    };
  };
};
type Review = {
  request_id: string;
  report_id: string;
  action: string;
  actor: string;
  reason: string;
  opinion_unverified: string;
  at: string;
};
type Case = {
  case_id: string;
  business_status: string;
  runs: Run[];
  reports: Report[];
  reviews: Review[];
  jobs: Job[];
  progress?: {
    model_calls: number;
    tool_calls: number;
    prompt_tokens: number;
    completion_tokens: number;
    usage_incomplete: boolean;
  };
};
type Summary = {
  case_id: string;
  question: string;
  execution_status: string;
  jobs: Job[];
};
type Evidence = { reference: Ref; content: unknown };

const labels: Record<string, string> = {
  queued: "排队中",
  running: "调查中",
  registered: "已登记",
  awaiting_input: "待澄清",
  paused: "已暂停",
  interrupted: "执行中断",
  completed: "调查完成",
  partial: "结果不完整",
  failed: "执行失败",
  awaiting_review: "待审核",
  blocked: "校验未通过",
  accept: "已接受",
  reject: "已驳回",
  modify: "已修改",
  investigating: "调查中",
  passed: "校验通过",
};
const roleNames: Record<string, string> = {
  viewer: "只读",
  investigator: "调查员",
  reviewer: "审核员",
};
const statusOf = (item: Summary) =>
  item.jobs.at(-1)?.status ?? item.execution_status;
const message = (error: unknown) =>
  error instanceof Error ? error.message : "操作未完成，请重试。";
const pretty = (value: unknown) =>
  typeof value === "string" ? value : JSON.stringify(value, null, 2);

function Badge({ status }: { status: string }) {
  return (
    <span className={"badge badge-" + status}>
      <i />
      {labels[status] ?? status}
    </span>
  );
}
function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const [cases, setCases] = useState<Summary[]>([]);
  const [selected, setSelected] = useState(location.hash.slice(1));
  const [detail, setDetail] = useState<Case | null>(null);
  const [question, setQuestion] = useState("");
  const [followup, setFollowup] = useState(false);
  const [clarification, setClarification] = useState("");
  const [tab, setTab] = useState("report");
  const [version, setVersion] = useState("");
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [evidenceError, setEvidenceError] = useState("");
  const [reason, setReason] = useState("");
  const [opinion, setOpinion] = useState("");
  const [editing, setEditing] = useState<Draft | null>(null);
  const [notice, setNotice] = useState("");
  const [refreshId, setRefreshId] = useState(0);
  const actionKey = user ? `${user.workspace}/${user.user_id}` : "";

  useEffect(() => {
    api<User>("/api/me")
      .then(setUser)
      .catch((e) => {
        if (!(e instanceof ApiError && e.status === 401)) setError(message(e));
      })
      .finally(() => setChecking(false));
  }, []);
  useEffect(() => {
    const onHash = () => setSelected(location.hash.slice(1));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    if (!user) return;
    const pending = pendingBody(actionKey + "/start");
    if (pending) {
      setQuestion(String(pending.question));
      setFollowup(Boolean(pending.case_id));
      setNotice(
        "上一次提交尚未收到确认。请保留原内容重试，系统会使用同一请求编号。",
      );
    }
  }, [user, actionKey]);
  useEffect(() => {
    setDetail(null);
    setVersion("");
    setEditing(null);
    setEvidence(null);
    setEvidenceError("");
    setReason("");
    setOpinion("");
    setTab("report");
  }, [selected]);
  useEffect(() => {
    if (!user) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const [list, current] = await Promise.all([
          api<Summary[]>("/api/cases", { signal: controller.signal }),
          selected && selected !== "new"
            ? api<Case>("/api/cases/" + selected, { signal: controller.signal })
            : Promise.resolve(null),
        ]);
        if (active) {
          setCases(list);
          setDetail(current);
        }
      } catch (e) {
        if (active) {
          setError(message(e));
          if (e instanceof ApiError && e.status === 401) {
            setUser(null);
            setDetail(null);
            setCases([]);
          }
        }
      } finally {
        if (active) timer = setTimeout(poll, 2000);
      }
    };
    void poll();
    return () => {
      active = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [user, selected, refreshId]);

  const act = async (operation: () => Promise<void>) => {
    if (locked.current) return;
    locked.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await operation();
      setRefreshId((i) => i + 1);
    } catch (e) {
      setError(message(e));
    } finally {
      locked.current = false;
      setBusy(false);
    }
  };
  const choose = (id: string) => {
    location.hash = id;
    setSelected(id);
    setError("");
    setNotice("");
    setFollowup(false);
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void act(async () => {
      const job = await actionRequest<Job>(actionKey + "/start", "/api/jobs", {
        question: question.trim(),
        ...(followup && detail ? { case_id: detail.case_id } : {}),
      });
      setQuestion("");
      choose(job.case_id);
      setNotice("调查已登记。关闭或刷新页面不会取消执行。");
    });
  };
  const latestJob = detail?.jobs.at(-1);
  const latestRun = detail?.runs.at(-1);
  const report =
    detail?.reports.find((r) => r.report_id === version) ??
    detail?.reports.at(-1);
  const reportRun =
    detail?.runs.find((r) => r.run_id === report?.run_id) ?? latestRun;
  const pending = Boolean(
    latestJob &&
    ["queued", "running", "awaiting_input", "paused", "interrupted"].includes(
      latestJob.status,
    ),
  );
  const canInvestigate = Boolean(user?.roles.includes("investigator"));
  const canResume = canInvestigate && latestRun?.initiator === user?.user_id;
  const ownReport = latestRun?.initiator === user?.user_id;
  const canReview = Boolean(
    user?.roles.includes("reviewer") &&
    latestRun?.initiator &&
    !ownReport &&
    !pending &&
    report &&
    report === detail?.reports.at(-1) &&
    report.run_id === latestRun?.run_id &&
    !detail?.reviews.some((r) => r.report_id === report.report_id),
  );

  const resume = () =>
    void act(async () => {
      if (!latestRun) return;
      const key = actionKey + "/resume/" + latestRun.run_id;
      const stored = pendingBody(key);
      const job = await actionRequest<Job>(
        key,
        `/api/runs/${latestRun.run_id}/resume`,
        stored
          ? stored.clarification
            ? { clarification: stored.clarification }
            : {}
          : latestJob?.status === "awaiting_input"
            ? { clarification: clarification.trim() }
            : {},
      );
      setDetail((current) =>
        current ? { ...current, jobs: [...current.jobs, job] } : current,
      );
      setClarification("");
      setNotice("已提交，worker 将从持久化状态继续。");
    });
  const review = (action: string) =>
    void act(async () => {
      if (!detail || !report) return;
      await actionRequest(
        actionKey + "/review/" + report.report_id,
        `/api/cases/${detail.case_id}/review`,
        {
          report_id: report.report_id,
          action,
          reason: reason.trim(),
          opinion,
          ...(action === "modify" ? { draft: editing } : {}),
        },
      );
      setEditing(null);
      setVersion("");
      setReason("");
      setOpinion("");
      setNotice(
        action === "modify"
          ? "修改已保存为新版本，校验结果已更新。"
          : "审核决定已记录。",
      );
    });
  const showEvidence = async (refId: string) => {
    if (!reportRun) return;
    setTab("evidence");
    setEvidence(null);
    setEvidenceError("");
    try {
      setEvidence(
        await api<Evidence>(
          `/api/runs/${reportRun.run_id}/evidence/${encodeURIComponent(refId)}`,
        ),
      );
    } catch (e) {
      setEvidenceError(message(e));
    }
  };
  const citedText = (text: string) =>
    text.split(/(\[ev-[^\]]+\])/g).map((part, i) =>
      part.startsWith("[ev-") ? (
        <button
          key={i}
          className="citation"
          onClick={() => void showEvidence(part.slice(1, -1))}
        >
          {part.slice(1, 12)}… ↗
        </button>
      ) : (
        part
      ),
    );

  if (checking)
    return (
      <main className="login">
        <p>正在连接工作台…</p>
      </main>
    );
  if (!user)
    return (
      <main className="login">
        <div className="login-brand">
          <span className="logo">E</span>
          <span>EnterpriseIQ</span>
        </div>
        <section className="login-card">
          <span className="eyebrow">INVESTIGATION WORKBENCH</span>
          <h1>
            让每个判断
            <br />
            都有证据可循。
          </h1>
          <p>登录以查看调查、追溯来源并完成审核。</p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void act(async () => {
                const identity = await api<User>("/api/session", {
                  method: "POST",
                  body: JSON.stringify({ token }),
                });
                setToken("");
                setUser(identity);
              });
            }}
          >
            <label htmlFor="token">个人登录令牌</label>
            <input
              id="token"
              type="password"
              autoComplete="off"
              required
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="输入本地管理员提供的令牌"
            />
            <button className="primary" disabled={busy}>
              进入工作台 <span>↗</span>
            </button>
          </form>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          <small>本地受控演示 · 合成业务快照</small>
        </section>
        <p className="login-note">证据调查 / 来源校验 / 人工决定</p>
      </main>
    );

  return (
    <div className="shell">
      <aside className="sidebar">
        <a className="brand" href="#new">
          <span className="logo">E</span>
          <span>
            EnterpriseIQ<small>INVESTIGATION ENGINE</small>
          </span>
        </a>
        <div className="workspace">
          <span className="workspace-icon">
            {user.workspace[0]?.toUpperCase()}
          </span>
          <div>
            {user.workspace}
            <small>调查工作空间</small>
          </div>
          <span className="live-dot" />
        </div>
        <button
          className="new-button"
          disabled={!canInvestigate}
          onClick={() => choose("new")}
        >
          <span>＋</span> 发起调查
        </button>
        <div className="nav-label">
          工作台 <span>{cases.length.toString().padStart(2, "0")}</span>
        </div>
        <div className="case-list">
          {cases.length === 0 ? (
            <p className="sidebar-empty">暂无调查。创建第一个 Case 开始。</p>
          ) : (
            [...cases].reverse().map((c) => (
              <button
                className={
                  "case-link " + (selected === c.case_id ? "active" : "")
                }
                key={c.case_id}
                onClick={() => choose(c.case_id)}
              >
                <span className="case-question">{c.question}</span>
                <span className="case-meta">
                  {c.case_id.slice(0, 8)} <Badge status={statusOf(c)} />
                </span>
              </button>
            ))
          )}
        </div>
        <div className="sidebar-bottom">
          <span className="avatar">{user.user_id[0]?.toUpperCase()}</span>
          <div>
            <strong>{user.user_id}</strong>
            <small>{user.roles.map((r) => roleNames[r]).join(" / ")}</small>
          </div>
          <button
            className="logout"
            aria-label="退出登录"
            onClick={() =>
              void act(async () => {
                await api("/api/session", { method: "DELETE" });
                setUser(null);
                setDetail(null);
                setCases([]);
                setToken("");
              })
            }
          >
            ↪
          </button>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <span>
            工作空间 <b>/</b> 调查工作台
          </span>
          <span className="snapshot-label">
            <i />
            合成快照 · 只读调查
          </span>
        </header>
        <div className="content">
          <div className="page-heading">
            <div>
              <span className="eyebrow">EVIDENCE INTO DECISIONS</span>
              <h1>
                调查工作台<span className="heading-dot">.</span>
              </h1>
              <p>从业务问题出发，沿证据形成可复查的判断。</p>
            </div>
            <span className="outline-tag">M3 · LOCAL PILOT</span>
          </div>
          <section className="metrics" aria-label="调查概览">
            <div>
              <span>全部调查</span>
              <strong>
                {cases.length}
                <small>CASES</small>
              </strong>
            </div>
            <div>
              <span>执行中</span>
              <strong>
                {
                  cases.filter((c) =>
                    ["queued", "running"].includes(statusOf(c)),
                  ).length
                }
                <small>ACTIVE</small>
              </strong>
            </div>
            <div>
              <span>待补充与恢复</span>
              <strong>
                {
                  cases.filter((c) =>
                    ["awaiting_input", "paused", "interrupted"].includes(
                      statusOf(c),
                    ),
                  ).length
                }
                <small>NEEDS INPUT</small>
              </strong>
            </div>
            <div>
              <span>已完成调查</span>
              <strong>
                {cases.filter((c) => statusOf(c) === "completed").length}
                <small>COMPLETED</small>
              </strong>
            </div>
          </section>
          {error && (
            <div role="alert" className="error banner">
              {error}
              <button
                onClick={() => {
                  setError("");
                  setRefreshId((i) => i + 1);
                }}
              >
                刷新状态
              </button>
            </div>
          )}
          {notice && (
            <div role="status" className="notice banner">
              {notice}
            </div>
          )}
          {pendingActions(actionKey).map((pendingAction) => (
            <div className="warning" key={pendingAction.key}>
              有一项提交尚未收到确认。可安全重试原请求，或先核对状态。{" "}
              <button
                className="secondary"
                disabled={busy}
                onClick={() =>
                  void act(async () => {
                    const result = await actionRequest<{ case_id?: string }>(
                      pendingAction.key,
                      pendingAction.path,
                      pendingAction.body,
                    );
                    if (result.case_id) choose(result.case_id);
                    setNotice("原提交已确认。");
                  })
                }
              >
                重试原提交
              </button>
            </div>
          ))}
          {(!selected || selected === "new" || followup) && (
            <section className="panel composer">
              <div className="section-title">
                <span className="number">01</span>
                <div>
                  <h2>{followup ? "继续这个 Case 的调查" : "提出业务问题"}</h2>
                  <p>明确对象、范围与所需输出；缺少口径时会等待你的补充。</p>
                </div>
              </div>
              <form onSubmit={submit}>
                <label className="sr-only" htmlFor="question">
                  调查问题
                </label>
                <textarea
                  id="question"
                  required
                  maxLength={4000}
                  disabled={!canInvestigate}
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  placeholder="例如：Vantara 的扩展交易被什么阻塞？有哪些明确的推进条件和待确认事项？"
                  rows={4}
                />
                <div className="composer-bottom">
                  <small>调查独立执行，刷新或离开页面后仍可查看状态。</small>
                  <button
                    className="primary"
                    disabled={busy || !canInvestigate || !question.trim()}
                  >
                    提交调查 <span>↗</span>
                  </button>
                </div>
              </form>
              {!canInvestigate && (
                <p className="muted">
                  当前身份可查看已有调查；发起调查需要 investigator 角色。
                </p>
              )}
            </section>
          )}
          {selected && selected !== "new" && !detail && (
            <section className="panel">
              <Empty>正在读取调查记录…</Empty>
            </section>
          )}
          {detail && !followup && (
            <section className="panel investigation">
              <div className="case-heading">
                <div>
                  <span className="eyebrow">
                    CASE / {detail.case_id.slice(0, 8).toUpperCase()}
                  </span>
                  <h2>{latestRun?.question}</h2>
                  <div className="meta-line">
                    <Badge
                      status={
                        latestJob?.status ??
                        latestRun?.execution_status ??
                        "registered"
                      }
                    />
                    <Badge status={detail.business_status} />
                    <span>发起人 {latestRun?.initiator ?? "未登记"}</span>
                  </div>
                </div>
                <button
                  className="secondary"
                  disabled={!canInvestigate || pending}
                  onClick={() => {
                    setQuestion("");
                    setFollowup(true);
                  }}
                >
                  后续调查 ↗
                </button>
              </div>
              {latestJob?.status === "queued" && (
                <div className="state-note">
                  调查已进入持久化队列，等待本地 worker。页面将自动更新。
                </div>
              )}
              {latestJob?.status === "running" && (
                <div className="state-note">
                  <span className="pulse" />
                  调查正在独立执行。工具行为与证据将在持久化后显示。
                </div>
              )}
              {latestJob &&
                ["failed", "partial", "interrupted"].includes(
                  latestJob.status,
                ) && (
                  <div className="warning">
                    <strong>{labels[latestJob.status]}</strong>
                    <p>
                      {latestJob.error_code === "model_calls_disabled"
                        ? "当前运行未启用模型服务。请联系本地管理员；已保留失败记录，可在同一 Case 发起后续调查。"
                        : `原因：${latestJob.error_code ?? "请检查执行记录"}。已有证据与缺口继续保留。`}
                    </p>
                  </div>
                )}
              {latestJob &&
                ["awaiting_input", "paused", "interrupted"].includes(
                  latestJob.status,
                ) && (
                  <div className="clarification">
                    <h3>
                      {latestJob.status === "awaiting_input"
                        ? "需要澄清调查口径"
                        : "调查等待恢复"}
                    </h3>
                    <p>
                      {latestJob.status === "awaiting_input"
                        ? "请明确产品组件或地理区域等口径，补充后将继续同一调查。"
                        : "恢复会沿用同一 run 和剩余预算；意外停机时间按现有引擎规则计入。"}
                    </p>
                    {canResume ? (
                      <form
                        onSubmit={(e) => {
                          e.preventDefault();
                          resume();
                        }}
                      >
                        {latestJob.status === "awaiting_input" && (
                          <input
                            aria-label="补充口径"
                            required
                            value={clarification}
                            onChange={(e) => setClarification(e.target.value)}
                            placeholder="例如：按直接产品组件统计，不是地理区域"
                          />
                        )}
                        <button className="primary" disabled={busy}>
                          {latestJob.status === "awaiting_input"
                            ? "补充并继续"
                            : "恢复调查"}{" "}
                          ↗
                        </button>
                      </form>
                    ) : (
                      <small>由该调查的发起人补充或恢复。</small>
                    )}
                  </div>
                )}
              {detail.progress && (
                <div className="progress-line">
                  模型尝试 {detail.progress.model_calls} 次{" "}
                  <span>工具 {detail.progress.tool_calls} 次</span>
                  <span>
                    已知 Token{" "}
                    {detail.progress.prompt_tokens +
                      detail.progress.completion_tokens}
                    {detail.progress.usage_incomplete
                      ? "（含未知用量，非最终总额）"
                      : ""}
                  </span>
                </div>
              )}
              <nav className="tabs" aria-label="调查详情">
                {[
                  ["report", "调查报告"],
                  ["evidence", "来源证据"],
                  ["trace", "执行记录"],
                  ["reviews", "审核历史"],
                ].map(([key, title]) => (
                  <button
                    className={tab === key ? "selected" : ""}
                    onClick={() => setTab(key)}
                    key={key}
                  >
                    {title}
                    <span>
                      {key === "evidence"
                        ? Object.keys(reportRun?.evidence ?? {}).length
                        : key === "reviews"
                          ? detail.reviews.length
                          : key === "report"
                            ? detail.reports.length
                            : Object.keys(latestRun?.trace ?? {}).length}
                    </span>
                  </button>
                ))}
              </nav>
              <div className="tab-content">
                {tab === "report" && (
                  <>
                    {report ? (
                      <>
                        <div className="report-toolbar">
                          <label>
                            报告版本{" "}
                            <select
                              aria-label="报告版本"
                              value={report.report_id}
                              onChange={(e) => {
                                setVersion(e.target.value);
                                setEditing(null);
                              }}
                            >
                              {detail.reports.map((r) => (
                                <option value={r.report_id} key={r.report_id}>
                                  v{r.version} ·{" "}
                                  {labels[r.payload.report_validation] ??
                                    r.payload.report_validation}
                                </option>
                              ))}
                            </select>
                          </label>
                          <Badge status={report.payload.report_validation} />
                        </div>
                        {report.payload.report.formal_report ? (
                          <ReportText
                            text={report.payload.report.formal_report}
                            renderText={citedText}
                            onEvidence={() => setTab("evidence")}
                          />
                        ) : (
                          <div className="warning">
                            本版报告未通过校验，暂不能作为正式报告接受。
                            <details>
                              <summary>查看未解决缺口与诊断详情</summary>
                              <pre>
                                {pretty(report.payload.report.gaps ?? [])}
                              </pre>
                            </details>
                          </div>
                        )}
                        {Boolean(
                          report.payload.report.open_questions_unverified
                            ?.length,
                        ) && (
                          <div className="questions">
                            <h3>待确认事项</h3>
                            <ul>
                              {report.payload.report.open_questions_unverified?.map(
                                (q, i) => (
                                  <li key={i}>{q}</li>
                                ),
                              )}
                            </ul>
                          </div>
                        )}
                        {ownReport && user.roles.includes("reviewer") && (
                          <p className="review-boundary">
                            你是该调查的发起人，不能审核其报告。请由另一位审核员处理。
                          </p>
                        )}
                        {!latestRun?.initiator &&
                          user.roles.includes("reviewer") && (
                            <p className="review-boundary">
                              当前调查未登记可信发起人，不能通过工作台审核。
                            </p>
                          )}
                        {canReview && (
                          <section className="review-form">
                            <div className="section-title">
                              <span className="number">02</span>
                              <div>
                                <h3>核对证据后，记录审核决定</h3>
                                <p>
                                  事实修改会形成新版本并重新校验；原版与决定持续保留。
                                </p>
                              </div>
                            </div>
                            {editing && (
                              <DraftEditor
                                draft={editing}
                                onChange={setEditing}
                              />
                            )}
                            <label>
                              审核理由
                              <input
                                value={reason}
                                required
                                onChange={(e) => setReason(e.target.value)}
                                placeholder="说明接受、驳回或修改的依据"
                              />
                            </label>
                            <label>
                              补充意见（作为未验证意见保存）
                              <textarea
                                value={opinion}
                                onChange={(e) => setOpinion(e.target.value)}
                                rows={2}
                              />
                            </label>
                            <div className="review-actions">
                              {editing ? (
                                <>
                                  <button
                                    className="secondary"
                                    onClick={() => setEditing(null)}
                                  >
                                    取消修改
                                  </button>
                                  <button
                                    className="primary"
                                    disabled={busy || !reason.trim()}
                                    onClick={() => review("modify")}
                                  >
                                    保存新版本并校验
                                  </button>
                                </>
                              ) : (
                                <>
                                  <button
                                    className="secondary"
                                    onClick={() => {
                                      try {
                                        setEditing(
                                          parseDraft(report.payload.draft),
                                        );
                                      } catch {
                                        setError(
                                          "该报告没有可编辑的结构化草稿。",
                                        );
                                      }
                                    }}
                                  >
                                    修改报告
                                  </button>
                                  <button
                                    className="danger-button"
                                    disabled={busy || !reason.trim()}
                                    onClick={() => review("reject")}
                                  >
                                    驳回
                                  </button>
                                  <button
                                    className="primary"
                                    disabled={
                                      busy ||
                                      !reason.trim() ||
                                      report.payload.report_validation !==
                                        "passed"
                                    }
                                    onClick={() => review("accept")}
                                  >
                                    接受报告 ✓
                                  </button>
                                </>
                              )}
                            </div>
                          </section>
                        )}
                      </>
                    ) : (
                      <Empty>
                        调查报告尚未生成。执行状态、澄清请求与失败原因会显示在上方。
                      </Empty>
                    )}
                  </>
                )}
                {tab === "evidence" && (
                  <div className="evidence-layout">
                    <div className="evidence-list">
                      {Object.values(reportRun?.evidence ?? {}).map((ref) => (
                        <button
                          key={ref.evidence_id}
                          className={
                            evidence?.reference.evidence_id === ref.evidence_id
                              ? "active"
                              : ""
                          }
                          onClick={() => void showEvidence(ref.evidence_id)}
                        >
                          <span>{ref.source_id}</span>
                          <small>
                            {ref.entity_type} ·{" "}
                            {ref.json_pointer
                              ? "原始记录"
                              : `行 ${ref.start_line}–${ref.end_line}`}
                          </small>
                        </button>
                      ))}
                      {!Object.keys(reportRun?.evidence ?? {}).length && (
                        <Empty>暂无已保存证据</Empty>
                      )}
                    </div>
                    <div className="evidence-content">
                      {evidenceError ? (
                        <div role="alert" className="error">
                          {evidenceError}
                        </div>
                      ) : evidence ? (
                        <>
                          <h3>{evidence.reference.source_id}</h3>
                          <p className="source-path">
                            {evidence.reference.source_path}
                          </p>
                          <dl className="source-meta">
                            <dt>快照</dt>
                            <dd>{evidence.reference.snapshot_id}</dd>
                            <dt>定位</dt>
                            <dd>
                              {evidence.reference.json_pointer ??
                                `行 ${evidence.reference.start_line}–${evidence.reference.end_line}`}
                            </dd>
                            <dt>引用</dt>
                            <dd>{evidence.reference.evidence_id}</dd>
                          </dl>
                          <pre>{pretty(evidence.content)}</pre>
                        </>
                      ) : (
                        <Empty>
                          选择来源，查看引用对应的原始记录或正文片段。
                        </Empty>
                      )}
                    </div>
                  </div>
                )}
                {tab === "trace" && (
                  <>
                    <p className="muted">
                      仅展示可审计的工具行为与简短行动摘要。
                    </p>
                    <ol className="timeline">
                      {Object.values(latestRun?.trace ?? {}).map((event, i) => (
                        <li key={i}>
                          <span className="timeline-dot" />
                          <div>
                            <small>
                              {event.type === "tool" ? "工具" : "模型"} /{" "}
                              {event.number}
                            </small>
                            <strong>
                              {event.name ?? event.action_summary ?? "模型请求"}
                            </strong>
                            <p>{event.error ?? event.status ?? "已完成"}</p>
                          </div>
                        </li>
                      ))}
                    </ol>
                    {!Object.keys(latestRun?.trace ?? {}).length && (
                      <Empty>
                        暂无执行记录。持久化后的工具行为将在这里显示。
                      </Empty>
                    )}
                    {Boolean(latestRun?.gaps?.length) && (
                      <details>
                        <summary>证据缺口</summary>
                        <pre>{pretty(latestRun?.gaps)}</pre>
                      </details>
                    )}
                  </>
                )}
                {tab === "reviews" && (
                  <>
                    {detail.reviews.length ? (
                      <ol className="timeline">
                        {detail.reviews.map((r) => (
                          <li key={r.request_id}>
                            <span className="timeline-dot" />
                            <div>
                              <small>
                                {new Date(r.at).toLocaleString("zh-CN")} ·{" "}
                                {r.actor}
                              </small>
                              <strong>{labels[r.action]}</strong>
                              <p>{r.reason}</p>
                              {r.opinion_unverified && (
                                <p className="muted">
                                  未验证意见：{r.opinion_unverified}
                                </p>
                              )}
                            </div>
                          </li>
                        ))}
                      </ol>
                    ) : (
                      <Empty>
                        暂无审核事件。调查完成并校验后，由另一位审核员处理。
                      </Empty>
                    )}
                  </>
                )}
              </div>
            </section>
          )}
          {(!selected || selected === "new") && (
            <div className="principles">
              <div>
                <span>01 / TRACEABLE</span>
                <h3>来源可追溯</h3>
                <p>每条重要结论都回到快照中的原始证据。</p>
              </div>
              <div>
                <span>02 / BOUNDED</span>
                <h3>调查有边界</h3>
                <p>未知、冲突与不完整结果始终明确保留。</p>
              </div>
              <div>
                <span>03 / REVIEWED</span>
                <h3>决定有记录</h3>
                <p>报告版本与审核事件分别保存，便于复查。</p>
              </div>
            </div>
          )}
          <footer>
            EnterpriseIQ <span>证据驱动的业务调查</span>
            <span>固定快照 · ARR / ACV 分开 · 候选关联不等于根因</span>
          </footer>
        </div>
      </main>
    </div>
  );
}

function ReportText({
  text,
  renderText,
  onEvidence,
}: {
  text: string;
  renderText: (text: string) => ReactNode;
  onEvidence: () => void;
}) {
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];
  const cells = (line: string) =>
    line
      .trim()
      .slice(1, -1)
      .split("|")
      .map((value) => value.trim());
  for (let i = 0; i < lines.length; i++) {
    if (
      lines[i].startsWith("|") &&
      /^\|[\s:|\-]+\|$/.test(lines[i + 1] ?? "")
    ) {
      const headers = cells(lines[i]);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].startsWith("|"))
        rows.push(cells(lines[i++]));
      i--;
      blocks.push(
        <div className="report-table-scroll" key={i}>
          <table>
            <thead>
              <tr>
                {headers.map((header, j) => (
                  <th key={j} scope="col">
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, n) => (
                <tr key={n}>
                  {row.map((cell, j) => (
                    <td key={j}>{renderText(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
    } else if (lines[i].startsWith("来源工具调用：")) {
      blocks.push(
        <button key={i} className="citation" onClick={onEvidence}>
          查看本报告的来源证据 ↗
        </button>,
      );
    } else {
      blocks.push(<p key={i}>{renderText(lines[i])}</p>);
    }
  }
  return <div className="report-text">{blocks}</div>;
}

function parseDraft(raw: string): Draft {
  const value = JSON.parse(raw);
  if (
    !value ||
    !["investigation", "direct_component", "global"].includes(value.scope) ||
    !Array.isArray(value.claims) ||
    !value.claims.length ||
    !value.claims.every(
      (claim: unknown) =>
        claim &&
        typeof claim === "object" &&
        !Array.isArray(claim) &&
        typeof (claim as Claim).kind === "string",
    ) ||
    (value.open_questions !== undefined &&
      (!Array.isArray(value.open_questions) ||
        !value.open_questions.every(
          (item: unknown) => typeof item === "string",
        )))
  ) {
    throw new Error("该报告没有可编辑的结构化草稿。");
  }
  return { ...value, open_questions: value.open_questions ?? [] };
}

function DraftEditor({
  draft,
  onChange,
}: {
  draft: Draft;
  onChange: (draft: Draft) => void;
}) {
  return (
    <div className="draft-editor">
      <h3>修改报告事实</h3>
      <p className="muted">
        来源与数值修改均需重新校验。无依据的修改会被阻止。
      </p>
      {draft.claims.map((claim, index) => (
        <fieldset key={index}>
          <legend>
            主张 {index + 1} ·{" "}
            {(
              {
                field: "原始字段",
                quote: "原文摘录",
                arr: "金额",
                table: "数据表",
                eta: "ETA 观测",
                candidate: "候选关联",
                reference: "文本引用",
              } as Record<string, string>
            )[claim.kind] ?? claim.kind}
          </legend>
          {Object.entries(claim)
            .filter(
              ([key, value]) =>
                !["kind", "scope", "view", "calculation_call_id"].includes(
                  key,
                ) &&
                value !== null &&
                ["string", "number"].includes(typeof value),
            )
            .map(([key, value]) => (
              <label key={key}>
                {(
                  {
                    value: "字段值",
                    quote: "原文摘录",
                    field: "来源字段",
                    evidence_id: "证据引用",
                    subject_id: "归属事项",
                    amount: "金额",
                    currency: "币种",
                    top_n: "展示数量",
                    part_id: "产品组件",
                    ticket_evidence_id: "工单证据引用",
                    issue_evidence_id: "工程事项证据引用",
                    target_evidence_id: "目标证据引用",
                  } as Record<string, string>
                )[key] ?? key}
                <input
                  value={String(value)}
                  type={typeof value === "number" ? "number" : "text"}
                  onChange={(e) =>
                    onChange({
                      ...draft,
                      claims: draft.claims.map((c, i) =>
                        i === index
                          ? {
                              ...c,
                              [key]:
                                typeof value === "number"
                                  ? Number(e.target.value)
                                  : e.target.value,
                            }
                          : c,
                      ),
                    })
                  }
                />
              </label>
            ))}
        </fieldset>
      ))}
      <label>
        待确认事项（每行一项）
        <textarea
          rows={3}
          value={draft.open_questions.join("\n")}
          onChange={(e) =>
            onChange({ ...draft, open_questions: e.target.value.split("\n") })
          }
        />
      </label>
    </div>
  );
}
