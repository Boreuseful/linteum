import { describe, expect, it } from "vitest";
import { modeLabel } from "../src/ui/common/format";

describe("modeLabel", () => {
  it("shows the bare name for builtin modes", () => {
    expect(modeLabel({ name: "code", source: "builtin" })).toBe("code");
  });

  it("qualifies plugin modes", () => {
    expect(modeLabel({ name: "reviewer", source: "plugin:demo" })).toBe(
      "plugin.reviewer",
    );
  });

  it("falls back to the name when the source is unknown", () => {
    expect(modeLabel({ name: "custom" })).toBe("custom");
  });
});
