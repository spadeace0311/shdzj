import react from "@vitejs/plugin-react";
import type { UserConfig } from "vite";

type ProcessEnvironment = {
  process?: {
    env?: Record<string, string | undefined>;
  };
};

const apiProxyTarget =
  (globalThis as ProcessEnvironment).process?.env?.VITE_API_PROXY_TARGET ||
  "http://api:8000";

export default {
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    css: true,
    include: ["./tests/**/*.{test,spec}.{ts,tsx}"],
  },
} satisfies UserConfig & {
  test: {
    environment: string;
    globals: boolean;
    setupFiles: string[];
    css: boolean;
    include: string[];
  };
};
