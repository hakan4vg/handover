import { captureNeedsBrowserRestore, restoreBrowserDownload } from './download-fallback';

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

function siteOfDocument(url: string, referrer: string, ancestorOrigin = ''): string {
  return siteOf(url) || siteOf(referrer) || siteOf(ancestorOrigin);
}

function isHttp(url: string): boolean {
  try {
    const scheme = new URL(url).protocol;
    return scheme === 'http:' || scheme === 'https:';
  } catch {
    return false;
  }
}

function mediaSourceFromValues(
  currentSrc: string | null | undefined,
  elementSrc: string | null | undefined,
  childSrc: string | null | undefined,
  baseUrl: string,
): string {
  const candidates = [currentSrc, elementSrc, childSrc]
    .map((value) => value?.trim() || '')
    .filter(Boolean)
    .map((raw) => {
      try {
        return new URL(raw, baseUrl).href;
      } catch {
        return raw;
      }
    });
  return candidates.find((candidate) => isHttp(candidate)) || candidates[0] || '';
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
function policySite(): string {
  return siteOfDocument(window.location.href, document.referrer, window.location.ancestorOrigins?.item(0) ?? '');
}

function siteAllowed(): boolean {
  if (!policy) return false;
  const site = policySite();
  return !!site && !policy.excludedSites.includes(site);
}

function rememberBrowserOwnedClick(event: MouseEvent): void {
  if (event.defaultPrevented || event.button !== 0 || !(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)) return;
  if (!policy?.interceptDownloads || !siteAllowed()) return;
  const target = event.target;
  if (!(target instanceof Element)) return;
  const anchor = target.closest('a');
  if (!(anchor instanceof HTMLAnchorElement) || !anchor.hasAttribute('download') || anchor.hasAttribute('data-dm-browser-fallback')) return;
  const source = anchor.href;
  if (!isHttp(source)) return;
  let name: string | undefined;
  try {
    name = new URL(source).origin === new URL(window.location.href).origin
      ? cleanFilename(anchor.getAttribute('download'))
      : basenameFromUrl(source);
  } catch {
    name = basenameFromUrl(source);
  }
  void chrome.runtime.sendMessage({ type: 'browser-owned-download', payload: { source, name } });
}

function interceptDownloadClick(event: MouseEvent): void {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  if (policy && (!policy.interceptDownloads || !siteAllowed())) return;
  const target = event.target;
  if (!(target instanceof Element)) return;
  const anchor = target.closest('a');
  if (!(anchor instanceof HTMLAnchorElement) || !anchor.hasAttribute('download') || anchor.hasAttribute('data-dm-browser-fallback')) return;
  const source = anchor.href;
  if (!isHttp(source)) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  // Chromium drops the author-supplied filename for cross-origin targets
  // and saves under the server basename instead. Mirror that: honor the
  // download attribute only when the target is same-origin with the page.
  let authorName: string | undefined;
  try {
    authorName = new URL(source).origin === new URL(window.location.href).origin
      ? cleanFilename(anchor.getAttribute('download'))
      : undefined;
  } catch {
    authorName = undefined;
  }
  const name = authorName ?? basenameFromUrl(source);
  void chrome.runtime.sendMessage({
    type: 'ordinary-capture',
    payload: {
      source,
      name,
      pageUrl: window.location.href,
      userAgent: navigator.userAgent,
    },
  }).then((response) => {
    // Background answers ok:false only after its own downloads-API fallback
    // failed; the synthetic anchor is the last resort, not a duplicate.
    if (captureNeedsBrowserRestore(response)) restoreBrowserDownload(source, name);
  }).catch(() => {
    restoreBrowserDownload(source, name);
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
  return !!policy?.showMediaButtons && siteAllowed();
}

function visible(el: HTMLMediaElement): boolean {
  const rect = anchorRect(el);
  return (
    rect.width >= MIN_SIZE &&
    rect.height >= MIN_SIZE / 3 &&
    rect.bottom > 0 &&
    rect.right > 0 &&
    rect.top < window.innerHeight &&
    rect.left < window.innerWidth
  );
}

function anchorRect(el: HTMLMediaElement): DOMRect {
  const own = el.getBoundingClientRect();
  if (own.width > 0 && own.height > 0) return own;
  // Custom audio players commonly hide the native <audio> box while keeping
  // their rendered controls in a visible wrapper. Keep the generic policy
  // narrow: only a playing, enabled audio element may borrow a non-root
  // ancestor's visible rectangle. Hidden/paused media remains ineligible.
  if (!(el instanceof HTMLAudioElement) || el.paused || el.ended) return own;
  let parent = el.parentElement;
  for (let depth = 0; parent && depth < 5; depth += 1, parent = parent.parentElement) {
    if (parent === document.body || parent === document.documentElement) continue;
    const style = getComputedStyle(parent);
    const rect = parent.getBoundingClientRect();
    if (
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      rect.width >= MIN_SIZE &&
      rect.height >= MIN_SIZE / 3 &&
      rect.bottom > 0 &&
      rect.right > 0 &&
      rect.top < window.innerHeight &&
      rect.left < window.innerWidth
    ) return rect;
  }
  return own;
}

function keyFor(el: HTMLMediaElement): string {
  const existing = playerKeys.get(el);
  if (existing) return existing;
  const key = `player-${nextPlayerKey++}`;
  playerKeys.set(el, key);
  return key;
}

function sourceFor(el: HTMLMediaElement): string {
  const child = el.querySelector('source[src]')?.getAttribute('src');
  return mediaSourceFromValues(el.currentSrc, el.src, child, document.baseURI);
}

function usable(el: HTMLMediaElement): boolean {
  return !el.hasAttribute('disabled') && !!sourceFor(el);
}

// Media inside web-component players (Media Chrome / mux-video and similar)
// lives in open shadow roots; document.querySelectorAll('video, audio') cannot
// see it. Scan light DOM every tick, and pierce open shadow roots at most once
// per second to bound cost on heavy pages. The last full result is cached so a
// throttled tick does not drop shadow media and flicker the button. Closed
// shadow roots stay invisible.
let lastShadowScan = 0;
let mediaCache: (HTMLVideoElement | HTMLAudioElement)[] | null = null;
function collectMedia(): (HTMLVideoElement | HTMLAudioElement)[] {
  const light = Array.from(document.querySelectorAll('video, audio')) as (HTMLVideoElement | HTMLAudioElement)[];
  if (!active()) {
    mediaCache = null;
    return light;
  }
  if (performance.now() - lastShadowScan < 1000) return mediaCache ?? light;
  lastShadowScan = performance.now();
  const found = light.slice();
  const scan = (root: ParentNode): void => {
    root.querySelectorAll('*').forEach((el) => {
      const shadow = (el as HTMLElement).shadowRoot;
      if (!shadow) return;
      shadow.querySelectorAll('video, audio').forEach((media) => found.push(media as HTMLVideoElement | HTMLAudioElement));
      scan(shadow);
    });
  };
  scan(document);
  mediaCache = found;
  return found;
}

function reportPlayer(el: HTMLMediaElement, force = false): void {
  if (!active()) return;
  const now = Date.now();
  if (!force && now - (lastPlayerReports.get(el) ?? 0) < 500) return;
  lastPlayerReports.set(el, now);
  const source = sourceFor(el);
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
  const media = collectMedia();
  for (const el of media) {
    const item = el as HTMLVideoElement | HTMLAudioElement;
    if (item.matches(':hover') && visible(item) && usable(item)) return item;
  }
  let best: HTMLVideoElement | HTMLAudioElement | null = null;
  let bestArea = 0;
  media.forEach((el) => {
    const media = el as HTMLVideoElement | HTMLAudioElement;
    if (media.paused || media.ended || !visible(media) || !usable(media)) return;
    const rect = anchorRect(media);
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
  if (!current || !active() || !current.isConnected || !visible(current) || !usable(current)) {
    button?.remove();
    button = null;
    return false;
  }
  const el = ensureButton();
  const rect = anchorRect(current);
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
  const media = collectMedia();
  media.forEach((el) => observePlayer(el as HTMLVideoElement | HTMLAudioElement));
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

function mediaExtension(media: HTMLMediaElement, source: string): string {
  const type = media.getAttribute('type') || [...media.querySelectorAll('source')].find(item => item.src === source || item.getAttribute('src') === source)?.getAttribute('type') || '';
  const typeExtension: Record<string, string> = {
    'audio/mpeg': 'mp3',
    'audio/mp4': 'm4a',
    'audio/ogg': 'ogg',
    'audio/wav': 'wav',
    'audio/webm': 'webm',
    'video/mp4': 'mp4',
    'video/ogg': 'ogv',
    'video/webm': 'webm',
  };
  if (typeExtension[type.toLowerCase()]) return typeExtension[type.toLowerCase()];
  try {
    const extension = new URL(source).pathname.split('/').pop()?.split('.').pop()?.toLowerCase() || '';
    if (['m4a', 'mp3', 'ogg', 'ogv', 'wav', 'webm', 'mp4'].includes(extension)) return extension;
  } catch {
    // Use the media kind fallback below for non-URL sources.
  }
  return media instanceof HTMLAudioElement ? 'mp3' : 'mp4';
}

function captureName(media: HTMLMediaElement, source: string): string | undefined {
  return document.title ? `${document.title.slice(0, 80)}.${mediaExtension(media, source)}` : undefined;
}

async function capture(): Promise<void> {
  if (!current) return;
  const el = current as HTMLVideoElement;
  const source = sourceFor(el);
  try {
    const response = (await chrome.runtime.sendMessage({
      type: 'media-capture',
      payload: {
        source: isHttp(source) ? source : '',
        pageUrl: window.location.href,
        userAgent: navigator.userAgent,
        media: true,
        playerKey: keyFor(el),
        name: captureName(el, source),
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

document.addEventListener('click', rememberBrowserOwnedClick, true);
document.addEventListener('click', interceptDownloadClick, true);
document.addEventListener('mouseenter', track, true);
document.addEventListener('scroll', track, { capture: true, passive: true });
chrome.storage.onChanged.addListener(() => void refreshPolicy());

void refreshPolicy().then(() => {
  window.setInterval(track, 500);
  window.setInterval(refreshPolicy, 10_000);
  new MutationObserver(track).observe(document.documentElement, { childList: true, subtree: true });
  track();
});
