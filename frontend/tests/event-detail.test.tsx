import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import {
  ApiError,
  getCurrentAssessment,
  getEvent,
  getLossAssessment,
} from "../src/api/client";
import { EventDetailPage } from "../src/pages/EventDetailPage";
import type {
  AssessmentRunStatus,
  EventDetail,
  LossResult,
} from "../src/types";

vi.mock("../src/components/LossAssessmentPanel", () => ({
  LossAssessmentPanel: (props: {
    runId: string;
    fusedIntensityProductId?: string | null;
    fusedIntensityRunId?: string;
  }) => (
    <div
      data-testid="loss-panel"
      data-fused={props.fusedIntensityProductId ?? ""}
      data-run={props.runId ?? ""}
      data-fused-run={props.fusedIntensityRunId ?? ""}
    />
  ),
}));

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return {
    ...actual,
    getCurrentAssessment: vi.fn(),
    getEvent: vi.fn(),
    getLossAssessment: vi.fn(),
  };
});

const getCurrentAssessmentMock = vi.mocked(getCurrentAssessment);
const getEventMock = vi.mocked(getEvent);
const getLossAssessmentMock = vi.mocked(getLossAssessment);

const baseDetail: EventDetail = {
  id: "event-1",
  source: "cenc",
  place: "上海浦东新区",
  magnitude: "5.2",
  depth_km: "12.00",
  origin_time: "2026-09-17T02:30:05Z",
  longitude: "121.540000",
  latitude: "31.220000",
  institutional_level: "major",
  service_level: 2,
  response_suggestion: {
    institutional_level: "major",
    service_level: 2,
    downgraded: false,
    causes: ["上海市行政区域震级满足制度响应条件"],
    rule_version: "2026.1",
  },
  response_rule_version: "2026.1",
  revision_no: 1,
  event_kind: "formal",
  lifecycle_state: "formal_triggered",
  t1_at: "2026-09-17T02:31:00Z",
};

function renderDetail(eventId: string) {
  return render(
    <MemoryRouter initialEntries={[`/events/${eventId}`]}>
      <Routes>
        <Route path="/events/:eventId" element={<EventDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  getCurrentAssessmentMock.mockReset();
  getCurrentAssessmentMock.mockResolvedValue(null);
  getEventMock.mockReset();
  getLossAssessmentMock.mockReset();
});

test.each([
  ["test", "测试"],
  ["drill", "演练"],
] as const)("renders %s identifier directly on the detail page", async (kind, label) => {
  getEventMock.mockResolvedValue({
    ...baseDetail,
    event_kind: kind,
    institutional_level: null,
    service_level: null,
    response_suggestion: null,
    response_rule_version: null,
    lifecycle_state: "not_applicable",
  });

  renderDetail(`event-${kind}`);

  const section = (await screen.findByText("当前修订")).closest("section");
  expect(section).not.toBeNull();
  expect(screen.getByText(label)).toBeInTheDocument();
  expect(screen.getByText("不适用")).toBeInTheDocument();
});

test("renders an old suggestion snapshot without causes", async () => {
  getEventMock.mockResolvedValue({
    ...baseDetail,
    response_suggestion: {
      institutional_level: "major",
    } as EventDetail["response_suggestion"],
  });

  renderDetail("event-1");

  expect(await screen.findByText("暂无触发原因记录")).toBeInTheDocument();
  expect(screen.getByText("未降级")).toBeInTheDocument();
});

test("renders the event lifecycle state and immutable T1", async () => {
  getEventMock.mockResolvedValue(baseDetail);

  renderDetail("event-1");

  expect(await screen.findByText("正式报已触发评估")).toBeInTheDocument();
  expect(screen.getByText("T1")).toBeInTheDocument();
  expect(screen.getByText("2026/09/17 10:31:00")).toBeInTheDocument();
});

test("passes the first available fusion product into the loss map", async () => {
  const assessment: AssessmentRunStatus = {
    run_id: "run-1",
    event_id: "event-1",
    revision_id: "revision-1",
    run_no: 1,
    status: "completed",
    t1_at: "2026-09-17T02:31:00Z",
    deadline_at: "2026-09-17T02:36:00Z",
    completed_task_count: 0,
    failed_task_count: 0,
    total_task_count: 0,
    tasks: [],
    intensity: {
      run_id: "run-1",
      event_id: "event-1",
      revision_id: "revision-1",
      run_status: "completed",
      products: [
        {
          product_id: "instrument-1",
          product_type: "instrument",
          status: "available",
          quality_grade: null,
          coverage_ratio: 1,
          output_checksum: null,
          statistics: {},
        },
        {
          product_id: "fusion-1",
          product_type: "fusion",
          status: "available",
          quality_grade: null,
          coverage_ratio: 1,
          output_checksum: "f".repeat(64),
          statistics: {},
        },
      ],
    },
  };
  const loss: LossResult = {
    run_id: "run-1",
    event_id: "event-1",
    revision_id: "revision-1",
    effective_run_id: "run-1",
    is_fallback: false,
    products: [],
  };

  getEventMock.mockResolvedValue(baseDetail);
  getCurrentAssessmentMock.mockResolvedValue(assessment);
  getLossAssessmentMock.mockResolvedValue(loss);

  renderDetail("event-1");

  const panel = await screen.findByTestId("loss-panel");
  expect(panel).toHaveAttribute("data-fused", "fusion-1");
  expect(panel).toHaveAttribute("data-fused-run", "run-1");
});

test("passes the intensity response run ID when it differs from the outer run", async () => {
  const assessment: AssessmentRunStatus = {
    run_id: "run-outer",
    event_id: "event-1",
    revision_id: "revision-outer",
    run_no: 1,
    status: "completed",
    t1_at: "2026-09-17T02:31:00Z",
    deadline_at: "2026-09-17T02:36:00Z",
    completed_task_count: 0,
    failed_task_count: 0,
    total_task_count: 0,
    tasks: [],
    intensity: {
      run_id: "run-effective",
      event_id: "event-1",
      revision_id: "revision-effective",
      run_status: "completed",
      products: [
        {
          product_id: "fusion-1",
          product_type: "fusion",
          status: "available",
          quality_grade: null,
          coverage_ratio: 1,
          output_checksum: "f".repeat(64),
          statistics: {},
        },
      ],
    },
  };
  const loss: LossResult = {
    run_id: "run-outer",
    event_id: "event-1",
    revision_id: "revision-outer",
    effective_run_id: "run-outer",
    is_fallback: false,
    products: [],
  };

  getEventMock.mockResolvedValue(baseDetail);
  getCurrentAssessmentMock.mockResolvedValue(assessment);
  getLossAssessmentMock.mockResolvedValue(loss);

  renderDetail("event-1");

  const panel = await screen.findByTestId("loss-panel");
  expect(panel).toHaveAttribute("data-run", "run-outer");
  expect(panel).toHaveAttribute("data-fused-run", "run-effective");
});

test("treats a 404 ApiError from loss loading as an idle result", async () => {
  const assessment: AssessmentRunStatus = {
    run_id: "run-1",
    event_id: "event-1",
    revision_id: "revision-1",
    run_no: 1,
    status: "completed",
    t1_at: "2026-09-17T02:31:00Z",
    deadline_at: "2026-09-17T02:36:00Z",
    completed_task_count: 0,
    failed_task_count: 0,
    total_task_count: 0,
    tasks: [],
  };

  getEventMock.mockResolvedValue(baseDetail);
  getCurrentAssessmentMock.mockResolvedValue(assessment);
  getLossAssessmentMock.mockRejectedValue(
    new ApiError("loss_result_not_found", 404),
  );

  renderDetail("event-1");

  await screen.findByText("当前修订");
  await waitFor(() =>
    expect(getLossAssessmentMock).toHaveBeenCalledWith("run-1"),
  );
  expect(screen.queryByTestId("loss-panel")).not.toBeInTheDocument();
  expect(screen.queryByText("无法加载损失评估结果")).not.toBeInTheDocument();
});
