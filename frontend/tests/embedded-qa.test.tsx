import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import {
  createQaSession,
  getAssessmentProduction,
  getCommandHallOverview,
  getCurrentAssessment,
  getEvent,
  getLossAssessment,
  getQaAnswer,
  streamCommandHall,
  streamQaQuestion,
} from "../src/api/client";
import { LossMap } from "../src/components/LossMap";
import { EventDetailPage } from "../src/pages/EventDetailPage";
import { CommandHallPage } from "../src/pages/CommandHallPage";
import {
  MapActionProvider,
  useMapActionConsumer,
  useMapActionPublisher,
} from "../src/qa/MapActionContext";
import type {
  AssessmentRunStatus,
  CommandHallOverview,
  CommandHallTaskCounts,
  EventDetail,
  LossResult,
  ProductionRun,
  QaAnswer,
  QaCitation,
  QaMapAction,
  QaSession,
  QaStreamEvent,
  QaToolCall,
} from "../src/types";

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return {
    ...actual,
    createQaSession: vi.fn(),
    getAssessmentProduction: vi.fn(),
    getCommandHallOverview: vi.fn(),
    getCurrentAssessment: vi.fn(),
    getEvent: vi.fn(),
    getLossAssessment: vi.fn(),
    getQaAnswer: vi.fn(),
    streamCommandHall: vi.fn(),
    streamQaQuestion: vi.fn(),
  };
});

const createQaSessionMock = vi.mocked(createQaSession);
const getAssessmentProductionMock = vi.mocked(getAssessmentProduction);
const getCommandHallOverviewMock = vi.mocked(getCommandHallOverview);
const getCurrentAssessmentMock = vi.mocked(getCurrentAssessment);
const getEventMock = vi.mocked(getEvent);
const getLossAssessmentMock = vi.mocked(getLossAssessment);
const getQaAnswerMock = vi.mocked(getQaAnswer);
const streamCommandHallMock = vi.mocked(streamCommandHall);
const streamQaQuestionMock = vi.mocked(streamQaQuestion);

const eventDetail: EventDetail = {
  id: "event-1",
  source: "cenc",
  place: "上海浦东新区",
  magnitude: 5.2,
  depth_km: 12,
  origin_time: "2026-10-08T00:00:00Z",
  longitude: 121.5,
  latitude: 31.2,
  institutional_level: null,
  service_level: null,
  response_suggestion: null,
  response_rule_version: null,
  revision_no: 1,
  event_kind: "formal",
  lifecycle_state: "formal_triggered",
  t1_at: null,
};

const assessment: AssessmentRunStatus = {
  run_id: "run-1",
  event_id: "event-1",
  revision_id: "revision-1",
  run_no: 1,
  status: "completed",
  t1_at: null,
  deadline_at: "2026-10-08T00:10:00Z",
  completed_task_count: 0,
  failed_task_count: 0,
  total_task_count: 0,
  tasks: [],
};

const lossResult: LossResult = {
  run_id: "run-1",
  event_id: "event-1",
  revision_id: "revision-1",
  effective_run_id: "run-1",
  is_fallback: false,
  products: [],
};

const session: QaSession = {
  id: "s1",
  created_by: "operator",
  event_id: "event-1",
  snapshot_id: "snapshot-1",
  title: "当前问答",
  created_at: "2026-10-08T00:00:00Z",
  updated_at: "2026-10-08T00:00:00Z",
};

const citation: QaCitation = {
  citation_key: "C1",
  source_title: "上海市断裂带资料",
  version_label: "2026.1",
  locator: "p.12",
  excerpt: "断裂带展布于区域中部。",
  source_uri: null,
  checksum: "abcdef0123456789",
};

const toolCall: QaToolCall = {
  id: "tool-1",
  answer_id: "a1",
  tool_name: "fault.nearest",
  tool_status: "ok",
  arguments: {},
  result: null,
  limitations: [],
  duration_ms: 10,
  created_at: "2026-10-08T00:00:00Z",
};

const persistedAnswer: QaAnswer = {
  id: "a1",
  question_id: "q1",
  session_id: "s1",
  status: "completed",
  text: "距最近断层 12.4 公里。",
  structured: null,
  citation_keys: ["C1"],
  degraded_reasons: [],
  duration_ms: 120,
  created_at: "2026-10-08T00:00:00Z",
  updated_at: "2026-10-08T00:00:01Z",
  completed_at: "2026-10-08T00:00:01Z",
  citations: [citation],
  tool_calls: [toolCall],
  map_actions: [],
};

const qaEvents: QaStreamEvent[] = [
  { type: "answer_started", data: { answer_id: "a1" } },
  { type: "tool", data: { name: "fault.nearest", status: "ok" } },
  { type: "answer_delta", data: { text: "距最近断层 12.4 公里。" } },
  {
    type: "answer_completed",
    data: {
      answer_id: "a1",
      status: "completed",
      citations: [citation],
    },
  },
];

function renderEventDetail(eventId = "event-1") {
  return render(
    <MemoryRouter initialEntries={[`/events/${eventId}`]}>
      <Routes>
        <Route path="/events/:eventId" element={<EventDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function mockEventDetailApi() {
  getEventMock.mockResolvedValue(eventDetail);
  getCurrentAssessmentMock.mockResolvedValue(assessment);
  getAssessmentProductionMock.mockResolvedValue(
    null as unknown as ProductionRun,
  );
  getLossAssessmentMock.mockResolvedValue(lossResult);
}

function mockQaStream(events: QaStreamEvent[]) {
  createQaSessionMock.mockResolvedValue(session);
  streamQaQuestionMock.mockImplementation(
    async (_sessionId, _question, onEvent) => {
      for (const event of events) {
        onEvent(event);
      }
    },
  );
  getQaAnswerMock.mockResolvedValue(persistedAnswer);
}

function validLocateAction(overrides: Partial<QaMapAction> = {}): QaMapAction {
  return {
    action_type: "locate",
    target_ref: "event:epicenter",
    reason: "定位最近断层",
    valid_until: "",
    ...overrides,
  } as QaMapAction;
}

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

const hallOverview: CommandHallOverview = {
  event_id: "event-1",
  event: {
    event_id: "event-1",
    event_kind: "formal",
    event_type: "formal",
    place: "上海浦东新区",
    magnitude: 5.2,
    depth_km: 12,
    origin_time: "2026-10-08T00:00:00Z",
    t1_at: "2026-10-08T00:01:00Z",
    longitude: 121.5,
    latitude: 31.2,
    institutional_level: "general",
    service_level: 4,
    response_suggestion: {
      institutional_level: "general",
      service_level: 4,
    },
    lifecycle_state: "formal_triggered",
  },
  group_count: 0,
  groups: [],
  alerts: [],
  task_counts: emptyCounts,
  artifact_summary: {
    published_count: 0,
    status_counts: { complete: 0, degraded: 0, failed: 0 },
    latest_artifacts: [],
  },
  alert_summary: {},
  dual_version_count: 0,
  projection_version: 1,
  sync_status: "current",
  projection_lag_seconds: 0,
  projection_source_updated_at: null,
  updated_at: "2026-10-08T00:01:00Z",
};

function renderHall() {
  return render(
    <MemoryRouter initialEntries={["/command-hall/event-1"]}>
      <Routes>
        <Route path="/command-hall/:eventId" element={<CommandHallPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function ActionProbe() {
  const publish = useMapActionPublisher();
  const { action } = useMapActionConsumer();
  const actionValue = validLocateAction();
  return (
    <div>
      <button
        type="button"
        onClick={() => publish("event-1", actionValue)}
      >
        publish event-1
      </button>
      <button
        type="button"
        onClick={() => publish("event-2", actionValue)}
      >
        publish event-2
      </button>
      <span data-testid="action-state">{action?.action_type ?? ""}</span>
    </div>
  );
}

function LossActionHarness({ action }: { action: QaMapAction }) {
  const publish = useMapActionPublisher();
  return (
    <div>
      <button type="button" onClick={() => publish("event-1", action)}>
        publish loss action
      </button>
      <LossMap
        center={[31.2, 121.5]}
        tileUrlTemplate="http://localhost/tiles/{z}/{x}/{y}.png"
        townFeatures={[]}
        gridArtifact={null}
        fusedIntensityArtifact={null}
      />
    </div>
  );
}

beforeEach(() => {
  createQaSessionMock.mockReset();
  getAssessmentProductionMock.mockReset();
  getCommandHallOverviewMock.mockReset();
  getCurrentAssessmentMock.mockReset();
  getEventMock.mockReset();
  getLossAssessmentMock.mockReset();
  getQaAnswerMock.mockReset();
  streamCommandHallMock.mockReset();
  streamQaQuestionMock.mockReset();
});

test("event detail opens qa panel with the current event and publishes map action", async () => {
  mockEventDetailApi();
  mockQaStream([
    ...qaEvents,
    {
      type: "map_action",
      data: {
        action_type: "locate",
        target_ref: "fault:f1",
        reason: "定位最近断层",
      },
    },
  ]);

  renderEventDetail();

  expect(await screen.findByText("当前修订")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "智能问策" }));
  expect(screen.getByLabelText("事件上下文问答")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "最近断层在哪里" },
  });
  fireEvent.click(screen.getByRole("button", { name: "提问" }));

  expect(
    await screen.findByText("距最近断层 12.4 公里。"),
  ).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByTestId("loss-map-last-action")).toHaveTextContent(
      "locate",
    ),
  );
  expect(createQaSessionMock).toHaveBeenCalledWith({
    title: "当前问答",
    event_id: "event-1",
  });
});

test("closing the event qa drawer keeps the original event entry and scroll state", async () => {
  mockEventDetailApi();
  mockQaStream(qaEvents);
  window.scrollTo = vi.fn();
  window.scrollTo(0, 240);

  renderEventDetail();

  await screen.findByText("当前修订");
  fireEvent.click(screen.getByRole("button", { name: "智能问策" }));
  expect(screen.getByLabelText("事件上下文问答")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "关闭智能问策" }));

  await waitFor(() =>
    expect(screen.queryByLabelText("事件上下文问答")).not.toBeInTheDocument(),
  );
  expect(window.scrollTo).toHaveBeenCalledWith(0, 240);
  expect(screen.getByRole("button", { name: "智能问策" })).toBeInTheDocument();
});

test("map actions are isolated by event id and cleared when the provider event changes", () => {
  const { rerender } = render(
    <MapActionProvider eventId="event-1">
      <ActionProbe />
    </MapActionProvider>,
  );

  fireEvent.click(screen.getByRole("button", { name: "publish event-2" }));
  expect(screen.getByTestId("action-state")).toHaveTextContent("");

  fireEvent.click(screen.getByRole("button", { name: "publish event-1" }));
  expect(screen.getByTestId("action-state")).toHaveTextContent("locate");

  rerender(
    <MapActionProvider eventId="event-2">
      <ActionProbe />
    </MapActionProvider>,
  );
  expect(screen.getByTestId("action-state")).toHaveTextContent("");

  fireEvent.click(screen.getByRole("button", { name: "publish event-2" }));
  expect(screen.getByTestId("action-state")).toHaveTextContent("locate");
});

test("expired map actions are ignored by the loss map consumer", async () => {
  render(
    <MapActionProvider eventId="event-1">
      <LossActionHarness
        action={validLocateAction({
          valid_until: "2000-01-01T00:00:00Z",
        })}
      />
    </MapActionProvider>,
  );

  fireEvent.click(screen.getByRole("button", { name: "publish loss action" }));

  await waitFor(() =>
    expect(screen.getByTestId("loss-map-last-action")).toHaveTextContent(""),
  );
});

test("command hall keeps the qa entry unavailable until the current event loads", async () => {
  let resolveOverview!: (overview: CommandHallOverview) => void;
  getCommandHallOverviewMock.mockImplementation(
    () =>
      new Promise<CommandHallOverview>((resolve) => {
        resolveOverview = resolve;
      }),
  );
  streamCommandHallMock.mockImplementation(
    () => new Promise<void>(() => undefined),
  );

  renderHall();

  expect(screen.getByText("正在加载指挥大厅")).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "智能问策" }),
  ).not.toBeInTheDocument();

  await act(async () => {
    resolveOverview(hallOverview);
  });

  expect(
    await screen.findByRole("button", { name: "智能问策" }),
  ).toBeInTheDocument();
});

test("command hall qa shows citations and tool results without a map", async () => {
  getCommandHallOverviewMock.mockResolvedValue(hallOverview);
  streamCommandHallMock.mockImplementation(
    () => new Promise<void>(() => undefined),
  );
  mockQaStream(qaEvents);

  renderHall();

  fireEvent.click(
    await screen.findByRole("button", { name: "智能问策" }),
  );
  expect(screen.getByLabelText("事件上下文问答")).toBeInTheDocument();
  expect(screen.queryByTestId("qa-map")).not.toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "最近断层在哪里" },
  });
  fireEvent.click(screen.getByRole("button", { name: "提问" }));

  expect(
    await screen.findByText("距最近断层 12.4 公里。"),
  ).toBeInTheDocument();
  expect(screen.getByText("C1")).toBeInTheDocument();
  expect(screen.getAllByText("fault.nearest").length).toBeGreaterThan(0);
});
