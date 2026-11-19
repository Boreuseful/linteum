/** Helpers for exporting a rendered Mermaid diagram. */

/** Width / height from the SVG viewBox; 1 when it cannot be determined. */
export function svgAspect(svg: string): number {
  const match = /viewBox\s*=\s*["']([^"']+)["']/i.exec(svg);
  if (!match) return 1;

  const parts = match[1].trim().split(/[\s,]+/).map(Number);
  if (parts.length !== 4) return 1;

  const width = parts[2];
  const height = parts[3];
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return 1;
  }
  return width / height;
}

/** The diagram source as a fenced Markdown block. */
export function mermaidMarkdown(code: string): string {
  return ["```mermaid", code.replace(/\r?\n$/, ""), "```"].join("\n");
}

/** A readable file name derived from the diagram type (`flowchart`, etc.). */
export function mermaidFilename(code: string): string {
  const lines = code.split("\n").map((line) => line.trim());

  // Skip optional YAML front matter and `%%{init: ...}%%` directives.
  let index = 0;
  if (lines[index] === "---") {
    index += 1;
    while (index < lines.length && lines[index] !== "---") index += 1;
    index += 1;
  }
  while (
    index < lines.length &&
    (lines[index] === "" || lines[index].startsWith("%%"))
  ) {
    index += 1;
  }

  const first = lines[index];
  const type = (first ?? "diagram")
    .split(/\s+/)[0]
    .replace(/([a-z])([A-Z])/g, "$1-$2")
    .replace(/[^a-z0-9]+/gi, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase();

  return `mermaid-${type || "diagram"}.svg`;
}

/**
 * Inserts a background rect so a dark-theme SVG stays readable when opened
 * outside the app.
 */
export function withSvgBackground(svg: string, background: string): string {
  return svg.replace(
    /<svg\b[^>]*>/,
    (tag) => `${tag}<rect width="100%" height="100%" fill="${background}" />`,
  );
}
