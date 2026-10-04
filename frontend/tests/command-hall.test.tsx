import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  Routes,
} from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import commandHallStyles from "../src/styles.css?raw";
import {
  ApiError,
  getCommandHallActiveEvent,
  getCommandHallGroup,
  getCommandHallOverview,
  getCommandHallTask,
  streamCommandHall,
} from "../src/api/client";
import { CommandHallPage } from "../src/pages/CommandHallPage";
import type {
  CommandHallGroup,
  CommandHallGroupDetail,
  CommandHallOverview,
  CommandHallStreamEvent,
  CommandHallTaskCounts,
  CommandHallTaskDetail,
} from "../src/types";

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return {
    ...actual,
    getCommandHallActiveEvent: vi.fn(),
    getCommandHallGroup: vi.fn(),
    getCommandHallOverview: vi.fn(),
    getCommandHallTask: vi.fn(),
    streamCommandHall: vi.fn(),
  };
});

const emptyCounts: CommandHallTaskCounts = {
  total: 0,
  pending: 0,
  in_progress: 0,
  pending_review: 0,
  completed: 0,
  not_required: 0,
  failed: 0,
  overdue: 0,
  at_risk: 0,
  dual_version_count: 0,
};

const groupDefinitions = [
  ["news_information", "新闻信息值守组"],
  ["monitoring_forecast", "监测预报组"],
  ["comprehensive_coordination", "综合协调组"],
  ["damage_assessment", "震害评估组"],
  ["emergency_technology", "应急技术组"],
  ["logistics", "后勤保障组"],
  ["center_station", "中心站组"],
] as const;

const hallTask = {
  id: "task-1",
  event_id: "event-1",
  workgroup_code: "monitoring_forecast",
  task_code: "trend_consultation",
  title: "趋势会商",
  instruction: "组织震后趋势会商并形成会商意见",
  status: "in_progress",
  timeliness_state: "on_time",
  due_at: "2026-10-03T02:00:00Z",
  phase_code: "within_30m",
  priority: 10,
  source_type: "preplan",
  source_ref: null,
  activated_at: "2026-10-03T01:05:00Z",
  completed_at: null,
  closed_at: null,
  row_version: 3,
  created_at: "2026-10-03T01:05:00Z",
  updated_at: "2026-10-03T01:10:00Z",
  contributors: [
    {
      user_id: "user-1",
      username: "预报员",
      contribution_count: 2,
      first_contributed_at: "2026-10-03T01:10:00Z",
      last_contributed_at: "2026-10-03T01:12:00Z",
    },
  ],
  deliverable_count: 1,
  required_deliverable_count: 1,
  satisfied_required_deliverable_count: 1,
  dual_version_deliverable_count: 1,
  latest_deliverable: {
    deliverable_id: "deliverable-1",
    deliverable_code: "trend_opinion",
    title: "趋势会商意见",
    published_by: "预报员",
    published_role: "group_leader",
    published_at: "2026-10-03T01:30:00Z",
    version_no: 2,
    source_kind: "manual",
  },
  current_publication: {
    deliverable_id: "deliverable-1",
    version_id: "version-2",
    published_by: "预报员",
    published_role: "group_leader",
    published_at: "2026-10-03T01:30:00Z",
    source_kind: "manual",
  },
  notification_status_counts: { sent: 1 },
};

function makeGroup(
  code: string,
  name: string,
  displayOrder: number,
): CommandHallGroup {
  const tasks = code === "monitoring_forecast" ? [hallTask] : [];
  return {
    event_id: "event-1",
    workgroup_code: code,
    name,
    display_order: displayOrder,
    roster_version: 1,
    roster_fingerprint: `fingerprint-${displayOrder}`,
    leader: {
      user_id: `leader-${displayOrder}`,
      username: `${name}组长`,
      duty_role: "leader",
    },
    deputies: [
      {
        user_id: `deputy-${displayOrder}`,
        username: `${name}副组长`,
        duty_role: "deputy",
        deputy_order: 1,
      },
    ],
    members: [],
    attendance: [
      {
        user_id: `leader-${displayOrder}`,
        state: code === "monitoring_forecast" ? "absent" : "present",
        duty_role_in_snapshot: "leader",
      },
      {
        user_id: `deputy-${displayOrder}`,
        state: code === "monitoring_forecast" ? "present" : "absent",
        duty_role_in_snapshot: "deputy",
        deputy_order_in_snapshot: 1,
      },
    ],
    confirming_authority:
      code === "monitoring_forecast"
        ? {
            user_id: "deputy-2",
            role: "deputy",
            deputy_order: 1,
          }
        : {
            user_id: `leader-${displayOrder}`,
            role: "leader",
            deputy_order: null,
          },
    tasks,
    task_count: tasks.length,
    task_counts:
      code === "monitoring_forecast"
        ? {
            ...emptyCounts,
            total: 1,
            in_progress: 1,
          }
        : emptyCounts,
    latest_deliverable:
      code === "monitoring_forecast"
        ? hallTask.latest_deliverable
        : null,
    alert_summary:
      code === "damage_assessment"
        ? { critical: 1 }
        : {},
    projection_version: 1,
    updated_at: "2026-10-03T01:31:00Z",
  };
}

const overview: CommandHallOverview = {
  event_id: "event-1",
  event: {
    event_id: "event-1",
    event_kind: "formal",
    event_type: "formal",
    place: "上海浦东新区",
    magnitude: 5.3,
    depth_km: 10,
    origin_time: "2026-10-03T01:00:00Z",
    t1_at: "2026-10-03T01:05:00Z",
    longitude: 121.88,
    latitude: 30.94,
    institutional_level: "general",
    service_level: 4,
    response_suggestion: {
      institutional_level: "general",
      service_level: 4,
    },
    lifecycle_state: "formal_triggered",
  },
  group_count: 7,
  groups: groupDefinitions.map(([code, name], index) =>
    makeGroup(code, name, index + 1),
  ),
  alerts: [
    {
      id: "alert-1",
      event_id: "event-1",
      workgroup_code: "damage_assessment",
      task_id: null,
      alert_key: "overdue:damage",
      alert_type: "task.overdue",
      severity: "critical",
      status: "open",
      title: "震害评估任务超时",
      detail: {},
      first_seen_at: "2026-10-03T01:20:00Z",
      resolved_at: null,
      updated_at: "2026-10-03T01:20:00Z",
    },
  ],
  task_counts: {
    ...emptyCounts,
    total: 1,
    in_progress: 1,
  },
  artifact_summary: {
    published_count: 1,
    status_counts: { complete: 1, degraded: 0, failed: 0 },
    latest_artifacts: [
      {
        publication_id: "publication-1",
        artifact_id: "artifact-1",
        artifact_key: "map.intensity",
        output_profile: "professional",
        artifact_version: 1,
        status: "complete",
        quality_grade: "A",
        production_mode: "live",
        publication_mode: "automatic",
        is_forced: false,
        published_at: "2026-10-03T01:20:00Z",
        file_name: "仪器烈度图.png",
        format: "png",
      },
    ],
  },
  alert_summary: { critical: 1 },
  dual_version_count: 1,
  projection_version: 1,
  sync_status: "current",
  projection_lag_seconds: 0,
  projection_source_updated_at: null,
  updated_at: "2026-10-03T01:31:00Z",
};

const groupDetail: CommandHallGroupDetail = {
  event_id: "event-1",
  workgroup_code: "monitoring_forecast",
  group: {
    ...makeGroup("monitoring_forecast", "监测预报组", 2),
  },
  tasks: [hallTask],
  alerts: [],
  alert_summary: {},
  task_counts: {
    ...emptyCounts,
    total: 1,
    in_progress: 1,
  },
  projection_version: 1,
  updated_at: "2026-10-03T01:31:00Z",
};

const taskDetail: CommandHallTaskDetail = {
  id: hallTask.id,
  event_id: hallTask.event_id,
  task: hallTask,
  contributors: hallTask.contributors,
  deliverables: [
    {
      id: "deliverable-1",
      task_id: hallTask.id,
      deliverable_code: "trend_opinion",
      title: "趋势会商意见",
      is_required: true,
      requirement_kind: "manual_file_or_text",
      artifact_binding: null,
      display_order: 1,
      current_publication: {
        id: "publication-2",
        deliverable_id: "deliverable-1",
        version_id: "version-2",
        published_by: "预报员",
        published_role: "group_leader",
        published_at: "2026-10-03T01:30:00Z",
        superseded_at: null,
        publication_note: "会商后发布",
      },
      versions: [
        {
          id: "version-1",
          deliverable_id: "deliverable-1",
          version_no: 1,
          source_kind: "automatic",
          artifact_id: "artifact-1",
          artifact_publication_id: "publication-1",
          storage_key: null,
          file_name: "趋势会商意见-自动版.docx",
          checksum: "a".repeat(64),
          mime_type:
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          size_bytes: 1024,
          text_result: null,
          created_by: "system",
          basis_text: null,
          supersedes_version_id: null,
          created_at: "2026-10-03T01:10:00Z",
        },
        {
          id: "version-2",
          deliverable_id: "deliverable-1",
          version_no: 2,
          source_kind: "manual",
          artifact_id: null,
          artifact_publication_id: null,
          storage_key: "collaboration/manual.docx",
          file_name: "趋势会商意见-人工修订版.docx",
          checksum: "b".repeat(64),
          mime_type:
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          size_bytes: 2048,
          text_result: null,
          created_by: "预报员",
          basis_text: "根据最新余震序列修订",
          supersedes_version_id: "version-1",
          created_at: "2026-10-03T01:30:00Z",
        },
        {
          id: "version-candidate",
          deliverable_id: "deliverable-1",
          version_no: 3,
          source_kind: "manual",
          artifact_id: null,
          artifact_publication_id: null,
          storage_key: "collaboration/candidate.docx",
          file_name: "未确认候选版.docx",
          checksum: "c".repeat(64),
          mime_type:
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          size_bytes: 3072,
          text_result: null,
          created_by: "预报员",
          basis_text: "尚未确认",
          supersedes_version_id: "version-2",
          created_at: "2026-10-03T01:40:00Z",
        },
      ],
    },
  ],
  task_events: [],
  notifications: [],
  projection_version: 1,
};

const activeEventMock = vi.mocked(getCommandHallActiveEvent);
const overviewMock = vi.mocked(getCommandHallOverview);
const groupMock = vi.mocked(getCommandHallGroup);
const taskMock = vi.mocked(getCommandHallTask);
const streamMock = vi.mocked(streamCommandHall);

function renderHall(entry = "/command-hall") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/command-hall" element={<CommandHallPage />} />
        <Route path="/command-hall/:eventId" element={<CommandHallPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  activeEventMock.mockResolvedValue({ event_id: "event-1" });
  overviewMock.mockResolvedValue(overview);
  groupMock.mockResolvedValue(groupDetail);
  taskMock.mockResolvedValue(taskDetail);
  streamMock.mockImplementation(() => new Promise<void>(() => undefined));
});

afterEach(() => {
  vi.useRealTimers();
});

test("renders seven stable group cards and opens the selected group drawer", async () => {
  renderHall();

  expect(await screen.findAllByTestId("command-hall-group-card")).toHaveLength(
    7,
  );
  expect(screen.getByText("新闻信息值守组")).toBeInTheDocument();
  expect(screen.getByText("中心站组")).toBeInTheDocument();

  fireEvent.click(
    screen.getByRole("button", { name: /监测预报组/ }),
  );

  expect(await screen.findByRole("dialog")).toBeInTheDocument();
  expect(await screen.findByText("趋势会商")).toBeInTheDocument();
  expect(
    await screen.findByText("代理负责人：监测预报组副组长"),
  ).toBeInTheDocument();
  expect(
    await screen.findByText("1. 监测预报组副组长"),
  ).toBeInTheDocument();
});

test("labels automatic, manual, and forced artifact publications from backend fields", async () => {
  overviewMock.mockResolvedValue({
    ...overview,
    artifact_summary: {
      ...overview.artifact_summary,
      published_count: 3,
      latest_artifacts: [
        {
          ...overview.artifact_summary.latest_artifacts[0],
          publication_id: "publication-auto",
          artifact_key: "map.intensity",
          production_mode: "live",
          publication_mode: "automatic",
          is_forced: false,
        },
        {
          ...overview.artifact_summary.latest_artifacts[0],
          publication_id: "publication-manual",
          artifact_key: "doc.damage",
          production_mode: "live",
          publication_mode: "rebuild",
          is_forced: false,
        },
        {
          ...overview.artifact_summary.latest_artifacts[0],
          publication_id: "publication-forced",
          artifact_key: "doc.decision",
          production_mode: "live",
          publication_mode: "superadmin_override",
          is_forced: true,
        },
      ],
    },
  });

  renderHall("/command-hall/event-1");

  expect(await screen.findByText("自动版 · complete")).toBeInTheDocument();
  expect(screen.getByText("人工修订版 · complete")).toBeInTheDocument();
  expect(
    screen.getByText("超级管理员覆盖版 · complete"),
  ).toBeInTheDocument();
});

test("opens task detail and only presents the current published version", async () => {
  renderHall();

  fireEvent.click(
    await screen.findByRole("button", { name: /监测预报组/ }),
  );
  fireEvent.click(
    await screen.findByRole("button", {
      name: "查看任务详情：趋势会商",
    }),
  );

  expect(
    await screen.findByText("组织震后趋势会商并形成会商意见"),
  ).toBeInTheDocument();
  expect(screen.getByText("当前发布版 · 人工修订版")).toBeInTheDocument();
  expect(screen.getByText(/发布人：预报员/)).toBeInTheDocument();
  expect(screen.queryByText("未确认候选版.docx")).not.toBeInTheDocument();
  expect(screen.queryByText("尚未确认")).not.toBeInTheDocument();
});

test("test event marker remains visible", async () => {
  activeEventMock.mockResolvedValue({ event_id: "event-1" });
  overviewMock.mockResolvedValue({
    ...overview,
    event: { ...overview.event, event_kind: "test", event_type: "test" },
  });

  renderHall();

  expect(await screen.findByTestId("command-hall-event-marker")).toHaveTextContent(
    "测试",
  );
});

test("an SSE update refreshes the overview", async () => {
  let onStreamEvent:
    | ((event: CommandHallStreamEvent) => void)
    | undefined;
  streamMock.mockImplementation((_eventId, callback) => {
    onStreamEvent = callback;
    return new Promise<void>(() => undefined);
  });
  overviewMock
    .mockResolvedValueOnce(overview)
    .mockResolvedValueOnce({
      ...overview,
      event: { ...overview.event, place: "上海浦东新区（更新）" },
      projection_version: 2,
    });

  renderHall();

  expect(await screen.findByText("上海浦东新区")).toBeInTheDocument();
  await waitFor(() => expect(onStreamEvent).toBeDefined());

  await act(async () => {
    onStreamEvent?.({
      type: "projection.updated",
      data: { event_id: "event-1", projection_version: 2 },
    });
  });

  expect(await screen.findByText("上海浦东新区（更新）")).toBeInTheDocument();
  expect(overviewMock).toHaveBeenCalledTimes(2);
});

test("SSE refresh updates an open group and task drawer without reopening it", async () => {
  let onStreamEvent:
    | ((event: CommandHallStreamEvent) => void)
    | undefined;
  const originalDeliverable = taskDetail.deliverables[0];
  const originalVersions =
    originalDeliverable.versions as Record<string, unknown>[];
  streamMock.mockImplementation((_eventId, callback) => {
    onStreamEvent = callback;
    return new Promise<void>(() => undefined);
  });
  overviewMock
    .mockResolvedValueOnce(overview)
    .mockResolvedValueOnce({
      ...overview,
      projection_version: 2,
      updated_at: "2026-10-03T01:32:00Z",
    });
  groupMock
    .mockResolvedValueOnce(groupDetail)
    .mockResolvedValueOnce({
      ...groupDetail,
      projection_version: 2,
      updated_at: "2026-10-03T01:32:00Z",
      tasks: [
        {
          ...hallTask,
          title: "趋势会商（更新）",
        },
      ],
    });
  taskMock
    .mockResolvedValueOnce(taskDetail)
    .mockResolvedValueOnce({
      ...taskDetail,
      projection_version: 2,
      task: {
        ...hallTask,
        instruction: "使用最新余震序列重新会商",
      },
      deliverables: [
        {
          ...originalDeliverable,
          current_publication: {
            ...(originalDeliverable.current_publication as Record<
              string,
              unknown
            >),
            version_id: "version-override",
          },
          versions: [
            {
              ...originalVersions[1],
              id: "version-override",
              source_kind: "superadmin_override",
              file_name: "趋势会商意见-覆盖版.docx",
            },
          ],
        },
      ],
    });

  renderHall("/command-hall/event-1");

  fireEvent.click(
    await screen.findByRole("button", { name: /监测预报组/ }),
  );
  fireEvent.click(
    await screen.findByRole("button", {
      name: "查看任务详情：趋势会商",
    }),
  );
  expect(
    await screen.findByText("组织震后趋势会商并形成会商意见"),
  ).toBeInTheDocument();

  await waitFor(() => expect(onStreamEvent).toBeDefined());
  await act(async () => {
    onStreamEvent?.({
      type: "projection.updated",
      data: { event_id: "event-1", projection_version: 2 },
    });
  });

  expect(
    await screen.findByText("使用最新余震序列重新会商"),
  ).toBeInTheDocument();
  expect(screen.getByText("当前发布版 · 超级管理员覆盖版")).toBeInTheDocument();
  expect(groupMock).toHaveBeenCalledTimes(2);
  expect(taskMock).toHaveBeenCalledTimes(2);
});

test("SSE refresh failures keep the last valid group and task details visible", async () => {
  let onStreamEvent:
    | ((event: CommandHallStreamEvent) => void)
    | undefined;
  streamMock.mockImplementation((_eventId, callback) => {
    onStreamEvent = callback;
    return new Promise<void>(() => undefined);
  });
  overviewMock
    .mockResolvedValueOnce(overview)
    .mockResolvedValueOnce({
      ...overview,
      projection_version: 2,
      updated_at: "2026-10-03T01:32:00Z",
    });
  groupMock
    .mockResolvedValueOnce(groupDetail)
    .mockRejectedValueOnce(new ApiError("group unavailable", 503));
  taskMock
    .mockResolvedValueOnce(taskDetail)
    .mockRejectedValueOnce(new ApiError("task unavailable", 503));

  renderHall("/command-hall/event-1");

  fireEvent.click(
    await screen.findByRole("button", { name: /监测预报组/ }),
  );
  fireEvent.click(
    await screen.findByRole("button", {
      name: "查看任务详情：趋势会商",
    }),
  );
  expect(
    await screen.findByText("组织震后趋势会商并形成会商意见"),
  ).toBeInTheDocument();

  await waitFor(() => expect(onStreamEvent).toBeDefined());
  await act(async () => {
    onStreamEvent?.({
      type: "projection.updated",
      data: { event_id: "event-1", projection_version: 2 },
    });
  });

  const warnings = await screen.findAllByText(
    "详情同步失败，当前为最后有效版本",
  );
  expect(warnings).toHaveLength(2);
  expect(screen.getByTestId("command-hall-group-sync-warning")).toHaveTextContent(
    /最后有效更新：.*09:31:00/,
  );
  expect(screen.getByTestId("command-hall-task-sync-warning")).toHaveTextContent(
    /最后有效更新：.*09:10:00/,
  );
  expect(
    screen.getByRole("heading", { name: "趋势会商" }),
  ).toBeInTheDocument();
  expect(
    screen.getAllByText("组织震后趋势会商并形成会商意见"),
  ).toHaveLength(2);
});

test("five-second polling refresh also updates an open group and task drawer", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  streamMock.mockRejectedValue(new ApiError("事件流不可用", 0));
  overviewMock
    .mockResolvedValueOnce(overview)
    .mockResolvedValueOnce({
      ...overview,
      projection_version: 2,
    });
  groupMock
    .mockResolvedValueOnce(groupDetail)
    .mockResolvedValueOnce({
      ...groupDetail,
      projection_version: 2,
      tasks: [
        {
          ...hallTask,
          title: "趋势会商（轮询更新）",
        },
      ],
    });
  taskMock
    .mockResolvedValueOnce(taskDetail)
    .mockResolvedValueOnce({
      ...taskDetail,
      projection_version: 2,
      task: {
        ...hallTask,
        instruction: "轮询后的最新任务说明",
      },
    });

  renderHall("/command-hall/event-1");

  fireEvent.click(
    await screen.findByRole("button", { name: /监测预报组/ }),
  );
  fireEvent.click(
    await screen.findByRole("button", {
      name: "查看任务详情：趋势会商",
    }),
  );
  expect(
    await screen.findByText("组织震后趋势会商并形成会商意见"),
  ).toBeInTheDocument();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000);
  });

  expect(await screen.findByText("轮询后的最新任务说明")).toBeInTheDocument();
  expect(groupMock).toHaveBeenCalledTimes(2);
  expect(taskMock).toHaveBeenCalledTimes(2);
});

test("five-second polling refresh failures retain stale group and task details", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  streamMock.mockRejectedValue(new ApiError("事件流不可用", 0));
  overviewMock
    .mockResolvedValueOnce(overview)
    .mockResolvedValueOnce({
      ...overview,
      projection_version: 2,
      updated_at: "2026-10-03T01:32:00Z",
    });
  groupMock
    .mockResolvedValueOnce(groupDetail)
    .mockRejectedValueOnce(new ApiError("group unavailable", 503));
  taskMock
    .mockResolvedValueOnce(taskDetail)
    .mockRejectedValueOnce(new ApiError("task unavailable", 503));

  renderHall("/command-hall/event-1");

  fireEvent.click(
    await screen.findByRole("button", { name: /监测预报组/ }),
  );
  fireEvent.click(
    await screen.findByRole("button", {
      name: "查看任务详情：趋势会商",
    }),
  );
  expect(
    await screen.findByText("组织震后趋势会商并形成会商意见"),
  ).toBeInTheDocument();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000);
  });

  const warnings = await screen.findAllByText(
    "详情同步失败，当前为最后有效版本",
  );
  expect(warnings).toHaveLength(2);
  expect(screen.getByTestId("command-hall-group-sync-warning")).toHaveTextContent(
    /最后有效更新：.*09:31:00/,
  );
  expect(screen.getByTestId("command-hall-task-sync-warning")).toHaveTextContent(
    /最后有效更新：.*09:10:00/,
  );
  expect(
    screen.getByRole("heading", { name: "趋势会商" }),
  ).toBeInTheDocument();
  expect(
    screen.getAllByText("组织震后趋势会商并形成会商意见"),
  ).toHaveLength(2);
});

test("SSE failure falls back to five-second overview polling", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  streamMock.mockRejectedValue(new ApiError("事件流不可用", 0));

  renderHall("/command-hall/event-1");

  await waitFor(() => expect(overviewMock).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(streamMock).toHaveBeenCalledTimes(1));

  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000);
  });

  expect(overviewMock.mock.calls.length).toBeGreaterThanOrEqual(2);
});

test("elapsed duration continues ticking without a projection update", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-10-03T01:00:05Z"));
  overviewMock.mockResolvedValue({
    ...overview,
    event: {
      ...overview.event,
      origin_time: "2026-10-03T01:00:00Z",
    },
  });

  renderHall("/command-hall/event-1");

  expect(await screen.findByText("5秒")).toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_000);
  });
  expect(screen.getByText("8秒")).toBeInTheDocument();
});

test("keeps probing active event after the empty state", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  activeEventMock
    .mockResolvedValueOnce({ event_id: null })
    .mockResolvedValue({ event_id: "event-1" });

  renderHall();

  expect(await screen.findByText("当前没有活跃事件")).toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000);
  });

  expect(await screen.findByText("上海浦东新区")).toBeInTheDocument();
  expect(activeEventMock).toHaveBeenCalledTimes(2);
});

test("switches from a test event to a newly active formal event", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  activeEventMock
    .mockResolvedValueOnce({ event_id: "test-event" })
    .mockResolvedValue({ event_id: "formal-event" });
  overviewMock.mockImplementation(async (eventId) => {
    if (eventId === "formal-event") {
      return {
        ...overview,
        event_id: "formal-event",
        event: {
          ...overview.event,
          event_id: "formal-event",
          event_kind: "formal",
          event_type: "formal",
          place: "正式事件震中",
        },
        groups: [],
      };
    }
    return {
      ...overview,
      event_id: "test-event",
      event: {
        ...overview.event,
        event_id: "test-event",
        event_kind: "test",
        event_type: "test",
        place: "测试事件震中",
      },
    };
  });

  renderHall();

  expect(await screen.findByText("测试事件震中")).toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000);
  });

  expect(await screen.findByText("正式事件震中")).toBeInTheDocument();
  expect(overviewMock).toHaveBeenCalledWith("test-event");
  expect(overviewMock).toHaveBeenCalledWith("formal-event");
});

test("shows syncing state and projection lag alert after five seconds", async () => {
  overviewMock.mockResolvedValue({
    ...overview,
    sync_status: "syncing",
    projection_lag_seconds: 5.25,
    projection_source_updated_at: "2026-10-03T01:25:44.750Z",
    alerts: [
      ...overview.alerts,
      {
        id: "projection-lag",
        event_id: "event-1",
        workgroup_code: null,
        task_id: null,
        alert_key: "projection.lag",
        alert_type: "projection.lag",
        severity: "warning",
        status: "open",
        title: "数据同步中",
        detail: { projection_lag_seconds: 5.25 },
        first_seen_at: "2026-10-03T01:25:44.750Z",
        resolved_at: null,
        updated_at: "2026-10-03T01:25:50Z",
      },
    ],
  });

  renderHall("/command-hall/event-1");

  expect(
    await screen.findByTestId("command-hall-sync-status"),
  ).toHaveTextContent("数据同步中");
  expect(screen.getByText("投影延迟")).toBeInTheDocument();
});

test("keeps the current sync state at the five-second threshold", async () => {
  overviewMock.mockResolvedValue({
    ...overview,
    sync_status: "current",
    projection_lag_seconds: 5,
    projection_source_updated_at: "2026-10-03T01:30:55Z",
  });

  renderHall("/command-hall/event-1");

  expect(
    await screen.findByTestId("command-hall-sync-status"),
  ).toHaveTextContent("数据已同步");
  expect(screen.queryByText("数据同步中")).not.toBeInTheDocument();
});

test("unmount aborts the command hall stream", async () => {
  let signal: AbortSignal | undefined;
  streamMock.mockImplementation((_eventId, _callback, requestSignal) => {
    signal = requestSignal;
    return new Promise<void>(() => undefined);
  });

  const view = renderHall("/command-hall/event-1");
  await waitFor(() => expect(signal).toBeDefined());

  view.unmount();

  expect(signal?.aborted).toBe(true);
});

test("the hall and drill-down expose no mutation controls", async () => {
  renderHall("/command-hall/event-1");

  fireEvent.click(
    await screen.findByRole("button", { name: /监测预报组/ }),
  );
  expect(await screen.findByText("趋势会商")).toBeInTheDocument();

  expect(
    screen.queryByRole("button", {
      name: /开始处理|提交成果|确认完成|发布此版本|上传候选文件|创建临时任务/,
    }),
  ).not.toBeInTheDocument();
});

test("shows a stable no-active-event state", async () => {
  activeEventMock.mockResolvedValue({ event_id: null });

  renderHall();

  expect(await screen.findByText("当前没有活跃事件")).toBeInTheDocument();
});

test("maps forbidden overview access to a stable error state", async () => {
  overviewMock.mockRejectedValue(new ApiError("forbidden", 403));

  renderHall("/command-hall/event-1");

  expect(
    await screen.findByText("当前账号无权查看该事件指挥大厅"),
  ).toBeInTheDocument();
  expect(screen.getByRole("alert")).toBeInTheDocument();
});

test("uses full-height target-display tracks and a wide drill-down drawer", () => {
  const hallRule = commandHallStyles.match(
    /\.command-hall\s*\{([\s\S]*?)\n\}/,
  )?.[1];
  expect(hallRule).toContain("height: 100vh;");
  expect(hallRule).toContain("box-sizing: border-box;");
  expect(hallRule).toContain("min-height: 0;");
  expect(commandHallStyles).toMatch(
    /\.command-hall-drawer\s*\{[\s\S]*?width:\s*clamp\(820px,\s*52vw,\s*3200px\);/,
  );
  expect(commandHallStyles).toMatch(/@media \(max-height:\s*1200px\)/);
});
