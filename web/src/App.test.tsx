import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";

const job = {
  job_id: "job-1",
  case_id: "case-1",
  run_id: "run-1",
  status: "awaiting_input",
  error_code: null,
};
const detail = {
  case_id: "case-1",
  business_status: "investigating",
  runs: [
    {
      run_id: "run-1",
      question: "按区域统计 ARR 前三",
      initiator: "alice",
      evidence: {},
      trace: {},
      gaps: [],
    },
  ],
  jobs: [job],
  reports: [],
  reviews: [],
};
beforeEach(() => {
  location.hash = "";
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function responses(caseData = detail) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) => {
      if (path === "/api/me")
        return Response.json({
          user_id: "alice",
          workspace: "demo",
          roles: ["investigator", "reviewer"],
        });
      if (path === "/api/cases")
        return Response.json([
          { case_id: "case-1", question: "按区域统计 ARR 前三", jobs: [job] },
        ]);
      if (path === "/api/cases/case-1") return Response.json(caseData);
      throw new Error("Unexpected path " + path);
    }),
  );
}

it("reload restores a waiting investigation and its clarification form", async () => {
  location.hash = "#case-1";
  responses();
  render(<App />);
  expect(
    await screen.findByRole("heading", { name: "需要澄清调查口径" }),
  ).toBeDefined();
  expect(screen.getByRole("textbox", { name: "补充口径" })).toBeDefined();
  expect(screen.getByRole("button", { name: "补充并继续 ↗" })).toBeDefined();
});

it("does not offer self-review to an investigator who also has reviewer role", async () => {
  location.hash = "#case-1";
  const completed = {
    ...detail,
    business_status: "awaiting_review",
    jobs: [{ ...job, status: "completed" }],
    reports: [
      {
        report_id: "report-1",
        run_id: "run-1",
        version: 1,
        payload: {
          report_validation: "passed",
          draft: "{}",
          report: { formal_report: "客户名称：Vantara" },
        },
      },
    ],
  };
  responses(completed as typeof detail);
  render(<App />);
  expect(await screen.findByText(/不能审核其报告/)).toBeDefined();
  expect(screen.queryByRole("button", { name: "接受报告 ✓" })).toBeNull();
});

it("double-clicking submit makes one request while acknowledgement is pending", async () => {
  responses();
  const original = globalThis.fetch;
  let submitted = 0;
  let finish: (response: Response) => void = () => {};
  vi.stubGlobal(
    "fetch",
    vi.fn((path: string, init?: RequestInit) => {
      if (path === "/api/jobs") {
        submitted++;
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
      }
      return original(path, init);
    }),
  );
  render(<App />);
  const input = await screen.findByRole("textbox", { name: "调查问题" });
  const user = userEvent.setup();
  await user.type(input, "查看客户名称");
  await user.dblClick(screen.getByRole("button", { name: "提交调查 ↗" }));
  expect(submitted).toBe(1);
  finish(Response.json({ ...job, status: "queued" }, { status: 202 }));
  await waitFor(() => expect(sessionStorage.length).toBe(0));
});

it("renders verified tabular amounts as accessible rows", async () => {
  location.hash = "#case-1";
  const completed = {
    ...detail,
    jobs: [{ ...job, status: "completed" }],
    reports: [
      {
        report_id: "report-1",
        run_id: "run-1",
        version: 1,
        payload: {
          report_validation: "passed",
          draft: "{}",
          report: {
            formal_report:
              "| 组件 | ARR |\n|---|---:|\n| PART-010 | 732000 |\n全局客户去重 ARR = 1041000。",
          },
        },
      },
    ],
  };
  responses(completed as typeof detail);
  render(<App />);
  expect(
    await screen.findByRole("columnheader", { name: "ARR" }),
  ).toBeDefined();
  expect(screen.getByRole("cell", { name: "732000" })).toBeDefined();
  expect(screen.getByText(/1041000/)).toBeDefined();
});

it("keeps the workbench usable when a blocked draft is valid JSON with no claims", async () => {
  location.hash = "#case-1";
  const blocked = {
    ...detail,
    runs: [{ ...detail.runs[0], initiator: "bob" }],
    jobs: [{ ...job, status: "failed" }],
    reports: [
      {
        report_id: "report-1",
        run_id: "run-1",
        version: 1,
        payload: {
          report_validation: "blocked",
          draft: "{}",
          report: { formal_report: null, gaps: [] },
        },
      },
    ],
  };
  responses(blocked as typeof detail);
  render(<App />);
  await userEvent
    .setup()
    .click(
      await screen.findByRole("button", { name: "修改报告" }),
    );
  expect(await screen.findByRole("alert")).toBeDefined();
  expect(screen.getByRole("heading", { name: /^调查工作台/ })).toBeDefined();
});
