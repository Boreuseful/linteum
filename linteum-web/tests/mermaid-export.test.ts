import { describe, expect, it } from "vitest";
import {
  mermaidFilename,
  mermaidMarkdown,
  svgAspect,
  withSvgBackground,
} from "../src/ui/chat/mermaidExport";

describe("mermaidMarkdown", () => {
  it("wraps the source in a mermaid fence", () => {
    expect(mermaidMarkdown("graph TD\nA-->B\n")).toBe(
      "```mermaid\ngraph TD\nA-->B\n```",
    );
  });
});

describe("mermaidFilename", () => {
  it("derives a slug from the diagram type", () => {
    expect(mermaidFilename("requirementDiagram\nrequirement x {}")).toBe(
      "mermaid-requirement-diagram.svg",
    );
    expect(mermaidFilename("graph TD\nA-->B")).toBe("mermaid-graph.svg");
  });

  it("skips directives and YAML front matter", () => {
    expect(mermaidFilename("%%{init: {}}%%\nflowchart LR\nA-->B")).toBe(
      "mermaid-flowchart.svg",
    );
    expect(
      mermaidFilename("---\nconfig:\n  theme: base\n---\nsequenceDiagram"),
    ).toBe("mermaid-sequence-diagram.svg");
  });

  it("falls back when there is no diagram type", () => {
    expect(mermaidFilename("")).toBe("mermaid-diagram.svg");
  });
});

describe("svgAspect", () => {
  it("reads the width / height ratio from the viewBox", () => {
    expect(svgAspect('<svg viewBox="0 0 400 200"></svg>')).toBe(2);
    expect(svgAspect("<svg viewBox='0 0 100 200'></svg>")).toBe(0.5);
  });

  it("falls back to a square when the viewBox is missing or invalid", () => {
    expect(svgAspect("<svg></svg>")).toBe(1);
    expect(svgAspect('<svg viewBox="0 0 0 200"></svg>')).toBe(1);
    expect(svgAspect('<svg viewBox="0 0 nope"></svg>')).toBe(1);
  });
});

describe("withSvgBackground", () => {
  it("inserts a background rect into the root svg", () => {
    const svg = '<svg xmlns="http://www.w3.org/2000/svg"><g/></svg>';
    expect(withSvgBackground(svg, "#123456")).toBe(
      '<svg xmlns="http://www.w3.org/2000/svg"><rect width="100%" height="100%" fill="#123456" /><g/></svg>',
    );
  });

  it("leaves non-svg input alone", () => {
    expect(withSvgBackground("nope", "#fff")).toBe("nope");
  });
});
