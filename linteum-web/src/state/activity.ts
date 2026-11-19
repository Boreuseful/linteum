import { MAX_ACTIVITY_ENTRIES } from "../config";
import type { ActivityEntry } from "./types";

let activityCounter = 0;

export function makeActivity(
  kind: ActivityEntry["kind"],
  message: string,
): ActivityEntry {
  activityCounter += 1;
  return {
    id: `activity-${Date.now().toString(36)}-${activityCounter.toString(36)}`,
    ts: Date.now(),
    kind,
    message,
  };
}

export function pushActivity(
  entries: ActivityEntry[],
  entry: ActivityEntry,
): ActivityEntry[] {
  const next = [...entries, entry];
  return next.length > MAX_ACTIVITY_ENTRIES
    ? next.slice(next.length - MAX_ACTIVITY_ENTRIES)
    : next;
}
