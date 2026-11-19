import { STORAGE_KEYS } from "../config";

/**
 * The open session is tab-scoped on purpose: a refresh or in-place reconnect
 * should restore it, but closing the tab (or opening the URL in a new one)
 * must start clean. `sessionStorage` has exactly those semantics.
 */
function tabStorage(): Storage | null {
  try {
    return sessionStorage;
  } catch {
    // Storage can be unavailable in private/embedded contexts.
    return null;
  }
}

export function readLastSession(): string | null {
  const storage = tabStorage();
  if (!storage) return null;
  try {
    const value = storage.getItem(STORAGE_KEYS.lastSession);
    if (!value) return null;
    const trimmed = value.trim();
    return trimmed || null;
  } catch {
    return null;
  }
}

export function writeLastSession(value: string | null): void {
  try {
    const storage = tabStorage();
    if (storage) {
      if (value === null) storage.removeItem(STORAGE_KEYS.lastSession);
      else storage.setItem(STORAGE_KEYS.lastSession, value);
    }
  } catch {
    // Preferences are best-effort only.
  }
  // Older builds wrote this id to localStorage. Clear it so a stale value in
  // another tab can never resurrect a session that tab did not open.
  try {
    localStorage.removeItem(STORAGE_KEYS.lastSession);
  } catch {
    // Ignore unavailable storage.
  }
}
