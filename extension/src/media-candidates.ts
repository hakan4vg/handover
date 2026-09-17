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
  /** The player's source at report time: a player may only be credited with
   *  traffic that happened after its current source appeared (this is what
   *  keeps an SPA navigation from re-using the previous video's URL). */
  currentSrc?: string;
  mediaIdentity?: string;
  /** when the current source was first reported (ms epoch) */
  srcAt?: number;
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

/** What to acquire for one player: the best source, the ordered alternatives
 *  the resident may fall back to, and the hints that help it resolve variants. */
export interface MediaCapturePlan extends MediaSelection {
  alternatives: string[];
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

/**
 * A URL whose bytes are a signed slice of an object: a path segment that
 * carries the range (Vimeo's `/v2/range/prot/<b64>/…`) or a `range=` parameter
 * that is part of the signature (`sparams` lists it, as YouTube's do).
 *
 * Such URLs are never sources. They name a few kilobytes of a stream and their
 * signatures expire within seconds (measured live: the same Vimeo fragment
 * answered 206 at +30 ms and 403 at +4.4 s), so a job built on one either
 * downloads a fragment as if it were a file (the 62 KB YouTube "downloads") or
 * fails on an expired token. They stay candidates/hints; only manifests and
 * whole objects are acquire-able.
 *
 * An *unsigned* `range=` parameter is a different thing: it can be stripped, so
 * `normalizeChunkUrl` turns that URL into a whole-object URL and it stays
 * acquire-able.
 */
export function isByteRangeFragment(url: string): boolean {
  try {
    const parsed = new URL(url);
    if (parsed.pathname.toLowerCase().split('/').includes('range')) return true;
    if (!parsed.searchParams.has('range')) return false;
    const sparams = (parsed.searchParams.get('sparams') || '').split(',').map((item) => item.trim().toLowerCase());
    return sparams.includes('range');
  } catch {
    return false;
  }
}

/** A URL is a usable source only after normalization removes any range it is
 *  allowed to remove. */
export function acquireableSource(url: string): boolean {
  return isHttpUrl(url) && !isByteRangeFragment(normalizeChunkUrl(url));
}

function isHttpUrl(url: string): boolean {
  try {
    const scheme = new URL(url).protocol;
    return scheme === 'http:' || scheme === 'https:';
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
  notBefore = 0,
): MediaSelection | undefined {
  // Ownership is per frame, not per tab: a cross-origin embedded player may
  // only inherit traffic observed in its own frame. The previous
  // `item.frameId === 0` clause let a host page's media be attributed to an
  // embedded player (proven live: an inline YouTube embed downloading the
  // Reddit post's video).
  const scoped = candidates.filter((item) =>
    item.tabId === tabId &&
    item.frameId === frameId &&
    (!documentId || item.documentId === documentId) &&
    item.at >= notBefore,
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

    // A byte-range fragment is a slice with a seconds-long signature: usable as
    // a hint, never as the thing a job is built on.
    const acquireable = (item: MediaCandidate) => acquireableSource(item.url);

    const videoCandidates = newest.filter((item) => item.role !== 'segment' && acquireable(item) && (item.kind === 'video' || (item.kind !== 'audio' && mediaKindFor(item.url, item.contentType) === 'video')) && (item.kind === 'video' || isLikelyRepresentation(item.url, item.contentType)));
    const audioPool = expectedKind === 'video' ? allNewest : newest;
    const audioCandidates = audioPool.filter((item) => item.role !== 'segment' && acquireable(item) && (item.kind === 'audio' || mediaKindFor(item.url, item.contentType) === 'audio') && (item.kind === 'audio' || isLikelyRepresentation(item.url, item.contentType)));
    const untypedCandidates = newest.filter((item) => item.role !== 'segment' && acquireable(item) && isLikelyRepresentation(item.url, item.contentType) && !videoCandidates.includes(item) && !audioCandidates.includes(item));

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

/**
 * Media this player plausibly owns, ranked best-first.
 *
 * A player whose bytes arrive through MSE/blob gives us no URL of its own, and
 * the requests that produced those bytes may have been issued by a realm no
 * content script can instrument (a worker, a cache-backed service worker, a
 * player library). What is always observable is the network traffic of the tab,
 * so ownership is decided by elimination: while exactly one player in the tab is
 * playing and visible, the media fetched for that tab is that player's media.
 *
 * Ranking prefers the player's own document, then manifests (a manifest is the
 * only thing that can rebuild an ordered presentation), then representations by
 * recency. Subtitles and segments are never handed over as the source.
 */
export function rankedMediaCandidates(
  candidates: MediaCandidate[],
  tabId: number,
  frameId: number,
  documentId?: string,
  expectedKind?: Exclude<MediaKind, 'unknown'>,
  now = Date.now(),
  limit = 6,
  notBefore = 0,
): string[] {
  const pool = candidates.filter((item) =>
    item.tabId === tabId &&
    item.frameId === frameId &&
    now - item.at <= 90_000 &&
    item.at >= notBefore &&
    item.role !== 'segment' &&
    acquireableSource(item.url) &&
    matchesKind(item, expectedKind),
  );
  const own = pool.filter((item) => !documentId || item.documentId === documentId);
  const ordered = (items: MediaCandidate[]) => [...items].sort((left, right) => right.at - left.at);
  const manifests = (items: MediaCandidate[]) => {
    const found = ordered(items.filter((item) => item.role === 'manifest'));
    const media = found.filter((item) => !isSubtitlePlaylist(item.url));
    return [...media.filter((item) => isLikelyMasterManifest(item.url)), ...media.filter((item) => !isLikelyMasterManifest(item.url)), ...found];
  };
  // Own-document traffic ranks first, then the rest of this frame's observed
  // traffic. It is never widened to the tab: another frame's media does not
  // belong to this player (SPEC §6.2.1).
  const representations = (items: MediaCandidate[]) => ordered(items.filter((item) =>
    item.role === 'unknown' && isLikelyRepresentation(item.url, item.contentType)));

  const ranked = [
    ...manifests(own),
    ...representations(own),
    ...manifests(pool.filter((item) => !own.includes(item))),
    ...representations(pool.filter((item) => !own.includes(item))),
  ];
  const seen = new Set<string>();
  const urls: string[] = [];
  for (const item of ranked) {
    const url = normalizeChunkUrl(item.url);
    if (seen.has(url)) continue;
    seen.add(url);
    urls.push(url);
    if (urls.length >= limit) break;
  }
  return urls;
}

/** Whether the given player is the only thing playing and visible in the tab,
 *  which is what makes tab-wide network traffic attributable to it. */
export function isSolePlayingPlayer(players: MediaPlayerEvidence[], tabId: number, frameId: number, playerKey: string, documentId?: string, now = Date.now()): boolean {
  const playing = players.filter((item) =>
    item.tabId === tabId && now - item.at <= 15_000 && item.playing && item.visible);
  if (playing.length !== 1) return false;
  const only = playing[0];
  return only.playerKey === playerKey && only.frameId === frameId && (!documentId || only.documentId === documentId);
}

/**
 * The capture plan for one player: exact/owned evidence first, then the
 * ownership-by-elimination fallback for players whose bytes the page pipeline
 * cannot see. Both paths come from the same ranking, so the source, the
 * alternatives and the hints can never disagree.
 */
export function planMediaCapture(
  candidates: MediaCandidate[],
  players: MediaPlayerEvidence[],
  tabId: number,
  frameId: number,
  playerKey?: string,
  documentId?: string,
  now = Date.now(),
  expectedKind?: Exclude<MediaKind, 'unknown'>,
): MediaCapturePlan | undefined {
  // A player can only own traffic that happened after its current source
  // appeared: without this, a YouTube SPA navigation re-uses the previous
  // video's URL, and a Reddit page's video can be attributed to an embedded
  // player it does not belong to. 2 s of slack covers requests that started
  // just before the source flipped.
  const clicked = players.find((item) =>
    item.playerKey === playerKey &&
    item.tabId === tabId &&
    item.frameId === frameId &&
    (!documentId || item.documentId === documentId),
  );
  const notBefore = clicked?.srcAt !== undefined ? Math.max(0, clicked.srcAt - 2_000) : 0;
  const exact = chooseMediaSelection(candidates, tabId, frameId, playerKey, documentId, expectedKind, notBefore);
  if (exact?.source) {
    const alternatives = rankedMediaCandidates(candidates, tabId, frameId, documentId, expectedKind, now, 6, notBefore)
      .filter((url) => url !== normalizeChunkUrl(exact.source));
    return { ...exact, alternatives: alternatives.slice(0, 5) };
  }
  if (!playerKey || !isSolePlayingPlayer(players, tabId, frameId, playerKey, documentId, now)) return undefined;
  const ranked = rankedMediaCandidates(candidates, tabId, frameId, documentId, expectedKind, now, 6, notBefore);
  if (!ranked.length) return undefined;
  // The pool's own ordering already put manifests before representations, so the
  // head is the source and the tail is what the resident may try next.
  const [source, ...rest] = ranked;
  // Hints steer variant choice, so they only apply to a manifest source; a
  // progressive source is acquired exactly as handed over.
  const hints = roleFor(source) === 'manifest'
    ? rest.filter((url) => roleFor(url) === 'unknown' && isLikelyRepresentation(url)).slice(0, 8)
    : [];
  return { source, selectedSegments: hints, alternatives: rest.slice(0, 5) };
}
