import { describe, expect, it } from "vitest";
import {
  pluginNeedsRestart,
  pluginRuntimeBadge,
  withAllowed,
} from "../src/state/plugins";

describe("plugin status", () => {
  it("marks an enabled but not-yet-loaded plugin as restart pending", () => {
    const plugin = { enabled: true, state: "disabled" };
    expect(pluginNeedsRestart(plugin)).toBe(true);
    expect(pluginRuntimeBadge(plugin)).toBe("restart");
  });

  it("treats the other inactive states the same way after enabling", () => {
    expect(pluginNeedsRestart({ enabled: true, state: "discovered" })).toBe(true);
    expect(pluginNeedsRestart({ enabled: true, state: "unloaded" })).toBe(true);
  });

  it("keeps a loaded, enabled plugin in its runtime state", () => {
    expect(pluginNeedsRestart({ enabled: true, state: "active" })).toBe(false);
    expect(pluginRuntimeBadge({ enabled: true, state: "active" })).toBe("active");
  });

  it("marks a disabled but still-running plugin as restart pending", () => {
    const plugin = { enabled: false, state: "active" };
    expect(pluginNeedsRestart(plugin)).toBe(true);
    expect(pluginRuntimeBadge(plugin)).toBe("restart");
    expect(pluginNeedsRestart({ enabled: false, state: "loading" })).toBe(true);
    expect(pluginNeedsRestart({ enabled: false, state: "quiescing" })).toBe(true);
  });

  it("keeps a disabled and inactive plugin disabled", () => {
    expect(pluginNeedsRestart({ enabled: false, state: "disabled" })).toBe(false);
    expect(pluginRuntimeBadge({ enabled: false, state: "disabled" })).toBe("disabled");
  });

  it("does not infer a restart when the gateway omits enabled", () => {
    expect(pluginNeedsRestart({ state: "active" })).toBe(false);
    expect(pluginRuntimeBadge({ state: "active" })).toBe("active");
    expect(pluginRuntimeBadge({ state: "future-state" })).toBe("unknown");
    expect(pluginRuntimeBadge({})).toBe("unknown");
  });
});

describe("allow list mirroring", () => {
  it("mirrors the gateway's append/remove edit in order", () => {
    expect(withAllowed(["a", "b"], "demo", true)).toEqual(["a", "b", "demo"]);
    expect(withAllowed(["a", "demo", "b"], "demo", false)).toEqual(["a", "b"]);
  });

  it("does not duplicate or reorder existing ids", () => {
    expect(withAllowed(["a", "demo"], "demo", true)).toEqual(["a", "demo"]);
    expect(withAllowed(["a", "b"], "demo", false)).toEqual(["a", "b"]);
  });

  it("stays unknown when the gateway omitted the allow list", () => {
    expect(withAllowed(null, "demo", true)).toBeNull();
    expect(withAllowed(null, "demo", false)).toBeNull();
  });
});
