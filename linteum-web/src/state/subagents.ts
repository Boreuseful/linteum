import type { SubagentInfo } from "../protocol/frames";

function definedOnly<T extends object>(value: T): Partial<T> {
  const result: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    if (item !== undefined) result[key] = item;
  }
  return result as Partial<T>;
}

export function mergeSubagent(
  current: SubagentInfo | undefined,
  update: Partial<SubagentInfo> & { callId: string },
): SubagentInfo {
  const base: SubagentInfo = current ?? {
    callId: update.callId,
    tools: [],
    status: "requested",
  };
  return {
    ...base,
    ...definedOnly(update),
    tools: update.tools ?? base.tools,
  };
}

export function upsertSubagent(
  subagents: Record<string, SubagentInfo>,
  info: SubagentInfo,
): Record<string, SubagentInfo> {
  if (!info.callId) return subagents;
  return {
    ...subagents,
    [info.callId]: mergeSubagent(subagents[info.callId], info),
  };
}
