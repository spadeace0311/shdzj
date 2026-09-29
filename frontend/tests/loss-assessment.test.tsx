import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import {
  ApiError,
  getCurrentAssessment,
  getEvent,
  getLossAssessment,
} from "../src/api/client";
import { LossAssessmentPanel } from "../src/components/LossAssessmentPanel";
import { EventDetailPage } from "../src/pages/EventDetailPage";
import type {
  AssessmentRunStatus,
  EventDetail,
  LossProductSummary,
  LossResult,
} from "../src/types";

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

const completeProduct: LossProductSummary = {
  product_id: "product-1",
  product_type: "building_damage",
  status: "complete",
  quality_grade: "L2",
  calibration_status: "reference_uncalibrated",
  coverage_ratio: 0.99,
  partial_scope: false,
  needs_review: false,
  spatialized_estimate: false,
  algorithm_version: "building-structure-matrix-v1",
  parameter_version: "test-only-v1",
  region_profile_version: "shanghai-loss-region-v1",
  output_checksum: "a".repeat(64),
  statistics: {
    collapsed_area_m2: { low: 10, central: 20, high: 30 },
  },
  metrics: [
    {
      area_scope: "city",
      area_code: "310000",
      area_name: "上海市",
      metric_key: "collapsed_area_m2",
      value_type: "central",
      value_status: "available",
      numeric_value: 20,
      unit: "m2",
      precision: 2,
      quality_grade: "L2",
      note: null,
    },
  ],
  reason: null,
};

const result: LossResult = {
  run_id: "run-1",
  event_id: "event-1",
  revision_id: "revision-1",
  effective_run_id: "run-1",
  is_fallback: false,
  products: [completeProduct],
};

const detail: EventDetail = {
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

const assessment: AssessmentRunStatus = {
  run_id: "run-1",
  event_id: "event-1",
  revision_id: "revision-1",
  run_no: 1,
  status: "completed",
  t1_at: "2026-09-17T02:31:00Z",
  deadline_at: "2026-09-17T02:36:00Z",
  completed_task_count: 9,
  failed_task_count: 0,
  total_task_count: 9,
  tasks: [],
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
  getEventMock.mockReset();
  getLossAssessmentMock.mockReset();
});

test("renders loss values and quality evidence", () => {
  render(<LossAssessmentPanel runId="run-1" result={result} />);

  expect(screen.getByText("建筑破坏")).toBeInTheDocument();
  expect(screen.getByText("值班结果")).toBeInTheDocument();
  expect(screen.getByText("L2")).toBeInTheDocument();
  expect(screen.getByText("参考参数未本地校准")).toBeInTheDocument();
  expect(screen.getByText("99.0%")).toBeInTheDocument();
  expect(screen.getByText("20.00")).toBeInTheDocument();
});

test("does not render an empty success state when no product exists", () => {
  render(
    <LossAssessmentPanel
      runId="run-1"
      result={{ ...result, products: [] }}
    />,
  );

  expect(screen.getByText("损失评估结果尚未发布")).toBeInTheDocument();
});

test("labels scenarios and distinguishes unavailable and not-applicable values", () => {
  const product: LossProductSummary = {
    ...completeProduct,
    metrics: [
      {
        area_scope: "city",
        area_code: "310000",
        area_name: "上海市",
        metric_key: "collapsed_area_m2",
        value_type: "low",
        value_status: "available",
        numeric_value: 10,
        unit: "m2",
        precision: 2,
        quality_grade: "L2",
        note: null,
      },
      {
        area_scope: "city",
        area_code: "310000",
        area_name: "上海市",
        metric_key: "collapsed_area_m2",
        value_type: "central",
        value_status: "zero",
        numeric_value: 0,
        unit: "m2",
        precision: 2,
        quality_grade: "L2",
        note: null,
      },
      {
        area_scope: "city",
        area_code: "310000",
        area_name: "上海市",
        metric_key: "collapsed_area_m2",
        value_type: "high",
        value_status: "unavailable",
        numeric_value: null,
        unit: "m2",
        precision: 2,
        quality_grade: "L2",
        note: null,
      },
      {
        area_scope: "city",
        area_code: "310000",
        area_name: "上海市",
        metric_key: "affected_population",
        value_type: "low",
        value_status: "not_applicable",
        numeric_value: null,
        unit: "count",
        precision: 2,
        quality_grade: "L2",
        note: null,
      },
    ],
  };

  render(
    <LossAssessmentPanel
      runId="run-1"
      result={{ ...result, products: [product] }}
    />,
  );

  expect(screen.getByText("低值")).toBeInTheDocument();
  expect(screen.getByText("中值")).toBeInTheDocument();
  expect(screen.getByText("高值")).toBeInTheDocument();
  expect(screen.getByText("0.00")).toBeInTheDocument();
  expect(screen.getAllByText("不可用").length).toBeGreaterThan(0);
  expect(screen.getByText("不适用")).toBeInTheDocument();
});

test("shows spatialized estimate and partial scope evidence", () => {
  const product: LossProductSummary = {
    ...completeProduct,
    spatialized_estimate: true,
    partial_scope: true,
  };

  render(
    <LossAssessmentPanel
      runId="run-1"
      result={{ ...result, products: [product] }}
    />,
  );

  expect(screen.getByText("空间化估算")).toBeInTheDocument();
  expect(screen.getByText("部分范围")).toBeInTheDocument();
});

test("renders partial, review, and unavailable product statuses", () => {
  const products: LossProductSummary[] = [
    {
      ...completeProduct,
      product_id: "partial",
      product_type: "economic_loss",
      status: "partial",
    },
    {
      ...completeProduct,
      product_id: "review",
      product_type: "casualties",
      status: "complete",
      needs_review: true,
    },
    {
      ...completeProduct,
      product_id: "unavailable",
      product_type: "resource_demand",
      status: "unavailable",
      reason: "缺少应急资源参数",
    },
  ];

  render(
    <LossAssessmentPanel
      runId="run-1"
      result={{ ...result, products }}
    />,
  );

  expect(screen.getByText("部分可用")).toBeInTheDocument();
  expect(screen.getByText("待复核")).toBeInTheDocument();
  expect(screen.getByText("不可计算")).toBeInTheDocument();
  expect(screen.getByText("缺少应急资源参数")).toBeInTheDocument();
});

test("hides the loss panel when the loss endpoint returns 404", async () => {
  getEventMock.mockResolvedValue(detail);
  getCurrentAssessmentMock.mockResolvedValue(assessment);
  getLossAssessmentMock.mockRejectedValue(new ApiError("loss_result_not_found", 404));

  renderDetail("event-1");

  await screen.findByText("当前修订");
  await waitFor(() => expect(getLossAssessmentMock).toHaveBeenCalledWith("run-1"));
  expect(screen.queryByText("损失评估结果")).not.toBeInTheDocument();
  expect(screen.queryByText("无法加载损失评估结果")).not.toBeInTheDocument();
});

test("shows a retry state for non-404 loss failures", async () => {
  getEventMock.mockResolvedValue(detail);
  getCurrentAssessmentMock.mockResolvedValue(assessment);
  getLossAssessmentMock
    .mockRejectedValueOnce(new Error("network unavailable"))
    .mockResolvedValueOnce(result);

  renderDetail("event-1");

  expect(await screen.findByText("无法加载损失评估结果")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));

  expect(await screen.findByText("损失评估结果")).toBeInTheDocument();
  await waitFor(() => expect(getLossAssessmentMock).toHaveBeenCalledTimes(2));
});
