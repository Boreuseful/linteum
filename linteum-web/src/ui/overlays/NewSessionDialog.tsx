import { useEffect, useState } from "react";
import { DEFAULT_WORKSPACE, STORAGE_KEYS } from "../../config";
import { useStrings } from "../../hooks/useStrings";
import { useAppState } from "../../hooks/useStore";
import { needsProfileFields, profileCatalog } from "../../state/configuration";
import { controller } from "../../transport/controller";
import { Dialog } from "../common/Dialog";

function initialWorkspace(): string {
  try {
    return localStorage.getItem(STORAGE_KEYS.lastWorkspace) || DEFAULT_WORKSPACE;
  } catch {
    return DEFAULT_WORKSPACE;
  }
}

export function NewSessionDialog({ onClose }: { onClose: () => void }) {
  const strings = useStrings();
  const { connection, configuration } = useAppState();
  const { defaultProfile, names } = profileCatalog(configuration.fields);
  const [workspace, setWorkspace] = useState(initialWorkspace);
  const [title, setTitle] = useState("");
  const [profile, setProfile] = useState("");

  // The profile catalogue is a separate `config.get` scope; fetch it on demand
  // when the settings page has not loaded the profile fields yet.  Checking the
  // fields (rather than `schema`) also covers a schema-only settings bootstrap.
  useEffect(() => {
    if (connection.status === "ready" && needsProfileFields(configuration)) {
      controller.refreshConfig(["agent.llm", "agent.profiles"]);
    }
  }, [connection.status, configuration.fields]);

  const submit = () => {
    const trimmed = workspace.trim();
    if (!trimmed) return;
    controller.createSession(
      trimmed,
      title.trim() || undefined,
      profile || undefined,
    );
    onClose();
  };

  return (
    <Dialog
      title={strings.overlays.newSessionTitle}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            {strings.overlays.cancel}
          </button>
          <button className="btn btn-primary" onClick={submit} disabled={!workspace.trim()}>
            {strings.overlays.create}
          </button>
        </>
      }
    >
      <div className="form-row">
        <label className="label" htmlFor="new-workspace">
          {strings.overlays.workspaceLabel}
        </label>
        <input
          id="new-workspace"
          className="input"
          value={workspace}
          spellCheck={false}
          autoFocus
          onChange={(event) => setWorkspace(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") submit();
          }}
        />
        <div className="form-hint">{strings.overlays.resolvedOnHost}</div>
      </div>
      <div className="form-row">
        <label className="label" htmlFor="new-title">
          {strings.overlays.titleLabel}
        </label>
        <input
          id="new-title"
          className="input"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") submit();
          }}
        />
      </div>
      <div className="form-row">
        <label className="label" htmlFor="new-profile">
          {strings.overlays.profileLabel}
        </label>
        <select
          id="new-profile"
          className="input"
          value={profile}
          onChange={(event) => setProfile(event.target.value)}
        >
          <option value="">
            {strings.overlays.sessionProfileDefault} ({defaultProfile})
          </option>
          <option value="default">{strings.overlays.sessionProfileDefaultLlm}</option>
          {names
            .filter((name) => name !== "default")
            .map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
        </select>
        <div className="form-hint">{strings.overlays.sessionProfileHint}</div>
      </div>
    </Dialog>
  );
}
