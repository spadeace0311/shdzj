const DEFAULT_API_HEALTH_URL = "http://127.0.0.1:8000";

function normalizeUrl(value?: string): string {
  return (value ?? "").trim().replace(/\/+$/, "");
}

export function resolveApiHealthUrl(
  healthUrl?: string,
  apiBaseUrl?: string,
): string {
  return normalizeUrl(healthUrl) || normalizeUrl(apiBaseUrl) || DEFAULT_API_HEALTH_URL;
}
