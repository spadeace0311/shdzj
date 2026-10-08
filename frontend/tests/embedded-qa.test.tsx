import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import * as maplibregl from "maplibre-gl";
import { beforeEach, expect, test, vi } from "vitest";

import {
  clearAccessToken,
  createQaSession,
  getAssessmentProduction,
  getCommandHallOverview,
  getCurrentAssessment,
  getEvent,
  getLossAssessment,
  getQaAnswer,
  setAccessToken,
  streamCommandHall,
  streamQaQuestion,
} from "../src/api/client";
import { LossMap } from "../src/components/LossMap";
import { QaPanel } from "../src/components/QaPanel";
import { EventDetailPage } from "../src/pages/EventDetailPage";
import { CommandHallPage } from "../src/pages/CommandHallPage";
import stylesCss from "../src/styles.css?raw";
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

interface MockEmbeddedMap {
  options: Record<string, unknown>;
  sources: Map<string, Record<string, unknown>>;
  layers: Map<string, Record<string, unknown>>;
  addSource: ReturnType<typeof vi.fn>;
  addLayer: ReturnType<typeof vi.fn>;
  getSource: ReturnType<typeof vi.fn>;
  getLayer: ReturnType<typeof vi.fn>;
  getZoom: ReturnType<typeof vi.fn>;
  flyTo: ReturnType<typeof vi.fn>;
  fitBounds: ReturnType<typeof vi.fn>;
  setFilter: ReturnType<typeof vi.fn>;
  setLayoutProperty: ReturnType<typeof vi.fn>;
  setPaintProperty: ReturnType<typeof vi.fn>;
  removeLayer: ReturnType<typeof vi.fn>;
  removeSource: ReturnType<typeof vi.fn>;
  on: ReturnType<typeof vi.fn>;
  remove: ReturnType<typeof vi.fn>;
}

vi.mock("maplibre-gl", () => {
  const maps: MockEmbeddedMap[] = [];
  return {
    Map: vi.fn((options: Record<string, unknown>) => {
      const sources = new Map<string, Record<string, unknown>>();
      const layers = new Map<string, Record<string, unknown>>();
      const map: MockEmbeddedMap = {
        options,
        sources,
        layers,
        addSource: vi.fn((id: string, source: Record<string, unknown>) => {
          sources.set(id, source);
        }),
        addLayer: vi.fn((layer: Record<string, unknown>) => {
          layers.set(layer.id as string, layer);
        }),
        getSource: vi.fn((id: string) => sources.get(id)),
        getLayer: vi.fn((id: string) => layers.get(id)),
        getZoom: vi.fn(() => 9.5),
        flyTo: vi.fn(),
        fitBounds: vi.fn(),
        setFilter: vi.fn(),
        setLayoutProperty: vi.fn(),
        setPaintProperty: vi.fn(),
        removeLayer: vi.fn((id: string) => {
          layers.delete(id);
        }),
        removeSource: vi.fn((id: string) => {
          sources.delete(id);
        }),
        on: vi.fn(),
        remove: vi.fn(() => {
          sources.clear();
          layers.clear();
        }),
      };
      maps.push(map);
      return map;
    }),
    __maps: maps,
  };
});

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

function embeddedMapModule(): { __maps: MockEmbeddedMap[] } {
  return maplibregl as unknown as { __maps: MockEmbeddedMap[] };
}

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

const auditedAnswer: QaAnswer = {
  ...persistedAnswer,
  structured: {
    nearest_fault_km: 12.4,
    notes: ["工具值与文档值一致"],
  },
  degraded_reasons: ["reranker_unavailable"],
  model_name: "deepseek-chat",
  model_version: "chat-v1",
  prompt_version: "qa-2026-10-08",
  execution_plan: {
    intent: "knowledge_query",
  },
  tool_call_summary: [
    {
      name: "fault.nearest",
      status: "ok",
      source: "shanghai.fault",
      version: "v1",
    },
  ],
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

function renderQaPanel(eventId: string) {
  return render(
    <MapActionProvider eventId={eventId}>
      <QaPanel eventId={eventId} mode="event" onClose={vi.fn()} />
    </MapActionProvider>,
  );
}

function askQuestion(question: string) {
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: question },
  });
  fireEvent.click(screen.getByRole("button", { name: "提问" }));
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
  clearAccessToken();
  createQaSessionMock.mockReset();
  getAssessmentProductionMock.mockReset();
  getCommandHallOverviewMock.mockReset();
  getCurrentAssessmentMock.mockReset();
  getEventMock.mockReset();
  getLossAssessmentMock.mockReset();
  getQaAnswerMock.mockReset();
  streamCommandHallMock.mockReset();
  streamQaQuestionMock.mockReset();
  embeddedMapModule().__maps.length = 0;
});

test("event detail opens qa panel with the current event and publishes map action", async () => {
  setAccessToken("embedded-qa-token");
  mockEventDetailApi();
  mockQaStream([
    ...qaEvents,
    {
      type: "map_action",
      data: {
        action_type: "locate",
        target_ref: "fault:f1",
        coordinates: [121.5, 31.2],
        feature_id: "f1",
        reason: "定位最近断层",
        valid_until: "2099-01-01T00:00:00Z",
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
  const map = embeddedMapModule().__maps.at(-1);
  expect(map?.flyTo).toHaveBeenCalledWith({
    center: [121.5, 31.2],
    zoom: 9.5,
  });
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

test("embedded qa renders structured, degraded and audit results", async () => {
  mockQaStream(qaEvents);
  getQaAnswerMock.mockResolvedValue(auditedAnswer);

  renderQaPanel("event-1");
  askQuestion("查看结构化与审计结果");

  expect(await screen.findByTestId("qa-structured")).toHaveTextContent(
    "nearest_fault_km",
  );
  expect(screen.getByTestId("qa-degraded")).toHaveTextContent(
    "reranker_unavailable",
  );
  const audit = screen.getByTestId("qa-audit");
  expect(audit).toHaveTextContent("deepseek-chat");
  expect(audit).toHaveTextContent("chat-v1");
  expect(audit).toHaveTextContent("qa-2026-10-08");
  expect(audit).toHaveTextContent("knowledge_query");
});

test("embedded qa recovers from an answer_id carried by answer_delta", async () => {
  const recoveryEvents: QaStreamEvent[] = [
    { type: "retrieval", data: { answer_id: "a1", count: 1 } },
    {
      type: "answer_delta",
      data: { answer_id: "a1", text: "断流前部分结果。" },
    },
    {
      type: "error",
      data: { answer_id: "a1", code: "stream_failed", recoverable: true },
    },
  ];
  mockQaStream(recoveryEvents);
  getQaAnswerMock.mockResolvedValue(persistedAnswer);

  renderQaPanel("event-1");
  askQuestion("从任意事件恢复");

  expect(
    await screen.findByText("距最近断层 12.4 公里。"),
  ).toBeInTheDocument();
  expect(getQaAnswerMock).toHaveBeenCalledWith("a1");
});

test("stale session creation does not leak into a changed event", async () => {
  let resolveOldSession!: (value: QaSession) => void;
  const newSession: QaSession = {
    ...session,
    id: "s2",
    event_id: "event-2",
  };
  createQaSessionMock
    .mockImplementationOnce(
      () =>
        new Promise<QaSession>((resolve) => {
          resolveOldSession = resolve;
        }),
    )
    .mockImplementationOnce(async () => newSession);
  streamQaQuestionMock.mockImplementation(async () => undefined);
  getQaAnswerMock.mockResolvedValue(persistedAnswer);

  const { rerender } = renderQaPanel("event-1");
  askQuestion("旧事件问题");
  await waitFor(() =>
    expect(createQaSessionMock).toHaveBeenCalledTimes(1),
  );

  rerender(
    <MapActionProvider eventId="event-2">
      <QaPanel eventId="event-2" mode="event" onClose={vi.fn()} />
    </MapActionProvider>,
  );

  await act(async () => {
    resolveOldSession(session);
  });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "提问" })).toBeEnabled(),
  );

  askQuestion("新事件问题");
  await waitFor(() =>
    expect(createQaSessionMock).toHaveBeenCalledTimes(2),
  );
  expect(createQaSessionMock).toHaveBeenNthCalledWith(2, {
    title: "当前问答",
    event_id: "event-2",
  });
  await waitFor(() =>
    expect(streamQaQuestionMock).toHaveBeenCalledWith(
      "s2",
      expect.anything(),
      expect.anything(),
      expect.anything(),
    ),
  );
});

test("double clicking ask creates only one session", async () => {
  let resolveSession!: (value: QaSession) => void;
  createQaSessionMock.mockImplementation(
    () =>
      new Promise<QaSession>((resolve) => {
        resolveSession = resolve;
      }),
  );
  streamQaQuestionMock.mockImplementation(async () => undefined);
  getQaAnswerMock.mockResolvedValue(persistedAnswer);

  renderQaPanel("event-1");
  askQuestion("快速双击问题");

  const askButton = screen.getByRole("button", { name: "提问" });
  await waitFor(() => expect(askButton).toBeDisabled());
  fireEvent.click(askButton);

  expect(createQaSessionMock).toHaveBeenCalledTimes(1);

  await act(async () => {
    resolveSession(session);
  });
  await waitFor(() => expect(askButton).toBeEnabled());
  expect(createQaSessionMock).toHaveBeenCalledTimes(1);
});

test("event detail reserves layout width while the qa drawer is open", async () => {
  mockEventDetailApi();
  mockQaStream(qaEvents);

  renderEventDetail();

  await screen.findByText("当前修订");
  const pageSection = document.querySelector(".page-section");
  expect(pageSection).not.toHaveClass("page-section--qa-open");

  fireEvent.click(screen.getByRole("button", { name: "智能问策" }));

  expect(document.querySelector(".page-section")).toHaveClass(
    "page-section--qa-open",
  );
  expect(stylesCss).toMatch(
    /\.page-section--qa-open\s*\{[^}]*max-width:\s*min\(1720px,\s*calc\(100%\s*-\s*560px\)\);/,
  );
});

test("hall qa scrim starts below the command hall header", () => {
  expect(stylesCss).toMatch(
    /\.qa-panel-layer--hall \.qa-panel__scrim\s*,\s*\.qa-panel-layer--event \.qa-panel__scrim\s*\{[^}]*top:\s*var\(--qa-hall-top,\s*0\);/,
  );
});
