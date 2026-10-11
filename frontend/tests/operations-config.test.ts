import { describe, expect, it } from "vitest";

import { resolveApiHealthUrl } from "../e2e/operations.config";

describe("resolveApiHealthUrl", () => {
  it("uses the local API default when both values are unset", () => {
    expect(resolveApiHealthUrl()).toBe("http://127.0.0.1:8000");
  });

  it("falls back from empty or whitespace health URL to API base URL", () => {
    expect(resolveApiHealthUrl("", "http://api.example:8000")).toBe(
      "http://api.example:8000",
    );
    expect(resolveApiHealthUrl("   ", "http://api.example:8000")).toBe(
      "http://api.example:8000",
    );
  });

  it("falls back to the local API default when all values are empty", () => {
    expect(resolveApiHealthUrl("", "")).toBe("http://127.0.0.1:8000");
    expect(resolveApiHealthUrl("   ", "   ")).toBe("http://127.0.0.1:8000");
  });

  it("prefers an explicit health URL over the API base URL", () => {
    expect(
      resolveApiHealthUrl(
        "http://health.example:9000",
        "http://api.example:8000",
      ),
    ).toBe("http://health.example:9000");
  });

  it("trims explicit values and removes trailing slashes", () => {
    expect(
      resolveApiHealthUrl(
        "  http://health.example:9000///  ",
        "http://api.example:8000",
      ),
    ).toBe("http://health.example:9000");
    expect(
      resolveApiHealthUrl("", "  http://api.example:8000///  "),
    ).toBe("http://api.example:8000");
  });
});
