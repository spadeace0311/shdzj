import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { ArtifactCenterPage } from "../src/pages/ArtifactCenterPage";
import { setAccessToken } from "../src/api/client";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

test("lists grouped artifacts and downloads with an authenticated blob", async () => {
  setAccessToken("test-token");
  const fetchMock = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(
      jsonResponse({
        production_run_id: "run-1",
        status: "partial",
        required_output_count: 39,
        complete_count: 37,
        degraded_count: 1,
        failed_count: 1,
        timeout_count: 0,
        needs_review_count: 1,
        artifacts: [
          {
            artifact_id: "artifact-1",
            artifact_key: "map.epicenter",
            display_name: "震中位置分布图",
            status: "complete",
            quality_grade: "A",
            needs_review: false,
            artifact_version: 1,
            file_name: "浦东新区_5.1级地震_震中位置分布图_V001_20260930-153000.jpg",
            download_url: "/api/v1/artifacts/artifact-1/download",
            thumbnail_url: "/api/v1/artifacts/artifact-1/thumbnail",
          },
        ],
      }),
    )
    .mockResolvedValueOnce(new Response(new Blob(["image"]), { status: 200 }));

  render(
    <ArtifactCenterPage
      eventId="event-1"
      userRole="group_member"
      workgroup="应急技术组"
    />,
  );

  expect(await screen.findByText("震中位置分布图")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "下载原文件" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  expect(fetchMock.mock.calls[1][1]?.headers).toMatchObject({
    Authorization: "Bearer test-token",
  });
});

test("shows override only to superadmin", () => {
  const { rerender } = render(
    <ArtifactCenterPage
      eventId="event-1"
      userRole="group_member"
      workgroup="应急技术组"
    />,
  );
  expect(screen.queryByRole("button", { name: "强制覆盖" })).not.toBeInTheDocument();

  rerender(
    <ArtifactCenterPage
      eventId="event-1"
      userRole="superadmin"
      workgroup={null}
    />,
  );
  expect(screen.getByRole("button", { name: "强制覆盖" })).toBeInTheDocument();
});
