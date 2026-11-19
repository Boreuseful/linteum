import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { useStrings } from "../../../hooks/useStrings";
import { useAppState } from "../../../hooks/useStore";
import { copyText, formatTokens, modeLabel } from "../../common/format";
import { PanelEmpty } from "../../common/EmptyState";
import { Stat } from "../../common/Stat";
import type { PanelProps } from "../types";

export function InfoPanel(_props: PanelProps) {
  const strings = useStrings();
  const state = useAppState();
  const active = state.active;
  const [copied, setCopied] = useState(false);

  if (!active) return <PanelEmpty>{strings.sidebar.empty}</PanelEmpty>;

  const onCopy = async () => {
    if (await copyText(active.id)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <>
      <div className="stat-grid">
        <Stat label={strings.panel.session} value={active.id} />
        <Stat label={strings.panel.workspace} value={active.info?.workspace ?? "—"} />
        <Stat label={strings.panel.phase} value={active.phase} />
        <Stat label={strings.panel.round} value={String(active.round)} />
        <Stat label={strings.panel.model} value={active.boundModel ?? "—"} />
        <Stat
          label={strings.panel.mode}
          value={active.mode ? modeLabel(active.mode) : "—"}
        />
        <Stat
          label={strings.panel.inputTokens}
          value={formatTokens(active.usage.input_tokens)}
        />
        <Stat
          label={strings.panel.outputTokens}
          value={formatTokens(active.usage.output_tokens)}
        />
        <Stat
          label={strings.panel.totalTokens}
          value={formatTokens(
            (active.usage.input_tokens ?? 0) + (active.usage.output_tokens ?? 0),
          )}
        />
        <Stat
          label={strings.panel.contextDigest}
          value={(active.contextDigest ?? "—").slice(0, 16)}
        />
        <Stat
          label={strings.panel.background}
          value={String(Object.keys(active.background).length)}
        />
        <Stat label={strings.panel.kind} value={active.info?.kind ?? strings.panel.root} />
      </div>
      <button className="btn btn-sm" onClick={() => void onCopy()}>
        {copied ? <Check size={13} /> : <Copy size={13} />} {strings.panel.copySessionId}
      </button>
    </>
  );
}
