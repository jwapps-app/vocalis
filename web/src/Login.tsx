import { useEffect, useState } from "react";
import { authStatus, login, setupCredentials } from "./api";

/**
 * The sign-in gate, doubling as first-run setup.
 *
 * Which one it is depends on whether the server already has credentials. That
 * makes a fresh install self-explanatory — the first person to open it is
 * asked to choose a username and password — without anyone editing a compose
 * file or reading a generated password out of container logs.
 *
 * An instance set up before usernames existed signs in on its password alone;
 * `usernameSet` says so, and the field is hidden rather than asking for
 * something the server does not yet have. It is chosen once, straight after
 * signing in.
 */
export default function Login({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [usernameSet, setUsernameSet] = useState(true);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    authStatus().then(
      (s) => {
        setConfigured(s.configured);
        setUsernameSet(s.username_set);
      },
      () => setError("Cannot reach the server.")
    );
  }, []);

  // Asked for whenever the server has one, and on first run where we set it.
  const wantsUsername = !configured || usernameSet;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!configured && password !== confirm) {
      setError("Those don't match.");
      return;
    }
    setBusy(true);
    try {
      if (configured) await login(username, password);
      else await setupCredentials(username, password);
      onAuthenticated();
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
      setBusy(false);
    }
  }

  if (configured === null) return null;   // avoid flashing the wrong prompt

  const tooShort = !configured && password.length > 0 && password.length < 8;
  const incomplete =
    password.length < (configured ? 1 : 8) ||
    (wantsUsername && username.trim().length < (configured ? 1 : 3));

  return (
    <section className="card login">
      <h2>{configured ? "Sign in" : "Create your login"}</h2>
      <p className="hint">
        {configured
          ? "Vocalis is locked to a single account."
          : "Vocalis has no login yet. Set one now — anyone who can reach this " +
            "address can set it, so do this before exposing it beyond your network."}
      </p>

      <form onSubmit={submit}>
        {wantsUsername && (
          <div className="field">
            <label className="field-label" htmlFor="username">
              Username
            </label>
            <input
              id="username"
              type="text"
              value={username}
              autoFocus
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              autoComplete="username"
              onChange={(e) => setUsername(e.target.value)}
            />
          </div>
        )}

        <div className="field">
          <label className="field-label" htmlFor="password">
            Password
          </label>
          <input
            id="password"
            type="password"
            value={password}
            autoFocus={!wantsUsername}
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

        <button type="submit" className="btn btn-primary" disabled={busy || incomplete}>
          {busy ? "…" : configured ? "Sign in" : "Create login"}
        </button>
        {tooShort && <p className="hint">At least 8 characters.</p>}
        {error && <p className="error">{error}</p>}
      </form>
    </section>
  );
}
