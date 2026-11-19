import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { STORAGE_KEYS } from "../src/config";
import { readLastSession, writeLastSession } from "../src/state/tabSession";

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => {
      values.delete(key);
    },
    setItem: (key, value) => {
      values.set(key, value);
    },
  };
}

let tabStorage: Storage;
let local: Storage;

beforeEach(() => {
  tabStorage = memoryStorage();
  local = memoryStorage();
  vi.stubGlobal("sessionStorage", tabStorage);
  vi.stubGlobal("localStorage", local);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("tab-scoped last session", () => {
  it("writes and reads the active session in sessionStorage only", () => {
    writeLastSession("sess-1");

    expect(readLastSession()).toBe("sess-1");
    expect(tabStorage.getItem(STORAGE_KEYS.lastSession)).toBe("sess-1");
    expect(local.getItem(STORAGE_KEYS.lastSession)).toBeNull();
  });

  it("removes the record when the session is left", () => {
    writeLastSession("sess-1");
    writeLastSession(null);

    expect(readLastSession()).toBeNull();
  });

  it("clears a legacy localStorage value so another tab cannot restore it", () => {
    local.setItem(STORAGE_KEYS.lastSession, "stale");

    writeLastSession("sess-1");

    expect(local.getItem(STORAGE_KEYS.lastSession)).toBeNull();
  });

  it("treats blank values as absent and trims real ids", () => {
    writeLastSession("   ");
    expect(readLastSession()).toBeNull();

    writeLastSession("  sess-1  ");
    expect(readLastSession()).toBe("sess-1");
  });

  it("degrades quietly when sessionStorage is unavailable", () => {
    vi.stubGlobal("sessionStorage", undefined);

    expect(readLastSession()).toBeNull();
    expect(() => writeLastSession("sess-1")).not.toThrow();
  });
});
