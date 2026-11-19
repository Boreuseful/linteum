import { Archive } from "lucide-react";
import { useStrings } from "../../../hooks/useStrings";
import type { CompactionItem as CompactionItemModel } from "../../../state/types";
import { formatTokens, truncate } from "../../common/format";

export function CompactionItemView({ item }: { item: CompactionItemModel }) {
  const strings = useStrings();
  return (
    <div className="compaction-card">
      <Archive size={15} style={{ flex: "none", marginTop: 2 }} />
      <div>
        <strong>{strings.chat.compactionTitle}</strong>{" "}
        {item.status === "started" ? (
          <span className="muted">{strings.chat.compactionPending}</span>
        ) : (
          <span className="muted">
            {strings.chat.compactionApplied}
            {item.tokens !== undefined
              ? ` · ${formatTokens(item.tokens)} ${strings.sidebar.tokens}`
              : ""}
            {item.fallback ? ` · ${strings.chat.compactionFallback}` : ""}
          </span>
        )}
        {item.summary ? (
          <div className="small secondary" style={{ marginTop: 4 }}>
            {truncate(item.summary, 320)}
          </div>
        ) : null}
        {item.errorInfo ? (
          <div className="small" style={{ color: "var(--danger)", marginTop: 4 }}>
            {item.errorInfo.code}: {item.errorInfo.message}
          </div>
        ) : null}
      </div>
    </div>
  );
}
