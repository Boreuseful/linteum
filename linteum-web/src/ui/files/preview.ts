/** How the file pane renders one workspace file. */
export type FilePreviewKind = "text" | "svg" | "pdf" | "csv" | "markdown";

/** Hard cap on CSV body rows rendered into the DOM. */
export const MAX_CSV_ROWS = 1000;

/** Pick the preview renderer from the file extension (case-insensitive). */
export function filePreviewKind(path: string): FilePreviewKind {
  const name = path.slice(path.lastIndexOf("/") + 1).toLowerCase();
  const dot = name.lastIndexOf(".");
  if (dot <= 0) return "text";
  switch (name.slice(dot + 1)) {
    case "svg":
      return "svg";
    case "pdf":
      return "pdf";
    case "csv":
      return "csv";
    case "md":
    case "markdown":
      return "markdown";
    default:
      return "text";
  }
}

export interface CsvTable {
  /** First record, rendered as the table head. */
  header: string[];
  /** Body records, already capped at the requested row limit. */
  rows: string[][];
  /** The source had more body rows than the render cap. */
  truncated: boolean;
}

/**
 * Parse comma-separated text (RFC 4180 in spirit): quoted fields may contain
 * commas, doubled quotes and line breaks. The parser is lenient about an
 * unterminated quote and skips blank lines. Only the first `maxRows` body
 * rows are retained.
 */
export function parseCsv(text: string, maxRows: number = MAX_CSV_ROWS): CsvTable {
  const source = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  const records: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;

  const endField = () => {
    row.push(field);
    field = "";
  };
  const endRow = () => {
    endField();
    if (!(row.length === 1 && row[0] === "")) {
      records.push(row);
    }
    row = [];
  };

  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (inQuotes) {
      if (char === '"') {
        if (source[index + 1] === '"') {
          field += '"';
          index += 1;
        } else {
          inQuotes = false;
        }
      } else {
        field += char;
      }
      continue;
    }
    if (char === '"') {
      if (field.length === 0) {
        inQuotes = true;
      } else {
        field += char;
      }
      continue;
    }
    if (char === ",") {
      endField();
      continue;
    }
    if (char === "\n" || char === "\r") {
      if (char === "\r" && source[index + 1] === "\n") index += 1;
      endRow();
      continue;
    }
    field += char;
  }

  // Flush the last record when the input did not end with a line break.
  if (field.length > 0 || row.length > 0) {
    endRow();
  }

  if (records.length === 0) {
    return { header: [], rows: [], truncated: false };
  }
  const [header, ...body] = records;
  const truncated = body.length > maxRows;
  return {
    header,
    rows: truncated ? body.slice(0, maxRows) : body,
    truncated,
  };
}

/**
 * Inline SVG as an image data URL. Render it through <img>, never as inline
 * HTML: an image document cannot run scripts or fetch same-origin pages.
 */
export function svgDataUrl(svg: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}
