import type { BackgroundTaskInfo } from "../protocol/frames";

function definedOnly<T extends object>(value: T): Partial<T> {
  const result: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    if (item !== undefined) result[key] = item;
  }
  return result as Partial<T>;
}

export function upsertTask(
  tasks: Record<string, BackgroundTaskInfo>,
  task: BackgroundTaskInfo,
): Record<string, BackgroundTaskInfo> {
  if (!task.taskId) return tasks;
  const existing = tasks[task.taskId];
  return {
    ...tasks,
    [task.taskId]: existing ? { ...existing, ...definedOnly(task) } : task,
  };
}

export function mergeTasks(
  tasks: Record<string, BackgroundTaskInfo>,
  incoming: BackgroundTaskInfo[],
): Record<string, BackgroundTaskInfo> {
  return incoming.reduce(upsertTask, tasks);
}
