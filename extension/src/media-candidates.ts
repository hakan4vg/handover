export type MediaRole = 'unknown' | 'manifest' | 'segment';
export type MediaKind = 'unknown' | 'audio' | 'video';

export interface MediaCandidate {
  url: string;
  tabId: number;
  frameId: number;
  at: number;
  /** When the request behind this entry started. A player may only be
   *  credited with requests that *started* after its current source did; a
   *  slow answer to an older request must not look current (SPEC §6.2.1). */
  startedAt?: number;
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

function beganAt(item: MediaCandidate): number {
  return item.startedAt ?? item.at;
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
  const representations = [...candidates]
    .filter((item) => item.role === 'unknown' && isLikelyRepresentation(item.url, item.contentType))
    .sort((left, right) => right.at - left.at);
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

function matchesKind(candidate: MediaCandidate, expectedKind?: Exclude<MediaKind, 'unknown'>): boolean {
  if (!expectedKind) return true;
  const kind = candidate.kind && candidate.kind !== 'unknown'
    ? candidate.kind
    : mediaKindFor(candidate.url, candidate.contentType);
  return kind === 'unknown' || kind === expectedKind;
}

/** Path segments that name one presentation: an asset id, a session id, a
 *  hash. Long and mixing letters with digits once a file extension is set
 *  aside, unlike `v2`, `avf` or `playlist.m3u8`. A manifest, its variant playlists and its segments share them;
 *  another video on the same page does not. */
function identifyingSegments(url: string): Set<string> {
  try {
    return new Set(new URL(url).pathname.split('/')
      .map((part) => part.replace(/\.[a-z0-9]{2,5}$/i, ''))
      .filter((part) => part.length >= 8 && /\d/.test(part) && /[a-z]/i.test(part)));
  } catch {
    return new Set();
  }
}

function sharedCount(left: Set<string>, right: Set<string>): number {
  let count = 0;
  for (const part of left) if (right.has(part)) count += 1;
  return count;
}

/** One presentation's traffic: its manifests, and the requests (segments,
 *  representations) that belong to it. */
interface Stream {
  manifests: MediaCandidate[];
  ids: Set<string>;
  members: MediaCandidate[];
  /** when the latest of its non-manifest requests began */
  activeAt: number;
}

function streamsOf(pool: MediaCandidate[]): Stream[] {
  const streams: Stream[] = [];
  const manifests = [...pool]
    .filter((item) => item.role === 'manifest' && !isSubtitlePlaylist(item.url))
    .sort((left, right) => beganAt(left) - beganAt(right));
  for (const manifest of manifests) {
    const ids = identifyingSegments(manifest.url);
    const stream = ids.size ? streams.find((item) => sharedCount(item.ids, ids) > 0) : undefined;
    if (stream) {
      stream.manifests.push(manifest);
      for (const id of ids) stream.ids.add(id);
    } else {
      streams.push({ manifests: [manifest], ids, members: [], activeAt: 0 });
    }
  }
  for (const item of pool) {
    if (item.role === 'manifest') continue;
    const ids = identifyingSegments(item.url);
    let best: Stream | undefined;
    let bestCount = 0;
    for (const stream of streams) {
      const count = sharedCount(stream.ids, ids);
      if (count > bestCount) {
        best = stream;
        bestCount = count;
      }
    }
    if (!best) continue;
    best.members.push(item);
    best.activeAt = Math.max(best.activeAt, beganAt(item));
  }
  return streams;
}

/** A stream's source: its multivariant manifest when one was seen. */
function streamSource(stream: Stream): MediaCandidate {
  return stream.manifests.find((item) => isLikelyMasterManifest(item.url)) ?? stream.manifests[0];
}

export type MediaCapturePlanResult = MediaCapturePlan | { unresolved: 'not-played' | 'not-found' };

/**
 * The capture plan for the player the user clicked.
 *
 * Which stream belongs to which player is read from the tab's network traffic,
 * which the extension sees whatever realm fetched it (page, worker, service
 * worker). A page often holds several presentations (a preview, an ad, the
 * next video); the one this player plays is the one whose segments the tab
 * keeps fetching after the player's current source appeared. A paused preview
 * fetches nothing.
 *
 * When nothing has been fetched for any stream yet (the player has not
 * started), a page with a single presentation still resolves; with several,
 * the user is asked to play first rather than handed a guess.
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
): MediaCapturePlanResult {
  // A player can only own traffic that happened after its current source
  // appeared: without this, a YouTube SPA navigation re-uses the previous
  // video's URL. 2 s of slack covers requests that started just before the
  // source flipped. Ownership is per frame: another frame's media does not
  // belong to this player (an inline embed is not the host page's video).
  const clicked = players.find((item) =>
    item.playerKey === playerKey &&
    item.tabId === tabId &&
    item.frameId === frameId &&
    (!documentId || item.documentId === documentId),
  );
  const notBefore = clicked?.srcAt !== undefined ? Math.max(0, clicked.srcAt - 2_000) : 0;
  const unresolved = { unresolved: clicked?.playing ? 'not-found' : 'not-played' } as const;
  const scoped = candidates.filter((item) =>
    item.tabId === tabId &&
    item.frameId === frameId &&
    (!documentId || !item.documentId || item.documentId === documentId) &&
    beganAt(item) >= notBefore,
  );
  const pool = scoped.filter((item) => matchesKind(item, expectedKind));

  const streams = streamsOf(pool);
  if (streams.length) {
    const active = streams.filter((stream) => stream.activeAt > 0).sort((left, right) => right.activeAt - left.activeAt);
    const stream = active[0] ?? (streams.length === 1 ? streams[0] : undefined);
    if (!stream) return unresolved;
    const source = streamSource(stream);
    const variants = stream.manifests
      .filter((item) => item !== source)
      .sort((left, right) => right.at - left.at)
      .map((item) => normalizeChunkUrl(item.url));
    const hints = [...variants, ...activeRepresentationHints(stream.members)].slice(0, 8);
    return { source: normalizeChunkUrl(source.url), selectedSegments: hints, alternatives: variants.slice(0, 5) };
  }

  // No manifest: progressive media. The tab's traffic is this player's only
  // while it is the one playing.
  const playing = players.filter((item) => item.tabId === tabId && now - item.at <= 15_000 && item.playing && item.visible);
  const sole = playing.length === 1 && playing[0].playerKey === playerKey && playing[0].frameId === frameId;
  if (!sole) return unresolved;
  const newest = (items: MediaCandidate[]) => [...items].sort((left, right) => right.at - left.at);
  const acquireable = (item: MediaCandidate) => item.role !== 'segment' && acquireableSource(item.url);
  const ofKind = (item: MediaCandidate, kind: Exclude<MediaKind, 'unknown'>) =>
    (item.kind === kind || (item.kind !== (kind === 'video' ? 'audio' : 'video') && mediaKindFor(item.url, item.contentType) === kind)) &&
    (item.kind === kind || isLikelyRepresentation(item.url, item.contentType));
  const video = newest(pool).filter((item) => acquireable(item) && ofKind(item, 'video'));
  // A video's separate audio track is the other half of the same player.
  const audio = newest(scoped).filter((item) => acquireable(item) && ofKind(item, 'audio'));
  const untyped = newest(pool).filter((item) => acquireable(item) && isLikelyRepresentation(item.url, item.contentType) && !video.includes(item) && !audio.includes(item));
  if (expectedKind === 'audio') {
    const primary = audio[0] ?? untyped.find((item) => mediaKindFor(item.url, item.contentType) !== 'video');
    return primary ? { source: normalizeChunkUrl(primary.url), selectedSegments: [], alternatives: [] } : { unresolved: 'not-found' };
  }
  const primary = video[0] ?? untyped.find((item) => mediaKindFor(item.url, item.contentType) !== 'audio');
  if (!primary) return { unresolved: 'not-found' };
  return {
    source: normalizeChunkUrl(primary.url),
    selectedSegments: [],
    alternatives: [],
    ...(audio[0] ? { companionAudio: normalizeChunkUrl(audio[0].url) } : {}),
  };
}
