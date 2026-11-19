import hljs from "highlight.js/lib/common";

/**
 * File-extension → highlight.js language. Only languages bundled by
 * `highlight.js/lib/common` are listed; everything else falls back to
 * `plaintext` (still HTML-escaped, so it is safe for innerHTML).
 */
const BY_EXTENSION: Record<string, string> = {
  py: "python",
  pyw: "python",
  pyi: "python",
  rs: "rust",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  ksh: "bash",
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "javascript",
  ts: "typescript",
  mts: "typescript",
  cts: "typescript",
  tsx: "typescript",
  json: "json",
  jsonc: "json",
  yml: "yaml",
  yaml: "yaml",
  md: "markdown",
  markdown: "markdown",
  css: "css",
  scss: "scss",
  less: "less",
  html: "xml",
  htm: "xml",
  xml: "xml",
  svg: "xml",
  go: "go",
  java: "java",
  c: "c",
  h: "c",
  cc: "cpp",
  cpp: "cpp",
  cxx: "cpp",
  hpp: "cpp",
  hh: "cpp",
  hxx: "cpp",
  cs: "csharp",
  kt: "kotlin",
  kts: "kotlin",
  swift: "swift",
  rb: "ruby",
  php: "php",
  sql: "sql",
  lua: "lua",
  pl: "perl",
  pm: "perl",
  r: "r",
  ini: "ini",
  cfg: "ini",
  conf: "ini",
  toml: "ini",
  diff: "diff",
  patch: "diff",
  graphql: "graphql",
  gql: "graphql",
  m: "objectivec",
  mm: "objectivec",
  vb: "vbnet",
  wasm: "wasm",
};

/** Extensionless files with a known language. */
const BY_NAME: Record<string, string> = {
  makefile: "makefile",
  gnumakefile: "makefile",
  dockerfile: "bash",
  ".bashrc": "bash",
  ".zshrc": "bash",
  ".profile": "bash",
  ".gitignore": "plaintext",
  ".gitattributes": "plaintext",
};

/** The highlight.js language for `path`, or `null` when unknown. */
export function languageForPath(path: string): string | null {
  const name = path.split(/[\\/]/).pop()?.toLowerCase() ?? "";
  if (!name) return null;
  const byName = BY_NAME[name] ?? BY_NAME[name.replace(/\./g, "")];
  if (byName) return byName;
  const dot = name.lastIndexOf(".");
  if (dot < 0) return null;
  return BY_EXTENSION[name.slice(dot + 1)] ?? null;
}

/** Highlight `text` for `path`; returns escaped HTML for `dangerouslySetInnerHTML`. */
export function highlightFile(text: string, path: string): string {
  const language = languageForPath(path);
  if (language && hljs.getLanguage(language)) {
    return hljs.highlight(text, { language, ignoreIllegals: true }).value;
  }
  return hljs.highlight(text, { language: "plaintext", ignoreIllegals: true }).value;
}
