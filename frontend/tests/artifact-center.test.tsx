import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { ArtifactCenterPage } from "../src/pages/ArtifactCenterPage";
import { newIdempotencyKey } from "../src/components/ArtifactOverrideDialog";
import { setAccessToken } from "../src/api/client";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function isUuidV4(value: string): boolean {
  return (
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
      value,
    )
  );
}

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

test("shows override only to superadmin", async () => {
  setAccessToken("test-token");
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    jsonResponse({
      production_run_id: "run-1",
      status: "completed",
      required_output_count: 39,
      complete_count: 39,
      degraded_count: 0,
      failed_count: 0,
      timeout_count: 0,
      needs_review_count: 0,
      artifacts: [],
    }),
  );

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
  expect(
    await screen.findByRole("button", { name: "强制覆盖" }),
  ).toBeInTheDocument();
});

test("generates UUIDv4 idempotency keys with and without crypto.randomUUID", () => {
  expect(isUuidV4(newIdempotencyKey())).toBe(true);

  const fallbackCrypto = {
    getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto),
  } as Crypto;
  vi.stubGlobal("crypto", fallbackCrypto);

  expect(isUuidV4(newIdempotencyKey())).toBe(true);
});

test("does not expose override for historical version selection", async () => {
  setAccessToken("test-token");
  const currentArtifact = {
    artifact_id: "current-1",
    artifact_key: "map.epicenter",
    display_name: "震中位置分布图",
    status: "complete",
    quality_grade: "A",
    needs_review: false,
    artifact_version: 1,
    file_name: "epicenter.jpg",
    format: "jpg",
    size_bytes: 100,
    generated_at: "2026-09-30T07:00:00Z",
    download_url: "/api/v1/artifacts/current-1/download",
    thumbnail_url: "/api/v1/artifacts/current-1/thumbnail",
    output_profile: "a3v-professional",
    production_mode: "test",
    publication_mode: "automatic",
  };
  const historicalArtifact = {
    ...currentArtifact,
    artifact_id: "historical-1",
    artifact_key: "doc.background",
    display_name: "地震背景信息",
    artifact_version: 2,
    file_name: "background.docx",
    format: "docx",
    thumbnail_url: null,
  };

  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof Request
          ? input.url
          : String(input);

    if (url.includes("/assessments/events/event-1/current")) {
      return jsonResponse({
        run_id: "run-1",
        event_id: "event-1",
        revision_id: "revision-1",
        run_no: 1,
        status: "completed",
        t1_at: null,
        deadline_at: "2026-09-30T07:05:00Z",
        completed_task_count: 0,
        failed_task_count: 0,
        total_task_count: 0,
        tasks: [],
      });
    }
    if (url.includes("/assessments/runs/run-1/production")) {
      return jsonResponse({
        production_run_id: "run-1",
        assessment_run_id: "run-1",
        status: "completed",
        production_mode: "test",
        launch_mode: "assessment_child",
        generation_seq: 1,
        generation_scope: "full",
        deadline_basis_at: "2026-09-30T07:00:00Z",
        deadline_at: "2026-09-30T07:05:00Z",
        required_output_count: 39,
        complete_count: 39,
        degraded_count: 0,
        failed_count: 0,
        timeout_count: 0,
        needs_review_count: 0,
        is_current: true,
        artifacts: [currentArtifact],
        context_fingerprint: "",
        catalog_version: "2026.09.30",
        template_versions: {},
        data_asset_versions: {},
        renderer_versions: {},
        marker: "【测试】",
      });
    }
    if (url.includes("/events/event-1/artifact-versions")) {
      return jsonResponse([historicalArtifact]);
    }
    return jsonResponse([currentArtifact]);
  });

  render(
    <ArtifactCenterPage
      eventId="event-1"
      userRole="superadmin"
      workgroup={null}
      loadMode="event"
    />,
  );

  await screen.findByRole("button", { name: /震中位置分布图/ });
  const overrideButton = screen.getByRole("button", { name: "强制覆盖" });
  expect(overrideButton).toBeDisabled();

  fireEvent.click(screen.getByRole("button", { name: "地震背景信息" }));
  expect(overrideButton).toBeDisabled();

  fireEvent.click(screen.getByRole("button", { name: /震中位置分布图/ }));
  await waitFor(() => expect(overrideButton).toBeEnabled());
});
