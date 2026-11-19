import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ConfigPushCoalescer,
  mergeConfigPushes,
  type ConfigPush,
} from "../src/transport/configPush";

describe("config push coalescing", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps the latest revision and the key union", () => {
    const merged = mergeConfigPushes(
      { revision: "rev-2", keys: [{ key: "agent.llm.model", scope: "new_session" }] },
      { revision: "rev-3", keys: [{ key: "logging.level", scope: "live" }] },
    );
    expect(merged).toEqual({
      revision: "rev-3",
      keys: [
        { key: "agent.llm.model", scope: "new_session" },
        { key: "logging.level", scope: "live" },
      ],
    });
  });

  it("deduplicates keys within the window", () => {
    const merged = mergeConfigPushes(
      { revision: "rev-2", keys: [{ key: "logging.level", scope: "live" }] },
      {
        revision: "rev-3",
        keys: [
          { key: "logging.level", scope: "live" },
          { key: "logging.format", scope: "live" },
        ],
      },
    );
    expect(merged.keys).toEqual([
      { key: "logging.level", scope: "live" },
      { key: "logging.format", scope: "live" },
    ]);
  });

  it("waits for the first 100ms window and dispatches once", () => {
    const emitted: ConfigPush[] = [];
    const coalescer = new ConfigPushCoalescer((push) => emitted.push(push));

    coalescer.push({
      revision: "rev-2",
      keys: [{ key: "agent.llm.model", scope: "new_session" }],
    });
    vi.advanceTimersByTime(99);
    expect(emitted).toEqual([]);

    coalescer.push({
      revision: "rev-3",
      keys: [{ key: "logging.level", scope: "live" }],
    });
    vi.advanceTimersByTime(1);
    expect(emitted).toEqual([
      {
        revision: "rev-3",
        keys: [
          { key: "agent.llm.model", scope: "new_session" },
          { key: "logging.level", scope: "live" },
        ],
      },
    ]);

    vi.advanceTimersByTime(100);
    expect(emitted).toHaveLength(1);
  });

  it("starts a new window after a dispatch", () => {
    const emitted: ConfigPush[] = [];
    const coalescer = new ConfigPushCoalescer((push) => emitted.push(push));
    coalescer.push({ revision: "rev-2", keys: [] });
    vi.advanceTimersByTime(100);
    coalescer.push({ revision: "rev-3", keys: [] });
    vi.advanceTimersByTime(100);
    expect(emitted.map((push) => push.revision)).toEqual(["rev-2", "rev-3"]);
  });

  it("clears a pending window", () => {
    const emitted: ConfigPush[] = [];
    const coalescer = new ConfigPushCoalescer((push) => emitted.push(push));
    coalescer.push({ revision: "rev-2", keys: [] });
    coalescer.clear();
    vi.advanceTimersByTime(200);
    expect(emitted).toEqual([]);
  });
});
