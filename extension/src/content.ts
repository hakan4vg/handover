import { captureNeedsBrowserRestore, restoreBrowserDownload } from './download-fallback';

// Local copies (not imported): MV3 content scripts must be classic scripts,
// so they cannot share an ES module chunk with the service worker.
import type { BrowserPolicy, MediaFilterSettings } from './shared';

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
const BUTTON_STYLE_ID = 'dm-media-download-style';
const MIN_SIZE = 120;
const DEFAULT_MEDIA_FILTERS: MediaFilterSettings = { minimumSizeBytes: 0, excludedFileTypes: ['gif'] };
const MEDIA_FILTER_MIME_TYPES: Record<string, string> = {
  'audio/aac': 'aac',
  'audio/flac': 'flac',
  'audio/mpeg': 'mp3',
  'audio/mp4': 'm4a',
  'audio/ogg': 'ogg',
  'audio/wav': 'wav',
  'audio/webm': 'webm',
  'image/gif': 'gif',
  'image/webp': 'webp',
  'video/mp4': 'mp4',
  'video/ogg': 'ogv',
  'video/quicktime': 'mov',
  'video/webm': 'webm',
  'video/x-m4v': 'm4v',
};

type MediaElement = HTMLVideoElement | HTMLAudioElement;

let policy: BrowserPolicy | null = null;
let mediaFilters: MediaFilterSettings = { ...DEFAULT_MEDIA_FILTERS, excludedFileTypes: [...DEFAULT_MEDIA_FILTERS.excludedFileTypes] };
let mediaFilterGeneration = 0;
const mediaFilterCache = new Map<string, { allowed: boolean; expires: number }>();
let current: HTMLVideoElement | HTMLAudioElement | null = null;
let button: HTMLButtonElement | null = null;
let frame: number | null = null;
let captureInFlight = false;
let lastPointerX: number | null = null;
let lastPointerY: number | null = null;
let pointerRAF: number | null = null;
let buttonStyle: HTMLStyleElement | null = null;
const mediaIdentities = new WeakMap<HTMLMediaElement, { source: string; identity: string }>();
const mediaFilterKeys = new WeakMap<HTMLMediaElement, string>();
const observedPlayers = new WeakSet<HTMLMediaElement>();

let nextMediaIdentity = 1;

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
  if (!event.isTrusted) return;
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
  // A click synthesized by page script (anchor.click(), dispatchEvent) is not
  // the user's download action: leave it to the browser, whose own download
  // handling (and the downloads-API handoff) applies as for any other page
  // download, instead of letting the page aim resident fetches directly.
  if (!event.isTrusted) return;
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  if (policy && (!policy.interceptDownloads || !siteAllowed())) return;
  const target = event.target;
  if (!(target instanceof Element)) return;
  const anchor = target.closest('a');
  if (!(anchor instanceof HTMLAnchorElement) || !anchor.hasAttribute('download') || anchor.hasAttribute('data-dm-browser-fallback')) return;
  const source = anchor.href;
  if (!isHttp(source)) return;
  // After the extension is reloaded or updated, scripts already injected
  // into open tabs are orphaned: runtime.id is gone and sendMessage throws.
  // Such a script must leave the click to the browser, not swallow it.
  if (!chrome.runtime?.id) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  // Chromium drops the author-supplied filename for cross-origin targets.
  // Mirror that: honor the download attribute only when the target is
  // same-origin with the page. Either way the name is only a hint: the
  // server's filename outranks it, then the URL, decided by the resident
  // once it sees the response.
  let authorName: string | undefined;
  try {
    authorName = new URL(source).origin === new URL(window.location.href).origin
      ? cleanFilename(anchor.getAttribute('download'))
      : undefined;
  } catch {
    authorName = undefined;
  }
  const name = authorName;
  let sent: Promise<unknown>;
  try {
    sent = chrome.runtime.sendMessage({
      type: 'ordinary-capture',
      payload: {
        source,
        name,
        pageUrl: window.location.href,
        userAgent: navigator.userAgent,
      },
    });
  } catch (error) {
    // An invalidated extension context throws here synchronously, not as a
    // rejection; the click was already prevented, so hand it back now.
    sent = Promise.reject(error);
  }
  void sent.then((response) => {
    // Background answers ok:false only after its own downloads-API fallback
    // failed; the synthetic anchor is the last resort, not a duplicate.
    if (captureNeedsBrowserRestore(response)) restoreBrowserDownload(source, name);
  }).catch(() => {
    restoreBrowserDownload(source, name);
  });
}

let policyTimer: number | null = null;

function applyMediaFilters(value: unknown): void {
  if (!value || typeof value !== 'object') return;
  const record = value as Partial<MediaFilterSettings>;
  const minimumSizeBytes = typeof record.minimumSizeBytes === 'number' && Number.isFinite(record.minimumSizeBytes) && record.minimumSizeBytes >= 0
    ? Math.min(Math.floor(record.minimumSizeBytes), Number.MAX_SAFE_INTEGER)
    : mediaFilters.minimumSizeBytes;
  const excludedFileTypes = Array.isArray(record.excludedFileTypes)
    ? [...new Set(record.excludedFileTypes.filter((item): item is string => typeof item === 'string').map((item) => {
      const token = item.trim().toLowerCase();
      return token.includes('/') ? mediaFileTypeForLocal('', token) : token.replace(/^\./, '');
    }).filter((item): item is string => !!item && /^[a-z0-9]{1,12}$/.test(item)))]
    : mediaFilters.excludedFileTypes;
  const changed = minimumSizeBytes !== mediaFilters.minimumSizeBytes
    || excludedFileTypes.length !== mediaFilters.excludedFileTypes.length
    || excludedFileTypes.some((item, index) => item !== mediaFilters.excludedFileTypes[index]);
  if (!changed) return;
  mediaFilters = { minimumSizeBytes, excludedFileTypes };
  mediaFilterGeneration += 1;
  mediaFilterCache.clear();
}

async function refreshPolicy(): Promise<void> {
  try {
    const response = (await chrome.runtime.sendMessage({ type: 'get-policy', includeMediaFilters: true })) as {
      ok?: boolean;
      policy?: BrowserPolicy;
      mediaFilters?: MediaFilterSettings;
    };
    if (response?.policy) {
      policy = response.policy;
      applyMediaFilters(response.mediaFilters);
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

function currentSrcFor(el: HTMLMediaElement): string {
  const raw = el.currentSrc || el.getAttribute('src') || el.querySelector('source[src]')?.getAttribute('src') || '';
  if (!raw.trim()) return '';
  try {
    return new URL(raw, document.baseURI).href;
  } catch {
    return raw.trim();
  }
}

function mediaIdentityFor(el: HTMLMediaElement, source = currentSrcFor(el)): string {
  const existing = mediaIdentities.get(el);
  if (existing?.source === source) return existing.identity;
  const identity = `media-${nextMediaIdentity++}:${source}`;
  mediaIdentities.set(el, { source, identity });
  return identity;
}

function sourceFor(el: HTMLMediaElement): string {
  const child = el.querySelector('source[src]')?.getAttribute('src');
  return mediaSourceFromValues(el.currentSrc, el.src, child, document.baseURI);
}

function mediaFileTypeForLocal(url: string, contentType = ''): string | undefined {
  const mime = contentType.toLowerCase().split(';', 1)[0].trim();
  if (MEDIA_FILTER_MIME_TYPES[mime]) return MEDIA_FILTER_MIME_TYPES[mime];
  if (mime.startsWith('audio/') || mime.startsWith('video/') || mime.startsWith('image/')) {
    const subtype = mime.slice(mime.indexOf('/') + 1).replace(/^x-/, '').split('+', 1)[0].trim();
    if (subtype) return subtype === 'mpeg' ? 'mp3' : subtype;
  }
  try {
    const parsed = new URL(url, document.baseURI);
    const queryMime = (parsed.searchParams.get('mime') || '').toLowerCase().split(';', 1)[0].trim();
    if (MEDIA_FILTER_MIME_TYPES[queryMime]) return MEDIA_FILTER_MIME_TYPES[queryMime];
    const extension = parsed.pathname.toLowerCase().match(/\.([a-z0-9]{1,12})$/)?.[1];
    return extension || undefined;
  } catch {
    return undefined;
  }
}

function sourceTypeHint(el: HTMLMediaElement, source: string): string {
  const sourceElement = [...el.querySelectorAll('source')].find((item) => item.src === source || item.getAttribute('src') === source);
  return el.getAttribute('type') || sourceElement?.getAttribute('type') || '';
}

function filterCacheKey(el: HTMLMediaElement, source: string): string {
  const identity = mediaIdentityFor(el, currentSrcFor(el) || source);
  const key = `${identity}:${mediaFilterGeneration}`;
  const previous = mediaFilterKeys.get(el);
  if (previous && previous !== key) mediaFilterCache.delete(previous);
  mediaFilterKeys.set(el, key);
  return key;
}

function requestMediaFilter(el: HTMLMediaElement, source: string, key: string): void {
  const cached = mediaFilterCache.get(key);
  if (cached && cached.expires > Date.now()) return;
  if (mediaFilterCache.size >= 128 && !mediaFilterCache.has(key)) {
    const oldest = mediaFilterCache.keys().next().value;
    if (oldest) mediaFilterCache.delete(oldest);
  }
  mediaFilterCache.set(key, { allowed: cached?.allowed ?? true, expires: Infinity });
  const generation = mediaFilterGeneration;
  void (async () => {
    let allowed = true;
    let expires = Date.now() + 3000;
    try {
      const playerKind = el instanceof HTMLAudioElement ? 'audio' : 'video';
      // Filters apply to a source the page names; a blob player's source is
      // only resolved when it is captured.
      if (isHttp(source)) {
        const response = await chrome.runtime.sendMessage({
          type: 'check-media-filters',
          payload: { source, playerKind },
        }) as { ok?: boolean; allowed?: boolean; totalBytes?: number; reason?: string } | undefined;
        allowed = response?.allowed !== false;
        if (isHttp(source) && response?.ok && (response.totalBytes !== undefined || response.reason === 'excluded-type')) expires = Infinity;
      }
    } catch {
      // Retry unknown metadata without blocking playback or source requests.
    }
    if (generation !== mediaFilterGeneration || mediaFilterKeys.get(el) !== key) return;
    mediaFilterCache.set(key, { allowed, expires });
    if (allowed !== (cached?.allowed ?? true)) track();
  })();
}

function mediaFilterAllowed(el: HTMLMediaElement, source: string): boolean {
  const localType = mediaFileTypeForLocal(source, sourceTypeHint(el, source));
  if (localType && mediaFilters.excludedFileTypes.includes(localType)) return false;
  const key = filterCacheKey(el, source);
  requestMediaFilter(el, source, key);
  return mediaFilterCache.get(key)?.allowed !== false;
}

function usable(el: HTMLMediaElement): boolean {
  const source = sourceFor(el);
  return !el.hasAttribute('disabled') && !!source && mediaFilterAllowed(el, source);
}

// Media inside web-component players (Media Chrome / mux-video and similar)
// lives in open shadow roots; document.querySelectorAll('video, audio') cannot
// see it. Keep light and shadow results incrementally fresh, and pierce open
// shadow roots at most once per second to bound cost on heavy pages. Closed
// shadow roots stay invisible.
let lastShadowScan = 0;
let lightMediaCache: MediaElement[] | null = null;
let shadowMediaCache: MediaElement[] = [];
let mediaCache: MediaElement[] | null = null;
let mediaObserver: MutationObserver | null = null;

const mediaMutationOptions: MutationObserverInit = {
  childList: true,
  subtree: true,
  attributes: true,
  attributeFilter: ['src', 'disabled', 'type'],
};

function mediaInNode(node: Node): MediaElement[] {
  const found: MediaElement[] = [];
  if (node instanceof HTMLVideoElement || node instanceof HTMLAudioElement) found.push(node);
  if (node instanceof Element || node instanceof DocumentFragment || node instanceof Document) {
    node.querySelectorAll('video, audio').forEach((media) => {
      if (media instanceof HTMLVideoElement || media instanceof HTMLAudioElement) found.push(media);
    });
  }
  return found;
}

function appendMedia(cache: MediaElement[], values: MediaElement[]): void {
  const known = new Set(cache);
  values.forEach((media) => {
    if (!known.has(media)) {
      known.add(media);
      cache.push(media);
    }
  });
}

function mediaRootIsConnected(media: MediaElement, root: Document | ShadowRoot): boolean {
  return media.isConnected && media.getRootNode() === root;
}

function rebuildMediaCache(): MediaElement[] {
  const light = lightMediaCache ?? [];
  shadowMediaCache = shadowMediaCache.filter((media) => {
    const root = media.getRootNode();
    return typeof ShadowRoot !== 'undefined' && root instanceof ShadowRoot && mediaRootIsConnected(media, root);
  });
  mediaCache = [...light, ...shadowMediaCache];
  return mediaCache;
}

function updateMediaCache(records: MutationRecord[]): void {
  if (lightMediaCache === null) return;
  let lightChanged = false;
  let shadowChanged = false;
  for (const record of records) {
    if (record.type !== 'childList') continue;
    const root = record.target.getRootNode();
    const isShadow = typeof ShadowRoot !== 'undefined' && root instanceof ShadowRoot;
    const cache = isShadow ? shadowMediaCache : lightMediaCache;
    record.addedNodes.forEach((node) => appendMedia(cache, mediaInNode(node)));
    if (isShadow) shadowChanged = true;
    else lightChanged = true;
  }
  if (lightChanged) {
    lightMediaCache = lightMediaCache.filter((media) => mediaRootIsConnected(media, document));
  }
  if (shadowChanged || lightChanged) rebuildMediaCache();
}

function scanShadowMedia(): void {
  if (typeof ShadowRoot === 'undefined') return;
  const found: MediaElement[] = [];
  const known = new Set<MediaElement>();
  const scan = (root: ParentNode): void => {
    root.querySelectorAll('*').forEach((el) => {
      const shadow = (el as HTMLElement).shadowRoot;
      if (!shadow) return;
      mediaObserver?.observe(shadow, mediaMutationOptions);
      shadow.querySelectorAll('video, audio').forEach((media) => {
        if ((media instanceof HTMLVideoElement || media instanceof HTMLAudioElement) && !known.has(media)) {
          known.add(media);
          found.push(media);
        }
      });
      scan(shadow);
    });
  };
  scan(document);
  shadowMediaCache = found;
  lastShadowScan = performance.now();
  rebuildMediaCache();
}

function collectMedia(scanShadow = true): MediaElement[] {
  if (lightMediaCache === null) lightMediaCache = Array.from(document.querySelectorAll('video, audio')) as MediaElement[];
  if (!active()) {
    mediaCache = null;
    return lightMediaCache;
  }
  const now = performance.now();
  // The full-document shadow scan is the expensive part of tracking: rescan
  // every second while shadow-hosted media exists, every five otherwise.
  const shadowInterval = shadowMediaCache.length ? 1000 : 5000;
  if (scanShadow && (mediaCache === null || now - lastShadowScan >= shadowInterval)) scanShadowMedia();
  return mediaCache ?? rebuildMediaCache();
}

function isPointerOver(el: HTMLMediaElement, x: number | null, y: number | null): boolean {
  if (x === null || y === null) return el.matches(':hover');
  const rect = anchorRect(el);
  if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) {
    return true;
  }
  try {
    const container = el.closest?.(
      '.html5-video-player, [data-testid*="video"], [class*="player"], figure, .video-js, .plyr'
    ) || el.parentElement;
    if (container && container !== document.body && container !== document.documentElement) {
      const cRect = container.getBoundingClientRect();
      if (
        cRect.width >= MIN_SIZE &&
        cRect.height >= MIN_SIZE / 3 &&
        x >= cRect.left &&
        x <= cRect.right &&
        y >= cRect.top &&
        y <= cRect.bottom
      ) {
        return true;
      }
    }
  } catch {
    // Ignore invalid selector queries
  }
  return el.matches(':hover');
}

function observePlayer(el: HTMLMediaElement): void {
  if (observedPlayers.has(el)) return;
  observedPlayers.add(el);
  for (const event of ['play', 'playing', 'pause', 'ended', 'loadedmetadata', 'emptied', 'mouseenter', 'mouseleave']) el.addEventListener(event, track, { passive: true });
}

/** What the element was fed, as page-probe.ts recorded it: asked and answered
 *  synchronously through events on the element itself. */
function playerEvidence(el: HTMLMediaElement): unknown {
  let answer: unknown;
  const listen = (event: Event) => {
    answer ??= (event as CustomEvent).detail;
  };
  el.addEventListener('dm-player-evidence', listen);
  try {
    el.dispatchEvent(new CustomEvent('dm-player-evidence-request', { bubbles: true, composed: true }));
  } finally {
    el.removeEventListener('dm-player-evidence', listen);
  }
  try {
    return typeof answer === 'string' ? JSON.parse(answer) : undefined;
  } catch {
    return undefined;
  }
}

function isPointerOverButton(btn: HTMLButtonElement, x: number | null, y: number | null): boolean {
  if (x === null || y === null) return btn.matches(':hover');
  const rect = btn.getBoundingClientRect();
  return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
}

function pick(): HTMLVideoElement | HTMLAudioElement | null {
  // Hovering the button itself must keep the current player: the button is a
  // separate fixed element, so pointer on the button must not dismiss it.
  if (current?.isConnected && button?.isConnected && (document.activeElement === button || isPointerOverButton(button, lastPointerX, lastPointerY))) {
    return current;
  }
  const media = collectMedia(false);
  // 1. Hovered media (playing OR paused)
  for (const el of media) {
    const item = el as HTMLVideoElement | HTMLAudioElement;
    if (isPointerOver(item, lastPointerX, lastPointerY) && visible(item) && usable(item)) return item;
  }
  // 2. Best playing media
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
  if (!buttonStyle?.isConnected) {
    const existing = document.getElementById(BUTTON_STYLE_ID);
    buttonStyle = existing instanceof HTMLStyleElement ? existing : document.createElement('style');
    buttonStyle.id = BUTTON_STYLE_ID;
    buttonStyle.textContent = `
#${BUTTON_ID}{all:initial;box-sizing:border-box;position:fixed;z-index:2147483647;display:inline-flex;align-items:center;justify-content:center;height:26px;min-width:26px;padding:5px;border:1px solid rgba(255,255,255,.18);border-radius:7px;background:rgba(20,24,30,.55);color:#fff;font:500 11px/1.2 system-ui,sans-serif;cursor:pointer;opacity:.8;backdrop-filter:blur(4px);transition:opacity .16s,background .16s;overflow:hidden}
#${BUTTON_ID} svg{display:block;flex:0 0 14px;width:14px;height:14px;pointer-events:none}
#${BUTTON_ID}::after{content:attr(data-label);display:block;max-width:0;margin-left:0;opacity:0;white-space:nowrap;overflow:hidden;transition:max-width .16s,margin-left .16s,opacity .16s}
#${BUTTON_ID}:hover,#${BUTTON_ID}:focus-visible{opacity:1;background:rgba(20,24,30,.82)}
#${BUTTON_ID}:hover::after,#${BUTTON_ID}:focus-visible::after{max-width:96px;margin-left:6px;opacity:1}
#${BUTTON_ID}:focus-visible{outline:2px solid #fff;outline-offset:2px}
#${BUTTON_ID}:disabled{cursor:wait}
@media(prefers-reduced-motion:reduce){#${BUTTON_ID},#${BUTTON_ID}::after{transition:none}}
`;
    document.documentElement.appendChild(buttonStyle);
  }
  if (button?.isConnected) return button;
  button = document.createElement('button');
  button.id = BUTTON_ID;
  button.type = 'button';
  button.dataset.label = 'Download';
  button.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12m-4-4 4 4 4-4M5 17v4h14v-4"/></svg>';
  button.setAttribute('aria-label', 'Download this media');
  button.addEventListener('click', (event) => {
    // Only the user may press it: the button sits in the page's DOM, so page
    // script could otherwise click it and start resident fetches at will.
    if (!event.isTrusted) return;
    event.stopPropagation();
    event.preventDefault();
    void capture();
  });
  document.documentElement.appendChild(button);
  return button;
}

function positionButton(): boolean {
  if (!current || !active() || !current.isConnected || !visible(current) || !usable(current)) {
    button?.remove();
    button = null;
    return false;
  }
  const el = ensureButton();
  const rect = anchorRect(current);
  const top = `${Math.max(6, rect.top + 6)}px`;
  const left = `${Math.max(32, Math.min(document.documentElement.clientWidth - 6, rect.right - 6))}px`;
  buttonMoved = el.style.top !== top || el.style.left !== left;
  el.style.top = top;
  el.style.left = left;
  el.style.transform = 'translateX(-100%)';
  return true;
}

// The button follows its media without a standing per-frame loop, which kept
// an otherwise idle page rendering 60 frames a second for as long as the
// button showed. Anything that can move the media (scroll, window or media
// resize, a CSS transition or animation, fullscreen, the pointer, track()
// finding it moved) starts a short burst of frames that repositions the
// button until it has held still for FOLLOW_STILL_FRAMES frames, then stops.
const FOLLOW_STILL_FRAMES = 10;
let stillFrames = 0;
let buttonMoved = false;
let watchedMedia: MediaElement | null = null;
const mediaResize = typeof ResizeObserver === 'function' ? new ResizeObserver(() => follow()) : null;

function follow(): void {
  stillFrames = 0;
  if (current && frame === null) frame = requestAnimationFrame(loop);
}

function loop(): void {
  frame = null;
  if (!positionButton()) {
    current = null;
    watchCurrent();
    return;
  }
  stillFrames = buttonMoved ? 0 : stillFrames + 1;
  if (stillFrames < FOLLOW_STILL_FRAMES) frame = requestAnimationFrame(loop);
}

/** Watch the current media's box (and its parent's, which an audio element
 *  may borrow as its anchor) so a player that grows, shrinks or reflows
 *  moves the button without polling. */
function watchCurrent(): void {
  if (watchedMedia === current || !mediaResize) return;
  mediaResize.disconnect();
  watchedMedia = current;
  if (!current) return;
  mediaResize.observe(current);
  if (current.parentElement) mediaResize.observe(current.parentElement);
}

/** Position now, and keep following for a few frames if the media moved. */
function reposition(): void {
  if (!positionButton()) {
    current = null;
    watchCurrent();
    return;
  }
  if (buttonMoved) follow();
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
    current = next;
    if (frame !== null) {
      cancelAnimationFrame(frame);
      frame = null;
    }
    button?.remove();
    button = null;
    watchCurrent();
  }
  // Positioning here, not only in rAF, keeps the control independent of rAF,
  // which backgrounded pages throttle.
  if (current) reposition();
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
  if (!current || captureInFlight) return;
  const el = current;
  const currentSrc = currentSrcFor(el);
  const playerKind = el instanceof HTMLAudioElement ? 'audio' : 'video';
  captureInFlight = true;
  if (button) {
    button.disabled = true;
    button.dataset.label = 'Download';
    button.title = '';
    button.setAttribute('aria-label', 'Download this media');
  }
  try {
    const source = currentSrc.startsWith('http:') || currentSrc.startsWith('https:') ? currentSrc : '';
    const response = (await chrome.runtime.sendMessage({
      type: 'media-capture',
      payload: {
        source: isHttp(source) ? source : '',
        ...(isHttp(source) ? {} : { player: playerEvidence(el) }),
        pageUrl: window.location.href,
        userAgent: navigator.userAgent,
        media: true,
        playerKind,
        name: captureName(el, source || currentSrc),
      },
    })) as { ok?: boolean; error?: string; reason?: string };
    if (!response?.ok) flashError(response?.error, response?.reason === 'not-played' ? 'Play first' : response?.reason === 'not-visible' ? 'Not visible' : undefined);
  } catch {
    flashError();
  } finally {
    captureInFlight = false;
    if (button) button.disabled = false;
  }
}

function flashError(error?: string, label = 'Unavailable'): void {
  if (!button) return;
  button.dataset.label = label;
  button.title = error || 'Media unavailable';
  button.setAttribute('aria-label', `${button.title}. Retry download`);
}

function onPointerMove(event: PointerEvent | MouseEvent): void {
  lastPointerX = event.clientX;
  lastPointerY = event.clientY;
  if (pointerRAF !== null) return;
  pointerRAF = window.requestAnimationFrame(() => {
    pointerRAF = null;
    if (!active()) return;
    const next = pick();
    if (next !== current) {
      track();
    } else if (current && button?.isConnected) {
      reposition();
    }
  });
}

document.addEventListener('click', rememberBrowserOwnedClick, true);
document.addEventListener('click', interceptDownloadClick, true);
window.addEventListener('pointermove', onPointerMove, { passive: true });
window.addEventListener('mouseleave', () => {
  lastPointerX = null;
  lastPointerY = null;
  track();
}, { passive: true });
document.addEventListener('mouseenter', track, true);
document.addEventListener('scroll', track, { capture: true, passive: true });
window.addEventListener('resize', follow, { passive: true });
document.addEventListener('fullscreenchange', follow);
// Players that slide, dock or expand with CSS move without resizing; follow
// for the length of the transition or animation (the burst outlasts it by a
// few still frames once it ends).
for (const type of ['transitionstart', 'transitionend', 'animationstart', 'animationend']) {
  document.addEventListener(type, () => { if (current) follow(); }, { capture: true, passive: true });
}
chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === 'local' && (changes['dm-policy'] || changes['dm-media-filters'])) void refreshPolicy();
});
// No standing poll: storage changes push browser-side edits, and returning to
// a tab picks up edits made in the resident (the worker re-reads its policy
// on each of these and again before every interception decision).
if (window.top === window) {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void refreshPolicy();
  });
  window.addEventListener('focus', () => { void refreshPolicy(); });
}

void refreshPolicy().then(() => {
  window.setInterval(() => {
    if (!document.hidden) track();
  }, 500);
  let queuedTrack: number | null = null;
  const scheduleTrack = () => {
    if (queuedTrack !== null) return;
    queuedTrack = window.setTimeout(() => {
      queuedTrack = null;
      track();
    }, 100);
  };
  mediaObserver = new MutationObserver((records) => {
    updateMediaCache(records);
    scheduleTrack();
  });
  mediaObserver.observe(document.documentElement, mediaMutationOptions);
  track();
});
