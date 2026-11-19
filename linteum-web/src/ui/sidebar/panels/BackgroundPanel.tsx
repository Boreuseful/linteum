import { ListTodo, RefreshCw } from "lucide-react";
import { useEffect } from "react";
import { useStrings } from "../../../hooks/useStrings";
import { useAppState } from "../../../hooks/useStore";
import { controller } from "../../../transport/controller";
import { PanelEmpty, PanelLoading } from "../../common/EmptyState";
import { formatDuration } from "../../common/format";
import { StatusBadge } from "../../common/StatusBadge";
import type { PanelProps } from "../types";

export function BackgroundPanel(_props: PanelProps) {
  const strings = useStrings();
  const state = useAppState();
  const active = state.active;
  const open = state.ui.openPanels.background === true;

  useEffect(() => {
    if (open && active) controller.refreshTasks();
  }, [open, active?.id]);

  if (!active) return <PanelEmpty>{strings.sidebar.empty}</PanelEmpty>;

  const tasks = Object.values(active.background).sort(
    (a, b) => (b.startedSeq ?? 0) - (a.startedSeq ?? 0),
  );

  return (
    <>
      <div className="panel-toolbar">
        <button
          className="btn btn-sm"
          onClick={() => controller.refreshTasks()}
          disabled={active.tasksLoading}
        >
          <RefreshCw size={13} /> {strings.sidebar.refresh}
        </button>
      </div>

      {active.tasksLoading && tasks.length === 0 ? (
        <PanelLoading label={strings.sidebar.loading} />
      ) : null}

      {tasks.length === 0 && !active.tasksLoading ? (
        <PanelEmpty>{strings.sidebar.noBackground}</PanelEmpty>
      ) : null}

      {tasks.map((task) => {
        const status = task.status ?? "running";
        const stoppable = status === "running" || status === "queued";
        return (
          <div className="task-row" key={task.taskId}>
            <div className="task-row-head">
              <ListTodo size={14} />
              <span className="task-name">{task.name ?? task.taskId}</span>
              <StatusBadge status={status} />
            </div>
            <div className="task-meta">
              <span>{task.kind ?? strings.chat.toolFallback}</span>
              <span className="mono">{task.taskId.slice(0, 12)}</span>
              {task.adopted ? (
                <span className="badge badge-accent">{strings.sidebar.adopted}</span>
              ) : null}
              {task.durationMs ? <span>{formatDuration(task.durationMs)}</span> : null}
            </div>
            {stoppable ? (
              <div className="task-actions">
                <button className="btn btn-sm" onClick={() => controller.stopTask(task.taskId)}>
                  {strings.sidebar.stopTask}
                </button>
              </div>
            ) : null}
          </div>
        );
      })}
    </>
  );
}
