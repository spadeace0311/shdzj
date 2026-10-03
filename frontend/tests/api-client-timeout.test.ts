import { afterEach, expect, test, vi } from "vitest";

import {
  importDataAsset,
  listDataAssets,
  publishCollaborationDeliverableVersion,
  setAccessToken,
  uploadCollaborationDeliverableVersion,
} from "../src/api/client";


afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});


test("ordinary requests keep the ten-second timeout", async () => {
  vi.useFakeTimers();
  setAccessToken("token");
  vi.stubGlobal(
    "fetch",
    vi.fn((_path: string, init: RequestInit) => {
      return new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        });
      });
    }),
  );

  const request = listDataAssets();
  const rejection = request.catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(10_000);

  await expect(rejection).resolves.toEqual(
    expect.objectContaining({ status: 408 }),
  );
});


test("multipart imports are not aborted by the ordinary request timeout", async () => {
  vi.useFakeTimers();
  setAccessToken("token");
  let resolveFetch!: (response: Response) => void;
  const fetchMock = vi.fn((_path: string, init: RequestInit) => {
    if (init.signal) {
      init.signal.addEventListener("abort", () => {
        throw new Error("multipart import was aborted");
      });
    }
    return new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
  });
  vi.stubGlobal("fetch", fetchMock);

  const request = importDataAsset("shanghai.admin.town", {
    version: "2023.1",
    source_uri: "https://example.gov.invalid/town.geojson",
    change_note: "test",
    file: new File(["{}"], "town.geojson", { type: "application/geo+json" }),
  });
  await vi.advanceTimersByTimeAsync(60_000);
  expect(fetchMock).toHaveBeenCalledTimes(1);

  resolveFetch(
    new Response(
      JSON.stringify({
        job_id: "job-1",
        version_id: "version-1",
        asset_key: "shanghai.admin.town",
        version: "2023.1",
        status: "queued",
      }),
      {
        status: 202,
        headers: { "Content-Type": "application/json" },
      },
    ),
  );

  await expect(request).resolves.toEqual(
    expect.objectContaining({ job_id: "job-1" }),
  );
});


test("deliverable uploads use a finite long timeout", async () => {
  vi.useFakeTimers();
  setAccessToken("token");
  vi.stubGlobal(
    "fetch",
    vi.fn((_path: string, init: RequestInit) => {
      return new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        });
      });
    }),
  );

  const request = uploadCollaborationDeliverableVersion(
    "deliverable-1",
    new File(["result"], "result.pdf", { type: "application/pdf" }),
    "",
    "upload-key",
  );
  const rejection = request.catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(120_000);

  await expect(rejection).resolves.toEqual(
    expect.objectContaining({ status: 408 }),
  );
});


test("publish requests keep the ordinary finite timeout", async () => {
  vi.useFakeTimers();
  setAccessToken("token");
  vi.stubGlobal(
    "fetch",
    vi.fn((_path: string, init: RequestInit) => {
      return new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        });
      });
    }),
  );

  const request = publishCollaborationDeliverableVersion(
    "deliverable-1",
    "version-1",
    4,
    "确认发布",
    "publish-key",
  );
  const rejection = request.catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(10_000);

  await expect(rejection).resolves.toEqual(
    expect.objectContaining({ status: 408 }),
  );
});
