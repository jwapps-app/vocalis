import { useEffect, useState } from "react";
import { authStatus, login, setupPassword } from "./api";

/**
 * The password gate, doubling as first-run setup.
 *
 * Which one it is depends on whether the server already has a password. That
 * makes a fresh install self-explanatory — the first person to open it is
 * asked to choose one — without anyone editing a compose file or reading a
 * generated password out of container logs.
 */
export default function Login({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    authStatus().then(
      (s) => setConfigured(s.configured),
      () => setError("Cannot reach the server.")
    );
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (configured === false && password !== confirm) {
      setError("Those don't match.");
      return;
    }
    setBusy(true);
    try {
      if (configured) await login(password);
      else await setupPassword(password);
      onAuthenticated();
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
      setBusy(false);
    }
  }

  if (configured === null) return null;   // avoid flashing the wrong prompt

  return (
    <section className="card login">
      <h2>{configured ? "Sign in" : "Choose a password"}</h2>
      <p className="hint">
        {configured
          ? "Vocalis is locked to one password."
          : "Vocalis has no password yet. Set one now — anyone who can reach this " +
            "address can set it, so do this before exposing it beyond your network."}
      </p>

      <form onSubmit={submit}>
        <div className="field">
          <label className="field-label" htmlFor="password">
            Password
          </label>
          <input
            id="password"
            type="password"
            value={password}
            autoFocus
            autoComplete={configured ? "current-password" : "new-password"}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        {!configured && (
          <div className="field">
            <label className="field-label" htmlFor="confirm">
              Repeat it
            </label>
            <input
              id="confirm"
              type="password"
              value={confirm}
              autoComplete="new-password"
              onChange={(e) => setConfirm(e.target.value)}
            />
          </div>
        )}

        <button
          type="submit"
          className="btn btn-primary"
          disabled={busy || password.length < (configured ? 1 : 8)}
        >
          {busy ? "…" : configured ? "Sign in" : "Set password"}
        </button>
        {!configured && password.length > 0 && password.length < 8 && (
          <p className="hint">At least 8 characters.</p>
        )}
        {error && <p className="error">{error}</p>}
      </form>
    </section>
  );
}
