import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { ArtifactProgressCard } from "../src/components/ArtifactProgressCard";

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
