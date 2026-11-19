import { describe, expect, it } from "vitest";
import { BOTTOM_THRESHOLD, evaluateScroll } from "../src/ui/chat/scroll";

function sample(previousTop: number, top: number, height = 1000) {
  return { previousTop, top, height, viewport: 400 };
}

describe("evaluateScroll", () => {
  it("treats the bottom band as pinned", () => {
    // distance = 1000 - 590 - 400 = 10
    expect(evaluateScroll(sample(500, 590))).toBe("bottom");
    expect(BOTTOM_THRESHOLD).toBeGreaterThanOrEqual(10);
  });

  it("detaches only on an upward move past the threshold", () => {
    // distance = 1000 - 400 - 400 = 200
    expect(evaluateScroll(sample(600, 400))).toBe("detach");
  });

  it("ignores content growth after a programmatic scroll", () => {
    // The viewport did not move up; the container just got taller.
    expect(evaluateScroll(sample(590, 590, 1200))).toBe("ignore");
    expect(evaluateScroll(sample(400, 600, 1400))).toBe("ignore");
  });
});
