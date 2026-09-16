export type MediaRole = 'unknown' | 'manifest' | 'segment';
export type MediaKind = 'unknown' | 'audio' | 'video';

export interface MediaCandidate {
  url: string;
  tabId: number;
  frameId: number;
  at: number;
  role: MediaRole;
  kind?: MediaKind;
  contentType?: string;
  totalBytes?: number;
  documentId?: string;
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

export interface MediaEvidence {
  currentSrc: string;
  sourceIdentity: string;
  source?: string;
  playerKind?: Exclude<MediaKind, 'unknown'>;
  companionAudio?: string;
  selectedSegments: string[];
}

export interface MediaSelection {
  source: string;
  selectedSegments: string[];
  companionAudio?: string;
}

export function roleFor(url: string, contentType = ''): MediaRole {
  const hint = `${contentType} ${url}`.toLowerCase();
  if (hint.includes('mpegurl') || hint.includes('dash+xml') || /\.(?:m3u8|mpd)(?:[?#]|$)/i.test(url)) return 'manifest';
  if (hint.includes('video/mp2t') || hint.includes('iso.segment') || /(?:^|[./_-])(?:m4[asv]|ts|cmf[av])(?:[?#]|$)/i.test(url)) return 'segment';
  return 'unknown';
}

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
    const parsed = new URL(url);
    const mimeParam = (parsed.searchParams.get('mime') || '').toLowerCase();
    if (mimeParam.startsWith('audio/')) return 'audio';
    if (mimeParam.startsWith('video/')) return 'video';
    const path = parsed.pathname.toLowerCase();
    if (/(^|[/_-])audio([/_.-]|$)/.test(path) || /\.(?:m4a|aac|mp3|oga|ogg|opus|flac)(?:$|[?#])/.test(path)) return 'audio';
    if (/(^|[/_-])video([/_.-]|$)/.test(path) || /\.(?:webm|m4v|ogv)(?:$|[?#])/.test(path)) return 'video';
  } catch {
    return 'unknown';
  }
  return 'unknown';
}

export function isLikelyRepresentation(url: string, contentType = ''): boolean {
  const mime = contentType.toLowerCase().split(';', 1)[0].trim();
  if (mime.startsWith('video/') || mime.startsWith('audio/') || mime === 'image/gif' || mime === 'image/webp') return true;
  try {
    const parsed = new URL(url);
    if (/\.(?:gif|mp4|webp|webm|m4a|m4v|mov|ogv|mp3|aac|ogg|opus|flac)(?:$|[?#])/i.test(parsed.pathname)) return true;
    const mimeParam = (parsed.searchParams.get('mime') || '').toLowerCase();
    return mimeParam.startsWith('video/') || mimeParam.startsWith('audio/');
  } catch {
    return false;
  }
}

export function isMediaCandidate(candidate: Pick<MediaCandidate, 'url' | 'role' | 'kind' | 'contentType'>, contentType = ''): boolean {
  return candidate.role !== 'unknown'
    || (candidate.kind !== undefined && candidate.kind !== 'unknown')
    || isLikelyRepresentation(candidate.url, contentType || candidate.contentType || '');
}

export function normalizeChunkUrl(url: string): string {
  try {
    const parsed = new URL(url);
    parsed.hash = '';
    if (parsed.searchParams.has('range')) {
      const signedParameters = (parsed.searchParams.get('sparams') || '').split(',').map((item) => item.trim().toLowerCase());
      if (!signedParameters.includes('range')) parsed.searchParams.delete('range');
    }
    return parsed.href;
  } catch {
    return url;
  }
}

function activeRepresentationHints(candidates: MediaCandidate[]): string[] {
  const representations = candidates.filter((item) => item.role === 'unknown' && isLikelyRepresentation(item.url, item.contentType));
  if (!representations.length) return [];
  const newestByKind = new Map<Exclude<MediaKind, 'unknown'>, MediaCandidate>();
  for (const item of representations) {
    const kind = item.kind && item.kind !== 'unknown' ? item.kind : mediaKindFor(item.url, item.contentType);
    if (kind !== 'unknown' && !newestByKind.has(kind)) newestByKind.set(kind, item);
  }
  if (!newestByKind.size) return representations.slice(0, 1).map((item) => normalizeChunkUrl(item.url));
  return [...newestByKind.values()]
    .sort((left, right) => right.at - left.at)
    .map((item) => normalizeChunkUrl(item.url));
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
  const kind = candidate.kind && candidate.kind !== 'unknown'
    ? candidate.kind
    : mediaKindFor(candidate.url, candidate.contentType);
  return kind === 'unknown' || kind === expectedKind;
}

export function chooseMediaSelection(
  candidates: MediaCandidate[],
  tabId: number,
  frameId: number,
  playerKey?: string,
  documentId?: string,
  expectedKind?: Exclude<MediaKind, 'unknown'>,
): MediaSelection | undefined {
  const scoped = candidates.filter((item) =>
    item.tabId === tabId &&
    (item.frameId === frameId || item.frameId === 0) &&
    (!documentId || item.documentId === documentId),
  );
  const chooseFromPool = (pool: MediaCandidate[]): MediaSelection | undefined => {
    const allNewest = [...pool].sort((left, right) => right.at - left.at);
    const newest = allNewest.filter((item) => matchesKind(item, expectedKind));
    const manifests = newest.filter((item) => item.role === 'manifest');
    const mediaManifests = manifests.filter((item) => !isSubtitlePlaylist(item.url));
    const manifest = mediaManifests.find((item) => isLikelyMasterManifest(item.url)) ?? mediaManifests[0] ?? manifests[0];

    if (manifest) {
      const childManifests = mediaManifests
        .filter((item) => item.url !== manifest.url)
        .slice(0, 4)
        .map((item) => normalizeChunkUrl(item.url));
      const hints = activeRepresentationHints(newest).slice(0, 8);
      return {
        source: normalizeChunkUrl(manifest.url),
        selectedSegments: [...childManifests, ...hints].slice(0, 8),
      };
    }

    if (pool.some((item) => item.role === 'segment')) return undefined;

    const videoCandidates = newest.filter((item) => item.role !== 'segment' && (item.kind === 'video' || (item.kind !== 'audio' && mediaKindFor(item.url, item.contentType) === 'video')) && (item.kind === 'video' || isLikelyRepresentation(item.url, item.contentType)));
    const audioPool = expectedKind === 'video' ? allNewest : newest;
    const audioCandidates = audioPool.filter((item) => item.role !== 'segment' && (item.kind === 'audio' || mediaKindFor(item.url, item.contentType) === 'audio') && (item.kind === 'audio' || isLikelyRepresentation(item.url, item.contentType)));
    const untypedCandidates = newest.filter((item) => item.role !== 'segment' && isLikelyRepresentation(item.url, item.contentType) && !videoCandidates.includes(item) && !audioCandidates.includes(item));

    if (expectedKind === 'audio') {
      const primaryAudio = audioCandidates[0] ?? untypedCandidates.find((item) => mediaKindFor(item.url, item.contentType) !== 'video');
      return primaryAudio ? { source: normalizeChunkUrl(primaryAudio.url), selectedSegments: [] } : undefined;
    }

    const primaryVideo = videoCandidates[0] ?? untypedCandidates.find((item) => mediaKindFor(item.url, item.contentType) !== 'audio');
    if (!primaryVideo) {
      if (expectedKind === 'video') return undefined;
      const primaryAudio = audioCandidates[0] ?? untypedCandidates[0];
      return primaryAudio ? { source: normalizeChunkUrl(primaryAudio.url), selectedSegments: [] } : undefined;
    }

    const companionAudio = audioCandidates[0];
    return {
      source: normalizeChunkUrl(primaryVideo.url),
      selectedSegments: [],
      ...(companionAudio ? { companionAudio: normalizeChunkUrl(companionAudio.url) } : {}),
    };
  };

  if (!playerKey) return chooseFromPool(scoped);
  const owned = scoped.filter((item) => item.playerKey === playerKey);
  if (!owned.length) return undefined;
  return chooseFromPool(owned);
}

export function chooseMediaCandidate(candidates: MediaCandidate[], tabId: number, frameId: number, playerKey?: string, documentId?: string): string | undefined {
  return chooseMediaSelection(candidates, tabId, frameId, playerKey, documentId)?.source;
}

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
    matchesKind(item, expectedKind),
  );
  const mediaSources = new Set(workerCandidates.filter((item) => item.role !== 'segment').map((item) => normalizeChunkUrl(item.url)));
  if (mediaSources.size < 1 || mediaSources.size > 2 || !workerCandidates.some((item) => item.documentId !== documentId)) return undefined;
  if (mediaSources.size === 2) {
    const hasVideo = workerCandidates.some((item) => item.kind === 'video' || (item.kind !== 'audio' && mediaKindFor(item.url, item.contentType) === 'video'));
    const hasAudio = workerCandidates.some((item) => item.kind === 'audio' || mediaKindFor(item.url, item.contentType) === 'audio');
    if (!hasVideo || !hasAudio) return undefined;
  }
  const selection = chooseMediaSelection(workerCandidates, tabId, 0, undefined, undefined, expectedKind);
  if (!selection) return undefined;
  return workerCandidates.some((item) => item.role === 'manifest' || item.role === 'segment')
    ? selection
    : { ...selection, selectedSegments: [] };
}
