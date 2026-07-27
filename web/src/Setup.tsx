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

export default function Setup() {
  const [worker, setWorker] = useState<Worker | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const tick = () =>
      getWorker()
        .then((r) => setWorker(r.worker))
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
          <strong>Download the helper</strong>
          <p className="hint">
            A small bundle, already pointed at this server — no configuration to fill in.
          </p>
          <a className="btn btn-primary" href={bundleUrl}>
            Download narrator (.zip)
          </a>
        </li>
        <li>
          <strong>Unpack and install</strong>
          <p className="hint">
            You'll need <code>ffmpeg</code> first — on a Mac,{" "}
            <code>brew install ffmpeg</code>; on Linux, <code>apt install ffmpeg</code>.
            Then, in a terminal:
          </p>
          <pre className="cmd">
            unzip vocalis-worker.zip{"\n"}
            cd vocalis-worker{"\n"}
            ./install.sh
          </pre>
          <p className="hint">
            It sets up Python, downloads the voice model, and registers a background
            service — <code>launchd</code> on a Mac, <code>systemd</code> on Linux — so it
            runs from now on without a terminal open.
          </p>
        </li>
        <li>
          <strong>Watch it connect</strong>
          <p className="hint">
            The status above turns green within a few seconds of the install finishing.
            The first book is slower while the voice model downloads.
          </p>
        </li>
      </ol>

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
