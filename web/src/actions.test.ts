import { describe, it, expect, vi, beforeEach } from "vitest";
import { actionRequest } from "./actions";

beforeEach(() => sessionStorage.clear());

describe("durable browser submission", () => {
  it("retries an uncertain request with the same ID across reloads", async () => {
    const ids: string[] = [];
    const send = vi.fn(async (_path: string, body: Record<string, unknown>) => {
      ids.push(String(body.request_id));
      if (ids.length === 1) throw new Error("connection lost after commit");
      return { job_id: body.request_id };
    });
    await expect(
      actionRequest("alice/start", "/api/jobs", { question: "调查客户" }, send),
    ).rejects.toThrow("connection lost");
    await actionRequest(
      "alice/start",
      "/api/jobs",
      { question: "调查客户" },
      send,
    );
    expect(ids[1]).toBe(ids[0]);
    expect(sessionStorage.length).toBe(0);
  });

  it("does not replace an uncertain request with different content", async () => {
    const send = vi.fn(async () => {
      throw new Error("offline");
    });
    await expect(
      actionRequest("alice/start", "/api/jobs", { question: "first" }, send),
    ).rejects.toThrow("offline");
    await expect(
      actionRequest("alice/start", "/api/jobs", { question: "second" }, send),
    ).rejects.toThrow("待确认");
    expect(send).toHaveBeenCalledTimes(1);
  });
});
