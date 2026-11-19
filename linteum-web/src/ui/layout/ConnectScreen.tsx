import { Sparkles } from "lucide-react";
import { useState } from "react";
import { useStrings } from "../../hooks/useStrings";
import { useAppState } from "../../hooks/useStore";
import { controller } from "../../transport/controller";

function isLoopback(url: string): boolean {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname;
    return host === "localhost" || host === "127.0.0.1" || host === "::1" || host === "[::1]";
  } catch {
    return false;
  }
}

export function ConnectScreen() {
  const strings = useStrings();
  const state = useAppState();
  const [url, setUrl] = useState(state.connection.url);
  const { status, error, retryAt } = state.connection;
  const busy = status === "connecting" || status === "handshaking";
  const label = busy
    ? strings.connect.connecting
    : status === "reconnecting"
      ? strings.connect.reconnect
      : strings.connect.button;

  return (
    <div className="connect-screen">
      <div className="connect-card">
        <div className="welcome-icon">
          <Sparkles size={26} />
        </div>
        <h1>{strings.connect.title}</h1>
        <p>{strings.connect.subtitle}</p>

        <div className="form-row">
          <label className="label" htmlFor="gateway-url">
            {strings.connect.urlLabel}
          </label>
          <input
            id="gateway-url"
            className="input"
            value={url}
            spellCheck={false}
            onChange={(event) => setUrl(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") controller.connect(url);
            }}
          />
        </div>

        <div className="connect-actions">
          <button
            className="btn btn-primary"
            onClick={() => controller.connect(url)}
            disabled={busy}
          >
            {busy ? (
              <>
                <span className="spinner" /> {label}
              </>
            ) : (
              label
            )}
          </button>
          <span className="small muted">
            {status === "reconnecting" && retryAt
              ? `${strings.connect.nextRetry} ${Math.max(
                  0,
                  Math.round((retryAt - Date.now()) / 1000),
                )}s`
              : strings.status[status]}
          </span>
        </div>

        {error ? (
          <div className="form-error">
            {error.code}: {error.message}
          </div>
        ) : null}

        {!isLoopback(url) ? (
          <div className="form-error">{strings.connect.insecureWarning}</div>
        ) : null}

        <div className="connect-note">uv run linteum serve --ws</div>
        <div className="small muted">{strings.connect.recent}</div>
      </div>
    </div>
  );
}
