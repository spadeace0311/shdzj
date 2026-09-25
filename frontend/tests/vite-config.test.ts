// @vitest-environment node

import { afterEach, describe, expect, it, vi } from "vitest";

type ViteConfig = {
  server: {
    proxy: {
      "/api": {
        target: string;
      };
    };
  };
};

async function loadViteConfig() {
  vi.resetModules();
  return (await import("../vite.config")).default as ViteConfig;
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("Vite API proxy target", () => {
  it("uses VITE_API_PROXY_TARGET when it is set", async () => {
    vi.stubEnv("VITE_API_PROXY_TARGET", "http://127.0.0.1:8000");

    const config = await loadViteConfig();

    expect(config.server.proxy["/api"].target).toBe("http://127.0.0.1:8000");
  });

  it("defaults to the Compose API service when the variable is not set", async () => {
    vi.stubEnv("VITE_API_PROXY_TARGET", undefined);

    const config = await loadViteConfig();

    expect(config.server.proxy["/api"].target).toBe("http://api:8000");
  });
});
