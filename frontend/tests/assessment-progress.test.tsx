import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { AssessmentProgressCard } from "../src/components/AssessmentProgressCard";
import type { AssessmentRunStatus } from "../src/types";

const run: AssessmentRunStatus = {
  run_id: "run-1",
  event_id: "event-1",
  revision_id: "revision-1",
  run_no: 1,
  status: "running",
  t1_at: "2026-09-26T01:00:00Z",
  deadline_at: "2026-09-26T01:05:00Z",
  completed_task_count: 3,
  failed_task_count: 0,
  total_task_count: 9,
  tasks: [
    "intensity.model",
    "intensity.instrument",
    "intensity.fusion",
    "loss.population",
    "loss.casualties",
    "loss.buildings",
    "loss.economic",
    "report.rapid_assessment",
    "workgroup.response_tasks",
  ].map((taskKey, index) => ({
    id: `task-${index + 1}`,
    task_key: taskKey,
    task_type: "assessment",
    component: taskKey,
    priority: 100 - index,
    sequence: index + 1,
    status: index < 3 ? "succeeded" : "pending",
    deadline_at: "2026-09-26T01:05:00Z",
    started_at: null,
    completed_at: null,
    attempt_count: 0,
    max_attempts: 3,
    last_error: null,
  })),
};

test("renders assessment progress and task status dots", () => {
  render(<AssessmentProgressCard run={run} />);

  expect(screen.getByText("评估编排")).toBeInTheDocument();
  expect(screen.getByText("V1")).toBeInTheDocument();
  expect(screen.getByText("执行中")).toBeInTheDocument();
  expect(screen.getByText("3 / 9")).toBeInTheDocument();
  expect(screen.getByText("距 T1 +5 分钟截止")).toBeInTheDocument();
  expect(screen.getAllByTestId("assessment-task-status")).toHaveLength(9);
});

test("does not render when no assessment run exists", () => {
  render(<AssessmentProgressCard run={null} />);

  expect(screen.queryByText("评估编排")).not.toBeInTheDocument();
});
