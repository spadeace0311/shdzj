import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { ApiError, createManualEvent } from "../src/api/client";
import { ManualEventPage } from "../src/pages/ManualEventPage";

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return {
    ...actual,
    createManualEvent: vi.fn(),
  };
});

const createManualEventMock = vi.mocked(createManualEvent);

function renderManualPage() {
  return render(
    <MemoryRouter>
      <ManualEventPage />
    </MemoryRouter>,
  );
}

function fillRequiredFields() {
  fireEvent.change(screen.getByLabelText("发震时刻"), {
    target: { value: "2026-09-17T10:30" },
  });
  fireEvent.change(screen.getByLabelText("经度"), { target: { value: "121.54" } });
  fireEvent.change(screen.getByLabelText("纬度"), { target: { value: "31.22" } });
  fireEvent.change(screen.getByLabelText("震级"), { target: { value: "3.2" } });
  fireEvent.change(screen.getByLabelText("震源深度"), { target: { value: "8" } });
  fireEvent.change(screen.getByLabelText("数据来源"), {
    target: { value: "shanghai-network" },
  });
}

beforeEach(() => {
  createManualEventMock.mockReset();
});

test("submits all required manual event fields", async () => {
  createManualEventMock.mockResolvedValue({ event_id: "event-2" });

  renderManualPage();
  fillRequiredFields();
  fireEvent.click(screen.getByRole("button", { name: "启动评估" }));

  expect(
    await screen.findByText("人工地震事件已提交并启动评估"),
  ).toBeInTheDocument();
  expect(createManualEventMock).toHaveBeenCalledWith({
    origin_time: "2026-09-17T10:30:00+08:00",
    longitude: "121.54",
    latitude: "31.22",
    magnitude: "3.2",
    depth_km: "8",
    source: "shanghai-network",
    event_kind: "manual",
  });
});

test("shows a clear failure state when submission fails", async () => {
  createManualEventMock.mockRejectedValue(new Error("validation failed"));

  renderManualPage();
  fillRequiredFields();
  fireEvent.click(screen.getByRole("button", { name: "启动评估" }));

  expect(await screen.findByText("网络连接失败，请稍后重试")).toBeInTheDocument();
});

test.each([
  [401, "登录状态已失效，请重新登录"],
  [403, "当前账号无权创建人工事件"],
  [404, "事件接口不存在"],
  [422, "输入数据不合法，请检查数值范围"],
  [503, "事件存储服务暂不可用，请稍后重试"],
  [408, "请求超时，请稍后重试"],
  [0, "网络连接失败，请稍后重试"],
] as const)("maps status %s to a clear submission error", async (status, message) => {
  createManualEventMock.mockRejectedValue(new ApiError("backend detail", status));

  renderManualPage();
  fillRequiredFields();
  fireEvent.click(screen.getByRole("button", { name: "启动评估" }));

  expect(await screen.findByText(message)).toBeInTheDocument();
});
