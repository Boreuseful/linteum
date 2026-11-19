import { RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { useStrings } from "../../hooks/useStrings";
import type { ContextInfo } from "../../protocol/commands";
import type { ErrorInfo } from "../../protocol/envelope";
import { controller } from "../../transport/controller";
import { Dialog } from "../common/Dialog";
import { PanelEmpty, PanelLoading } from "../common/EmptyState";
import { formatTokens } from "../common/format";
import { Stat } from "../common/Stat";
import { formatContextMessage } from "./contextMessage";

interface LoadState {
  loading: boolean;
  loadingMore: boolean;
  context: ContextInfo | null;
  messages: unknown[];
  error: ErrorInfo | null;
}

/**
 * Full child-session context, one bounded page at a time. `session.context`
 * returns the provider-facing plan (system blocks plus the child's message
 * history); the gateway limits each page to the frame budget, so the dialog
 * follows `next_offset` until `has_more` is false.
 */
export function ChildContextDialog({
  childId,
  title,
  onClose,
}: {
  childId: string;
  title?: string;
  onClose: () => void;
}) {
  const strings = useStrings();
  const [reloadKey, setReloadKey] = useState(0);
  const [state, setState] = useState<LoadState>({
    loading: true,
    loadingMore: false,
    context: null,
    messages: [],
    error: null,
  });

  useEffect(() => {
    let cancelled = false;
    setState((previous) => ({
      ...previous,
      loading: true,
      loadingMore: false,
      error: null,
    }));
    void controller.loadChildContext(childId, 0).then((result) => {
      if (cancelled) return;
      setState({
        loading: false,
        loadingMore: false,
        context: result.context,
        messages: result.context?.messages ?? [],
        error: result.error,
      });
    });
    return () => {
      cancelled = true;
    };
  }, [childId, reloadKey]);

  const loadMore = () => {
    const context = state.context;
    if (!context || !context.has_more || state.loading || state.loadingMore) {
      return;
    }
    setState((previous) => ({ ...previous, loadingMore: true }));
    void controller
      .loadChildContext(childId, context.next_offset)
      .then((result) => {
        setState((previous) => {
          if (!result.context) {
            return { ...previous, loadingMore: false, error: result.error };
          }
          return {
            ...previous,
            loadingMore: false,
            context: result.context,
            messages: [...previous.messages, ...result.context.messages],
            error: null,
          };
        });
      });
  };

  const context = state.context;
  const dialogTitle = title
    ? `${strings.sidebar.childContext} · ${title}`
    : strings.sidebar.childContext;

  return (
    <Dialog
      title={dialogTitle}
      onClose={onClose}
      wide
      headerExtra={
        <button
          className="icon-btn"
          onClick={() => setReloadKey((key) => key + 1)}
          title={strings.sidebar.refresh}
          aria-label={strings.sidebar.refresh}
          disabled={state.loading}
        >
          <RefreshCw size={15} />
        </button>
      }
    >
      <div className="context-meta">
        <span className="mono truncate" title={childId}>
          {childId}
        </span>
        {context ? (
          <>
            <span>·</span>
            <span>{context.source}</span>
            <span>·</span>
            <span>{context.kind}</span>
            <span>·</span>
            <span>{context.phase}</span>
            <span>·</span>
            <span>
              {strings.panel.roundShort} {context.round}
            </span>
          </>
        ) : null}
      </div>

      {state.loading && !context ? (
        <PanelLoading label={strings.sidebar.loading} />
      ) : null}

      {state.error ? (
        <div className="form-error">
          {state.error.code}: {state.error.message}
        </div>
      ) : null}

      {context ? (
        <>
          <div className="stat-grid">
            <Stat
              label={strings.panel.digest}
              value={context.digest.slice(0, 16) || "—"}
            />
            <Stat
              label={strings.panel.estimatedTokens}
              value={formatTokens(context.estimated_tokens)}
            />
            <Stat
              label={strings.panel.messages}
              value={String(context.messages_total)}
            />
            <Stat
              label={strings.panel.tools}
              value={String(context.tools.length)}
            />
          </div>

          {context.tools.length > 0 ? (
            <div className="row" style={{ flexWrap: "wrap" }}>
              {context.tools.map((tool) => (
                <span className="badge" key={tool}>
                  {tool}
                </span>
              ))}
            </div>
          ) : null}

          {!context.system_blocks_included ? (
            <div className="small muted">{strings.overlays.systemBlocksMissing}</div>
          ) : null}

          {context.truncated ? (
            <div className="small muted">{strings.sidebar.truncated}</div>
          ) : null}

          <div className="context-messages">
            {state.messages.length === 0 ? (
              <PanelEmpty>{strings.overlays.noMessages}</PanelEmpty>
            ) : null}
            {state.messages.map((message, index) => {
              const view = formatContextMessage(message);
              return (
                <div
                  className={`context-message${view.role === "system" ? " system" : ""}`}
                  key={index}
                >
                  <div className="context-message-role">{view.role}</div>
                  <pre className="context-message-text">{view.text || "—"}</pre>
                </div>
              );
            })}
          </div>

          {context.has_more ? (
            <div className="row">
              <button
                className="btn btn-sm"
                onClick={loadMore}
                disabled={state.loadingMore}
              >
                {state.loadingMore
                  ? strings.sidebar.loading
                  : strings.sidebar.loadMore}
              </button>
              <span className="small muted">
                {state.messages.length} / {context.messages_total}
              </span>
            </div>
          ) : null}

          {state.loading ? (
            <div className="small muted">{strings.sidebar.loading}</div>
          ) : null}
        </>
      ) : null}
    </Dialog>
  );
}
