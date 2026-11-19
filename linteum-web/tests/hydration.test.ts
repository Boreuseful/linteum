import { describe, expect, it } from "vitest";
import type { ViewFrame } from "../src/protocol/frames";
import { HydrationBuffer, isHydrationFrame } from "../src/transport/hydration";

function userFrame(seq: number, text: string): ViewFrame {
  return {
    kind: "user.message",
    wire: "user.message",
    seq,
    ordinal: 0,
    replay: true,
    ephemeral: false,
    text,
  };
}

describe("HydrationBuffer", () => {
  it("collects frames in order until the subscription completes", () => {
    const buffer = new HydrationBuffer();
    buffer.begin("sess-1");
    buffer.push("sess-1", userFrame(1, "hello"));
    buffer.push("sess-1", userFrame(2, "world"));

    expect(buffer.take("sess-1").map((frame) => frame.seq)).toEqual([1, 2]);
    expect(buffer.take("sess-1")).toEqual([]);
  });

  it("never hands one session's replay to another session", () => {
    const buffer = new HydrationBuffer();
    buffer.begin("sess-1");
    buffer.push("sess-1", userFrame(1, "hello"));

    expect(buffer.take("sess-2")).toEqual([]);
    expect(buffer.take("sess-1")).toHaveLength(1);
  });

  it("starts fresh when a frame belongs to a newly opened session", () => {
    const buffer = new HydrationBuffer();
    buffer.begin("sess-1");
    buffer.push("sess-1", userFrame(1, "old"));
    buffer.push("sess-2", userFrame(1, "new"));

    expect(buffer.take("sess-1")).toEqual([]);
    expect(buffer.take("sess-2").map((frame) => frame.kind)).toEqual(["user.message"]);
  });

  it("discards only the matching session", () => {
    const buffer = new HydrationBuffer();
    buffer.begin("sess-1");
    buffer.push("sess-1", userFrame(1, "hello"));

    buffer.discard("sess-2");
    expect(buffer.take("sess-1")).toHaveLength(1);

    buffer.begin("sess-1");
    buffer.push("sess-1", userFrame(1, "hello"));
    buffer.discard("sess-1");
    expect(buffer.take("sess-1")).toEqual([]);
  });
});

describe("isHydrationFrame", () => {
  it("buffers the active session's frames while it hydrates", () => {
    expect(isHydrationFrame({ id: "sess-1", hydrating: true }, { sessionId: "sess-1" })).toBe(true);
    expect(isHydrationFrame({ id: "sess-1", hydrating: true }, {})).toBe(true);
  });

  it("does not buffer another session's frames", () => {
    expect(isHydrationFrame({ id: "sess-1", hydrating: true }, { sessionId: "sess-2" })).toBe(false);
  });

  it("dispatches frames once hydration is complete", () => {
    expect(isHydrationFrame({ id: "sess-1", hydrating: false }, { sessionId: "sess-1" })).toBe(
      false,
    );
    expect(isHydrationFrame(null, {})).toBe(false);
  });
});
