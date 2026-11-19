import { Bot, ChevronRight } from "lucide-react";
import { useStrings } from "../../../hooks/useStrings";
import { store } from "../../../hooks/useStore";
import type { SubagentItem as SubagentItemModel } from "../../../state/types";
import { formatDuration, formatTokens, truncate } from "../../common/format";
import { StatusBadge } from "../../common/StatusBadge";

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="subagent-stat">
      <span className="subagent-stat-label">{label}</span>
      <span className="subagent-stat-value">{value}</span>
    </div>
  );
}

export function SubagentItemView({ item }: { item: SubagentItemModel }) {
  const strings = useStrings();
  const sub = item.subagent;
  const tokens = (sub.tokensInput ?? 0) + (sub.tokensOutput ?? 0);

  return (
    <div className="subagent-card">
      <div className="subagent-head">
        <Bot size={16} />
        <span className="subagent-title">{sub.title ?? sub.callId}</span>
        <StatusBadge status={sub.status} />
        <button
          className="btn btn-sm"
          style={{ marginLeft: "auto" }}
          onClick={() => store.dispatch({ type: "ui/selectPanel", panelId: "subagents" })}
        >
          {strings.panel.view} <ChevronRight size={13} />
        </button>
      </div>
      <div className="subagent-grid">
        <Stat
          label={strings.panel.childSession}
          value={sub.childSessionId?.slice(0, 12) ?? "—"}
        />
        <Stat label={strings.panel.phase} value={sub.phase ?? "—"} />
        <Stat
          label={strings.panel.roundShort}
          value={sub.round !== undefined ? String(sub.round) : "—"}
        />
        <Stat label={strings.panel.lastTool} value={sub.lastTool ?? "—"} />
        <Stat
          label={strings.panel.tokensLabel}
          value={tokens > 0 ? formatTokens(tokens) : "—"}
        />
        <Stat
          label={strings.panel.roundsLabel}
          value={
            sub.rounds !== undefined
              ? `${sub.rounds}${sub.durationMs ? ` · ${formatDuration(sub.durationMs)}` : ""}`
              : "—"
          }
        />
      </div>
      {sub.task ? <div className="small secondary">{truncate(sub.task, 240)}</div> : null}
      {sub.summary ? (
        <div className="subagent-summary">{truncate(sub.summary, 600)}</div>
      ) : null}
    </div>
  );
}
