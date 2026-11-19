import { describe, expect, it } from "vitest";
import { highlightFile, languageForPath } from "../src/ui/files/highlight";

describe("file highlighting", () => {
  it("maps common extensions and file names", () => {
    expect(languageForPath("/w/a.py")).toBe("python");
    expect(languageForPath("/w/a.rs")).toBe("rust");
    expect(languageForPath("/w/run.sh")).toBe("bash");
    expect(languageForPath("/w/Makefile")).toBe("makefile");
    expect(languageForPath("/w/unknown.zzz")).toBeNull();
  });

  it("colors Python functions and variables", () => {
    const html = highlightFile("def greet(name):\n    return name\n", "/w/a.py");
    expect(html).toContain("hljs-keyword");
    expect(html).toContain("hljs-title function_");
    expect(html).toContain("hljs-params");
  });

  it("colors Rust and shell keywords", () => {
    expect(highlightFile("fn main() { let x = 1; }", "/w/a.rs")).toContain(
      "hljs-keyword",
    );
    expect(
      highlightFile("#!/bin/bash\nfor f in *; do echo $f; done", "/w/a.sh"),
    ).toContain("hljs-keyword");
  });

  it("escapes markup for unknown languages", () => {
    const html = highlightFile("<script>alert(1)</script>", "/w/a.zzz");
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });
});
