import { describe, expect, it } from "vitest";
import {
  resolveMarkdownTarget,
  type MarkdownTargetOptions,
} from "../src/ui/files/markdownTargets";

const rawUrl = (root: string, path: string) => `raw:${root}:${path}`;

const options: MarkdownTargetOptions = {
  root: "/ws",
  filePath: "/ws/docs/readme.md",
  rawUrl,
  kind: "link",
};

describe("resolveMarkdownTarget", () => {
  it("keeps http(s) links external", () => {
    expect(resolveMarkdownTarget("https://example.com/a?b#c", options)).toEqual({
      kind: "external",
      url: "https://example.com/a?b#c",
      external: true,
    });
  });

  it("allows mailto only for links", () => {
    expect(resolveMarkdownTarget("mailto:dev@example.com", options)?.kind).toBe(
      "external",
    );
    expect(
      resolveMarkdownTarget("mailto:dev@example.com", {
        ...options,
        kind: "image",
      }),
    ).toBeNull();
  });

  it("resolves relative paths against the document directory", () => {
    expect(resolveMarkdownTarget("./img/a.png", { ...options, kind: "image" })).toEqual({
      kind: "workspace",
      path: "/ws/docs/img/a.png",
      url: "raw:/ws:/ws/docs/img/a.png",
      external: false,
    });
  });

  it("resolves root-relative paths against the workspace root", () => {
    expect(resolveMarkdownTarget("/assets/a.png", { ...options, kind: "image" })).toEqual({
      kind: "workspace",
      path: "/ws/assets/a.png",
      url: "raw:/ws:/ws/assets/a.png",
      external: false,
    });
  });

  it("allows parent traversal while it stays inside the workspace", () => {
    const target = resolveMarkdownTarget("../shared/a.png", options);
    expect(target?.kind === "workspace" ? target.path : null).toBe(
      "/ws/shared/a.png",
    );
  });

  it("rejects paths that escape the workspace", () => {
    expect(resolveMarkdownTarget("../../etc/passwd", options)).toBeNull();
    expect(resolveMarkdownTarget("/../etc/passwd", options)).toBeNull();
  });

  it("decodes URL-escaped path segments", () => {
    const target = resolveMarkdownTarget("./my%20file.md", options);
    expect(target?.kind === "workspace" ? target.path : null).toBe(
      "/ws/docs/my file.md",
    );
  });

  it("keeps the fragment and drops the query from workspace URLs", () => {
    const target = resolveMarkdownTarget("./a.md?raw=1#section", options);
    expect(target?.kind).toBe("workspace");
    expect(target?.kind === "workspace" ? target.path : "").toBe("/ws/docs/a.md");
    expect(target?.url).toBe("raw:/ws:/ws/docs/a.md#section");
  });

  it("allows same-page anchors only for links", () => {
    expect(resolveMarkdownTarget("#section", options)).toEqual({
      kind: "external",
      url: "#section",
      external: false,
    });
    expect(
      resolveMarkdownTarget("#section", { ...options, kind: "image" }),
    ).toBeNull();
  });

  it("rejects unsafe or unsupported schemes", () => {
    expect(resolveMarkdownTarget("javascript:alert(1)", options)).toBeNull();
    expect(resolveMarkdownTarget("data:image/png;base64,AAAA", options)).toBeNull();
    expect(resolveMarkdownTarget("file:///etc/passwd", options)).toBeNull();
  });

  it("returns null when the provider cannot serve bytes", () => {
    expect(
      resolveMarkdownTarget("./a.md", { ...options, rawUrl: () => null }),
    ).toBeNull();
  });
});
