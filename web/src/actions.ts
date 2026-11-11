type Payload = Record<string, unknown>;
type Sender = (path: string, body: Payload) => Promise<unknown>;

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  const value = await response.json();
  if (!response.ok)
    throw new ApiError(
      typeof value.detail === "string"
        ? value.detail
        : "请求内容不符合要求，请检查输入。",
      response.status,
    );
  return value;
}

// Keep the exact request until its acknowledgement. A reload must not create a second run.
export async function actionRequest<T>(
  key: string,
  path: string,
  body: Payload,
  send: Sender = (url, payload) =>
    api(url, { method: "POST", body: JSON.stringify(payload) }),
): Promise<T> {
  const storageKey = "enterpriseiq:pending:" + key;
  const fingerprint = JSON.stringify({ path, body });
  const saved = sessionStorage.getItem(storageKey);
  let pending: { fingerprint: string; payload: Payload };
  if (saved) {
    pending = JSON.parse(saved);
    if (pending.fingerprint !== fingerprint)
      throw new Error("上一次提交仍待确认，请先重试原内容。");
  } else {
    pending = {
      fingerprint,
      payload: { ...body, request_id: crypto.randomUUID() },
    };
    sessionStorage.setItem(storageKey, JSON.stringify(pending));
  }
  try {
    const result = await send(path, pending.payload);
    sessionStorage.removeItem(storageKey);
    return result as T;
  } catch (error) {
    // A definitive rejection did not execute the operation. Network/5xx remain uncertain.
    if (
      error instanceof ApiError &&
      error.status >= 400 &&
      error.status < 500 &&
      error.status !== 409
    )
      sessionStorage.removeItem(storageKey);
    throw error;
  }
}

export function pendingBody(key: string): Payload | null {
  const saved = sessionStorage.getItem("enterpriseiq:pending:" + key);
  return saved ? JSON.parse(saved).payload : null;
}

export function pendingActions(
  prefix: string,
): { key: string; path: string; body: Payload }[] {
  const result = [];
  for (let i = 0; i < sessionStorage.length; i++) {
    const key = sessionStorage.key(i)!;
    if (key.startsWith("enterpriseiq:pending:" + prefix + "/")) {
      const pending = JSON.parse(sessionStorage.getItem(key)!);
      const request = JSON.parse(pending.fingerprint);
      result.push({
        key: key.slice("enterpriseiq:pending:".length),
        path: request.path,
        body: request.body,
      });
    }
  }
  return result;
}
