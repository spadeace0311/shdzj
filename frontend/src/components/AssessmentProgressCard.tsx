import {
  formatDateTime,
  type AssessmentRunStatus,
  type AssessmentRunStatusValue,
  type AssessmentTaskStatusValue,
} from "../types";

interface AssessmentProgressCardProps {
  run: AssessmentRunStatus | null;
}

const RUN_STATUS_LABELS: Record<AssessmentRunStatusValue, string> = {
  pending: "待执行",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  canceled: "已取消",
};

const TASK_STATUS_LABELS: Record<AssessmentTaskStatusValue, string> = {
  pending: "待执行",
  running: "执行中",
  succeeded: "已完成",
  failed: "失败",
  skipped: "已跳过",
  canceled: "已取消",
};

export function AssessmentProgressCard({ run }: AssessmentProgressCardProps) {
  if (!run) {
    return null;
  }

  return (
    <section className="assessment-progress" aria-labelledby="assessment-progress-title">
      <header className="assessment-progress__header">
        <div>
          <p className="eyebrow">评估运行</p>
          <h2 id="assessment-progress-title">评估编排</h2>
        </div>
        <div className="assessment-progress__badges">
          <span className="revision-chip">V{run.run_no}</span>
          <span className={`assessment-run-status assessment-run-status--${run.status}`}>
            {RUN_STATUS_LABELS[run.status]}
          </span>
        </div>
      </header>

      <dl className="assessment-progress__facts">
        <div>
          <dt>任务进度</dt>
          <dd>{run.completed_task_count} / {run.total_task_count}</dd>
        </div>
        <div>
          <dt>T1</dt>
          <dd>
            {run.t1_at
              ? formatDateTime(run.t1_at)
              : "不适用（人工/测试/演练）"}
          </dd>
        </div>
        <div>
          <dt>截止时间</dt>
          <dd>{formatDateTime(run.deadline_at)}</dd>
        </div>
        <div>
          <dt>时限</dt>
          <dd>{formatDeadlineLabel(run)}</dd>
        </div>
      </dl>

      <div className="assessment-task-progress">
        <div className="assessment-task-progress__header">
          <span>任务状态</span>
          <span>
            失败 {run.failed_task_count} · 总任务 {run.total_task_count}
          </span>
        </div>
        <ul className="assessment-task-strip" aria-label="评估任务状态">
          {run.tasks.slice(0, 9).map((task) => (
            <li
              key={task.id}
              className={`assessment-task-dot assessment-task-dot--${task.status}`}
              data-testid="assessment-task-status"
              aria-label={`${TASK_STATUS_LABELS[task.status]}：${task.task_key}`}
              title={`${TASK_STATUS_LABELS[task.status]} · ${task.task_key}`}
            />
          ))}
        </ul>
      </div>
    </section>
  );
}

function formatDeadlineLabel(run: AssessmentRunStatus): string {
  if (!run.t1_at) {
    return "截止时限未知";
  }
  const t1 = new Date(run.t1_at).getTime();
  const deadline = new Date(run.deadline_at).getTime();
  if (Number.isNaN(t1) || Number.isNaN(deadline) || deadline < t1) {
    return "截止时限未知";
  }
  const minutes = Math.round((deadline - t1) / 60_000);
  return `距 T1 +${minutes} 分钟截止`;
}
