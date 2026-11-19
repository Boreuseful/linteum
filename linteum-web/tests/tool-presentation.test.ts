import { describe, expect, it } from "vitest";
import {
  parseArgsPreview,
  presentToolArgs,
  presentToolResult,
  rawArgsText,
} from "../src/ui/common/toolPresentation";

describe("presentToolArgs", () => {
  it("renders a bash command and its working directory", () => {
    const view = presentToolArgs("bash", {
      command: "git status\n--short",
      cwd: "src",
    });
    expect(view).toEqual({
      structured: true,
      summary: "git status",
      fields: [
        { key: "command", value: "git status\n--short" },
        { key: "cwd", value: "src" },
      ],
    });
  });

  it("omits the default working directory", () => {
    const view = presentToolArgs("bash", { command: "ls", cwd: "." });
    expect(view.fields).toEqual([{ key: "command", value: "ls" }]);
  });

  it("summarises write_file with its path and keeps the content", () => {
    const view = presentToolArgs("write_file", {
      path: "src/app.ts",
      text: "export {};\n",
    });
    expect(view.summary).toBe("src/app.ts");
    expect(view.structured).toBe(true);
    expect(view.fields).toEqual([
      { key: "path", value: "src/app.ts" },
      { key: "content", value: "export {};\n" },
    ]);
  });

  it("renders edit_file as path, original and replacement", () => {
    const view = presentToolArgs("edit_file", {
      path: "a.py",
      old: "x = 1",
      new: "x = 2",
    });
    expect(view.fields).toEqual([
      { key: "path", value: "a.py" },
      { key: "old", value: "x = 1" },
      { key: "new", value: "x = 2" },
    ]);
  });

  it("renders read_file with its path", () => {
    const view = presentToolArgs("read_file", { path: "README.md" });
    expect(view).toEqual({
      structured: true,
      summary: "README.md",
      fields: [{ key: "path", value: "README.md" }],
    });
  });

  it("renders list_dir with a directory, defaulting to the root", () => {
    expect(presentToolArgs("list_dir", {}).fields).toEqual([
      { key: "directory", value: "." },
    ]);
  });

  it("falls back to one-line JSON for unknown tools", () => {
    const view = presentToolArgs("web_search", { q: "hello" });
    expect(view.structured).toBe(false);
    expect(view.summary).toBe('{"q":"hello"}');
    expect(view.fields).toEqual([]);
  });

  it("falls back when a known tool receives malformed arguments", () => {
    const view = presentToolArgs("bash", null);
    expect(view.structured).toBe(false);
    expect(view.fields).toEqual([]);
  });

  it("falls back when bash has no command", () => {
    expect(presentToolArgs("bash", { cwd: "src" }).structured).toBe(false);
  });

  it("renders the gateway's truncated preview wrapper as partial fields", () => {
    const view = presentToolArgs("write_file", {
      truncated: true,
      preview: '{"path": "src/app.ts", "text": "export const x = 1;\\n',
    });
    expect(view).toEqual({
      structured: true,
      summary: "src/app.ts",
      fields: [
        { key: "path", value: "src/app.ts" },
        { key: "content", value: "export const x = 1;\n…" },
      ],
    });
  });

  it("unescapes embedded quotes in a partial preview", () => {
    const view = presentToolArgs("edit_file", {
      truncated: true,
      preview: '{"path": "a.ts", "old": "say \\"hi\\"", "new": "say \\"bye\\"',
    });
    expect(view.fields).toEqual([
      { key: "path", value: "a.ts" },
      { key: "old", value: 'say "hi"…' },
      { key: "new", value: 'say "bye"…' },
    ]);
  });
});

describe("presentToolResult", () => {
  it("splits bash stdout and stderr and reports a non-zero exit", () => {
    const view = presentToolResult("bash", {
      returncode: 2,
      stdout: "out",
      stderr: "err",
      cwd: ".",
    });
    expect(view).toEqual({
      structured: true,
      fields: [
        { key: "stdout", value: "out" },
        { key: "stderr", value: "err" },
      ],
      exitCode: 2,
    });
  });

  it("treats a zero exit code as the normal case", () => {
    const view = presentToolResult("bash", {
      returncode: 0,
      stdout: "ok",
      stderr: "",
      cwd: ".",
    });
    expect(view).toEqual({
      structured: true,
      fields: [{ key: "stdout", value: "ok" }],
    });
    expect(view?.exitCode).toBeUndefined();
  });

  it("renders read_file output as file content", () => {
    const view = presentToolResult("read_file", "line one\nline two");
    expect(view).toEqual({
      structured: true,
      fields: [{ key: "fileContent", value: "line one\nline two" }],
    });
  });

  it("returns null for tools without a result renderer", () => {
    expect(presentToolResult("web_search", { hits: 2 })).toBeNull();
  });

  it("returns null when bash output is not the expected record", () => {
    expect(presentToolResult("bash", "plain")).toBeNull();
  });

  it("renders a truncated bash result as partial stdout", () => {
    const view = presentToolResult("bash", {
      truncated: true,
      preview: '{"returncode": 0, "stdout": "line one\\nline two',
    });
    expect(view?.fields).toEqual([
      { key: "stdout", value: "line one\nline two…" },
    ]);
    expect(view?.exitCode).toBeUndefined();
  });
});

describe("parseArgsPreview", () => {
  it("parses the gateway's JSON preview string", () => {
    const view = parseArgsPreview('{"path":"a.ts","text":"hi"}');
    expect(view.parsed).toBe(true);
    expect(view.args).toEqual({ path: "a.ts", text: "hi" });
    expect(view.raw).toBe('{"path":"a.ts","text":"hi"}');
  });

  it("keeps a truncated preview as raw text", () => {
    const view = parseArgsPreview('{"path":"a.ts","text":"hi…');
    expect(view.parsed).toBe(false);
    expect(view.args).toBeUndefined();
    expect(view.raw).toBe('{"path":"a.ts","text":"hi…');
  });

  it("accepts an already-parsed preview object", () => {
    const view = parseArgsPreview({ path: "a.ts" });
    expect(view.parsed).toBe(true);
    expect(view.args).toEqual({ path: "a.ts" });
    expect(view.raw).toContain('"path": "a.ts"');
  });

  it("handles an absent preview", () => {
    expect(parseArgsPreview(undefined)).toEqual({
      args: undefined,
      raw: "",
      parsed: false,
    });
  });
});

describe("rawArgsText", () => {
  it("unwraps a truncated preview instead of showing the wrapper JSON", () => {
    expect(
      rawArgsText({ truncated: true, preview: '{"path": "a.ts"' }),
    ).toBe('{"path": "a.ts"');
  });

  it("pretty-prints normal arguments", () => {
    expect(rawArgsText({ path: "a.ts" })).toBe('{\n  "path": "a.ts"\n}');
  });
});
