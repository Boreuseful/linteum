import { Info } from "lucide-react";
import { useStrings } from "../../../hooks/useStrings";
import type { NoticeItem as NoticeItemModel } from "../../../state/types";
import type { Strings } from "../../../strings";

function text(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

export function noticeText(
  strings: Strings,
  kind: string,
  payload: Record<string, unknown>,
): string {
  const s = strings.chat;
  switch (kind) {
    case "interrupt":
      return s.noticeInterrupted;
    case "budget": {
      const reason = text(payload.reason);
      return `${s.noticeBudget}${reason ? `: ${reason}` : ""}.`;
    }
    case "provider":
      return `${strings.settings.provider} ${text(payload.provider) ?? ""} ${
        text(payload.status) ?? "degraded"
      }.`;
    case "params_dropped": {
      const keys = Array.isArray(payload.keys) ? payload.keys.join(", ") : "unknown";
      return `${s.noticeParamsDropped}: ${keys}.`;
    }
    case "warnings": {
      const warnings = Array.isArray(payload.warnings)
        ? payload.warnings.join("; ")
        : s.noticeSeeActivity;
      return `${s.noticeWarnings}: ${warnings}`;
    }
    case "context_transplanted":
      return s.noticeModelSwitched;
    case "plugin":
      return `${strings.settings.plugins} ${text(payload.plugin_id) ?? ""} ${
        text(payload.status) ?? s.noticeChanged
      }.`;
    case "steering_failed":
      return `${s.noticeSteeringFailed} ${text(payload.tool) ?? s.toolFallback} ${
        s.noticeToBackground
      }${text(payload.code) ? ` (${text(payload.code)})` : ""}.`;
    case "subagent_cancel":
      return s.noticeSubagentCancelled;
    case "background_cancel":
      return s.noticeBackgroundCancel;
    case "gap":
      return s.noticeHistoryReplayed;
    case "memory.write.applied":
      return s.noticeMemoryUpdated;
    case "memory.deleted":
      return s.noticeMemoryDeleted;
    case "session.erased":
      return s.noticeSessionErased;
    default:
      return kind || s.noticeUnknown;
  }
}

export function NoticeItemView({ item }: { item: NoticeItemModel }) {
  const strings = useStrings();
  return (
    <div className="notice-item">
      <Info size={14} className="notice-icon" />
      <span>{noticeText(strings, item.noticeKind, item.payload)}</span>
    </div>
  );
}
