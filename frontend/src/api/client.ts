import type {
  EventDetail,
  EventIngestResponse,
  EventSummary,
  ManualEventInput,
  TokenResponse,
} from "../types";

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

async function parseError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload.detail === "string") {
      return payload.detail;
    }
  } catch {
    // Ignore non-JSON error bodies and use the HTTP status fallback below.
  }
  return `请求失败（HTTP ${response.status}）`;
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    throw new ApiError(await parseError(response), response.status);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export async function login(username: string, password: string): Promise<TokenResponse> {
  const body = new URLSearchParams({ username, password });
  const response = await fetch("/api/v1/auth/login", {
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
