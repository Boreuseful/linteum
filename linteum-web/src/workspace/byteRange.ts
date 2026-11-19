/**
 * HTTP byte-range parsing for the demo workspace bridge's raw endpoint.
 *
 * Chrome's embedded PDF viewer streams linearized PDFs through `Range`
 * requests. Serving a full `200` instead of `206 Partial Content` can leave it
 * showing only the first pages.
 */

export interface ByteRange {
  start: number;
  end: number;
}

/**
 * Parse a single-range `Range` header.
 *
 * Returns `null` when the header should be ignored (absent, malformed or
 * multi-range) and `"invalid"` when the range is unsatisfiable (HTTP 416).
 */
export function parseByteRange(
  header: string | undefined,
  size: number,
): ByteRange | "invalid" | null {
  if (!header) return null;
  const match = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!match) return null;
  const [, rawStart, rawEnd] = match;
  if (rawStart === "" && rawEnd === "") return null;
  if (rawStart === "") {
    const suffix = Number(rawEnd);
    if (!Number.isInteger(suffix) || suffix <= 0) return "invalid";
    return { start: Math.max(0, size - suffix), end: size - 1 };
  }
  const start = Number(rawStart);
  if (!Number.isInteger(start) || start < 0 || start >= size) return "invalid";
  const end = rawEnd === "" ? size - 1 : Number(rawEnd);
  if (!Number.isInteger(end) || end < start) return "invalid";
  return { start, end: Math.min(end, size - 1) };
}
