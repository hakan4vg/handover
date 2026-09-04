import { restoreBrowserDownload } from './download-fallback';

// Local copies (not imported): MV3 content scripts must be classic scripts,
// so they cannot share an ES module chunk with the service worker.
import type { BrowserPolicy } from './shared';

function siteOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '').toLowerCase();
  } catch {
    return '';
  }
}

function isHttp(url: string): boolean {
  try {
    const scheme = new URL(url).protocol;
    return scheme === 'http:' || scheme === 'https:';
  } catch {
    return false;
  }
}

const BUTTON_ID = 'dm-media-download-button';
const MIN_SIZE = 120;

let policy: BrowserPolicy | null = null;
let current: HTMLVideoElement | HTMLAudioElement | null = null;
let button: HTMLButtonElement | null = null;
let frame: number | null = null;
let nextPlayerKey = 1;
const playerKeys = new WeakMap<HTMLMediaElement, string>();
const observedPlayers = new WeakSet<HTMLMediaElement>();
const lastPlayerReports = new WeakMap<HTMLMediaElement, number>();

function cleanFilename(value: string | null | undefined): string | undefined {
  const leaf = value?.trim().split('/').pop()?.split('\\').pop()?.trim();
  return leaf || undefined;
}

function basenameFromUrl(url: string): string | undefined {
  try {
    return cleanFilename(new URL(url).pathname);
  } catch {
    return undefined;
  }
}

// Explicit <a download> clicks are the generic pre-browser action Chromium
// exposes safely. Capture in the document's capture phase, before page
// handlers/default navigation can consume a one-use URL. Other browser-owned
// downloads retain the observe-only downloads.onCreated fallback.
function interceptDownloadClick(event: MouseEvent): void {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  if (!policy?.interceptDownloads) return;
  const target = event.target;
  if (!(target instanceof Element)) return;
  const anchor = target.closest('a');
  if (!(anchor instanceof HTMLAnchorElement) || !anchor.hasAttribute('download') || anchor.hasAttribute('data-dm-browser-fallback')) return;
  const source = anchor.href;
  if (!isHttp(source)) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  void chrome.runtime.sendMessage({
    type: 'ordinary-capture',
    payload: {
      source,
      name: cleanFilename(anchor.getAttribute('download')) ?? basenameFromUrl(source),
      pageUrl: window.location.href,
    },
  }).catch(() => {
    restoreBrowserDownload(source, cleanFilename(anchor.getAttribute('download')) ?? basenameFromUrl(source));
  });
}

let policyTimer: number | null = null;

async function refreshPolicy(): Promise<void> {
  try {
    const response = (await chrome.runtime.sendMessage({ type: 'get-policy' })) as {
      ok?: boolean;
      policy?: BrowserPolicy;
    };
    if (response?.policy) {
      policy = response.policy;
      // Back off once the worker answers; it pushes nothing on its own.
      if (policyTimer !== null) {
        window.clearInterval(policyTimer);
        policyTimer = null;
      }
      return;
    }
  } catch {
    policy = null;
  }
  // The service worker may still be waking (install/first message); retry
  // fast until the first answer instead of assuming the first attempt works.
  if (policyTimer === null) policyTimer = window.setInterval(refreshPolicy, 1000);
}

function active(): boolean {
  if (!policy?.showMediaButtons) return false;
  const site = siteOf(window.location.href);
  return !!site && !policy.excludedSites.includes(site);
}

function visible(el: HTMLMediaElement): boolean {
  const rect = el.getBoundingClientRect();
  return (
    rect.width >= MIN_SIZE &&
    rect.height >= MIN_SIZE / 3 &&
    rect.bottom > 0 &&
    rect.right > 0 &&
    rect.top < window.innerHeight &&
    rect.left < window.innerWidth
  );
}

function keyFor(el: HTMLMediaElement): string {
  const existing = playerKeys.get(el);
  if (existing) return existing;
  const key = `player-${nextPlayerKey++}`;
  playerKeys.set(el, key);
  return key;
}

function reportPlayer(el: HTMLMediaElement, force = false): void {
  if (!active()) return;
  const now = Date.now();
  if (!force && now - (lastPlayerReports.get(el) ?? 0) < 500) return;
  lastPlayerReports.set(el, now);
  const source = el.currentSrc || el.src || '';
  void chrome.runtime.sendMessage({
    type: 'media-player-state',
    payload: {
      playerKey: keyFor(el),
      source: isHttp(source) ? source : '',
      active: el === current,
      hovered: el.matches(':hover'),
      playing: !el.paused && !el.ended,
      visible: visible(el),
    },
  }).catch(() => undefined);
}

function observePlayer(el: HTMLMediaElement): void {
  keyFor(el);
  if (observedPlayers.has(el)) return;
  observedPlayers.add(el);
  const update = () => {
    track();
    reportPlayer(el, true);
  };
  for (const event of ['play', 'playing', 'pause', 'ended', 'loadedmetadata', 'emptied', 'mouseenter', 'mouseleave']) el.addEventListener(event, update, { passive: true });
}

function pick(): HTMLVideoElement | HTMLAudioElement | null {
  // Hovering the button itself must keep the current player: the button is a
  // separate fixed element, so :hover on the media is lost while the pointer
  // is over the button. Without this the control vanishes from under the
  // cursor and can never be clicked.
  if (current?.isConnected && button?.isConnected && button.matches(':hover')) return current;
  const hovered = document.querySelectorAll('video, audio');
  for (const el of hovered) {
    const media = el as HTMLVideoElement | HTMLAudioElement;
    if (media.matches(':hover') && visible(media)) return media;
  }
  let best: HTMLVideoElement | HTMLAudioElement | null = null;
  let bestArea = 0;
  document.querySelectorAll('video, audio').forEach((el) => {
    const media = el as HTMLVideoElement | HTMLAudioElement;
    if (media.paused || media.ended || !visible(media)) return;
    const rect = media.getBoundingClientRect();
    const area = rect.width * rect.height;
    if (area > bestArea) {
      bestArea = area;
      best = media;
    }
  });
  return best;
}

function ensureButton(): HTMLButtonElement {
  if (button?.isConnected) return button;
  button = document.createElement('button');
  button.id = BUTTON_ID;
  button.type = 'button';
  button.textContent = 'Download';
  button.setAttribute('aria-label', 'Download this media');
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    event.preventDefault();
    void capture();
  });
  const style = document.createElement('style');
  style.textContent = `#${BUTTON_ID}{position:fixed;z-index:2147483647;padding:6px 12px;border:0;border-radius:6px;background:#0878ed;color:#fff;font:600 12px/1.4 system-ui,sans-serif;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.3)}#${BUTTON_ID}:hover{background:#006bd6}`;
  document.documentElement.appendChild(style);
  document.documentElement.appendChild(button);
  return button;
}

let loopCount = 0;

function positionButton(): boolean {
  if (!current || !active() || !current.isConnected || !visible(current)) {
    button?.remove();
    button = null;
    return false;
  }
  const el = ensureButton();
  const rect = current.getBoundingClientRect();
  el.style.top = `${Math.max(8, rect.top + 10)}px`;
  el.style.left = `${Math.max(8, rect.right - el.offsetWidth - 12)}px`;
  return true;
}

function loop(): void {
  frame = null;
  loopCount++;
  // rAF gives smooth following during scroll/resize, but it can be throttled
  // in backgrounded pages — track() positions directly too, so the control
  // never depends on rAF alone to exist.
  if (!positionButton()) {
    current = null;
    return;
  }
  frame = requestAnimationFrame(loop);
}

function track(): void {
  // Discipline: every DOM reaction below must be idempotent (guarded appends,
  // same-value style writes). track() runs on a timer AND on a whole-document
  // MutationObserver; any non-idempotent write here (e.g. rewriting
  // document.title every tick) re-triggers the observer into a
  // self-perpetuating loop that starves the page's main thread. Proven live.
  const media = document.querySelectorAll('video, audio');
  media.forEach((el) => observePlayer(el as HTMLMediaElement));
  const next = active() ? pick() : null;
  if (next !== current) {
    const previous = current;
    current = next;
    if (frame !== null) {
      cancelAnimationFrame(frame);
      frame = null;
    }
    button?.remove();
    button = null;
    if (previous) reportPlayer(previous, true);
  }
  if (current) reportPlayer(current, true);
  if (current && frame === null) {
    if (!positionButton()) current = null;
    else frame = requestAnimationFrame(loop);
  }
}

async function capture(): Promise<void> {
  if (!current) return;
  const el = current as HTMLVideoElement;
  const source = el.currentSrc || el.src || window.location.href;
  try {
    const response = (await chrome.runtime.sendMessage({
      type: 'media-capture',
      payload: {
        source: isHttp(source) ? source : '',
        pageUrl: window.location.href,
        media: true,
        playerKey: keyFor(el),
        name: document.title ? `${document.title.slice(0, 80)}.mp4` : undefined,
      },
    })) as { ok?: boolean; error?: string };
    if (!response?.ok) flashError();
  } catch {
    flashError();
  }
}

function flashError(): void {
  if (!button) return;
  const original = button.textContent;
  button.textContent = 'Unavailable';
  window.setTimeout(() => {
    if (button) button.textContent = original;
  }, 1600);
}

void refreshPolicy().then(() => {
  window.setInterval(track, 500);
  window.setInterval(refreshPolicy, 10_000);
  new MutationObserver(track).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener('click', interceptDownloadClick, true);
  document.addEventListener('mouseenter', track, true);
  document.addEventListener('scroll', track, { capture: true, passive: true });
  chrome.storage.onChanged.addListener(() => void refreshPolicy());
  track();
});
