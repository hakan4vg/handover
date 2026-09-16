import { APP_BRIDGE_ORIGIN, APP_BRIDGE_TIMEOUT_MS, DEFAULT_MEDIA_FILTERS, DEFAULT_POLICY, isHttp, mediaFileTypeFor, normalizeMediaFilterSettings, siteOf, type BrowserPolicy, type MediaFilterSettings } from './shared';
import { chooseWorkerMediaSelection, isLikelyRepresentation, isMediaCandidate, mediaKindFor, normalizeChunkUrl, roleFor, type MediaCandidate, type MediaEvidence, type MediaKind, type MediaPlayerEvidence } from './media-candidates';

const POLICY_KEY = 'dm-policy';
const MEDIA_FILTERS_KEY = 'dm-media-filters';

// Bounded ring of recent media-ish traffic per tab. M0 proof vehicle for the
// generic current-media mechanism (SPEC §6): content scripts report the
// element the user interacts with, this buffer supplies the real network
// source behind blob:/MSE players. URLs only, no bodies, no cookies.
const recentMedia: MediaCandidate[] = [];
const recentPlayers: MediaPlayerEvidence[] = [];
const MEDIA_BUFFER_MAX = 60;
const MEDIA_BUFFER_MS = 90_000;
const PLAYER_BUFFER_MAX = 40;
const PLAYER_BUFFER_MS = 15_000;

// Last-resolved acquisition source per player scope. The traffic ring above
// is bounded and shared, so long playback evicts the manifest that a later
// capture needs (F07). This record keeps the playback's current source for
// its lifetime and is only a fallback: live traffic that resolves wins, so a
// quality change in the site player still follows the new representation
// (SPEC §6.1). Session storage carries the records across service-worker
// restarts; the ring cannot.
const RESOLVED_MEDIA_KEY = 'dm-resolved-media';
const RESOLVED_MEDIA_MAX = 24;
const RESOLVED_MEDIA_TTL_MS = 15 * 60_000;
interface ResolvedMedia { scope: string; source: string; selectedSegments: string[]; companionAudio?: string; at: number }
const resolvedMedia: ResolvedMedia[] = [];

function mediaScope(tabId: number, frameId: number, documentId?: string, playerKey?: string, sourceIdentity?: string): string {
  const base = `${tabId}/${frameId}/${documentId ?? ''}/${playerKey ?? ''}`;
  return sourceIdentity === undefined ? base : `${base}/${sourceIdentity}`;
}

function pruneResolvedMedia(now = Date.now()): void {
  for (let index = resolvedMedia.length - 1; index >= 0; index -= 1) {
    if (now - resolvedMedia[index].at > RESOLVED_MEDIA_TTL_MS) resolvedMedia.splice(index, 1);
  }
  while (resolvedMedia.length > RESOLVED_MEDIA_MAX) resolvedMedia.shift();
}

function persistResolvedMedia(): void {
  try {
    void chrome.storage.session?.set({ [RESOLVED_MEDIA_KEY]: resolvedMedia });
  } catch {
    // Session persistence is best-effort; the in-memory record still serves.
  }
}

async function hydrateResolvedMedia(): Promise<void> {
  try {
    const stored = await chrome.storage.session?.get(RESOLVED_MEDIA_KEY);
    const records = stored?.[RESOLVED_MEDIA_KEY];
    if (!Array.isArray(records)) return;
    const now = Date.now();
    for (const record of records) {
      if (
        record && typeof record.scope === 'string' && typeof record.source === 'string' &&
        Array.isArray(record.selectedSegments) && typeof record.at === 'number' &&
        now - record.at <= RESOLVED_MEDIA_TTL_MS && isHttp(record.source)
      ) {
        const companionAudio = typeof record.companionAudio === 'string' && isHttp(record.companionAudio) ? record.companionAudio : undefined;
        resolvedMedia.push({ scope: record.scope, source: record.source, selectedSegments: record.selectedSegments.filter((item: unknown): item is string => typeof item === 'string' && isHttp(item)).slice(0, 8), ...(companionAudio ? { companionAudio } : {}), at: record.at });
      }
    }
    pruneResolvedMedia(now);
  } catch {
    // Start empty when session storage is unavailable.
  }
}

function rememberResolvedMedia(scope: string, source: string, selectedSegments: string[], companionAudio?: string): void {
  pruneResolvedMedia();
  const existing = resolvedMedia.find((item) => item.scope === scope);
  if (existing) {
    existing.source = source;
    existing.selectedSegments = selectedSegments.slice(0, 8);
    existing.companionAudio = companionAudio;
    existing.at = Date.now();
  } else {
    resolvedMedia.push({ scope, source, selectedSegments: selectedSegments.slice(0, 8), ...(companionAudio ? { companionAudio } : {}), at: Date.now() });
  }
  pruneResolvedMedia();
  persistResolvedMedia();
}

function takeResolvedMedia(scope: string): ResolvedMedia | undefined {
  pruneResolvedMedia();
  return resolvedMedia.find((item) => item.scope === scope);
}

function invalidateOtherResolvedMedia(tabId: number, frameId: number, documentId: string | undefined, playerKey: string | undefined, sourceIdentity: string | undefined): void {
  if (!playerKey || !sourceIdentity) return;
  const prefix = `${mediaScope(tabId, frameId, documentId, playerKey)}/`;
  const current = mediaScope(tabId, frameId, documentId, playerKey, sourceIdentity);
  let changed = false;
  for (let index = resolvedMedia.length - 1; index >= 0; index -= 1) {
    if (resolvedMedia[index].scope.startsWith(prefix) && resolvedMedia[index].scope !== current) {
      resolvedMedia.splice(index, 1);
      changed = true;
    }
  }
  if (changed) persistResolvedMedia();
}

let policy: BrowserPolicy = { ...DEFAULT_POLICY };
let policyLoadError = '';
let policyReady: Promise<void> = Promise.resolve();
let mediaFilters: MediaFilterSettings = { ...DEFAULT_MEDIA_FILTERS, excludedFileTypes: [...DEFAULT_MEDIA_FILTERS.excludedFileTypes] };
let mediaFiltersReady: Promise<void> = Promise.resolve();

type PendingBrowserFallback = { source: string; name?: string; at: number };
const pendingBrowserFallbacks: PendingBrowserFallback[] = [];
const pendingBrowserOwnedDownloads: PendingBrowserFallback[] = [];
const BROWSER_FALLBACK_TTL_MS = 30_000;
const BROWSER_OWNED_DOWNLOAD_TTL_MS = 10_000;
const BROWSER_FALLBACK_MAX = 20;
const BROWSER_OWNED_DOWNLOAD_MAX = 20;

function pruneBrowserFallbacks(now = Date.now()): void {
  while (pendingBrowserFallbacks.length && now - pendingBrowserFallbacks[0].at > BROWSER_FALLBACK_TTL_MS) pendingBrowserFallbacks.shift();
  while (pendingBrowserFallbacks.length > BROWSER_FALLBACK_MAX) pendingBrowserFallbacks.shift();
}

function rememberBrowserFallback(source: string, name?: string): void {
  pruneBrowserFallbacks();
  pendingBrowserFallbacks.push({ source, name, at: Date.now() });
}

function consumeBrowserFallback(item: chrome.downloads.DownloadItem): boolean {
  pruneBrowserFallbacks();
  const name = cleanFilename(item.filename);
  const index = pendingBrowserFallbacks.findIndex((pending) =>
    (pending.source === item.url || pending.source === item.finalUrl) &&
    (!pending.name || !name || pending.name === name),
  );
  if (index < 0) return false;
  pendingBrowserFallbacks.splice(index, 1);
  return true;
}

function rememberBrowserOwnedDownload(source: string, name?: string): void {
  const now = Date.now();
  while (pendingBrowserOwnedDownloads.length && now - pendingBrowserOwnedDownloads[0].at > BROWSER_OWNED_DOWNLOAD_TTL_MS) pendingBrowserOwnedDownloads.shift();
  while (pendingBrowserOwnedDownloads.length >= BROWSER_OWNED_DOWNLOAD_MAX) pendingBrowserOwnedDownloads.shift();
  pendingBrowserOwnedDownloads.push({ source, name, at: now });
}

function consumeBrowserOwnedDownload(item: chrome.downloads.DownloadItem): boolean {
  const now = Date.now();
  while (pendingBrowserOwnedDownloads.length && now - pendingBrowserOwnedDownloads[0].at > BROWSER_OWNED_DOWNLOAD_TTL_MS) pendingBrowserOwnedDownloads.shift();
  const name = cleanFilename(item.filename);
  const index = pendingBrowserOwnedDownloads.findIndex((pending) =>
    (pending.source === item.url || pending.source === item.finalUrl) &&
    (!pending.name || !name || pending.name === name),
  );
  if (index < 0) return false;
  pendingBrowserOwnedDownloads.splice(index, 1);
  return true;
}

async function loadPolicy(): Promise<void> {
  try {
    const stored = await chrome.storage.local.get(POLICY_KEY);
    const saved = stored[POLICY_KEY] as Partial<BrowserPolicy> | undefined;
    if (saved) {
      policy = {
        interceptDownloads: saved.interceptDownloads ?? DEFAULT_POLICY.interceptDownloads,
        showMediaButtons: saved.showMediaButtons ?? DEFAULT_POLICY.showMediaButtons,
        excludedSites: Array.isArray(saved.excludedSites) ? saved.excludedSites : [],
      };
    }
    policyLoadError = '';
  } catch (reason) {
    policy = { ...DEFAULT_POLICY };
    policyLoadError = reason instanceof Error && reason.message ? reason.message : 'Could not load browser integration settings.';
  }
}

async function savePolicy(next: BrowserPolicy = policy): Promise<void> {
  await chrome.storage.local.set({ [POLICY_KEY]: next });
}

async function loadMediaFilters(): Promise<void> {
  try {
    const stored = await chrome.storage.local.get(MEDIA_FILTERS_KEY);
    mediaFilters = normalizeMediaFilterSettings(stored[MEDIA_FILTERS_KEY]);
  } catch {
    mediaFilters = { ...DEFAULT_MEDIA_FILTERS, excludedFileTypes: [...DEFAULT_MEDIA_FILTERS.excludedFileTypes] };
  }
}

async function saveMediaFilters(next: MediaFilterSettings = mediaFilters): Promise<void> {
  await chrome.storage.local.set({ [MEDIA_FILTERS_KEY]: next });
}

function adoptResidentPolicy(response: unknown): boolean {
  const remote = (response as { policy?: Partial<BrowserPolicy> } | undefined)?.policy;
  if (!remote || typeof remote.interceptDownloads !== 'boolean') return false;
  const next = {
    interceptDownloads: remote.interceptDownloads,
    showMediaButtons: remote.showMediaButtons ?? policy.showMediaButtons,
    excludedSites: Array.isArray(remote.excludedSites)
      ? remote.excludedSites.filter((site): site is string => typeof site === 'string')
      : policy.excludedSites,
  };
  const changed = next.interceptDownloads !== policy.interceptDownloads
    || next.showMediaButtons !== policy.showMediaButtons
    || next.excludedSites.length !== policy.excludedSites.length
    || next.excludedSites.some((site, index) => site !== policy.excludedSites[index]);
  policy = next;
  return changed;
}

async function syncPolicyFromResident(): Promise<void> {
  const response = await sendApp({ type: 'get-policy' });
  if (adoptResidentPolicy(response)) await savePolicy();
}

let residentPolicySync: Promise<void> | null = null;
let residentPolicySyncedAt = 0;

async function refreshResidentPolicy(): Promise<void> {
  if (Date.now() - residentPolicySyncedAt < 1000) return;
  if (!residentPolicySync) {
    residentPolicySync = syncPolicyFromResident()
      .then(() => { residentPolicySyncedAt = Date.now(); })
      .finally(() => { residentPolicySync = null; });
  }
  await residentPolicySync;
}

async function sendApp(message: unknown): Promise<unknown> {
  const type = (message as { type?: string })?.type;
  const route = type === 'get-policy' ? '/v1/policy' : type === 'open-manager' ? '/v1/manager' : type === 'update-policy' ? '/v1/policy' : '/v1/capture';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), APP_BRIDGE_TIMEOUT_MS);
  try {
    const response = await fetch(`${APP_BRIDGE_ORIGIN}${route}`, {
      method: type === 'get-policy' ? 'GET' : 'POST',
      headers: type === 'get-policy' ? undefined : { 'Content-Type': 'application/json' },
      body: type === 'get-policy' ? undefined : JSON.stringify(message),
      signal: controller.signal,
    });
    const payload = await response.json() as unknown;
    if (!response.ok && typeof payload === 'object' && payload !== null && 'error' in payload) return payload;
    return payload;
  } catch {
    return { ok: false, error: 'Download Manager is not running' };
  } finally {
    clearTimeout(timer);
  }
}

function pruneMedia(now = Date.now()): void {
  while (recentMedia.length && now - recentMedia[0].at > MEDIA_BUFFER_MS) recentMedia.shift();
  while (recentMedia.length > MEDIA_BUFFER_MAX) recentMedia.shift();
}

function prunePlayers(now = Date.now()): void {
  while (recentPlayers.length && now - recentPlayers[0].at > PLAYER_BUFFER_MS) recentPlayers.shift();
  while (recentPlayers.length > PLAYER_BUFFER_MAX) recentPlayers.shift();
}

function cleanUserAgent(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const candidate = value.trim();
  if (!candidate || candidate.length > 512 || /[\r\n]/.test(candidate)) return undefined;
  return candidate;
}

const MEDIA_EVIDENCE_URL_MAX = 4096;
const MEDIA_EVIDENCE_ID_MAX = 256;

function cleanMediaUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || value.length === 0 || value.length > MEDIA_EVIDENCE_URL_MAX) return undefined;
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return undefined;
    parsed.hash = '';
    return parsed.href;
  } catch {
    return undefined;
  }
}

function cleanMediaEvidence(value: unknown, expectedKind?: Exclude<MediaKind, 'unknown'>, pageUrl?: string): MediaEvidence | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const item = value as Partial<MediaEvidence>;
  if (typeof item.currentSrc !== 'string' || item.currentSrc.length === 0 || item.currentSrc.length > MEDIA_EVIDENCE_URL_MAX) return undefined;
  if (typeof item.sourceIdentity !== 'string' || item.sourceIdentity.length === 0 || item.sourceIdentity.length > MEDIA_EVIDENCE_ID_MAX) return undefined;
  let currentSrc: string | undefined;
  if (item.currentSrc.startsWith('blob:')) {
    try {
      const parsed = new URL(item.currentSrc);
      const pageOrigin = pageUrl ? new URL(pageUrl).origin : '';
      if (parsed.protocol !== 'blob:' || (pageOrigin && pageOrigin !== 'null' && parsed.origin !== pageOrigin)) return undefined;
      currentSrc = parsed.href;
    } catch {
      return undefined;
    }
  } else {
    currentSrc = cleanMediaUrl(item.currentSrc);
  }
  const source = item.source === undefined ? undefined : cleanMediaUrl(item.source);
  const playerKind = item.playerKind === 'audio' || item.playerKind === 'video' ? item.playerKind : undefined;
  const companionAudio = item.companionAudio === undefined ? undefined : cleanMediaUrl(item.companionAudio);
  if (!currentSrc || (expectedKind && playerKind && expectedKind !== playerKind) || (source !== undefined && !item.currentSrc.startsWith('blob:') && source !== currentSrc) || (item.source !== undefined && !source) || (item.companionAudio !== undefined && !companionAudio) || (companionAudio !== undefined && (expectedKind ?? playerKind) !== 'video')) return undefined;
  const selectedValues = Array.isArray(item.selectedSegments) ? item.selectedSegments : [];
  const selectedSegments = selectedValues
      .map((candidate) => cleanMediaUrl(candidate))
      .filter((candidate): candidate is string => !!candidate)
      .filter((candidate) => {
        const role = roleFor(candidate);
        return role === 'manifest' || (role === 'unknown' && isLikelyRepresentation(candidate));
      })
      .slice(0, 8);
  if (selectedValues.length > 8 || selectedSegments.length !== selectedValues.length) return undefined;
  return { currentSrc, sourceIdentity: item.sourceIdentity, ...(source ? { source } : {}), ...(playerKind ? { playerKind } : {}), ...(companionAudio ? { companionAudio } : {}), selectedSegments };
}

function observedKind(url: string, tabId: number, frameId: number, documentId?: string): MediaKind {
  const candidate = [...recentMedia]
    .filter((item) => item.url === url && item.tabId === tabId && item.frameId === frameId && item.documentId === documentId)
    .sort((left, right) => right.at - left.at)[0];
  return candidate ? (candidate.kind && candidate.kind !== 'unknown' ? candidate.kind : mediaKindFor(url, candidate.contentType)) : mediaKindFor(url);
}

function rememberEvidence(evidence: MediaEvidence, tabId: number, frameId: number, documentId: string | undefined, playerKey: string | undefined, expectedKind?: Exclude<MediaKind, 'unknown'>): void {
  const sourceKind = evidence.playerKind ?? expectedKind ?? (evidence.source ? mediaKindFor(evidence.source) : 'unknown');
  if (evidence.source) rememberMedia(evidence.source, tabId, frameId, roleFor(evidence.source), documentId, playerKey, sourceKind);
  if (evidence.companionAudio) rememberMedia(evidence.companionAudio, tabId, frameId, roleFor(evidence.companionAudio), documentId, playerKey, 'audio');
  for (const hint of evidence.selectedSegments) rememberMedia(hint, tabId, frameId, roleFor(hint), documentId, playerKey, mediaKindFor(hint));
}

function selectionFromEvidence(evidence: MediaEvidence | undefined, expectedKind?: Exclude<MediaKind, 'unknown'>): { source: string; selectedSegments: string[]; companionAudio?: string } | undefined {
  if (!evidence?.source || !isHttp(evidence.source)) return undefined;
  const sourceKind = mediaKindFor(evidence.source);
  if (expectedKind && sourceKind !== 'unknown' && sourceKind !== expectedKind) return undefined;
  const selection: { source: string; selectedSegments: string[]; companionAudio?: string } = {
    source: roleFor(evidence.source) === 'unknown' ? normalizeChunkUrl(evidence.source) : evidence.source,
    selectedSegments: evidence.selectedSegments.slice(0, 8),
  };
  if (expectedKind === 'video' && evidence.companionAudio && roleFor(evidence.source) === 'unknown') selection.companionAudio = normalizeChunkUrl(evidence.companionAudio);
  return selection;
}

function rememberPlayer(payload: Record<string, unknown>, tabId: number, frameId: number, documentId?: string): void {
  const playerKey = typeof payload.playerKey === 'string' ? payload.playerKey.trim() : '';
  if (!playerKey) return;
  const now = Date.now();
  prunePlayers(now);
  const evidence: MediaPlayerEvidence = {
    playerKey,
    tabId,
    frameId,
    at: now,
    documentId,
    active: payload.active === true,
    hovered: payload.hovered === true,
    playing: payload.playing === true,
    visible: payload.visible === true,
  };
  const existing = recentPlayers.find((item) => item.playerKey === playerKey && item.tabId === tabId && item.frameId === frameId && item.documentId === documentId);
  if (existing) Object.assign(existing, evidence);
  else recentPlayers.push(evidence);
}

function cleanFilename(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const leaf = value.trim().split('/').pop()?.split('\\').pop()?.trim();
  return leaf || undefined;
}

function ordinaryCaptureError(source: string, pageUrl: string): string | undefined {
  const pageSite = siteOf(pageUrl);
  if (!policy.interceptDownloads) return 'ordinary interception disabled';
  if (pageSite && policy.excludedSites.includes(pageSite)) return 'site excluded';
  if (!isHttp(source)) return 'invalid source';
  return undefined;
}

function mediaCapturePolicyError(pageUrl: string): string | undefined {
  if (!policy.showMediaButtons) return 'media buttons disabled';
  const pageSite = siteOf(pageUrl);
  if (pageSite && policy.excludedSites.includes(pageSite)) return 'site excluded';
  return undefined;
}

async function captureOrdinary(payload: Record<string, unknown>): Promise<{ ok: boolean; error?: string }> {
  await policyReady;
  const source = typeof payload.source === 'string' ? payload.source.trim() : '';
  const pageUrl = typeof payload.pageUrl === 'string' ? payload.pageUrl : '';
  const captureError = ordinaryCaptureError(source, pageUrl);
  if (captureError) {
    return { ok: false, error: captureError };
  }
  const response = (await sendApp({
    type: 'capture-acquisition',
    payload: {
      source,
      name: cleanFilename(payload.name),
      pageUrl: typeof payload.pageUrl === 'string' ? payload.pageUrl : undefined,
      referrer: typeof payload.pageUrl === 'string' ? payload.pageUrl : undefined,
      userAgent: cleanUserAgent(payload.userAgent),
    },
  })) as { ok?: boolean };
  if (response?.ok) return { ok: true };
  // The pre-browser path consumed the anchor event. If the resident app is
  // unavailable, preserve the user's download through the extension API;
  // the onCreated listener ignores downloads started by this extension.
  try {
    rememberBrowserFallback(source, cleanFilename(payload.name));
    const id = await chrome.downloads.download({
      url: source,
      filename: cleanFilename(payload.name),
      saveAs: false,
    });
    return { ok: typeof id === 'number' };
  } catch {
    // No safe fallback remains. Do not navigate the page or invent success.
    return { ok: false, error: 'Download Manager and browser fallback are unavailable' };
  }
}

function responseHeader(details: chrome.webRequest.OnHeadersReceivedDetails, name: string): string {
  return details.responseHeaders?.find((header) => header.name.toLowerCase() === name)?.value?.trim() ?? '';
}

function wholeMediaResponse(details: chrome.webRequest.OnHeadersReceivedDetails, role: ReturnType<typeof roleFor>, kind: MediaKind, contentType: string): boolean {
  if (role !== 'unknown') return false;
  if (/^(?:text\/|application\/(?:json|javascript|xml|x-javascript))/i.test(contentType)) return false;
  if (kind !== 'unknown' || /^(?:audio|video|image)\//i.test(contentType)) return true;
  return !!mediaFileTypeFor(details.url, contentType) && /\.(?:aac|flac|gif|m4a|m4v|mov|mp3|mp4|oga|ogg|ogv|opus|wav|webm|webp)(?:[?#]|$)/i.test(details.url);
}

function totalBytesForResponse(details: chrome.webRequest.OnHeadersReceivedDetails, role: ReturnType<typeof roleFor>, kind: MediaKind, contentType: string): number | undefined {
  if (!wholeMediaResponse(details, role, kind, contentType)) return undefined;
  const contentRange = responseHeader(details, 'content-range');
  const rangeTotal = contentRange.match(/^bytes\s+\d+-\d+\/(\d+)$/i)?.[1];
  if (rangeTotal) {
    const total = Number(rangeTotal);
    if (Number.isSafeInteger(total) && total > 0) return total;
  }
  if (details.statusCode !== undefined && details.statusCode !== 200) return undefined;
  const contentLength = Number(responseHeader(details, 'content-length'));
  return Number.isSafeInteger(contentLength) && contentLength > 0 ? contentLength : undefined;
}

function rememberMedia(url: string, tabId: number, frameId: number, role = roleFor(url), documentId?: string, playerKey?: string, kind: MediaKind = mediaKindFor(url), contentType = '', totalBytes?: number): void {
  if (!isHttp(url)) return;
  pruneMedia();
  const existing = recentMedia.find((item) => item.url === url && item.tabId === tabId && item.frameId === frameId && item.documentId === documentId);
  if (existing) {
    if (role === 'manifest' || existing.role === 'unknown') existing.role = role;
    if (playerKey && !existing.playerKey) existing.playerKey = playerKey;
    if (kind !== 'unknown' || !existing.kind) existing.kind = kind;
    if (contentType && !existing.contentType) existing.contentType = contentType;
    if (totalBytes !== undefined) existing.totalBytes = totalBytes;
    existing.at = Date.now();
    return;
  }
  recentMedia.push({ url, tabId, frameId, at: Date.now(), role, kind, contentType: contentType || undefined, ...(totalBytes === undefined ? {} : { totalBytes }), documentId, playerKey });
}

function mediaCandidateForSource(source: string, tabId?: number, frameId = 0, documentId?: string): MediaCandidate | undefined {
  const normalized = normalizeChunkUrl(source);
  return [...recentMedia]
    .filter((item) =>
      item.url === source || normalizeChunkUrl(item.url) === normalized,
    )
    .filter((item) => tabId === undefined || (item.tabId === tabId && (item.frameId === frameId || item.frameId === 0) && (!documentId || item.documentId === documentId)))
    .sort((left, right) => right.at - left.at)[0];
}

type MediaFilterDecision = { allowed: boolean; type?: string; totalBytes?: number; reason?: 'excluded-type' | 'below-minimum' };

function mediaFilterDecision(source: string, tabId?: number, frameId = 0, documentId?: string, contentType = '', companionAudio?: string): MediaFilterDecision {
  const candidate = mediaCandidateForSource(source, tabId, frameId, documentId);
  const observedContentType = candidate?.contentType || contentType;
  const role = candidate?.role ?? roleFor(source, observedContentType);
  const type = mediaFileTypeFor(source, observedContentType);
  if (type && mediaFilters.excludedFileTypes.includes(type)) return { allowed: false, type, reason: 'excluded-type', ...(candidate?.totalBytes === undefined ? {} : { totalBytes: candidate.totalBytes }) };
  if (role === 'manifest') return { allowed: true };
  if (role === 'segment') return { allowed: true, ...(type ? { type } : {}) };
  const totalBytes = candidate?.totalBytes;
  if (mediaFilters.minimumSizeBytes > 0 && totalBytes !== undefined) {
    const companionTotal = companionAudio === undefined
      ? undefined
      : mediaCandidateForSource(companionAudio, tabId, frameId, documentId)?.totalBytes;
    const aggregate = companionTotal === undefined ? undefined : totalBytes + companionTotal;
    if ((aggregate === undefined && companionAudio === undefined && totalBytes < mediaFilters.minimumSizeBytes) || (aggregate !== undefined && aggregate < mediaFilters.minimumSizeBytes)) {
      return { allowed: false, ...(type ? { type } : {}), totalBytes, reason: 'below-minimum' };
    }
  }
  return { allowed: true, ...(type ? { type } : {}), ...(totalBytes === undefined ? {} : { totalBytes }) };
}

// Observe (never block) response traffic that feeds media elements.
chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const type = details.type;
    if (type !== 'media' && type !== 'xmlhttprequest' && type !== 'other') return;
    const role = roleFor(details.url);
    const kind = mediaKindFor(details.url);
    if (type !== 'media' && !isMediaCandidate({ url: details.url, role, kind })) return;
    rememberMedia(details.url, details.tabId, details.frameId, role, details.documentId, undefined, kind);
  },
  { urls: ['<all_urls>'] },
);

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0) return undefined;
    const type = details.type;
    if (type !== 'media' && type !== 'xmlhttprequest' && type !== 'other') return undefined;
    const contentType = responseHeader(details, 'content-type');
    const role = roleFor(details.url, contentType);
    const kind = mediaKindFor(details.url, contentType);
    if (type !== 'media' && !isMediaCandidate({ url: details.url, role, kind }, contentType)) return undefined;
    rememberMedia(details.url, details.tabId, details.frameId, role, details.documentId, undefined, kind, contentType, totalBytesForResponse(details, role, kind, contentType));
    return undefined;
  },
  { urls: ['<all_urls>'] },
  ['responseHeaders'],
);

// Bounded observation of reproducible URL-encoded POST bodies.
const FORM_BODY_MAX = 64 * 1024;
const FORM_BODY_TTL_MS = 60_000;
const FORM_BODY_PER_URL = 4;
const recentFormBodies: Array<{ url: string; body: string; at: number; tabId: number; frameId: number }> = [];

function pruneFormBodies(now = Date.now()): void {
  while (recentFormBodies.length && now - recentFormBodies[0].at > FORM_BODY_TTL_MS) recentFormBodies.shift();
  while (recentFormBodies.length > 64) recentFormBodies.shift();
}

function formBodyFromDetails(details: chrome.webRequest.OnBeforeRequestDetails): string | undefined {
  const formData = details.requestBody?.formData;
  if (!formData) return undefined;
  const params = new URLSearchParams();
  for (const [key, values] of Object.entries(formData)) {
    for (const value of values) {
      if (typeof value !== 'string') return undefined;
      params.append(key, value);
    }
  }
  const text = params.toString();
  return text.length > 0 && text.length <= FORM_BODY_MAX ? text : undefined;
}

chrome.webRequest.onBeforeRequest.addListener(
  (details): undefined => {
    if (details.tabId < 0 || details.method !== 'POST') return undefined;
    const body = formBodyFromDetails(details);
    if (body === undefined) return undefined;
    pruneFormBodies();
    const url = details.url.split('#')[0];
    const queued = recentFormBodies.filter((item) => item.url === url);
    if (queued.length >= FORM_BODY_PER_URL) {
      const oldest = recentFormBodies.findIndex((item) => item.url === url);
      if (oldest >= 0) recentFormBodies.splice(oldest, 1);
    }
    recentFormBodies.push({ url, body, at: Date.now(), tabId: details.tabId, frameId: details.frameId });
    return undefined;
  },
  { urls: ['<all_urls>'] },
  ['requestBody'],
);

function takeFormBody(url: string, tabId?: number): string | undefined {
  pruneFormBodies();
  const now = Date.now();
  let index = tabId !== undefined && tabId >= 0
    ? recentFormBodies.findIndex((item) => item.url === url && item.tabId === tabId)
    : -1;
  if (index < 0) index = recentFormBodies.findIndex((item) => item.url === url);
  if (index < 0) return undefined;
  const [found] = recentFormBodies.splice(index, 1);
  return now - found.at <= FORM_BODY_TTL_MS ? found.body : undefined;
}

// Downloads without an interceptable page anchor are handed over
// transactionally: pause Chromium, create the native provisional job, then
// cancel Chromium. Any failed step resumes the browser and rolls back the
// native provisional job so exactly one owner remains.
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  const source = item.finalUrl || item.url || '';
  if (consumeBrowserFallback(item) || consumeBrowserOwnedDownload(item) || item.byExtensionId === chrome.runtime.id || !isHttp(source)) {
    suggest();
    return;
  }
  void (async () => {
    let paused = false;
    let nativeId: string | undefined;
    try {
      await policyReady;
      if (ordinaryCaptureError(source, item.referrer ?? '')) {
        return;
      }
      try {
        await chrome.downloads.pause(item.id);
        paused = true;
      } catch {
        return;
      }
      // The Downloads API exposes no tab/frame identifier, so do not guess a
      // User-Agent from another document on this fallback path.
      // A matching observed POST is replayed once; otherwise acquisition uses GET.
      const postBody = takeFormBody(source);
      const response = await sendApp({
        type: 'capture-acquisition',
        payload: {
          source,
          name: cleanFilename(item.filename),
          pageUrl: item.referrer,
          referrer: item.referrer,
          ...(postBody === undefined ? {} : { method: 'POST', postBody }),
        },
      }) as { ok?: boolean; id?: string };
      if (!response?.ok || typeof response.id !== 'string') {
        await chrome.downloads.resume(item.id).catch(() => undefined);
        paused = false;
        return;
      }
      nativeId = response.id;
      try {
        await chrome.downloads.cancel(item.id);
        paused = false;
      } catch {
        await sendApp({ type: 'cancel-acquisition', payload: { id: nativeId } });
        nativeId = undefined;
        await chrome.downloads.resume(item.id).catch(() => undefined);
        paused = false;
      }
    } finally {
      if (paused) await chrome.downloads.resume(item.id).catch(() => undefined);
      suggest();
    }
  })();
  return true;
});

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  void (async () => {
    const type = (message as { type?: string })?.type;
    if (type === 'get-policy') {
      await Promise.all([policyReady, mediaFiltersReady]);
      try {
        await refreshResidentPolicy();
      } catch {
        // Keep the last browser-owned policy when the resident app is stopped.
      }
      const includeMediaFilters = (message as { includeMediaFilters?: boolean })?.includeMediaFilters === true;
      const extra = includeMediaFilters ? { mediaFilters } : {};
      reply(policyLoadError ? { ok: false, error: policyLoadError, policy, ...extra } : { ok: true, policy, ...extra });
    } else if (type === 'get-media-filters') {
      await mediaFiltersReady;
      reply({ ok: true, mediaFilters });
    } else if (type === 'update-media-filters') {
      await mediaFiltersReady;
      const previous = mediaFilters;
      const requested = (message as { filters?: unknown; patch?: unknown }).filters ?? (message as { patch?: unknown }).patch;
      const next = requested && typeof requested === 'object'
        ? normalizeMediaFilterSettings({ ...mediaFilters, ...(requested as Record<string, unknown>) })
        : mediaFilters;
      try {
        await saveMediaFilters(next);
      } catch (reason) {
        reply({ ok: false, error: reason instanceof Error && reason.message ? reason.message : 'Could not save media filter settings.', mediaFilters: previous });
        return;
      }
      mediaFilters = next;
      reply({ ok: true, mediaFilters });
    } else if (type === 'check-media-filters') {
      await mediaFiltersReady;
      const payload = (message as { payload?: Record<string, unknown> }).payload ?? {};
      const source = typeof payload.source === 'string' ? cleanMediaUrl(payload.source) : undefined;
      if (!source) {
        reply({ ok: true, allowed: true });
        return;
      }
      const tabId = sender.tab?.id;
      const companionAudio = typeof payload.companionAudio === 'string' ? cleanMediaUrl(payload.companionAudio) : undefined;
      const result = mediaFilterDecision(source, tabId, sender.frameId ?? 0, sender.documentId, typeof payload.contentType === 'string' ? payload.contentType : '', companionAudio);
      reply({ ok: true, ...result });
    } else if (type === 'update-policy') {
      const patch = (message as { patch?: Partial<BrowserPolicy> }).patch ?? {};
      const previous = policy;
      const next: BrowserPolicy = { ...policy, excludedSites: [...policy.excludedSites] };
      if (typeof patch.interceptDownloads === 'boolean') next.interceptDownloads = patch.interceptDownloads;
      if (typeof patch.showMediaButtons === 'boolean') next.showMediaButtons = patch.showMediaButtons;
      if (Array.isArray(patch.excludedSites)) {
        next.excludedSites = patch.excludedSites.filter((site): site is string => typeof site === 'string');
      }
      try {
        await savePolicy(next);
      } catch (reason) {
        reply({ ok: false, error: reason instanceof Error && reason.message ? reason.message : 'Could not save browser integration settings.', policy: previous });
        return;
      }
      policy = next;
      policyLoadError = '';
      await sendApp({ type: 'update-policy', payload: policy });
      residentPolicySyncedAt = Date.now();
      reply({ ok: true, policy });
    } else if (type === 'ordinary-capture') {
      const payload = (message as { payload?: Record<string, unknown> }).payload ?? {};
      reply(await captureOrdinary(payload));
    } else if (type === 'browser-owned-download') {
      const payload = (message as { payload?: Record<string, unknown> }).payload ?? {};
      const source = typeof payload.source === 'string' ? payload.source.trim() : '';
      if (isHttp(source)) rememberBrowserOwnedDownload(source, cleanFilename(payload.name));
      reply({ ok: isHttp(source) });
    } else if (type === 'media-player-state') {
      const tabId = sender.tab?.id;
      if (tabId !== undefined) rememberPlayer((message as { payload?: Record<string, unknown> }).payload ?? {}, tabId, sender.frameId ?? 0, sender.documentId);
      reply({ ok: true });
    } else if (type === 'media-capture') {
      await Promise.all([policyReady, mediaFiltersReady]);
      const payload = (message as { payload?: Record<string, unknown> }).payload ?? {};
      const pageUrl = typeof payload.pageUrl === 'string' ? payload.pageUrl : undefined;
      const policyError = mediaCapturePolicyError(pageUrl ?? '');
      if (policyError) {
        reply({ ok: false, error: policyError });
        return;
      }
      const documentId = sender.documentId;
      const expectedKind = payload.playerKind === 'audio' || payload.playerKind === 'video' ? payload.playerKind : undefined;
      const playerKey = typeof payload.playerKey === 'string' && payload.playerKey.length <= 128 ? payload.playerKey : undefined;
      const evidence = cleanMediaEvidence(payload.pageEvidence, expectedKind, pageUrl);
      const sourceIdentity = evidence?.sourceIdentity ?? (typeof payload.mediaIdentity === 'string' && payload.mediaIdentity.length <= MEDIA_EVIDENCE_ID_MAX ? payload.mediaIdentity : undefined);
      const scope = sender.tab?.id === undefined ? undefined : mediaScope(sender.tab.id, sender.frameId ?? 0, documentId, playerKey, sourceIdentity);
      if (sender.tab?.id !== undefined) invalidateOtherResolvedMedia(sender.tab.id, sender.frameId ?? 0, documentId, playerKey, sourceIdentity);
      let source = typeof payload.source === 'string' ? cleanMediaUrl(payload.source) ?? '' : '';
      let selectedSegments: string[] = [];
      let companionAudio: string | undefined;
      if (sender.tab?.id !== undefined && evidence) {
        rememberEvidence(evidence, sender.tab.id, sender.frameId ?? 0, documentId, playerKey, expectedKind);
        const exact = selectionFromEvidence(evidence, expectedKind);
        if (exact) {
          source = exact.source;
          selectedSegments = exact.selectedSegments;
          companionAudio = exact.companionAudio;
        }
      }
      const directKind = isHttp(source) && sender.tab?.id !== undefined
        ? observedKind(source, sender.tab.id, sender.frameId ?? 0, documentId)
        : mediaKindFor(source);
      if (expectedKind && directKind !== 'unknown' && directKind !== expectedKind && roleFor(source) !== 'manifest') source = '';
      if (isHttp(source) && roleFor(source) === 'unknown') source = normalizeChunkUrl(source);
      if (!isHttp(source) && sender.tab?.id !== undefined) {
        const selection = chooseWorkerMediaSelection(recentMedia, recentPlayers, sender.tab.id, sender.frameId ?? 0, playerKey, documentId, Date.now(), expectedKind);
        source = selection?.source ?? '';
        selectedSegments = selection?.selectedSegments ?? [];
        companionAudio = selection?.companionAudio;
      }
      if (!isHttp(source) && scope !== undefined) {
        const remembered = takeResolvedMedia(scope);
        if (remembered !== undefined) {
          source = remembered.source;
          selectedSegments = [...remembered.selectedSegments];
          companionAudio = remembered.companionAudio;
        }
      }
      if (isHttp(source) && scope !== undefined) {
        rememberResolvedMedia(scope, source, selectedSegments, companionAudio);
      }
      if (!isHttp(source)) {
        reply({ ok: false, error: 'no exact media evidence for this player' });
        return;
      }
      const filterResult = mediaFilterDecision(source, sender.tab?.id, sender.frameId ?? 0, documentId, '', companionAudio);
      if (!filterResult.allowed) {
        reply({ ok: false, error: filterResult.reason === 'excluded-type' ? `media type ${filterResult.type ?? 'unknown'} is excluded` : 'media is below the minimum size' });
        return;
      }
      const userAgent = cleanUserAgent(payload.userAgent);
      const outboundPayload: Record<string, unknown> = {
        source,
        selectedSegments,
        pageUrl,
        referrer: pageUrl,
        userAgent,
        media: true,
        playerKind: expectedKind,
        ...(playerKey ? { playerKey } : {}),
        ...(cleanFilename(payload.name) ? { name: cleanFilename(payload.name) } : {}),
        ...(companionAudio ? { companionAudio } : {}),
      };
      reply(await sendApp({ type: 'media-capture', payload: outboundPayload }));
    } else if (type === 'open-manager') {
      reply(await sendApp({ type: 'open-manager' }));
    } else {
      reply({ ok: false, error: 'unsupported message' });
    }
  })();
  return true;
});

policyReady = loadPolicy();
mediaFiltersReady = loadMediaFilters();
void policyReady
  .then(() => refreshResidentPolicy())
  .catch(() => undefined);
void hydrateResolvedMedia();
