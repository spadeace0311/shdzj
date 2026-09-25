import type {
  CollectorStatus,
  EventDetail,
  EventIngestResponse,
  EventSummary,
  ManualEventInput,
  TokenResponse,
} from "../types";

const REQUEST_TIMEOUT_MS = 10_000;

let accessToken: string | null = null;

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function setAccessToken(token: string): void {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function clearAccessToken(): void {
  accessToken = null;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

async function fetchWithTimeout(path: string, init: RequestInit = {}): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = globalThis.setTimeout(
    () => controller.abort(),
    REQUEST_TIMEOUT_MS,
  );

  try {
    return await fetch(path, {
      ...init,
      signal: controller.signal,
    });
  } catch (error) {
    if (isAbortError(error)) {
      throw new ApiError("请求超时，请稍后重试", 408);
    }
    throw new ApiError("网络连接失败，请稍后重试", 0);
  } finally {
    globalThis.clearTimeout(timeoutId);
  }
}

async function parseError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload.detail === "string") {
      return payload.detail;
    }
    if (Array.isArray(payload.detail)) {
      const messages = payload.detail
        .map((item) => {
          if (
            typeof item === "object" &&
            item !== null &&
            "msg" in item &&
            typeof item.msg === "string"
          ) {
            return item.msg;
          }
          return "";
        })
        .filter(Boolean);
      if (messages.length > 0) {
        return messages.join("；");
      }
      return "请求参数不合法";
    }
  } catch {
    // Ignore non-JSON error bodies and use the HTTP status fallback below.
  }
  return `请求失败（HTTP ${response.status}）`;
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetchWithTimeout(path, init);
  if (!response.ok) {
    throw new ApiError(await parseError(response), response.status);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export function manualEventErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.status) {
      case 401:
        return "登录状态已失效，请重新登录";
      case 403:
        return "当前账号无权创建人工事件";
      case 404:
        return "事件接口不存在";
      case 408:
        return "请求超时，请稍后重试";
      case 422:
        return "输入数据不合法，请检查数值范围";
      case 503:
        return "事件存储服务暂不可用，请稍后重试";
      default:
        return "网络连接失败，请稍后重试";
    }
  }
  return "网络连接失败，请稍后重试";
}

export async function login(username: string, password: string): Promise<TokenResponse> {
  const body = new URLSearchParams({ username, password });
  const response = await fetchWithTimeout("/api/v1/auth/login", {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body,
  });

  if (!response.ok) {
    throw new ApiError(await parseError(response), response.status);
  }

  const token = (await response.json()) as TokenResponse;
  setAccessToken(token.access_token);
  return token;
}

export async function listEvents(): Promise<EventSummary[]> {
  return requestJson<EventSummary[]>("/api/v1/events");
}

export async function getEvent(eventId: string): Promise<EventDetail> {
  return requestJson<EventDetail>(`/api/v1/events/${encodeURIComponent(eventId)}`);
}

export async function getCollectorStatus(): Promise<CollectorStatus> {
  if (!accessToken) {
    throw new ApiError("请先登录后查看采集状态", 401);
  }

  return requestJson<CollectorStatus>("/api/v1/collector/status", {
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
  });
}

export async function createManualEvent(
  input: ManualEventInput,
): Promise<EventIngestResponse> {
  if (!accessToken) {
    throw new ApiError("请先登录后再创建人工事件", 401);
  }

  return requestJson<EventIngestResponse>("/api/v1/events/manual", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify(input),
  });
}
