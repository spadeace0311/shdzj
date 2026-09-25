import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { getEvent } from "../src/api/client";
import { EventDetailPage } from "../src/pages/EventDetailPage";
import type { EventDetail } from "../src/types";

vi.mock("../src/api/client", () => ({
  getEvent: vi.fn(),
}));

const getEventMock = vi.mocked(getEvent);

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
  getEventMock.mockReset();
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
  });

  renderDetail(`event-${kind}`);

  const section = (await screen.findByText("当前修订")).closest("section");
  expect(section).not.toBeNull();
  expect(screen.getByText(label)).toBeInTheDocument();
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
