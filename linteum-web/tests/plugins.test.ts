import { describe, expect, it } from "vitest";
import { parsePluginChanges, parsePluginSummary } from "../src/protocol/commands";

describe("plugin.list parsing", () => {
  it("preserves capability, extensibility and diagnostic fields", () => {
    const parsed = parsePluginSummary({
      id: "demo.plugin",
      version: "1.2.3",
      state: "active",
      enabled: true,
      trust: "trusted",
      description: "Adds demo tools",
      source: "/plugins/demo",
      tools: ["echo"],
      skills: ["demo-skill"],
      modes: ["demo-mode"],
      digest: "abc123",
      error: { code: "E_PLUGIN_LOAD", message: "boom", hint: "fix it" },
      warnings: ["deprecated manifest"],
      hooks: ["before_llm_request"],
      subscribes: ["tool.call.completed"],
      publishes: ["plugin.demo.event"],
      config: { api_key: "file", endpoint: "default" },
      secrets: ["API_KEY"],
    });

    expect(parsed).not.toBeNull();
    expect(parsed).toMatchObject({
      id: "demo.plugin",
      version: "1.2.3",
      state: "active",
      enabled: true,
      trust: "trusted",
      description: "Adds demo tools",
      source: "/plugins/demo",
      tools: ["echo"],
      skills: ["demo-skill"],
      modes: ["demo-mode"],
      digest: "abc123",
      warnings: ["deprecated manifest"],
      hooks: ["before_llm_request"],
      subscribes: ["tool.call.completed"],
      publishes: ["plugin.demo.event"],
      config: { api_key: "file", endpoint: "default" },
      secrets: ["API_KEY"],
      error: { code: "E_PLUGIN_LOAD", message: "boom", hint: "fix it" },
    });
  });

  it("drops malformed optional fields without losing the plugin", () => {
    const parsed = parsePluginSummary({
      id: "demo.plugin",
      tools: [1, "echo"],
      skills: "nope",
      modes: null,
      warnings: [false],
      config: { good: "file", bad: 1 },
      secrets: "not-a-list",
      error: { message: "no code" },
    });

    expect(parsed).not.toBeNull();
    expect(parsed?.tools).toEqual(["echo"]);
    expect(parsed?.skills).toEqual([]);
    expect(parsed?.modes).toEqual([]);
    expect(parsed?.warnings).toEqual([]);
    expect(parsed?.config).toEqual({ good: "file" });
    expect(parsed?.secrets).toEqual([]);
    expect(parsed?.error).toBeUndefined();
  });

  it("keeps enabled undefined when an older gateway omits it", () => {
    const parsed = parsePluginSummary({ id: "demo.plugin", state: "active" });
    expect(parsed?.enabled).toBeUndefined();
  });

  it("requires a plugin id", () => {
    expect(parsePluginSummary({ version: "1.0.0" })).toBeNull();
  });
});

describe("plugin.set_allowed changes", () => {
  it("parses load, unload and failed entries", () => {
    const changes = parsePluginChanges([
      { id: "demo", action: "load", state: "active" },
      { id: "old", action: "unload", state: "unloaded" },
      {
        id: "bad",
        action: "failed",
        state: "failed",
        error: { code: "E_PLUGIN_MANIFEST", message: "bad manifest", hint: "fix it" },
      },
      { action: "load", state: "active" },
    ]);

    expect(changes).toEqual([
      { id: "demo", action: "load", state: "active", error: undefined },
      { id: "old", action: "unload", state: "unloaded", error: undefined },
      {
        id: "bad",
        action: "failed",
        state: "failed",
        error: { code: "E_PLUGIN_MANIFEST", message: "bad manifest", hint: "fix it" },
      },
    ]);
  });

  it("defaults missing fields safely", () => {
    expect(parsePluginChanges([{ id: "demo" }])).toEqual([
      { id: "demo", action: "unchanged", state: "", error: undefined },
    ]);
    expect(parsePluginChanges("nope")).toEqual([]);
  });
});
