import { useState } from "react";
import { chooseUsername } from "./api";

/**
 * Asked once, of an instance created before usernames existed.
 *
 * Such an instance has a working password and signs in on that alone. Rather
 * than migrating it to a default — `admin` on every Vocalis in the world is
 * half the credential handed over for free — the person already signed in
 * picks the name themselves, and the ordinary two-field login applies from
 * then on.
 *
 * Deliberately has no "skip": the reason to add a username is to put the
 * instance somewhere it can be reached, and a prompt that can be dismissed is
 * one that gets dismissed.
 */
export default function ChooseUsername({ onChosen }: { onChosen: () => void }) {
  const [username, setUsername] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await chooseUsername(username);
      onChosen();
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
      setBusy(false);
    }
  }

  return (
    <section className="card login">
      <h2>Choose a username</h2>
      <p className="hint">
        This Vocalis was set up when a password was the whole login. Pick a
        username to go with it — from now on both are needed to sign in. Your
        password does not change.
      </p>

      <form onSubmit={submit}>
        <div className="field">
          <label className="field-label" htmlFor="new-username">
            Username
          </label>
          <input
            id="new-username"
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

        <button
          type="submit"
          className="btn btn-primary"
          disabled={busy || username.trim().length < 3}
        >
          {busy ? "…" : "Save username"}
        </button>
        {username.trim().length > 0 && username.trim().length < 3 && (
          <p className="hint">At least 3 characters.</p>
        )}
        {error && <p className="error">{error}</p>}
      </form>
    </section>
  );
}
