import react from "@vitejs/plugin-react";
import type { UserConfig } from "vite";

export default {
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://api:8000",
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
