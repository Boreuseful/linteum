import { describe, expect, it } from "vitest";
import {
  filePreviewKind,
  parseCsv,
  svgDataUrl,
} from "../src/ui/files/preview";

describe("filePreviewKind", () => {
  it("detects SVG, PDF, CSV and Markdown case-insensitively", () => {
    expect(filePreviewKind("/w/logo.svg")).toBe("svg");
    expect(filePreviewKind("/w/manual.PDF")).toBe("pdf");
    expect(filePreviewKind("/w/data.Csv")).toBe("csv");
    expect(filePreviewKind("/w/README.md")).toBe("markdown");
    expect(filePreviewKind("/w/guide.Markdown")).toBe("markdown");
  });

  it("treats other files and extensionless paths as text", () => {
    expect(filePreviewKind("/w/main.ts")).toBe("text");
    expect(filePreviewKind("/w/Makefile")).toBe("text");
    expect(filePreviewKind("/w/archive.csv.bak")).toBe("text");
    expect(filePreviewKind("/w/notes.mdx")).toBe("text");
    expect(filePreviewKind("/w/.gitignore")).toBe("text");
  });
});

describe("parseCsv", () => {
  it("splits a header and rows on commas", () => {
    expect(parseCsv("name,age\nAda,36\nGrace,85\n")).toEqual({
      header: ["name", "age"],
      rows: [
        ["Ada", "36"],
        ["Grace", "85"],
      ],
      truncated: false,
    });
  });

  it("keeps commas, escaped quotes and newlines inside quoted fields", () => {
    expect(
      parseCsv('id,note\n1,"hello, world"\n2,"say ""hi""\nagain"\n'),
    ).toEqual({
      header: ["id", "note"],
      rows: [
        ["1", "hello, world"],
        ["2", 'say "hi"\nagain'],
      ],
      truncated: false,
    });
  });

  it("handles CRLF line endings and a trailing newline", () => {
    expect(parseCsv("a,b\r\n1,2\r\n")).toEqual({
      header: ["a", "b"],
      rows: [["1", "2"]],
      truncated: false,
    });
  });

  it("strips a UTF-8 BOM", () => {
    expect(parseCsv("﻿a,b\n1,2\n").header).toEqual(["a", "b"]);
  });

  it("skips blank lines", () => {
    expect(parseCsv("a,b\n1,2\n\n3,4\n").rows).toEqual([
      ["1", "2"],
      ["3", "4"],
    ]);
  });

  it("keeps a trailing empty field", () => {
    expect(parseCsv("a,b\n1,\n").rows).toEqual([["1", ""]]);
  });

  it("caps the rendered rows and reports truncation", () => {
    const table = parseCsv("h\n0\n1\n2\n", 2);
    expect(table.header).toEqual(["h"]);
    expect(table.rows).toEqual([["0"], ["1"]]);
    expect(table.truncated).toBe(true);
  });

  it("returns an empty table for empty input", () => {
    expect(parseCsv("")).toEqual({ header: [], rows: [], truncated: false });
    expect(parseCsv("﻿")).toEqual({ header: [], rows: [], truncated: false });
  });

  it("tolerates an unterminated quote", () => {
    expect(parseCsv('a\n"oops').rows).toEqual([["oops"]]);
  });
});

describe("svgDataUrl", () => {
  it("builds an encoded image data URL", () => {
    const url = svgDataUrl('<svg xmlns="http://www.w3.org/2000/svg"><text>#</text></svg>');
    expect(url.startsWith("data:image/svg+xml;charset=utf-8,")).toBe(true);
    expect(decodeURIComponent(url.slice("data:image/svg+xml;charset=utf-8,".length))).toBe(
      '<svg xmlns="http://www.w3.org/2000/svg"><text>#</text></svg>',
    );
  });
});
