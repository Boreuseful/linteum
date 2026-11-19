import { describe, expect, it } from "vitest";
import { parseByteRange } from "../src/workspace/byteRange";

describe("parseByteRange", () => {
  const size = 1000;

  it("ignores an absent or unusable header", () => {
    expect(parseByteRange(undefined, size)).toBeNull();
    expect(parseByteRange("", size)).toBeNull();
    expect(parseByteRange("bytes=-", size)).toBeNull();
    expect(parseByteRange("bytes=0-1,5-6", size)).toBeNull();
    expect(parseByteRange("items=0-1", size)).toBeNull();
  });

  it("parses closed and open-ended ranges", () => {
    expect(parseByteRange("bytes=0-99", size)).toEqual({ start: 0, end: 99 });
    expect(parseByteRange("bytes=100-", size)).toEqual({ start: 100, end: 999 });
  });

  it("parses suffix ranges", () => {
    expect(parseByteRange("bytes=-100", size)).toEqual({ start: 900, end: 999 });
    expect(parseByteRange("bytes=-5000", size)).toEqual({ start: 0, end: 999 });
  });

  it("clamps an end beyond the file size", () => {
    expect(parseByteRange("bytes=990-5000", size)).toEqual({
      start: 990,
      end: 999,
    });
  });

  it("rejects unsatisfiable ranges", () => {
    expect(parseByteRange("bytes=1000-", size)).toBe("invalid");
    expect(parseByteRange("bytes=50-10", size)).toBe("invalid");
    expect(parseByteRange("bytes=-0", size)).toBe("invalid");
  });

  it("trims whitespace", () => {
    expect(parseByteRange(" bytes=0-9 ", size)).toEqual({ start: 0, end: 9 });
  });
});
