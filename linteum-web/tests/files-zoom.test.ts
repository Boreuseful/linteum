import { describe, expect, it } from "vitest";
import {
  IDENTITY_VIEW,
  MAX_ZOOM,
  MIN_ZOOM,
  clampZoom,
  isEditableTarget,
  zoomCommandForKey,
  zoomViewBy,
} from "../src/ui/files/zoom";

describe("clampZoom", () => {
  it("keeps the scale inside the allowed range", () => {
    expect(clampZoom(0.01)).toBe(MIN_ZOOM);
    expect(clampZoom(100)).toBe(MAX_ZOOM);
    expect(clampZoom(2)).toBe(2);
  });
});

describe("zoomViewBy", () => {
  it("zooms in and scales the pan offset by the same ratio", () => {
    expect(zoomViewBy({ scale: 1, x: 20, y: -10 }, 2)).toEqual({
      scale: 2,
      x: 40,
      y: -20,
    });
  });

  it("zooms out", () => {
    expect(zoomViewBy({ scale: 2, x: 40, y: -20 }, 0.5)).toEqual({
      scale: 1,
      x: 20,
      y: -10,
    });
  });

  it("clamps at the maximum without overshooting the pan", () => {
    const view = zoomViewBy({ scale: MAX_ZOOM / 1.5, x: 10, y: 5 }, 2);
    expect(view.scale).toBe(MAX_ZOOM);
    expect(view.x).toBeCloseTo(15);
    expect(view.y).toBeCloseTo(7.5);
  });

  it("clamps at the minimum", () => {
    expect(zoomViewBy({ scale: MIN_ZOOM * 2, x: 0, y: 0 }, 0.1).scale).toBe(
      MIN_ZOOM,
    );
  });

  it("keeps the identity view unchanged", () => {
    expect(zoomViewBy(IDENTITY_VIEW, 1)).toEqual(IDENTITY_VIEW);
  });
});

describe("zoomCommandForKey", () => {
  it("maps plus/equals, minus/underscore and zero", () => {
    expect(zoomCommandForKey("+")).toBe("in");
    expect(zoomCommandForKey("=")).toBe("in");
    expect(zoomCommandForKey("-")).toBe("out");
    expect(zoomCommandForKey("_")).toBe("out");
    expect(zoomCommandForKey("0")).toBe("reset");
  });

  it("ignores unrelated keys", () => {
    expect(zoomCommandForKey("a")).toBeNull();
    expect(zoomCommandForKey("Enter")).toBeNull();
  });
});

describe("isEditableTarget", () => {
  const target = (value: object) => value as unknown as EventTarget;

  it("recognises form fields and contenteditable hosts", () => {
    expect(isEditableTarget(target({ tagName: "INPUT" }))).toBe(true);
    expect(isEditableTarget(target({ tagName: "TEXTAREA" }))).toBe(true);
    expect(isEditableTarget(target({ tagName: "SELECT" }))).toBe(true);
    expect(isEditableTarget(target({ isContentEditable: true }))).toBe(true);
  });

  it("treats ordinary elements and null as non-editable", () => {
    expect(isEditableTarget(target({ tagName: "DIV" }))).toBe(false);
    expect(isEditableTarget(null)).toBe(false);
  });
});
