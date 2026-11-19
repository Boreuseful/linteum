/**
 * `remark-math` treats `$$x$$` on a single line as inline math. Papers, GitHub
 * and the VS Code Markdown preview render it as display math, so promote a line
 * that consists only of `$$...$$` to a fenced math block before parsing.
 *
 * Fenced code blocks are left untouched; a four-space indent is an indented
 * code block in CommonMark and is therefore not matched either.
 */
export function normalizeDisplayMath(text: string): string {
  if (!text.includes("$$")) return text;

  const lines = text.split("\n");
  let fence: string | null = null;

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const trimmed = line.trim();
    const marker = /^(`{3,}|~{3,})/.exec(trimmed);

    if (fence !== null) {
      if (marker && trimmed.startsWith(fence) && !trimmed.slice(fence.length).trim()) {
        fence = null;
      }
      continue;
    }

    if (marker) {
      fence = marker[1];
      continue;
    }

    const match = /^( {0,3})\$\$([^\n]+?)\$\$[ \t]*\r?$/.exec(line);
    // `$$a$$ and $$b$$` runs between two formulas; leave it as inline math.
    if (!match || match[2].includes("$$")) continue;

    const indent = match[1];
    lines[index] = `${indent}$$\n${indent}${match[2]}\n${indent}$$`;
  }

  return lines.join("\n");
}

/**
 * True when `block` is a fenced code block and its closing fence is present.
 * CommonMark parses an unterminated fence as a code block that runs to EOF, so
 * the node position alone cannot tell a streaming block from a finished one.
 */
export function isFenceClosed(block: string): boolean {
  const lines = block.trimEnd().split("\n");
  const open = /^\s*(`{3,}|~{3,})/.exec(lines[0] ?? "");
  const close = /^\s*(`{3,}|~{3,})[ \t]*$/.exec(lines[lines.length - 1] ?? "");
  if (!open || !close) return false;
  return close[1][0] === open[1][0] && close[1].length >= open[1].length;
}
