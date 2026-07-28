import { useEffect, useState } from "react";
import { bundleUrl, getWorker, Worker } from "./api";
import { InstallCard } from "./Install";

const DEVICE_LABEL: Record<string, string> = {
  mps: "Apple GPU (Metal)",
  cuda: "NVIDIA GPU (CUDA)",
  cpu: "CPU only — this will be very slow",
  unknown: "unknown device",
};

function lastSeen(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function StatusPanel({ worker }: { worker: Worker | null }) {
  if (!worker) {
    return (
      <div className="worker-status offline">
        <span className="pulse" />
        No narrator has ever connected. Install the helper below to begin.
      </div>
    );
  }
  if (!worker.online) {
    return (
      <div className="worker-status offline">
        <span className="pulse" />
        <div>
          <strong>Narrator offline</strong> — last seen {lastSeen(worker.last_seen)}
          {worker.hostname ? ` on ${worker.hostname}` : ""}. Books will wait until it
          reconnects. Start it with the command below, or reinstall.
        </div>
      </div>
    );
  }
  const cpu = worker.device === "cpu";
  const free = worker.free_gpu_gb;
  const solo = worker.max_concurrency === 1;
  return (
    <div className={`worker-status online${cpu ? " warn" : ""}`}>
      <span className="dot" />
      <div>
        <strong>Narrator connected</strong>
        {worker.hostname ? ` — ${worker.hostname}` : ""}
        <div className="hint">
          {DEVICE_LABEL[worker.device ?? "unknown"] ?? worker.device}
          {worker.device_name ? ` · ${worker.device_name}` : ""}
          {free != null ? ` · ${free.toFixed(1)} GB graphics memory free` : ""}
          {worker.max_concurrency
            ? ` · ${worker.max_concurrency} chapter${
                worker.max_concurrency > 1 ? "s" : ""
              } at a time`
            : ""}
        </div>
        {free != null && solo && !cpu && (
          // Headroom is measured per book, so this is actionable rather than
          // trivia: quitting a browser before starting can double the speed.
          <div className="hint">
            Other apps are using most of the graphics memory, so books narrate one
            chapter at a time. Quitting memory-hungry apps — browsers especially —
            before starting a book lets it narrate more at once.
          </div>
        )}
      </div>
    </div>
  );
}

/** The install command, with a one-tap copy — it is meant to be pasted on
 *  another machine, where retyping an address is its own source of error.
 *
 *  Built from window.location so it names the address this page was actually
 *  reached at. Anything derived from server configuration can disagree with
 *  reality: a stack published on one port while the container believes another
 *  hands out an installer pointing somewhere else entirely. */
function InstallCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard blocked (insecure origin) — the text is selectable anyway */
    }
  }

  return (
    <div className="install-command">
      <pre className="cmd">{command}</pre>
      <button type="button" className="btn btn-ghost btn-small" onClick={copy}>
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

export default function Setup() {
  const [worker, setWorker] = useState<Worker | null>(null);
  // Comes from the server complete with the address and the enrolment key, so
  // this page never has to assemble either.
  const [installCommand, setInstallCommand] = useState("");
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const tick = () =>
      getWorker()
        .then((r) => {
          setWorker(r.worker);
          setInstallCommand(r.install_command);
        })
        .catch(() => {})
        .finally(() => setLoaded(true));
    tick();
    const id = setInterval(tick, 5000);
    return () => clearInterval(id);
  }, []);

  return (
    <section className="card setup">
      <h2>The narrator</h2>
      <p className="hint">
        Vocalis records audiobooks on a machine with a GPU — your Mac, or any computer
        with an NVIDIA card. The web app and this narrator talk through the database, so
        the narrator can run on the same machine as the server or a different one on your
        network. Install it once; it starts with the machine and picks up jobs on its own.
      </p>

      {loaded && <StatusPanel worker={worker} />}

      <ol className="steps">
        <li>
          <strong>Run this on the machine with the GPU</strong>
          <p className="hint">
            Paste it into a terminal there. It asks for one thing — the database
            password from this server's <code>.env</code> — and works out the rest.
          </p>
          {installCommand && <InstallCommand command={installCommand} />}
          <p className="hint">
            It finds a Python version the voice model supports, installs{" "}
            <code>ffmpeg</code> if it's missing, downloads the model, and registers a
            background service (<code>launchd</code> on a Mac, <code>systemd</code> on
            Linux) so it keeps running without a terminal open.
          </p>
        </li>
        <li>
          <strong>Watch it connect</strong>
          <p className="hint">
            The panel above turns green within a few seconds of the install finishing.
            The first book is slower while the voice model downloads.
          </p>
        </li>
      </ol>

      <details className="notes">
        <summary>Rather download it yourself?</summary>
        <p className="hint">
          The command above just fetches this and runs it. Downloading through a
          browser works too, but macOS quarantines anything saved that way and will
          refuse to open the installer — you'd need{" "}
          <code>xattr -dr com.apple.quarantine .</code> in the unpacked folder first.
          Files fetched with <code>curl</code> carry no such tag, which is the only
          reason the one-liner exists.
        </p>
        <a className="btn btn-ghost" href={bundleUrl}>
          Download narrator (.zip)
        </a>
      </details>

      <h2>This app</h2>
      <InstallCard />

      <details className="notes">
        <summary>Windows, and running the narrator on another machine</summary>
        <p className="hint">
          <strong>Windows</strong> isn't covered by the installer yet. With an NVIDIA GPU
          the simplest path is WSL2, where the Linux instructions apply and CUDA passes
          through. A native Windows service is on the list.
        </p>
        <p className="hint">
          <strong>A narrator on a different machine</strong> reaches the database over your
          network. The bundle is built for the same-machine case; for a separate box, the
          server must expose its database port to your network and{" "}
          <code>WORKER_DB_HOSTPORT</code> must be set to the server's address before
          downloading. Ask if you want this wired up.
        </p>
      </details>
    </section>
  );
}
