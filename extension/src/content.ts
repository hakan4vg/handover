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
  const next = active() ? pick() : null;
  if (next !== current) {
    current = next;
    if (frame !== null) {
      cancelAnimationFrame(frame);
      frame = null;
    }
    button?.remove();
    button = null;
  }
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
  document.addEventListener('mouseenter', track, true);
  document.addEventListener('scroll', track, { capture: true, passive: true });
  chrome.storage.onChanged.addListener(() => void refreshPolicy());
  track();
});
