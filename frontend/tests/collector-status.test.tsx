import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import { getCollectorStatus } from "../src/api/client";
import { CollectorStatusPage } from "../src/pages/CollectorStatusPage";

vi.mock("../src/api/client", () => ({
  getCollectorStatus: vi.fn(),
}));

const getCollectorStatusMock = vi.mocked(getCollectorStatus);

const degradedStatus = {
  overall_state: "degraded" as const,
  providers: [
    {
      provider: "fan" as const,
      state: "degraded" as const,
      connected: false,
      last_http_status: null,
      last_connected_at: "2026-09-25T01:00:00Z",
      last_message_at: "2026-09-25T01:04:00Z",
      last_success_at: "2026-09-25T01:04:00Z",
      consecutive_failures: 3,
      reconnect_count: 4,
      last_error: "connection reset",
      updated_at: "2026-09-25T01:05:00Z",
    },
    {
      provider: "wolfx" as const,
      state: "healthy" as const,
      connected: true,
      last_http_status: 200,
      last_connected_at: "2026-09-25T01:05:00Z",
      last_message_at: "2026-09-25T01:05:10Z",
      last_success_at: "2026-09-25T01:05:10Z",
      consecutive_failures: 0,
      reconnect_count: 0,
      last_error: null,
      updated_at: "2026-09-25T01:05:10Z",
    },
  ],
  open_dead_letter_count: 2,
  boundary_version: "shanghai-public-2026.1",
  last_ingested_event_id: "event-1",
};

beforeEach(() => {
  getCollectorStatusMock.mockReset();
});

test("renders primary and backup provider states", async () => {
  getCollectorStatusMock.mockResolvedValue(degradedStatus);

  render(<CollectorStatusPage />);

  expect(await screen.findByText("总体状态：降级")).toBeInTheDocument();
  expect(screen.getByText("FAN 主链路")).toBeInTheDocument();
  expect(screen.getByText("Wolfx 备用链路")).toBeInTheDocument();
  expect(screen.getByText("待处理死信 2 条")).toBeInTheDocument();
  expect(screen.getByText("connection reset")).toBeInTheDocument();
});

test("shows a retry state when the request fails", async () => {
  getCollectorStatusMock
    .mockRejectedValueOnce(new Error("network unavailable"))
    .mockResolvedValueOnce(degradedStatus);

  render(<CollectorStatusPage />);

  expect(await screen.findByText("无法加载采集状态")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));

  expect(await screen.findByText("总体状态：降级")).toBeInTheDocument();
  expect(getCollectorStatusMock).toHaveBeenCalledTimes(2);
});

test("manually refreshes collector state", async () => {
  getCollectorStatusMock
    .mockResolvedValueOnce({ ...degradedStatus, overall_state: "healthy" })
    .mockResolvedValueOnce(degradedStatus);

  render(<CollectorStatusPage />);

  expect(await screen.findByText("总体状态：正常")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));

  expect(await screen.findByText("总体状态：降级")).toBeInTheDocument();
  await waitFor(() => expect(getCollectorStatusMock).toHaveBeenCalledTimes(2));
});
