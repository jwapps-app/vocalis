import { useEffect, useState } from "react";
import {
  canInstall,
  installBlockedReason,
  isInstalled,
  manualInstallHint,
  onInstallabilityChange,
  promptInstall,
} from "./pwa";

/** Re-renders when the browser offers, or withdraws, an install prompt. */
function useInstallability() {
  const read = () => ({
    installable: canInstall(),
    installed: isInstalled(),
    blocked: installBlockedReason(),
  });
  const [state, setState] = useState(read);
  useEffect(() => onInstallabilityChange(() => setState(read())), []);
  return state;
}

/** Compact masthead button. Renders nothing unless installing is possible. */
export function InstallButton() {
  const { installable } = useInstallability();
  if (!installable) return null;
  return (
    <button
      type="button"
      className="btn btn-ghost btn-small install-btn"
      onClick={() => promptInstall()}
      title="Install Vocalis as an app"
    >
      Install
    </button>
  );
}

/** The Setup page's fuller explanation, including the cases with no button. */
export function InstallCard() {
  const { installable, installed, blocked } = useInstallability();

  if (installed) {
    return (
      <div className="worker-status online">
        <span className="dot" />
        <div>
          <strong>Installed</strong>
          <div className="hint">
            Vocalis is running as an app. It updates itself whenever you open it
            with the server reachable.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="install-card">
      <div>
        <strong>Install Vocalis as an app</strong>
        <p className="hint">
          Runs in its own window with a Dock or Home Screen icon, no browser
          chrome. The library and narrator still live on your server — installing
          only changes how Vocalis is presented, not where the work happens.
        </p>
      </div>

      {installable && (
        <button type="button" className="btn btn-primary" onClick={() => promptInstall()}>
          Install
        </button>
      )}

      {/* No prompt to offer and nothing blocking it — say where the browser
          keeps its own install control, rather than leaving a dead end. */}
      {!installable && !blocked && <p className="hint">{manualInstallHint()}</p>}

      {blocked && (
        <div className="notice warn">
          <strong>Not installable from this address.</strong>{" "}
          {blocked}{" "}
          <span className="hint">
            Opening Vocalis at <code>http://localhost:8091</code> on the machine
            itself works today. To install it on a phone or another computer, the
            server needs to be reachable over HTTPS.
          </span>
        </div>
      )}
    </div>
  );
}
