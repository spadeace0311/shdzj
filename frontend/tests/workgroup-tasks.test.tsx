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
  useNavigate,
} from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import {
  ApiError,
  addCollaborationTextVersion,
  cancelCollaborationTask,
  completeCollaborationTask,
  createTemporaryTask,
  getCollaborationTask,
  getCommandHallTask,
  getEvent,
  listCollaborationTasks,
  publishCollaborationDeliverableVersion,
  returnCollaborationTask,
  setAccessToken,
  startCollaborationTask,
  submitCollaborationTask,
  uploadCollaborationDeliverableVersion,
} from "../src/api/client";
import { WorkgroupTasksPage } from "../src/pages/WorkgroupTasksPage";
import type {
  CommandHallTaskDetail,
  EventDetail,
  WorkgroupTask,
} from "../src/types";

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return {
    ...actual,
    addCollaborationTextVersion: vi.fn(),
    cancelCollaborationTask: vi.fn(),
    completeCollaborationTask: vi.fn(),
    createTemporaryTask: vi.fn(),
    getCollaborationTask: vi.fn(),
    getCommandHallTask: vi.fn(),
    getEvent: vi.fn(),
    listCollaborationTasks: vi.fn(),
    publishCollaborationDeliverableVersion: vi.fn(),
    returnCollaborationTask: vi.fn(),
    startCollaborationTask: vi.fn(),
    submitCollaborationTask: vi.fn(),
    uploadCollaborationDeliverableVersion: vi.fn(),
  };
});

const event: EventDetail = {
  id: "event-1",
  source: "CENC",
  place: "上海浦东新区",
  magnitude: 5.3,
  depth_km: 10,
  origin_time: "2026-10-03T01:00:00Z",
  longitude: 121.88,
  latitude: 30.94,
  institutional_level: "general",
  service_level: 4,
  response_suggestion: null,
  response_rule_version: null,
  revision_no: 1,
  event_kind: "formal",
  lifecycle_state: "formal_triggered",
  t1_at: "2026-10-03T01:05:00Z",
};

const pendingTask: WorkgroupTask = {
  id: "task-1",
  event_id: "event-1",
  workgroup_code: "monitoring_forecast",
  task_code: "trend_consultation",
  title: "趋势会商",
  status: "pending",
  timeliness_state: "on_time",
  due_at: "2026-10-03T02:00:00Z",
  row_version: 4,
  instruction: "组织震后趋势会商",
  priority: 10,
  phase_code: "within_30m",
  source_type: "preplan",
  source_ref: null,
  activated_at: "2026-10-03T01:00:00Z",
  completed_at: null,
  closed_at: null,
  created_at: "2026-10-03T01:00:00Z",
  updated_at: "2026-10-03T01:00:00Z",
  contributors: [],
  can_work: true,
  can_confirm: false,
};

const inProgressTask: WorkgroupTask = {
  ...pendingTask,
  id: "task-2",
  task_code: "earthquake_report",
  title: "填报震情",
  status: "in_progress",
  timeliness_state: "overdue",
  row_version: 5,
  phase_code: "30m_to_60m",
  contributors: [
    {
      user_id: "user-1",
      username: "张三",
      contribution_count: 2,
      first_contributed_at: "2026-10-03T01:10:00Z",
      last_contributed_at: "2026-10-03T01:20:00Z",
    },
  ],
};

const reviewTask: WorkgroupTask = {
  ...pendingTask,
  id: "task-3",
  title: "震源机制复核",
  status: "pending_review",
  row_version: 6,
  phase_code: "1h_to_2h",
};

const automaticVersion = {
  id: "version-auto",
  deliverable_id: "deliverable-1",
  version_no: 1,
  source_kind: "automatic",
  artifact_id: "artifact-1",
  artifact_publication_id: "publication-1",
  storage_key: null,
  file_name: "趋势会商图.png",
  checksum: "a".repeat(64),
  mime_type: "image/png",
  size_bytes: 1024,
  text_result: null,
  created_by: "system",
  basis_text: null,
  supersedes_version_id: null,
  created_at: "2026-10-03T01:05:00Z",
};

const manualVersion = {
  ...automaticVersion,
  id: "version-manual",
  version_no: 2,
  source_kind: "manual",
  artifact_id: null,
  artifact_publication_id: null,
  storage_key: "collaboration/manual.png",
  created_by: "operator",
  basis_text: "根据现场信息修订",
  supersedes_version_id: "version-auto",
  created_at: "2026-10-03T01:15:00Z",
};

const overrideVersion = {
  ...automaticVersion,
  id: "version-override",
  version_no: 3,
  source_kind: "superadmin_override",
  artifact_id: null,
  artifact_publication_id: null,
  storage_key: "collaboration/override.png",
  created_by: "superadmin",
  basis_text: "管理员纠正成果",
  supersedes_version_id: "version-manual",
  created_at: "2026-10-03T01:25:00Z",
};

const hallDetail: CommandHallTaskDetail = {
  id: inProgressTask.id,
  event_id: event.id,
  task: {
    ...inProgressTask,
  },
  contributors: inProgressTask.contributors as unknown as Record<
    string,
    unknown
  >[],
  deliverables: [
    {
      id: "deliverable-1",
      task_id: inProgressTask.id,
      deliverable_code: "manual_report",
      title: "震情专题报告",
      is_required: true,
      requirement_kind: "manual_file_or_text",
      artifact_binding: null,
      display_order: 1,
      current_publication: null,
      versions: [automaticVersion, manualVersion, overrideVersion],
    },
  ],
  task_events: [],
  notifications: [],
  projection_version: 2,
};

const listMock = vi.mocked(listCollaborationTasks);
const getTaskMock = vi.mocked(getCollaborationTask);
const getHallTaskMock = vi.mocked(getCommandHallTask);
const getEventMock = vi.mocked(getEvent);
const startMock = vi.mocked(startCollaborationTask);
const submitMock = vi.mocked(submitCollaborationTask);
const returnMock = vi.mocked(returnCollaborationTask);
const completeMock = vi.mocked(completeCollaborationTask);
const createTemporaryMock = vi.mocked(createTemporaryTask);
const cancelMock = vi.mocked(cancelCollaborationTask);
const uploadMock = vi.mocked(uploadCollaborationDeliverableVersion);
const textVersionMock = vi.mocked(addCollaborationTextVersion);
const publishMock = vi.mocked(publishCollaborationDeliverableVersion);

function WorkgroupTasksHarness({
  userRole,
  workgroup,
}: {
  userRole: string;
  workgroup: string | null;
}) {
  const navigate = useNavigate();
  return (
    <>
      <WorkgroupTasksPage
        userRole={userRole}
        workgroup={workgroup}
      />
      <button type="button" onClick={() => navigate("/tasks/event-2")}>
        切换到事件 B
      </button>
    </>
  );
}

function renderPage(
  userRole: string,
  workgroup: string | null,
  eventId = "event-1",
): ReturnType<typeof render> {
  return render(
    <MemoryRouter initialEntries={[`/tasks/${eventId}`]}>
      <Routes>
        <Route
          path="/tasks/:eventId"
          element={
            <WorkgroupTasksHarness
              userRole={userRole}
              workgroup={workgroup}
            />
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  setAccessToken("test-token");
  listMock.mockReset();
  getTaskMock.mockReset();
  getHallTaskMock.mockReset();
  getEventMock.mockReset();
  startMock.mockReset();
  submitMock.mockReset();
  returnMock.mockReset();
  completeMock.mockReset();
  createTemporaryMock.mockReset();
  cancelMock.mockReset();
  uploadMock.mockReset();
  textVersionMock.mockReset();
  publishMock.mockReset();
  getEventMock.mockResolvedValue(event);
  getHallTaskMock.mockResolvedValue(hallDetail);
});

test("group member sees start and submit actions but not confirm", async () => {
  listMock.mockResolvedValue([pendingTask, inProgressTask]);

  renderPage("group_member", "监测预报组");

  expect(await screen.findByRole("button", { name: "开始处理" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "填写结果" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "确认完成" })).not.toBeInTheDocument();
});

test("leader can confirm a pending review task while member cannot", async () => {
  const confirmableReview = { ...reviewTask, can_confirm: true };
  listMock.mockResolvedValue([confirmableReview]);
  const leaderRender = renderPage("group_leader", "监测预报组");

  expect(
    await screen.findByRole("button", { name: "确认完成" }),
  ).toBeInTheDocument();

  leaderRender.unmount();
  listMock.mockResolvedValue([reviewTask]);
  renderPage("group_member", "监测预报组");

  await screen.findByText(reviewTask.title);
  expect(
    screen.queryByRole("button", { name: "确认完成" }),
  ).not.toBeInTheDocument();
});

test("deputy without effective authority cannot confirm or cancel", async () => {
  listMock.mockResolvedValue([
    {
      ...reviewTask,
      can_confirm: false,
    },
    {
      ...inProgressTask,
      can_confirm: false,
    },
  ]);

  renderPage("group_deputy", "监测预报组");

  await screen.findByText(reviewTask.title);
  getTaskMock.mockResolvedValue(inProgressTask);
  expect(
    screen.queryByRole("button", { name: "确认完成" }),
  ).not.toBeInTheDocument();
  fireEvent.click(
    screen.getByRole("button", {
      name: `查看任务详情：${inProgressTask.title}`,
    }),
  );
  await screen.findByRole("heading", { name: inProgressTask.title });
  expect(
    screen.queryByRole("button", { name: "标记不再需要" }),
  ).not.toBeInTheDocument();
});

test("groups tasks by phase and shows status, timeliness, due time, and contributors", async () => {
  listMock.mockResolvedValue([pendingTask, inProgressTask]);

  renderPage("group_member", "监测预报组");

  expect(await screen.findByRole("heading", { name: "震后30分钟内" })).toBeInTheDocument();
  expect(
    screen.getByRole("heading", { name: "震后30分钟至1小时" }),
  ).toBeInTheDocument();
  expect(screen.getByText("待接收")).toBeInTheDocument();
  expect(screen.getAllByText("已超时").length).toBeGreaterThan(0);
  expect(screen.getAllByText(/2026\/10\/03/).length).toBeGreaterThan(0);
  expect(screen.getByText("张三")).toBeInTheDocument();
});

test("opens task details in the page and preserves immutable version source labels", async () => {
  listMock.mockResolvedValue([inProgressTask]);
  getTaskMock.mockResolvedValue(inProgressTask);

  renderPage("group_leader", "监测预报组");

  fireEvent.click(
    await screen.findByRole("button", { name: `查看任务详情：${inProgressTask.title}` }),
  );

  expect(await screen.findByText("系统自动版")).toBeInTheDocument();
  expect(screen.getByText("人工修订版")).toBeInTheDocument();
  expect(screen.getByText("超级管理员覆盖版")).toBeInTheDocument();
  expect(getTaskMock).toHaveBeenCalledWith(inProgressTask.id);
  expect(getHallTaskMock).toHaveBeenCalledWith(inProgressTask.id);
});

test("shows the non-removable test marker on tasks and details", async () => {
  listMock.mockResolvedValue([pendingTask]);
  getEventMock.mockResolvedValue({ ...event, event_kind: "test" });
  getTaskMock.mockResolvedValue(pendingTask);
  getHallTaskMock.mockResolvedValue({
    ...hallDetail,
    id: pendingTask.id,
    task: { ...pendingTask },
    deliverables: [],
  });

  renderPage("superadmin", null);

  expect((await screen.findAllByText("测试")).length).toBeGreaterThan(0);
  fireEvent.click(
    screen.getByRole("button", { name: `查看任务详情：${pendingTask.title}` }),
  );
  await screen.findByRole("heading", { name: pendingTask.title });
  expect(screen.getAllByText("测试").length).toBeGreaterThan(0);
});

test("clears the previous event selection when navigating to another event", async () => {
  const eventB = {
    ...event,
    id: "event-2",
    place: "上海松江区",
  };
  const taskB: WorkgroupTask = {
    ...pendingTask,
    id: "task-b",
    event_id: "event-2",
    title: "松江震情处理",
  };
  getEventMock.mockImplementation(async (id) =>
    id === "event-2" ? eventB : event,
  );
  listMock.mockImplementation(async (id) =>
    id === "event-2" ? [taskB] : [pendingTask],
  );
  getTaskMock.mockResolvedValue(pendingTask);

  renderPage("group_member", "监测预报组");

  fireEvent.click(
    await screen.findByRole("button", {
      name: `查看任务详情：${pendingTask.title}`,
    }),
  );
  await screen.findByRole("heading", { name: pendingTask.title });
  fireEvent.click(screen.getByRole("button", { name: "切换到事件 B" }));

  expect(await screen.findByText(taskB.title)).toBeInTheDocument();
  expect(screen.queryByText(pendingTask.title)).not.toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: pendingTask.title }),
  ).not.toBeInTheDocument();
});

test("hides the previous event tasks before the next event finishes loading", async () => {
  let resolveEventB!: (tasks: WorkgroupTask[]) => void;
  const delayedEventB = new Promise<WorkgroupTask[]>((resolve) => {
    resolveEventB = resolve;
  });
  const taskB: WorkgroupTask = {
    ...pendingTask,
    id: "task-b",
    event_id: "event-2",
    title: "事件 B 任务",
  };
  listMock.mockImplementation((id) =>
    id === "event-1" ? Promise.resolve([pendingTask]) : delayedEventB,
  );
  getEventMock.mockImplementation(async (id) =>
    id === "event-2"
      ? { ...event, id: "event-2", place: "上海松江区" }
      : event,
  );

  renderPage("group_member", "监测预报组");

  const startButton = await screen.findByRole("button", {
    name: "开始处理",
  });
  fireEvent.click(screen.getByRole("button", { name: "切换到事件 B" }));
  fireEvent.click(startButton);

  expect(screen.queryByText(pendingTask.title)).not.toBeInTheDocument();
  expect(startButton).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "填写结果" })).not.toBeInTheDocument();
  expect(startMock).not.toHaveBeenCalled();

  await act(async () => {
    resolveEventB([taskB]);
  });

  expect(await screen.findByText(taskB.title)).toBeInTheDocument();
  expect(screen.queryByText(pendingTask.title)).not.toBeInTheDocument();
  expect(startMock).not.toHaveBeenCalled();
});

test("clears task state when opening a task fails", async () => {
  listMock.mockResolvedValue([pendingTask]);
  getTaskMock.mockRejectedValue(new ApiError("task_not_found", 404));

  renderPage("group_member", "监测预报组");

  fireEvent.click(
    await screen.findByRole("button", {
      name: `查看任务详情：${pendingTask.title}`,
    }),
  );

  expect(await screen.findByRole("alert")).toHaveTextContent("task_not_found");
  expect(
    screen.queryByRole("button", { name: "开始处理" }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: pendingTask.title }),
  ).not.toBeInTheDocument();
});

test("clears task state when the post-mutation refresh fails", async () => {
  listMock
    .mockResolvedValueOnce([pendingTask])
    .mockRejectedValueOnce(new ApiError("storage_unavailable", 503));
  startMock.mockResolvedValue({
    ...pendingTask,
    status: "in_progress",
    row_version: 5,
  });

  renderPage("group_member", "监测预报组");

  fireEvent.click(await screen.findByRole("button", { name: "开始处理" }));

  expect(
    (await screen.findAllByText("storage_unavailable")).length,
  ).toBeGreaterThan(0);
  expect(screen.queryByText(pendingTask.title)).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "开始处理" }),
  ).not.toBeInTheDocument();
});

test("ignores a delayed list response from the previous event", async () => {
  let resolveEventA!: (tasks: WorkgroupTask[]) => void;
  const delayedEventA = new Promise<WorkgroupTask[]>((resolve) => {
    resolveEventA = resolve;
  });
  const taskB: WorkgroupTask = {
    ...pendingTask,
    id: "task-b",
    event_id: "event-2",
    title: "事件 B 任务",
  };
  listMock.mockImplementation((id) =>
    id === "event-1" ? delayedEventA : Promise.resolve([taskB]),
  );

  renderPage("group_member", "监测预报组");
  fireEvent.click(screen.getByRole("button", { name: "切换到事件 B" }));

  expect(await screen.findByText(taskB.title)).toBeInTheDocument();
  await act(async () => {
    resolveEventA([pendingTask]);
  });

  expect(screen.getByText(taskB.title)).toBeInTheDocument();
  expect(screen.queryByText(pendingTask.title)).not.toBeInTheDocument();
});

test("ignores a previous event mutation refresh after navigation", async () => {
  let resolveStart!: (task: WorkgroupTask) => void;
  const delayedStart = new Promise<WorkgroupTask>((resolve) => {
    resolveStart = resolve;
  });
  const eventB = {
    ...event,
    id: "event-2",
    place: "上海松江区",
  };
  const taskB: WorkgroupTask = {
    ...pendingTask,
    id: "task-b",
    event_id: "event-2",
    title: "事件 B 任务",
  };
  getEventMock.mockImplementation(async (id) =>
    id === "event-2" ? eventB : event,
  );
  listMock.mockImplementation(async (id) =>
    id === "event-2" ? [taskB] : [pendingTask],
  );
  startMock.mockReturnValue(delayedStart);

  renderPage("group_member", "监测预报组");

  fireEvent.click(await screen.findByRole("button", { name: "开始处理" }));
  fireEvent.click(screen.getByRole("button", { name: "切换到事件 B" }));
  expect(await screen.findByText(taskB.title)).toBeInTheDocument();

  await act(async () => {
    resolveStart({
      ...pendingTask,
      status: "in_progress",
      row_version: 5,
    });
  });

  expect(screen.getByText(taskB.title)).toBeInTheDocument();
  expect(screen.queryByText(pendingTask.title)).not.toBeInTheDocument();
  expect(listMock).toHaveBeenCalledTimes(2);
});

test("ignores a delayed task detail response after another task is opened", async () => {
  let resolveTaskA!: (task: WorkgroupTask) => void;
  const delayedTaskA = new Promise<WorkgroupTask>((resolve) => {
    resolveTaskA = resolve;
  });
  listMock.mockResolvedValue([pendingTask, inProgressTask]);
  getTaskMock.mockImplementation((taskId) =>
    taskId === pendingTask.id
      ? delayedTaskA
      : Promise.resolve(inProgressTask),
  );

  renderPage("group_member", "监测预报组");

  fireEvent.click(
    await screen.findByRole("button", {
      name: `查看任务详情：${pendingTask.title}`,
    }),
  );
  fireEvent.click(
    screen.getByRole("button", {
      name: `查看任务详情：${inProgressTask.title}`,
    }),
  );
  await screen.findByRole("heading", { name: inProgressTask.title });
  await act(async () => {
    resolveTaskA(pendingTask);
  });

  expect(
    screen.getByRole("heading", { name: inProgressTask.title }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: pendingTask.title }),
  ).not.toBeInTheDocument();
});

test("shows the exact conflict message when a task was changed by someone else", async () => {
  listMock.mockResolvedValue([pendingTask]);
  startMock.mockRejectedValue(new ApiError("task_version_conflict", 409));

  renderPage("group_member", "监测预报组");

  fireEvent.click(await screen.findByRole("button", { name: "开始处理" }));

  expect(
    await screen.findByText("任务已被其他人更新，请刷新后重试"),
  ).toBeInTheDocument();
});

test("refreshes tasks from the server after a successful mutation", async () => {
  listMock
    .mockResolvedValueOnce([pendingTask])
    .mockResolvedValueOnce([{ ...pendingTask, status: "in_progress", row_version: 5 }]);
  startMock.mockResolvedValue({
    ...pendingTask,
    status: "in_progress",
    row_version: 5,
  });

  renderPage("group_member", "监测预报组");

  fireEvent.click(await screen.findByRole("button", { name: "开始处理" }));

  await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2));
  expect(await screen.findByText("处理中")).toBeInTheDocument();
});

test("reuses one idempotency key when a timed-out text submission is retried", async () => {
  listMock.mockResolvedValue([inProgressTask]);
  getTaskMock.mockResolvedValue(inProgressTask);
  submitMock
    .mockRejectedValueOnce(new ApiError("请求超时，请稍后重试", 408))
    .mockResolvedValueOnce({
      ...inProgressTask,
      status: "pending_review",
      row_version: 6,
    });

  renderPage("group_member", "监测预报组");

  fireEvent.click(
    await screen.findByRole("button", {
      name: `查看任务详情：${inProgressTask.title}`,
    }),
  );
  fireEvent.change(await screen.findByLabelText("文字结果"), {
    target: { value: "已完成震情核对" },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交成果" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("请求超时");
  fireEvent.click(screen.getByRole("button", { name: "提交成果" }));

  await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(2));
  const firstKey = submitMock.mock.calls[0][3];
  const secondKey = submitMock.mock.calls[1][3];
  expect(firstKey).toBeTruthy();
  expect(secondKey).toBe(firstKey);
});

test("creates a temporary task with the fixed workgroup list and refreshes", async () => {
  listMock
    .mockResolvedValueOnce([])
    .mockResolvedValueOnce([{ ...pendingTask, source_type: "ad_hoc" }]);
  createTemporaryMock.mockResolvedValue({
    ...pendingTask,
    source_type: "ad_hoc",
  });

  renderPage("superadmin", null);

  fireEvent.change(await screen.findByLabelText("临时任务标题"), {
    target: { value: "联系区应急局" },
  });
  fireEvent.change(screen.getByLabelText("责任工作组"), {
    target: { value: "comprehensive_coordination" },
  });
  fireEvent.change(screen.getByLabelText("任务说明"), {
    target: { value: "核对受影响街镇信息" },
  });
  fireEvent.change(screen.getByLabelText("优先级"), {
    target: { value: "20" },
  });
  fireEvent.change(screen.getByLabelText("截止时间"), {
    target: { value: "2026-10-03T10:00" },
  });
  fireEvent.click(screen.getByRole("button", { name: "创建临时任务" }));

  await waitFor(() => expect(createTemporaryMock).toHaveBeenCalledTimes(1));
  expect(createTemporaryMock.mock.calls[0][1]).toEqual({
    workgroup_code: "comprehensive_coordination",
    title: "联系区应急局",
    instruction: "核对受影响街镇信息",
    priority: 20,
    due_at: "2026-10-03T02:00:00.000Z",
    continues_until_cancelled: false,
  });
  await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2));
  expect(screen.getAllByRole("option")).toHaveLength(7);
});

test("does not leave the next event temporary-task button disabled after switching", async () => {
  let resolveTemporary!: (task: WorkgroupTask) => void;
  const delayedTemporary = new Promise<WorkgroupTask>((resolve) => {
    resolveTemporary = resolve;
  });
  listMock.mockResolvedValue([]);
  getEventMock.mockImplementation(async (id) =>
    id === "event-2"
      ? { ...event, id: "event-2", place: "上海松江区" }
      : event,
  );
  createTemporaryMock.mockReturnValue(delayedTemporary);

  renderPage("superadmin", null);

  fireEvent.change(await screen.findByLabelText("临时任务标题"), {
    target: { value: "事件 A 临时任务" },
  });
  fireEvent.change(screen.getByLabelText("任务说明"), {
    target: { value: "事件 A 说明" },
  });
  fireEvent.change(screen.getByLabelText("截止时间"), {
    target: { value: "2026-10-03T10:00" },
  });
  fireEvent.click(screen.getByRole("button", { name: "创建临时任务" }));

  expect(
    screen.getByRole("button", { name: "创建临时任务" }),
  ).toBeDisabled();

  fireEvent.click(screen.getByRole("button", { name: "切换到事件 B" }));

  const nextEventButton = await screen.findByRole("button", {
    name: "创建临时任务",
  });
  expect(nextEventButton).toBeEnabled();

  await act(async () => {
    resolveTemporary({
      ...pendingTask,
      id: "task-a",
      source_type: "ad_hoc",
    });
  });

  expect(screen.getByRole("button", { name: "创建临时任务" })).toBeEnabled();
});

test("uploads a candidate file and confirms publication of that version", async () => {
  const confirmableTask = {
    ...inProgressTask,
    can_confirm: true,
  };
  const candidate = {
    ...manualVersion,
    id: "version-candidate",
    version_no: 1,
    supersedes_version_id: null,
  };
  const detailWithoutVersions: CommandHallTaskDetail = {
    ...hallDetail,
    deliverables: [
      {
        ...hallDetail.deliverables[0],
        versions: [],
      },
    ],
  };
  const detailWithCandidate: CommandHallTaskDetail = {
    ...hallDetail,
    deliverables: [
      {
        ...hallDetail.deliverables[0],
        versions: [candidate],
      },
    ],
  };
  listMock.mockResolvedValue([confirmableTask]);
  getTaskMock.mockResolvedValue(confirmableTask);
  getHallTaskMock
    .mockResolvedValueOnce(detailWithoutVersions)
    .mockResolvedValueOnce(detailWithCandidate)
    .mockResolvedValue(detailWithCandidate);
  uploadMock.mockResolvedValue(candidate);
  publishMock.mockResolvedValue({
    id: "publication-2",
    deliverable_id: "deliverable-1",
    version_id: candidate.id,
    published_by: "leader",
    published_role: "leader",
    published_at: "2026-10-03T01:30:00Z",
    superseded_at: null,
    publication_note: "确认发布",
    created_at: "2026-10-03T01:30:00Z",
  });

  renderPage("group_leader", "监测预报组");

  fireEvent.click(
    await screen.findByRole("button", {
      name: `查看任务详情：${inProgressTask.title}`,
    }),
  );
  const file = new File(["candidate"], "report.pdf", {
    type: "application/pdf",
  });
  fireEvent.change(
    await screen.findByLabelText("候选文件：震情专题报告"),
    {
      target: { files: [file] },
    },
  );
  fireEvent.change(screen.getByLabelText("候选版本说明：震情专题报告"), {
    target: { value: "补充人工审核结果" },
  });
  fireEvent.click(screen.getAllByRole("button", { name: "上传候选文件" })[0]);

  await waitFor(() => expect(uploadMock).toHaveBeenCalledTimes(1));
  expect(uploadMock.mock.calls[0][0]).toBe("deliverable-1");
  expect(uploadMock.mock.calls[0][1]).toBe(file);
  expect(uploadMock.mock.calls[0][2]).toBe("补充人工审核结果");
  expect(uploadMock.mock.calls[0][3]).toBeTruthy();

  expect(await screen.findByText("人工修订版")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "发布此版本" }));
  fireEvent.change(screen.getByLabelText("发布说明"), {
    target: { value: "确认发布" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认发布" }));

  await waitFor(() => expect(publishMock).toHaveBeenCalledTimes(1));
  expect(publishMock.mock.calls[0]).toEqual([
    "deliverable-1",
    "version-candidate",
    5,
    "确认发布",
    expect.any(String),
  ]);
});

test("creates a text version before submitting and completing a manual_text task", async () => {
  const textTask = {
    ...inProgressTask,
    can_confirm: true,
  };
  const textDeliverable = {
    id: "deliverable-text",
    task_id: textTask.id,
    deliverable_code: "manual_text_result",
    title: "人工文字结果",
    is_required: true,
    requirement_kind: "manual_text",
    artifact_binding: null,
    display_order: 1,
    current_publication: null,
    versions: [],
  };
  const textVersion = {
    ...manualVersion,
    id: "version-text",
    deliverable_id: textDeliverable.id,
    version_no: 1,
    source_kind: "manual",
    storage_key: null,
    file_name: null,
    text_result: { text: "已完成震情核对" },
  };
  const detailBefore: CommandHallTaskDetail = {
    ...hallDetail,
    id: textTask.id,
    task: { ...textTask },
    deliverables: [textDeliverable],
  };
  const detailAfterText: CommandHallTaskDetail = {
    ...detailBefore,
    deliverables: [{ ...textDeliverable, versions: [textVersion] }],
  };
  const submittedTask = {
    ...textTask,
    status: "pending_review" as const,
    row_version: 6,
  };
  const completedTask = {
    ...submittedTask,
    status: "completed" as const,
    row_version: 7,
  };
  listMock
    .mockResolvedValueOnce([textTask])
    .mockResolvedValueOnce([textTask])
    .mockResolvedValueOnce([submittedTask])
    .mockResolvedValueOnce([completedTask]);
  getTaskMock.mockResolvedValue(textTask);
  getHallTaskMock
    .mockResolvedValueOnce(detailBefore)
    .mockResolvedValueOnce(detailAfterText)
    .mockResolvedValue(detailAfterText);
  textVersionMock.mockResolvedValue(textVersion);
  submitMock.mockResolvedValue(submittedTask);
  completeMock.mockResolvedValue(completedTask);

  renderPage("group_leader", "监测预报组");

  fireEvent.click(
    await screen.findByRole("button", {
      name: `查看任务详情：${textTask.title}`,
    }),
  );
  fireEvent.change(await screen.findByLabelText("文字版本：人工文字结果"), {
    target: { value: "已完成震情核对" },
  });
  fireEvent.change(screen.getByLabelText("文字版本说明：人工文字结果"), {
    target: { value: "组长核验" },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交文字版本" }));

  await waitFor(() => expect(textVersionMock).toHaveBeenCalledTimes(1));
  expect(textVersionMock.mock.calls[0]).toEqual([
    "deliverable-text",
    { text: "已完成震情核对" },
    "组长核验",
    expect.any(String),
  ]);
  expect(await screen.findByText(/文字成果/)).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("文字结果"), {
    target: { value: "提交待确认" },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交成果" }));
  await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(1));

  const confirmButtons = await screen.findAllByRole("button", {
    name: "确认完成",
  });
  fireEvent.click(confirmButtons[confirmButtons.length - 1]);
  await waitFor(() => expect(completeMock).toHaveBeenCalledTimes(1));
  expect(listMock).toHaveBeenCalledTimes(4);
});

test("reuses one idempotency key when a timed-out text version is retried", async () => {
  const textDeliverable = {
    id: "deliverable-text",
    task_id: inProgressTask.id,
    deliverable_code: "manual_text_result",
    title: "人工文字结果",
    is_required: true,
    requirement_kind: "manual_text",
    artifact_binding: null,
    display_order: 1,
    current_publication: null,
    versions: [],
  };
  listMock.mockResolvedValue([inProgressTask]);
  getTaskMock.mockResolvedValue(inProgressTask);
  getHallTaskMock.mockResolvedValue({
    ...hallDetail,
    deliverables: [textDeliverable],
  });
  textVersionMock
    .mockRejectedValueOnce(new ApiError("请求超时，请稍后重试", 408))
    .mockResolvedValueOnce({
      ...manualVersion,
      id: "version-text",
      deliverable_id: textDeliverable.id,
      file_name: null,
      storage_key: null,
      text_result: { text: "已完成" },
    });

  renderPage("group_member", "监测预报组");

  fireEvent.click(
    await screen.findByRole("button", {
      name: `查看任务详情：${inProgressTask.title}`,
    }),
  );
  fireEvent.change(await screen.findByLabelText("文字版本：人工文字结果"), {
    target: { value: "已完成" },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交文字版本" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("请求超时");

  fireEvent.click(screen.getByRole("button", { name: "提交文字版本" }));
  await waitFor(() => expect(textVersionMock).toHaveBeenCalledTimes(2));

  const firstKey = textVersionMock.mock.calls[0][3];
  const secondKey = textVersionMock.mock.calls[1][3];
  expect(firstKey).toBeTruthy();
  expect(secondKey).toBe(firstKey);
});
