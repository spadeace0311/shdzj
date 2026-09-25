import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { listEvents } from "../src/api/client";
import { EventListPage } from "../src/pages/EventListPage";

vi.mock("../src/api/client", () => ({
  listEvents: vi.fn(),
}));

const listEventsMock = vi.mocked(listEvents);

function renderListPage() {
  return render(
    <MemoryRouter>
      <EventListPage />
    </MemoryRouter>,
  );
}

const formalEvent = {
  id: "event-1",
  source: "cenc",
  event_kind: "formal",
  place: "上海浦东新区",
  magnitude: "5.2",
  depth_km: "12.00",
  origin_time: "2026-09-17T02:30:05Z",
  longitude: "121.540000",
  latitude: "31.220000",
  institutional_level: "major",
  service_level: 2,
  revision_no: 1,
};

beforeEach(() => {
  listEventsMock.mockReset();
});

test("renders dual response levels", async () => {
  listEventsMock.mockResolvedValue([formalEvent]);

  renderListPage();

  expect(await screen.findByText("上海浦东新区")).toBeInTheDocument();
  expect(screen.getByText("重大响应")).toBeInTheDocument();
  expect(screen.getByText("服务响应二级")).toBeInTheDocument();
});

test("keeps test and drill identifiers visible", async () => {
  listEventsMock.mockResolvedValue([
    { ...formalEvent, id: "event-test", event_kind: "test", institutional_level: null },
    { ...formalEvent, id: "event-drill", event_kind: "drill", institutional_level: null },
  ]);

  renderListPage();

  const table = await screen.findByRole("table");
  expect(within(table).getByText("测试")).toBeInTheDocument();
  expect(within(table).getByText("演练")).toBeInTheDocument();
});

test("renders an empty state when no events are available", async () => {
  listEventsMock.mockResolvedValue([]);

  renderListPage();

  expect(await screen.findByText("暂无地震事件")).toBeInTheDocument();
});

test("renders a request failure state", async () => {
  listEventsMock.mockRejectedValue(new Error("network unavailable"));

  renderListPage();

  expect(await screen.findByText("无法加载事件列表")).toBeInTheDocument();
});
