import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import {
  createQaSession,
  getQaAnswer,
  listQaSessions,
  streamQaQuestion,
} from "../src/api/client";
import type {
  QaAnswer,
  QaCitation,
  QaMapAction,
  QaSession,
  QaStreamEvent,
  QaToolCall,
} from "../src/types";
import { QaConversation } from "../src/components/QaConversation";
import { applyQaMapAction } from "../src/qa/mapActions";
import type { QaActionMap, QaLayerCatalog } from "../src/qa/mapActions";
import { SmartQaPage } from "../src/pages/SmartQaPage";

vi.mock("maplibre-gl", () => {
  const maps: unknown[] = [];
  return {
    Map: vi.fn(() => {
      const sources = new Map<string, unknown>();
      const layers = new Map<string, unknown>();
      const map = {
        getZoom: vi.fn(() => 9),
        flyTo: vi.fn(),
        fitBounds: vi.fn(),
        getSource: vi.fn((id: string) => sources.get(id)),
        addSource: vi.fn((id: string, source: unknown) => {
          sources.set(id, source);
        }),
        getLayer: vi.fn((id: string) => layers.get(id)),
        addLayer: vi.fn((layer: Record<string, unknown>) => {
          layers.set(String(layer.id), layer);
        }),
        setFilter: vi.fn(),
        setLayoutProperty: vi.fn(),
        on: vi.fn(),
        remove: vi.fn(),
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
    listQaSessions: vi.fn(),
    getQaSession: vi.fn(),
    streamQaQuestion: vi.fn(),
    getQaAnswer: vi.fn(),
    sendQaFeedback: vi.fn(),
  };
});

const createQaSessionMock = vi.mocked(createQaSession);
const listQaSessionsMock = vi.mocked(listQaSessions);
const streamQaQuestionMock = vi.mocked(streamQaQuestion);
const getQaAnswerMock = vi.mocked(getQaAnswer);

const session: QaSession = {
  id: "s1",
  created_by: "operator",
  event_id: null,
  snapshot_id: "snapshot-1",
  title: "当前问答",
  created_at: "2026-10-08T00:00:00Z",
  updated_at: "2026-10-08T00:00:00Z",
};

const secondSession: QaSession = {
  ...session,
  id: "s2",
  title: "历史问答 2",
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
  text: "最近断裂带约 18.2 公里。",
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

const workflowEvents: QaStreamEvent[] = [
  {
    type: "retrieval",
    data: { answer_id: "a1", count: 3, degraded: false },
  },
  { type: "tool", data: { name: "fault.nearest", status: "ok" } },
  { type: "answer_delta", data: { text: "最近断裂带约 18.2 公里。" } },
  {
    type: "answer_completed",
    data: {
      answer_id: "a1",
      status: "completed",
      citations: [citation],
    },
  },
  {
    type: "map_action",
    data: {
      action_type: "buffer",
      target_ref: "event:epicenter",
      radius_km: 50,
      reason: "展示影响范围",
      valid_until: "2026-10-08T00:10:00Z",
    },
  },
];

function mockQaApi(events: QaStreamEvent[], answer = persistedAnswer) {
  listQaSessionsMock.mockResolvedValue([]);
  createQaSessionMock.mockResolvedValue(session);
  streamQaQuestionMock.mockImplementation(
    async (_sessionId, _question, onEvent) => {
      for (const event of events) {
        onEvent(event);
      }
    },
  );
  getQaAnswerMock.mockResolvedValue(answer);
}

beforeEach(() => {
  createQaSessionMock.mockReset();
  listQaSessionsMock.mockReset();
  streamQaQuestionMock.mockReset();
  getQaAnswerMock.mockReset();
});

test("streams an answer and shows citation, tool and map action", async () => {
  mockQaApi(workflowEvents);

  render(
    <MemoryRouter initialEntries={["/qa"]}>
      <SmartQaPage />
    </MemoryRouter>,
  );

  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "震中距最近断裂带多少公里" },
  });
  fireEvent.click(screen.getByRole("button", { name: "提问" }));

  expect(await screen.findByText("最近断裂带约 18.2 公里。")).toBeInTheDocument();
  expect(screen.getByText("C1")).toBeInTheDocument();
  expect(screen.getAllByText("fault.nearest").length).toBeGreaterThan(0);
  expect(screen.getByTestId("qa-map")).toBeInTheDocument();
});

test("keeps the three fixed columns and the QA route usable", async () => {
  listQaSessionsMock.mockResolvedValue([session]);

  render(
    <MemoryRouter initialEntries={["/qa"]}>
      <SmartQaPage />
    </MemoryRouter>,
  );

  expect(screen.getByTestId("qa-left-column")).toBeInTheDocument();
  expect(screen.getByTestId("qa-center-column")).toBeInTheDocument();
  expect(screen.getByTestId("qa-right-column")).toBeInTheDocument();
  expect(screen.getByLabelText("问题")).toBeInTheDocument();
  await screen.findByText("当前问答");
});

test("marks stream text as final only after answer_completed", () => {
  const deltaEvents: QaStreamEvent[] = [
    { type: "answer_delta", data: { text: "正在生成" } },
  ];
  const { rerender } = render(
    <QaConversation
      events={deltaEvents}
      answer={null}
      busy
      onAsk={vi.fn()}
      onFeedback={vi.fn()}
    />,
  );

  expect(screen.getByTestId("qa-answer")).not.toHaveClass("qa-answer--final");

  rerender(
    <QaConversation
      events={[
        ...deltaEvents,
        {
          type: "answer_completed",
          data: { answer_id: "a1", status: "completed" },
        },
      ]}
      answer={persistedAnswer}
      busy={false}
      onAsk={vi.fn()}
      onFeedback={vi.fn()}
    />,
  );

  expect(screen.getByTestId("qa-answer")).toHaveClass("qa-answer--final");
});

test("shows a retry prompt after error without clearing existing tools", () => {
  render(
    <QaConversation
      events={[
        { type: "tool", data: { name: "fault.nearest", status: "ok" } },
        { type: "answer_delta", data: { text: "已收到部分结果" } },
        {
          type: "error",
          data: { code: "model_interrupted", recoverable: true },
        },
      ]}
      answer={null}
      busy={false}
      onAsk={vi.fn()}
      onFeedback={vi.fn()}
    />,
  );

  expect(screen.getByText("fault.nearest")).toBeInTheDocument();
  expect(screen.getByText("已收到部分结果")).toBeInTheDocument();
  expect(screen.getByText(/可重试/)).toBeInTheDocument();
});

test("disables ask on empty input and while streaming", async () => {
  listQaSessionsMock.mockResolvedValue([session]);
  let finishStream!: () => void;
  streamQaQuestionMock.mockImplementation(
    () =>
      new Promise<void>((resolve) => {
        finishStream = resolve;
      }),
  );

  render(
    <MemoryRouter initialEntries={["/qa"]}>
      <SmartQaPage />
    </MemoryRouter>,
  );

  await screen.findByRole("button", { name: "当前问答" });
  const input = screen.getByLabelText("问题");
  const button = screen.getByRole("button", { name: "提问" });
  expect(button).toBeDisabled();

  fireEvent.change(input, { target: { value: "测试问题" } });
  expect(button).toBeEnabled();

  fireEvent.click(button);
  await waitFor(() => expect(button).toBeDisabled());

  finishStream();
  await waitFor(() => expect(button).toBeEnabled());
});

test("aborts the active stream when switching sessions", async () => {
  listQaSessionsMock.mockResolvedValue([session, secondSession]);
  let capturedSignal: AbortSignal | undefined;
  streamQaQuestionMock.mockImplementation(
    (_sessionId, _question, _onEvent, signal) => {
      capturedSignal = signal;
      return new Promise<void>(() => undefined);
    },
  );

  render(
    <MemoryRouter initialEntries={["/qa"]}>
      <SmartQaPage />
    </MemoryRouter>,
  );

  await screen.findByRole("button", { name: "当前问答" });
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "测试问题" },
  });
  fireEvent.click(screen.getByRole("button", { name: "提问" }));
  await screen.findByRole("button", { name: "历史问答 2" });
  fireEvent.click(screen.getByRole("button", { name: "历史问答 2" }));

  expect(capturedSignal?.aborted).toBe(true);
});

test("aborts the active stream on unmount", async () => {
  listQaSessionsMock.mockResolvedValue([session]);
  let capturedSignal: AbortSignal | undefined;
  streamQaQuestionMock.mockImplementation(
    (_sessionId, _question, _onEvent, signal) => {
      capturedSignal = signal;
      return new Promise<void>(() => undefined);
    },
  );

  const { unmount } = render(
    <MemoryRouter initialEntries={["/qa"]}>
      <SmartQaPage />
    </MemoryRouter>,
  );

  await screen.findByRole("button", { name: "当前问答" });
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "测试问题" },
  });
  fireEvent.click(screen.getByRole("button", { name: "提问" }));
  unmount();

  expect(capturedSignal?.aborted).toBe(true);
});

const testCatalog: QaLayerCatalog = {
  targets: {
    "event:epicenter": { center: [121.47, 31.23] },
  },
  layers: {
    faults: {
      id: "qa-faults",
      source: "qa-faults-source",
      featureIdProperty: "fault_key",
    },
    epicenter: {
      id: "qa-epicenter",
      source: "qa-epicenter-source",
      featureIdProperty: "event_id",
    },
  },
};

function makeActionMap(overrides: Record<string, unknown> = {}): QaActionMap {
  return {
    getZoom: vi.fn(() => 4),
    flyTo: vi.fn(),
    fitBounds: vi.fn(),
    getSource: vi.fn(() => undefined),
    addSource: vi.fn(),
    getLayer: vi.fn((id: string) => ({ id })),
    addLayer: vi.fn(),
    setFilter: vi.fn(),
    setLayoutProperty: vi.fn(),
    ...overrides,
  } as unknown as QaActionMap & {
    getSource: ReturnType<typeof vi.fn>;
    getLayer: ReturnType<typeof vi.fn>;
  };
}

function validAction(
  actionType: QaMapAction["action_type"],
): QaMapAction {
  const base = {
    reason: "地图联动",
    valid_until: "2026-10-08T00:10:00Z",
  };
  if (actionType === "locate") {
    return { action_type: "locate", target_ref: "event:epicenter", ...base };
  }
  if (actionType === "fit_bounds") {
    return {
      action_type: "fit_bounds",
      bounds: [120.9, 30.7, 122.0, 31.7],
      ...base,
    };
  }
  if (actionType === "buffer") {
    return {
      action_type: "buffer",
      target_ref: "event:epicenter",
      radius_km: 50,
      ...base,
    };
  }
  if (actionType === "highlight") {
    return {
      action_type: "highlight",
      target_ref: "fault:f1",
      layer_id: "faults",
      ...base,
    };
  }
  return {
    action_type: "set_layers",
    layers: ["faults"],
    ...base,
  };
}

test("applyQaMapAction executes the five valid action types", () => {
  const map = makeActionMap();
  applyQaMapAction(map, validAction("locate"), testCatalog);
  expect(map.flyTo).toHaveBeenCalledWith({
    center: [121.47, 31.23],
    zoom: 8,
  });

  applyQaMapAction(map, validAction("fit_bounds"), testCatalog);
  expect(map.fitBounds).toHaveBeenCalledWith(
    [
      [120.9, 30.7],
      [122.0, 31.7],
    ],
    { padding: 40 },
  );

  applyQaMapAction(map, validAction("buffer"), testCatalog);
  expect(map.addSource).toHaveBeenCalledWith(
    "qa-buffer",
    expect.objectContaining({ type: "geojson" }),
  );

  applyQaMapAction(map, validAction("highlight"), testCatalog);
  expect(map.setFilter).toHaveBeenCalledWith("qa-faults", [
    "==",
    ["get", "fault_key"],
    "f1",
  ]);

  applyQaMapAction(map, validAction("set_layers"), testCatalog);
  expect(map.setLayoutProperty).toHaveBeenCalledWith(
    "qa-faults",
    "visibility",
    "visible",
  );
  expect(map.setLayoutProperty).toHaveBeenCalledWith(
    "qa-epicenter",
    "visibility",
    "none",
  );
});

test("applyQaMapAction rejects unknown and invalid actions", () => {
  const map = makeActionMap();

  applyQaMapAction(
    map,
    { action_type: "script", target_ref: "https://evil.invalid" } as unknown as QaMapAction,
    testCatalog,
  );
  applyQaMapAction(
    map,
    { ...validAction("locate"), target_ref: "unknown:target" } as QaMapAction,
    testCatalog,
  );
  applyQaMapAction(
    map,
    {
      ...validAction("fit_bounds"),
      bounds: [181, 30, 182, 31],
    } as QaMapAction,
    testCatalog,
  );
  applyQaMapAction(
    map,
    { ...validAction("buffer"), radius_km: 0.05 } as QaMapAction,
    testCatalog,
  );
  applyQaMapAction(
    map,
    { ...validAction("highlight"), layer_id: "not-allowed" } as QaMapAction,
    testCatalog,
  );
  applyQaMapAction(
    map,
    { ...validAction("set_layers"), layers: ["not-allowed"] } as QaMapAction,
    testCatalog,
  );

  expect(map.flyTo).not.toHaveBeenCalled();
  expect(map.fitBounds).not.toHaveBeenCalled();
  expect(map.addSource).not.toHaveBeenCalled();
  expect(map.setFilter).not.toHaveBeenCalled();
  expect(map.setLayoutProperty).not.toHaveBeenCalled();
});

test("buffer action respects the radius bounds and unsafe target text", () => {
  const map = makeActionMap();

  applyQaMapAction(
    map,
    { ...validAction("buffer"), radius_km: 500 } as QaMapAction,
    testCatalog,
  );
  expect(map.addSource).toHaveBeenCalledTimes(1);

  applyQaMapAction(
    map,
    { ...validAction("buffer"), radius_km: 501 } as QaMapAction,
    testCatalog,
  );
  applyQaMapAction(
    map,
    {
      ...validAction("buffer"),
      target_ref: 'event:epicenter"><script>alert(1)</script>',
    } as QaMapAction,
    testCatalog,
  );
  expect(map.addSource).toHaveBeenCalledTimes(1);
});

test("locate uses a zoom no lower than eight when the map is already zoomed in", () => {
  const map = makeActionMap();
  (map.getZoom as ReturnType<typeof vi.fn>).mockReturnValue(12);

  applyQaMapAction(map, validAction("locate"), testCatalog);

  expect(map.flyTo).toHaveBeenCalledWith({
    center: [121.47, 31.23],
    zoom: 12,
  });
});
