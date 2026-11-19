import type { SessionInfo } from "../protocol/commands";

export function sortSessions(items: SessionInfo[]): SessionInfo[] {
  return [...items].sort((a, b) => b.updated_at - a.updated_at);
}

export function upsertSession(items: SessionInfo[], info: SessionInfo): SessionInfo[] {
  const index = items.findIndex((item) => item.session_id === info.session_id);
  if (index < 0) return sortSessions([...items, info]);
  const copy = items.slice();
  copy[index] = { ...items[index], ...info };
  return sortSessions(copy);
}
