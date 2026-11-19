import { useStrings } from "../../hooks/useStrings";
import type { Strings } from "../../strings";

type BadgeKey = keyof Strings["badges"];

const STATUS_MAP: Record<string, { key: BadgeKey; className: string }> = {
  requested: { key: "requested", className: "" },
  queued: { key: "queued", className: "badge-warning" },
  running: { key: "running", className: "badge-warning" },
  ok: { key: "done", className: "badge-success" },
  completed: { key: "done", className: "badge-success" },
  failed: { key: "failed", className: "badge-danger" },
  stopped: { key: "stopped", className: "" },
  backgrounded: { key: "background", className: "badge-accent" },
  adopted: { key: "adopted", className: "badge-accent" },
};

export function StatusBadge({ status }: { status: string }) {
  const strings = useStrings();
  const entry = STATUS_MAP[status];
  const label = entry ? strings.badges[entry.key] : status;
  return <span className={`badge ${entry?.className ?? ""}`.trim()}>{label}</span>;
}
