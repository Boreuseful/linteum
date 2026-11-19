import { describe, expect, it } from "vitest";
import {
  decodeEnvelope,
  encodeCommand,
  makeCommand,
  type FrameEnvelope,
} from "../src/protocol/envelope";
import { parseContextInfo } from "../src/protocol/commands";
import { parseFrame } from "../src/protocol/frames";

function frame(
  name: string,
  payload: Record<string, unknown>,
  extra: Partial<FrameEnvelope> = {},
): FrameEnvelope {
  return {
    v: 1,
    kind: "frame",
    id: "f-1",
    frame: name,
    payload,
    ephemeral: false,
    replay: false,
    ordinal: 0,
    seq: 1,
    ...extra,
  };
}

describe("envelopes", () => {
  it("decodes a command result", () => {
    const envelope = decodeEnvelope(
      JSON.stringify({
        v: 1,
        kind: "result",
        id: "r-1",
        reply_to: "c-1",
        ok: true,
        result: { protocol: 1 },
        name: "hello",
      }),
    );
    expect(envelope?.kind).toBe("result");
    if (envelope?.kind === "result") {
      expect(envelope.reply_to).toBe("c-1");
      expect(envelope.ok).toBe(true);
      expect(envelope.name).toBe("hello");
    }
  });

  it("decodes a frame envelope", () => {
    const envelope = decodeEnvelope(
      JSON.stringify({
        v: 1,
        kind: "frame",
        id: "f-1",
        frame: "assistant.delta",
        payload: { text: "hi" },
        ephemeral: true,
        replay: false,
        ordinal: 2,
      }),
    );
    expect(envelope?.kind).toBe("frame");
    if (envelope?.kind === "frame") {
      expect(envelope.frame).toBe("assistant.delta");
      expect(envelope.ephemeral).toBe(true);
      expect(envelope.ordinal).toBe(2);
    }
  });

  it("rejects malformed envelopes", () => {
    expect(decodeEnvelope("{}")).toBeNull();
    expect(decodeEnvelope("not json")).toBeNull();
    expect(decodeEnvelope(JSON.stringify({ kind: "frame" }))).toBeNull();
  });

  it("round-trips a command", () => {
    const command = makeCommand("ping", {}, "sess-1");
    const parsed = JSON.parse(encodeCommand(command)) as Record<string, unknown>;
    expect(parsed.kind).toBe("cmd");
    expect(parsed.name).toBe("ping");
    expect(parsed.session_id).toBe("sess-1");
    expect(parsed.id).toBe(command.id);
    expect(parsed.v).toBe(1);
  });
});

describe("frame parsing", () => {
  it("parses tool.result into a domain frame", () => {
    const parsed = parseFrame(
      frame("tool.result", {
        call_id: "call-1",
        name: "bash",
        ok: true,
        output: "done",
        output_ref: "art:abc",
      }),
    );
    expect(parsed?.kind).toBe("tool.result");
    if (parsed?.kind === "tool.result") {
      expect(parsed.callId).toBe("call-1");
      expect(parsed.name).toBe("bash");
      expect(parsed.ok).toBe(true);
      expect(parsed.output).toBe("done");
      expect(parsed.outputRef).toBe("art:abc");
    }
  });

  it("parses a snapshot", () => {
    const parsed = parseFrame(
      frame("snapshot", {
        last_seq: 7,
        phase: "thinking",
        round: 2,
        busy: true,
        pending: ["call-1"],
        pending_approvals: [
          { call_id: "call-1", name: "bash", side_effect: "write" },
        ],
        usage: { input_tokens: 10 },
        background: [
          { task_id: "task-1", name: "bash", status: "running" },
        ],
        background_running: 1,
        background_task_ids: ["task-1"],
      }),
    );
    expect(parsed?.kind).toBe("snapshot");
    if (parsed?.kind === "snapshot") {
      expect(parsed.snapshot.lastSeq).toBe(7);
      expect(parsed.snapshot.approvals[0]?.callId).toBe("call-1");
      expect(parsed.snapshot.background[0]?.taskId).toBe("task-1");
      expect(parsed.snapshot.usage.input_tokens).toBe(10);
    }
  });

  it("derives the mode id when the snapshot omits it", () => {
    // A freshly activated session seeds the snapshot mode from the host
    // summary, which has no id yet. Older gateways never send one.
    const parsed = parseFrame(
      frame("snapshot", {
        last_seq: 1,
        phase: "idle",
        round: 0,
        busy: false,
        pending: [],
        pending_approvals: [],
        usage: {},
        background: [],
        mode: { name: "code", source: "builtin", digest: "sha256:abc" },
      }),
    );
    expect(parsed?.kind).toBe("snapshot");
    if (parsed?.kind === "snapshot") {
      expect(parsed.snapshot.mode?.id).toBe("builtin/code");
      expect(parsed.snapshot.mode?.name).toBe("code");
    }
  });

  it("ignores unknown frames", () => {
    expect(parseFrame(frame("future.frame", { anything: true }))).toBeNull();
  });

  it("parses a config.changed control frame", () => {
    const parsed = parseFrame(
      frame(
        "config.changed",
        {
          revision: "rev-2",
          keys: [
            { key: "logging.level", scope: "live" },
            { key: 1, scope: "bad" },
            "not-a-key",
          ],
        },
        { ephemeral: true, seq: undefined },
      ),
    );
    expect(parsed).toMatchObject({
      kind: "config.changed",
      revision: "rev-2",
      keys: [{ key: "logging.level", scope: "live" }],
      ephemeral: true,
      seq: 0,
      replay: false,
      ordinal: 0,
    });
    expect(parsed?.sessionId).toBeUndefined();
  });

  it("drops config.changed without a revision", () => {
    expect(parseFrame(frame("config.changed", { keys: [] }))).toBeNull();
  });

  it("treats ephemeral deltas as non-durable", () => {
    const parsed = parseFrame(
      frame("assistant.delta", { text: "hi" }, { ephemeral: true, seq: undefined }),
    );
    expect(parsed?.kind).toBe("assistant.delta");
    expect(parsed?.ephemeral).toBe(true);
    expect(parsed?.seq).toBe(0);
  });
});

describe("parseContextInfo", () => {
  it("parses the paginated context envelope", () => {
    const context = parseContextInfo({
      session_id: "sess-1",
      kind: "subagent",
      phase: "running",
      round: 3,
      source: "replay",
      system_blocks_included: false,
      digest: "sha256:abc",
      estimated_tokens: 42,
      messages: [{ role: "user", content: "hi" }],
      messages_total: 9,
      offset: 2,
      next_offset: 3,
      has_more: true,
      truncated: true,
      tools: ["read_file"],
    });
    expect(context).toMatchObject({
      messages_total: 9,
      offset: 2,
      next_offset: 3,
      has_more: true,
      truncated: true,
    });
    expect(context?.messages).toHaveLength(1);
  });

  it("defaults paging fields for gateways that return the whole plan", () => {
    const context = parseContextInfo({
      session_id: "sess-1",
      messages: [{ role: "user" }, { role: "assistant" }],
    });
    expect(context).toMatchObject({
      messages_total: 2,
      offset: 0,
      next_offset: 2,
      has_more: false,
      truncated: false,
    });
  });
});
