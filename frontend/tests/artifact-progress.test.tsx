import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import {
  ArtifactProgressCard,
  buildArtifactProgressSummary,
} from "../src/components/ArtifactProgressCard";

test("progress card uses latest full run and shows grouped counts", () => {
  render(
    <ArtifactProgressCard
      production={{
        productionRunId: "run-1",
        status: "running",
        completeCount: 37,
        degradedCount: 1,
        failedCount: 1,
        timeoutCount: 0,
        needsReviewCount: 1,
        requiredOutputCount: 39,
        mapCount: 26,
        backgroundDocumentCount: 8,
        coreDocumentCount: 3,
        progressText: "37/39",
      }}
    />,
  );

  expect(screen.getByText("37/39")).toBeInTheDocument();
  expect(screen.getByText("图件")).toBeInTheDocument();
  expect(screen.getByText("背景文档")).toBeInTheDocument();
  expect(screen.getByText("核心文档")).toBeInTheDocument();
});

test("progress counts accepted degraded outputs as produced outputs", () => {
  const summary = buildArtifactProgressSummary({
    production_run_id: "run-2",
    status: "completed",
    required_output_count: 39,
    complete_count: 28,
    degraded_count: 11,
    failed_count: 0,
    timeout_count: 0,
    artifacts: [
      { artifact_key: "map.intensity" },
      { artifact_key: "map.building_grid" },
      { artifact_key: "doc.background" },
    ],
  } as never);

  expect(summary?.progressText).toBe("39/39");
});
