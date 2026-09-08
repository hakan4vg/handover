export type MediaRole = 'unknown' | 'manifest' | 'segment';
export type MediaKind = 'unknown' | 'audio' | 'video';

export interface MediaCandidate {
  url: string;
  tabId: number;
  frameId: number;
  at: number;
  role: MediaRole;
  kind?: MediaKind;
  documentId?: string;
  // one player is the active/hovered/playing target. It is deliberately
  // optional: a manifest can load before the page reports player state.
  playerKey?: string;
}

export interface MediaPlayerEvidence {
  playerKey: string;
  tabId: number;
  frameId: number;
  documentId?: string;
  at: number;
  active: boolean;
  hovered: boolean;
  playing: boolean;
  visible: boolean;
}

export interface MediaSelection {
  source: string;
  selectedSegments: string[];
}

export function roleFor(url: string, contentType = ''): MediaRole {
  const hint = `${contentType} ${url}`.toLowerCase();
  if (hint.includes('mpegurl') || hint.includes('dash+xml') || /\.(?:m3u8|mpd)(?:[?#]|$)/i.test(url)) return 'manifest';
  if (hint.includes('video/mp2t') || hint.includes('iso.segment') || /(?:^|[./_-])(?:m4[asv]|ts|cmf[av])(?:[?#]|$)/i.test(url)) return 'segment';
  return 'unknown';
}

// Subtitle/caption playlists are manifests too, but they are not the media the
// user is watching. Providers commonly name them subtitles/captions, so the
// generic selection prefers a media manifest over a subtitle one and falls back
// to subtitles only when nothing else exists.
export function isSubtitlePlaylist(url: string): boolean {
  try {
    const path = new URL(url).pathname.toLowerCase();
    return /(^|[/_-])(subtitles?|captions?)([/_.-]|$)/.test(path) || /\.(?:vtt|ttml)(?:[?#]|$)/i.test(url);
  } catch {
    return false;
  }
}

function isLikelyMasterManifest(url: string): boolean {
  try {
    const path = new URL(url).pathname.toLowerCase();
    const leaf = path.split('/').pop() ?? '';
    return /(?:^|[-_.])(master|playlist|manifest|multivariant)(?:[-_.]|$)/.test(leaf);
  } catch {
    return false;
  }
}

export function mediaKindFor(url: string, contentType = ''): MediaKind {
  const mime = contentType.toLowerCase().split(';', 1)[0].trim();
  if (mime.startsWith('audio/')) return 'audio';
  if (mime.startsWith('video/')) return 'video';
  try {
    const path = new URL(url).pathname.toLowerCase();
    if (/(^|[/_-])audio([/_.-]|$)/.test(path) || /\.(?:m4a|aac|mp3|oga|ogg|opus|flac)(?:$|[?#])/.test(path)) return 'audio';
    if (/(^|[/_-])video([/_.-]|$)/.test(path) || /\.(?:webm|m4v|ogv)(?:$|[?#])/.test(path)) return 'video';
  } catch {
    // Keep extensionless or malformed URLs untyped. The response MIME type,
    // when Chromium exposes it, is the authoritative generic classification.
  }
  return 'unknown';
}

export function isLikelyRepresentation(url: string): boolean {
  try {
    return /\.(?:mp4|webm|m4a|m4v|mov|ogv)(?:$|[?#])/i.test(new URL(url).pathname);
  } catch {
    return false;
  }
}

export function isMediaCandidate(candidate: Pick<MediaCandidate, 'url' | 'role' | 'kind'>): boolean {
  return candidate.role !== 'unknown'
    || (candidate.kind !== undefined && candidate.kind !== 'unknown')
    || isLikelyRepresentation(candidate.url);
}

function activeRepresentationHints(candidates: MediaCandidate[]): string[] {
  const representations = candidates.filter((item) => item.role === 'unknown' && isLikelyRepresentation(item.url));
  if (!representations.length) return [];
  const newestByKind = new Map<Exclude<MediaKind, 'unknown'>, MediaCandidate>();
  for (const item of representations) {
    const kind = item.kind && item.kind !== 'unknown' ? item.kind : mediaKindFor(item.url);
    if (kind !== 'unknown' && !newestByKind.has(kind)) newestByKind.set(kind, item);
  }
  // If Chromium did not expose a MIME type and the URL is extensionless, keep
  // one bounded hint instead of allowing every stale representation to reach
  // native parsing. A typed response always wins over this fallback.
  if (!newestByKind.size) return representations.slice(0, 1).map((item) => item.url);
  return [...newestByKind.values()]
    .sort((left, right) => right.at - left.at)
    .map((item) => item.url);
}

export function choosePlayerEvidence(players: MediaPlayerEvidence[], tabId: number, frameId: number, now = Date.now(), documentId?: string): MediaPlayerEvidence | undefined {
  const fresh = players.filter((item) => item.tabId === tabId && (item.frameId === frameId || item.frameId === 0) && (!documentId || item.documentId === documentId) && now - item.at <= 15_000);
  return [...fresh].sort((left, right) => {
    const rank = (item: MediaPlayerEvidence) => Number(item.active) * 8 + Number(item.hovered) * 4 + Number(item.playing) * 2 + Number(item.visible);
    return rank(right) - rank(left) || right.at - left.at;
  })[0];
}

function matchesKind(candidate: MediaCandidate, expectedKind?: Exclude<MediaKind, 'unknown'>): boolean {
  if (!expectedKind) return true;
  const kind = candidate.kind && candidate.kind !== 'unknown' ? candidate.kind : mediaKindFor(candidate.url);
  return kind === 'unknown' || kind === expectedKind;
}

export function chooseMediaSelection(candidates: MediaCandidate[], tabId: number, frameId: number, playerKey?: string, documentId?: string, expectedKind?: Exclude<MediaKind, 'unknown'>): MediaSelection | undefined {
  const scoped = candidates.filter((item) => item.tabId === tabId && (item.frameId === frameId || item.frameId === 0) && (!documentId || item.documentId === documentId) && matchesKind(item, expectedKind));
  const chooseFromPool = (pool: MediaCandidate[]): MediaSelection | undefined => {
    const newest = [...pool].sort((left, right) => right.at - left.at);
    const manifests = newest.filter((item) => item.role === 'manifest');
    // Prefer a likely multivariant master over newer child playlists. HLS
    // players request the active video and alternate-audio playlists after the
    // master; selecting the last response can capture audio-only traffic.
    const mediaManifests = manifests.filter((item) => !isSubtitlePlaylist(item.url));
    const manifest = mediaManifests.find((item) => isLikelyMasterManifest(item.url)) ?? mediaManifests[0] ?? manifests[0];
    // Once segmented traffic is present, an unknown MP4 may be only an MSE
    // initialization fragment. Never promote it to a complete download.
    if (!manifest && pool.some((item) => item.role === 'segment')) return undefined;
    const source = manifest?.url ?? newest.find((item) => item.role !== 'segment')?.url;
    if (!source) return undefined;
    const selectedSegments = (() => {
      const childManifests = mediaManifests
        .filter((item) => item.url !== manifest?.url)
        .slice(0, 4)
        .map((item) => item.url);
      const fragments = newest.filter((item) => item.role === 'segment').slice(0, 8).map((item) => item.url);
      if (childManifests.length || fragments.length) return [...childManifests, ...fragments].slice(0, 8);
      return activeRepresentationHints(newest).slice(0, 8);
    })();
    return {
      source,
      selectedSegments,
    };
  };
  if (!playerKey) return chooseFromPool(scoped);

  const owned = scoped.filter((item) => item.playerKey === playerKey);
  const assignedToOther = scoped.some((item) => item.playerKey && item.playerKey !== playerKey);
  // If another player's traffic is already identified but this player has no
  // identified traffic, selecting that manifest would be a visible cross-talk
  // bug. An unassigned manifest is safe only while no competing ownership is
  // known; otherwise fail clearly instead of downloading the wrong media.
  if (!owned.length && assignedToOther) return undefined;
  const pool = owned.length
    ? (assignedToOther ? owned : [...owned, ...scoped.filter((item) => !item.playerKey)])
    : scoped.filter((item) => !item.playerKey);
  return chooseFromPool(pool);
}

export function chooseMediaCandidate(candidates: MediaCandidate[], tabId: number, frameId: number, playerKey?: string, documentId?: string): string | undefined {
  return chooseMediaSelection(candidates, tabId, frameId, playerKey, documentId)?.source;
}

// Some players fetch blob/MSE media through a worker. Chromium can attribute
// those worker requests to frame 0 even when the clicked media element lives in
// a child frame with a different documentId. Keep the normal document-scoped
// selection first. Only use the frame-0 fallback when one fresh visible/playing
// player is the sole active player in the tab and exactly one unowned media URL
// is available; otherwise refusing is safer than cross-player capture.
export function chooseWorkerMediaSelection(
  candidates: MediaCandidate[],
  players: MediaPlayerEvidence[],
  tabId: number,
  frameId: number,
  playerKey?: string,
  documentId?: string,
  now = Date.now(),
  expectedKind?: Exclude<MediaKind, 'unknown'>,
): MediaSelection | undefined {
  const direct = chooseMediaSelection(candidates, tabId, frameId, playerKey, documentId, expectedKind);
  if (direct || frameId === 0 || !playerKey) return direct;

  const freshPlayer = players.some((item) =>
    item.tabId === tabId &&
    item.frameId === frameId &&
    item.playerKey === playerKey &&
    (!documentId || item.documentId === documentId) &&
    now - item.at <= 15_000 &&
    item.playing &&
    item.visible,
  );
  if (!freshPlayer) return undefined;

  const activeKeys = new Set(
    players
      .filter((item) => item.tabId === tabId && now - item.at <= 15_000 && item.playing && item.visible)
      .map((item) => item.playerKey),
  );
  if (activeKeys.size !== 1 || !activeKeys.has(playerKey)) return undefined;

  const workerCandidates = candidates.filter((item) =>
    item.tabId === tabId &&
    item.frameId === 0 &&
    !item.playerKey &&
    now - item.at <= 90_000 &&
    item.at - now <= 5_000 &&
    isMediaCandidate(item) &&
    matchesKind(item, expectedKind)
  );
  const mediaSources = new Set(workerCandidates.filter((item) => item.role !== 'segment').map((item) => item.url));
  if (mediaSources.size !== 1 || !workerCandidates.some((item) => item.documentId !== documentId)) return undefined;
  const selection = chooseMediaSelection(workerCandidates, tabId, 0, undefined, undefined, expectedKind);
  if (!selection) return undefined;
  return workerCandidates.some((item) => item.role === 'manifest' || item.role === 'segment')
    ? selection
    : { ...selection, selectedSegments: [] };
}
