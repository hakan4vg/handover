// What a player played, read from the player itself and matched to the tab's
// network responses.
//
// page-probe.ts records, for the clicked element, each SourceBuffer's MIME
// type and the byte length and time of what it was given. Players append the
// bytes they downloaded as they arrived (a DASH or fMP4 HLS segment, a range
// of a progressive file), so an append's length is the length of one response
// the tab received shortly before: that response's URL is what this player
// played. No URL is interpreted, so nothing here knows any site.

export interface ObservedResponse {
  /** As requested, with an unsigned `range` parameter removed. */
  url: string;
  /** When its headers arrived (ms since the epoch). */
  at: number;
  /** Body length: the Content-Range span of a 206, else the Content-Length. */
  bytes?: number;
  /** Length of the whole object, when the response says (for media filters). */
  total?: number;
  contentType?: string;
  manifest: boolean;
}

/** One SourceBuffer of the player: its MIME type and recent appends as
 *  [byte length, time]. */
export interface PlayerTrack {
  mime: string;
  appends: Array<[number, number]>;
}

export interface PlayedTrack {
  kind: 'video' | 'audio';
  /** The files the player's appends came from, oldest first. */
  files: string[];
}

/** A response may arrive this long before the append that uses it. */
const MATCH_BEFORE_MS = 120_000;
/** A player appends what already arrived; page and browser read the same clock. */
const MATCH_AFTER_MS = 250;
/** A player that converts what it downloads appends soon after the download. */
const CONVERTED_WITHIN_MS = 5_000;
const FILES_KEPT = 16;

export function isManifest(url: string, contentType = ''): boolean {
  const type = contentType.toLowerCase();
  if (type.includes('mpegurl') || type.includes('dash+xml')) return true;
  try {
    return /\.(?:m3u8|mpd)$/i.test(new URL(url).pathname);
  } catch {
    return false;
  }
}

/**
 * A `range=` query parameter that is not covered by the URL's signature
 * (`sparams`) only selects bytes; without it the URL names the whole object.
 * A signed one cannot be removed.
 */
export function normalizeChunkUrl(url: string): string {
  try {
    const parsed = new URL(url);
    parsed.hash = '';
    if (parsed.searchParams.has('range')) {
      const signed = (parsed.searchParams.get('sparams') || '').split(',').map((item) => item.trim().toLowerCase());
      if (!signed.includes('range')) parsed.searchParams.delete('range');
    }
    return parsed.href;
  } catch {
    return url;
  }
}

/** Body length and object length of a response, from its headers. */
export function responseLengths(status: number | undefined, contentRange: string, contentLength: string, contentEncoding: string): { bytes?: number; total?: number } {
  const range = contentRange.match(/^bytes\s+(\d+)-(\d+)\/(\d+|\*)$/i);
  if (status === 206 && range) {
    const total = Number(range[3]);
    return { bytes: Number(range[2]) - Number(range[1]) + 1, ...(Number.isSafeInteger(total) && total > 0 ? { total } : {}) };
  }
  const length = Number(contentLength);
  // A compressed body's length is not what the player is given.
  if (!Number.isSafeInteger(length) || length <= 0 || (contentEncoding && contentEncoding !== 'identity')) return {};
  return status === 200 ? { bytes: length, total: length } : { bytes: length };
}

function latest(responses: ObservedResponse[], test: (response: ObservedResponse) => boolean): ObservedResponse | undefined {
  for (let index = responses.length - 1; index >= 0; index -= 1) {
    if (test(responses[index])) return responses[index];
  }
  return undefined;
}

/** The files each of the player's tracks came from. */
export function playedTracks(tracks: PlayerTrack[], responses: ObservedResponse[]): PlayedTrack[] {
  const media = responses.filter((response) => !response.manifest).sort((left, right) => left.at - right.at);
  const used = new Set<ObservedResponse>();
  const played = new Map<'video' | 'audio', string[]>();
  for (const track of tracks) {
    const files: string[] = [];
    const unmatched: number[] = [];
    for (const [bytes, at] of track.appends) {
      const match = latest(media, (response) => !used.has(response) && response.bytes === bytes && response.at <= at + MATCH_AFTER_MS && response.at >= at - MATCH_BEFORE_MS);
      if (match) {
        used.add(match);
        files.push(match.url);
      } else {
        unmatched.push(at);
      }
    }
    // A player that converts what it downloads (an MPEG-TS stream remuxed for
    // MSE) appends other lengths than it fetched. Then the response that
    // arrived just before each append is taken as its source: weaker, so only
    // when lengths match too few appends to say anything.
    if (files.length * 2 < track.appends.length) {
      for (const at of unmatched) {
        const match = latest(media, (response) => !used.has(response) && response.at <= at + MATCH_AFTER_MS && response.at >= at - CONVERTED_WITHIN_MS);
        if (match) {
          used.add(match);
          files.push(match.url);
        }
      }
    }
    const kind = track.mime.trim().toLowerCase().startsWith('audio/') ? 'audio' : 'video';
    played.set(kind, [...(played.get(kind) ?? []), ...files]);
  }
  return [...played.entries()]
    .map(([kind, files]) => ({ kind, files: [...new Set(files)].slice(-FILES_KEPT) }))
    .filter((track) => track.files.length > 0);
}

/** The player's tracks from the page probe's answer, bounded. */
export function cleanPlayerTracks(value: unknown): PlayerTrack[] {
  if (!Array.isArray(value)) return [];
  return value.slice(0, 8).flatMap((track) => {
    if (!track || typeof track !== 'object') return [];
    const { mime, appends } = track as { mime?: unknown; appends?: unknown };
    if (typeof mime !== 'string' || !Array.isArray(appends)) return [];
    const clean = appends.slice(-200).filter((append): append is [number, number] =>
      Array.isArray(append) && append.length === 2 && append.every((item) => Number.isSafeInteger(item) && item >= 0));
    return [{ mime: mime.slice(0, 200), appends: clean }];
  });
}
