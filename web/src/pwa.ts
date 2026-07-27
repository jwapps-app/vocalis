/** Installability: service-worker registration and the install affordance.
 *
 * Service workers only run in a secure context — HTTPS, or localhost, which
 * browsers exempt. Reaching Vocalis over a plain-HTTP LAN address (the usual
 * way a phone would) is therefore *not* installable, and no code here can
 * change that. Rather than fail silently, `installBlockedReason()` reports it
 * so the Setup page can explain the situation instead of leaving someone
 * hunting a button that will never appear.
 */

/** Chrome/Edge/Android fire this; Safari never does. Not in lib.dom yet. */
interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

let deferredPrompt: BeforeInstallPromptEvent | null = null;
const listeners = new Set<() => void>();

const notify = () => listeners.forEach((fn) => fn());

export function onInstallabilityChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export const canInstall = () => deferredPrompt !== null;

/** True once running from the Home Screen / as an installed window. */
export function isInstalled(): boolean {
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    window.matchMedia("(display-mode: window-controls-overlay)").matches ||
    // Safari's own flag, still the only signal on iOS.
    (navigator as { standalone?: boolean }).standalone === true
  );
}

export function isIos(): boolean {
  const ua = navigator.userAgent;
  // iPadOS 13+ claims to be a Mac; the touch points give it away.
  return /iphone|ipad|ipod/i.test(ua) || (/macintosh/i.test(ua) && navigator.maxTouchPoints > 1);
}

/**
 * Where this browser hides its install control.
 *
 * Only Chromium fires `beforeinstallprompt`, so everywhere else the card would
 * otherwise promise an app with no way to get one. Naming the actual menu item
 * beats a button that never appears.
 */
export function manualInstallHint(): string | null {
  const ua = navigator.userAgent;
  if (isIos()) {
    return "In Safari, tap Share, then Add to Home Screen. Safari is the only iOS browser that can install apps.";
  }
  if (/firefox/i.test(ua)) {
    return "Firefox on the desktop cannot install web apps. Chrome, Edge or Safari can.";
  }
  if (/safari/i.test(ua) && !/chrome|chromium|edg/i.test(ua)) {
    return "In Safari, choose File, then Add to Dock.";
  }
  return "Look for the install icon at the right of the address bar, or find Install Vocalis in the browser menu.";
}

/**
 * Why installing is impossible here, or null if it is possible (or already
 * done). Distinguishes "your browser can't" from "this address can't", because
 * only the second one has a fix.
 */
export function installBlockedReason(): string | null {
  if (isInstalled() || canInstall()) return null;
  if (!window.isSecureContext) {
    return `Vocalis is being served over an insecure connection (${window.location.origin}). ` +
      "Browsers only allow apps to be installed from https:// addresses, or from " +
      "localhost on the machine itself.";
  }
  if (!("serviceWorker" in navigator)) {
    return "This browser does not support installable web apps.";
  }
  return null;
}

/** Show the browser's install dialog. Returns true if the user accepted. */
export async function promptInstall(): Promise<boolean> {
  if (!deferredPrompt) return false;
  const event = deferredPrompt;
  // A prompt event is single-use; drop it before awaiting so a double click
  // cannot fire it twice.
  deferredPrompt = null;
  notify();
  await event.prompt();
  const { outcome } = await event.userChoice;
  return outcome === "accepted";
}

export function registerServiceWorker(): void {
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault(); // suppress Chrome's mini-infobar; we have our own button
    deferredPrompt = event as BeforeInstallPromptEvent;
    notify();
  });

  window.addEventListener("appinstalled", () => {
    deferredPrompt = null;
    notify();
  });

  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;

  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").then(
      (reg) => {
        // A worker waiting behind an open tab would otherwise serve the old
        // shell until every tab closed — the same staleness that made a
        // rebuilt UI look broken before.
        const activate = () => reg.waiting?.postMessage("skip-waiting");
        if (reg.waiting) activate();
        reg.addEventListener("updatefound", () => {
          reg.installing?.addEventListener("statechange", function () {
            if (this.state === "installed" && navigator.serviceWorker.controller) activate();
          });
        });
      },
      (err) => console.warn("Service worker registration failed:", err)
    );

    let reloading = false;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      // Guard: without it, skipWaiting can loop the page.
      if (reloading) return;
      reloading = true;
      window.location.reload();
    });
  });
}
