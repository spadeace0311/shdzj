import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import {
  getOperationsOverview,
  getOperationsSettings,
  resumeOperationsDrill,
  triggerOperationsDrill,
  triggerOperationsTest,
  updateOperationsSettings,
} from "../api/client";
import {
  OperationsPage,
  type OperationsOverview,
  type OperationsSettings,
  type OperationsTriggerResult,
} from "../pages/OperationsPage";

vi.mock("../api/client", () => ({
  getOperationsOverview: vi.fn(),
  getOperationsSettings: vi.fn(),
  updateOperationsSettings: vi.fn(),
  triggerOperationsTest: vi.fn(),
  triggerOperationsDrill: vi.fn(),
  resumeOperationsDrill: vi.fn(),
}));

const getOverviewMock = vi.mocked(getOperationsOverview);
const getSettingsMock = vi.mocked(getOperationsSettings);
const updateSettingsMock = vi.mocked(updateOperationsSettings);
const triggerTestMock = vi.mocked(triggerOperationsTest);
const triggerDrillMock = vi.mocked(triggerOperationsDrill);
const resumeDrillMock = vi.mocked(resumeOperationsDrill);

const settings: OperationsSettings = {
  row_version: 4,
  daily_test_enabled: true,
  timezone: "Asia/Shanghai",
  daily_window_start: "02:00:00",
  daily_window_end: "04:00:00",
  test_magnitude_min: "5.0",
  test_magnitude_max: "5.9",
  test_depth_min_km: "5.00",
  test_depth_max_km: "20.00",
  drill_conflict_policy: "pause",
  updated_by: "operator",
  updated_at: "2026-10-11T00:00:00Z",
};

const overview: OperationsOverview = {
  observed_at: "2026-10-11T00:00:00Z",
  real_event_priority_active: false,
  settings,
  next_schedule: {
    id: "schedule-next",
    local_date: "2026-10-12",
    window_start: "2026-10-11T18:00:00Z",
    window_end: "2026-10-11T20:00:00Z",
    scheduled_at: "2026-10-11T18:00:00Z",
    state: "scheduled",
    scenario: null,
    random_seed: 1,
    event_id: null,
    failure_reason: null,
    triggered_at: null,
    created_at: "2026-10-11T00:00:00Z",
    updated_at: "2026-10-11T00:00:00Z",
  },
  today_schedule: {
    id: "schedule-today",
    local_date: "2026-10-11",
    window_start: "2026-10-10T18:00:00Z",
    window_end: "2026-10-10T20:00:00Z",
    scheduled_at: "2026-10-10T18:00:00Z",
    state: "triggered",
    scenario: {
      magnitude: "5.4",
      depth_km: "9.37",
      longitude: "121.4737",
      latitude: "31.2304",
      place: "上海市（系统测试）",
      source_event_id: "operations-test:scheduled",
      random_seed: 1,
      origin_time: "2026-10-10T18:00:00Z",
    },
    random_seed: 1,
    event_id: "event-scheduled",
    failure_reason: null,
    triggered_at: "2026-10-10T18:00:00Z",
    created_at: "2026-10-10T00:00:00Z",
    updated_at: "2026-10-10T18:00:00Z",
  },
  recent_runs: [
    {
      id: "run-1",
      event_id: "event-scheduled",
      schedule_id: "schedule-today",
      trigger_mode: "scheduled_test",
      operation_state: "completed",
      priority: 10,
      idempotency_key: "scheduled:schedule-today",
      triggered_by: "operations-worker",
      resume_required: false,
      detail: { revision_id: "revision-1" },
      started_at: "2026-10-10T18:00:00Z",
      paused_at: null,
      completed_at: "2026-10-10T18:05:00Z",
      canceled_at: null,
      created_at: "2026-10-10T18:00:00Z",
      updated_at: "2026-10-10T18:05:00Z",
    },
  ],
  active_run_count: 0,
};

const triggerResult = (mode: OperationsTriggerResult["trigger_mode"]) =>
  ({
    event_id: `event-${mode}`,
    run_id: `run-${mode}`,
    revision_id: `revision-${mode}`,
    trigger_mode: mode,
    operation_state: "queued",
    outcome: "triggered",
    idempotency_key: "uuid-key",
    event_kind: mode === "drill" ? "drill" : "test",
    triggered_assessment: true,
    resumed_from_run_id: null,
  }) satisfies OperationsTriggerResult;

function renderPage(userRole = "superadmin") {
  return render(
    <MemoryRouter>
      <OperationsPage userRole={userRole} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  getOverviewMock.mockReset();
  getSettingsMock.mockReset();
  updateSettingsMock.mockReset();
  triggerTestMock.mockReset();
  triggerDrillMock.mockReset();
  resumeDrillMock.mockReset();
  getOverviewMock.mockResolvedValue(overview);
  getSettingsMock.mockResolvedValue(settings);
  updateSettingsMock.mockImplementation(async (input) => ({
    ...settings,
    row_version: settings.row_version + 1,
    ...input,
    updated_by: "superadmin",
  }));
});

test("shows the next daily test, today plan, and recent runs", async () => {
  renderPage();

  expect(await screen.findByText("下一次每日测试")).toBeInTheDocument();
  expect(screen.getByText("今日计划")).toBeInTheDocument();
  expect(screen.getByText("最近运行")).toBeInTheDocument();
  expect(screen.getByText("已触发")).toBeInTheDocument();
  expect(screen.getByText("scheduled_test")).toBeInTheDocument();
});

test("shows a random Shanghai 5.0-5.9 test scenario and calls the operations API", async () => {
  triggerTestMock.mockResolvedValue(triggerResult("manual_test"));
  renderPage();

  expect(await screen.findByText(/随机上海 5\.0-5\.9 级测试场景/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "手工测试" }));

  expect(await screen.findByText(/手工测试已触发/)).toBeInTheDocument();
  expect(triggerTestMock).toHaveBeenCalledTimes(1);
  expect(triggerTestMock.mock.calls[0][0]).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
  );
});

test("superadmin can resume a paused drill run from the run table", async () => {
  getOverviewMock.mockResolvedValue({
    ...overview,
    recent_runs: [
      {
        ...overview.recent_runs[0],
        id: "paused-drill",
        trigger_mode: "drill",
        operation_state: "paused",
        resume_required: true,
      },
    ],
  });
  resumeDrillMock.mockResolvedValue({
    ...triggerResult("drill"),
    resumed_from_run_id: "paused-drill",
  });
  renderPage();

  fireEvent.click(
    await screen.findByRole("button", { name: "恢复演练" }),
  );

  expect(await screen.findByText(/演练恢复已触发/)).toBeInTheDocument();
  expect(resumeDrillMock).toHaveBeenCalledWith("paused-drill");
});

test("reuses the same idempotency key when the same request is retried", async () => {
  triggerTestMock
    .mockRejectedValueOnce(new Error("network unavailable"))
    .mockResolvedValueOnce(triggerResult("manual_test"));
  renderPage();

  fireEvent.click(
    await screen.findByRole("button", { name: "手工测试" }),
  );

  expect(await screen.findByText(/手工测试已触发/)).toBeInTheDocument();
  expect(triggerTestMock).toHaveBeenCalledTimes(2);
  expect(triggerTestMock.mock.calls[0][0]).toBe(triggerTestMock.mock.calls[1][0]);
});

test("generates a new idempotency key when the user clicks again", async () => {
  triggerTestMock
    .mockRejectedValueOnce(new Error("network unavailable"))
    .mockRejectedValueOnce(new Error("network unavailable"))
    .mockResolvedValueOnce(triggerResult("manual_test"));
  renderPage();

  const button = await screen.findByRole("button", { name: "手工测试" });
  fireEvent.click(button);
  await screen.findByText("操作未完成，可使用上次请求重试");
  fireEvent.click(button);

  expect(await screen.findByText(/手工测试已触发/)).toBeInTheDocument();
  expect(triggerTestMock).toHaveBeenCalledTimes(3);
  expect(triggerTestMock.mock.calls[0][0]).toBe(triggerTestMock.mock.calls[1][0]);
  expect(triggerTestMock.mock.calls[2][0]).not.toBe(triggerTestMock.mock.calls[1][0]);
});

test("triggers a drill and keeps the permanent drill identifier visible", async () => {
  triggerDrillMock.mockResolvedValue(triggerResult("drill"));
  renderPage();

  expect(await screen.findByText("演练标识：不可移除")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "演练测试" }));

  expect(await screen.findByText(/演练测试已触发/)).toBeInTheDocument();
  expect(triggerDrillMock).toHaveBeenCalledTimes(1);
  expect(screen.getByText("演练标识：不可移除")).toBeInTheDocument();
});

test.each([
  ["deferred", "演练测试已延后"],
  ["canceled", "演练测试已终止"],
] as const)(
  "shows %s drill feedback without a fake run number when there is no run",
  async (state, expectedText) => {
    triggerDrillMock.mockResolvedValue({
      ...triggerResult("drill"),
      event_id: null,
      run_id: null,
      revision_id: null,
      operation_state: state,
      outcome: state,
      triggered_assessment: false,
    });
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "演练测试" }),
    );

    expect(await screen.findByText(expectedText)).toBeInTheDocument();
    expect(screen.queryByText(/运行编号/)).not.toBeInTheDocument();
    expect(triggerDrillMock).toHaveBeenCalledTimes(1);
  },
);

test("disables manual test and shows the priority protection reason during a real event", async () => {
  getOverviewMock.mockResolvedValue({
    ...overview,
    real_event_priority_active: true,
  });
  renderPage();

  expect(await screen.findByText("真实事件优先保护中")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "手工测试" })).toBeDisabled();
});

test("allows superadmins to edit window, magnitude, depth, and conflict policy", async () => {
  renderPage("superadmin");

  const settingsSection = await screen.findByRole("region", {
    name: "运维设置",
  });
  fireEvent.change(within(settingsSection).getByLabelText("窗口开始"), {
    target: { value: "03:00" },
  });
  fireEvent.change(within(settingsSection).getByLabelText("窗口结束"), {
    target: { value: "05:00" },
  });
  fireEvent.change(within(settingsSection).getByLabelText("震级下限"), {
    target: { value: "5.2" },
  });
  fireEvent.change(within(settingsSection).getByLabelText("震级上限"), {
    target: { value: "6.1" },
  });
  fireEvent.change(within(settingsSection).getByLabelText("深度下限"), {
    target: { value: "6.00" },
  });
  fireEvent.change(within(settingsSection).getByLabelText("深度上限"), {
    target: { value: "22.00" },
  });
  fireEvent.change(within(settingsSection).getByLabelText("冲突策略"), {
    target: { value: "terminate" },
  });
  fireEvent.click(within(settingsSection).getByRole("button", { name: "保存设置" }));

  await waitFor(() => expect(updateSettingsMock).toHaveBeenCalledTimes(1));
  expect(updateSettingsMock).toHaveBeenCalledWith({
    expected_version: 4,
    daily_window_start: "03:00:00",
    daily_window_end: "05:00:00",
    test_magnitude_min: "5.2",
    test_magnitude_max: "6.1",
    test_depth_min_km: "6.00",
    test_depth_max_km: "22.00",
    drill_conflict_policy: "terminate",
  });
});

test("hides settings edit controls from non-superadmins", async () => {
  renderPage("group_leader");

  await screen.findByText("下一次每日测试");
  expect(screen.queryByRole("region", { name: "运维设置" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "保存设置" })).not.toBeInTheDocument();
});

test("shows stable error and empty texts without staying in a loading state", async () => {
  getOverviewMock.mockResolvedValue({
    ...overview,
    next_schedule: null,
    today_schedule: null,
    recent_runs: [],
  });
  renderPage();

  expect(await screen.findByText("暂无下一次每日测试计划")).toBeInTheDocument();
  expect(screen.getByText("今日暂无计划")).toBeInTheDocument();
  expect(screen.getByText("暂无最近运行记录")).toBeInTheDocument();
  expect(screen.queryByText("正在加载运维状态")).not.toBeInTheDocument();
});

test("shows a retry state when the overview request fails", async () => {
  getOverviewMock
    .mockRejectedValueOnce(new Error("network unavailable"))
    .mockResolvedValueOnce(overview);
  renderPage();

  expect(await screen.findByText("无法加载运维状态")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));

  expect(await screen.findByText("下一次每日测试")).toBeInTheDocument();
  expect(getOverviewMock).toHaveBeenCalledTimes(2);
});
