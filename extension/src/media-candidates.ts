export type MediaRole = 'unknown' | 'manifest' | 'segment';

export interface MediaCandidate {
  url: string;
  tabId: number;
  frameId: number;
  at: number;
  role: MediaRole;
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

export function roleFor(url: string, contentType = ''): MediaRole {
  const hint = `${contentType} ${url}`.toLowerCase();
  if (hint.includes('mpegurl') || hint.includes('dash+xml') || /\.(?:m3u8|mpd)(?:[?#]|$)/i.test(url)) return 'manifest';
  if (hint.includes('video/mp2t') || hint.includes('iso.segment') || /(?:^|[./_-])(?:m4s|ts|cmf[av])(?:[?#]|$)/i.test(url)) return 'segment';
  return 'unknown';
}

export function choosePlayerEvidence(players: MediaPlayerEvidence[], tabId: number, frameId: number, now = Date.now(), documentId?: string): MediaPlayerEvidence | undefined {
  const fresh = players.filter((item) => item.tabId === tabId && (item.frameId === frameId || item.frameId === 0) && (!documentId || item.documentId === documentId) && now - item.at <= 15_000);
  return [...fresh].sort((left, right) => {
    const rank = (item: MediaPlayerEvidence) => Number(item.active) * 8 + Number(item.hovered) * 4 + Number(item.playing) * 2 + Number(item.visible);
    return rank(right) - rank(left) || right.at - left.at;
  })[0];
}

export function chooseMediaCandidate(candidates: MediaCandidate[], tabId: number, frameId: number, playerKey?: string, documentId?: string): string | undefined {
  const scoped = candidates.filter((item) => item.tabId === tabId && (item.frameId === frameId || item.frameId === 0) && (!documentId || item.documentId === documentId));
  const chooseFromPool = (pool: MediaCandidate[]): string | undefined => {
    const manifest = [...pool].reverse().find((item) => item.role === 'manifest');
    if (manifest) return manifest.url;
    // Once segmented traffic is present, an unknown MP4 may be only an MSE
    // initialization fragment. Never promote it to a complete download.
    if (pool.some((item) => item.role === 'segment')) return undefined;
    return [...pool].reverse().find((item) => item.role !== 'segment')?.url;
  };
  if (!playerKey) return chooseFromPool(scoped);

  const owned = scoped.filter((item) => item.playerKey === playerKey);
  const assignedToOther = scoped.some((item) => item.playerKey && item.playerKey !== playerKey);
  // If another player's traffic is already identified but this player has no
  // identified traffic, selecting that manifest would be a visible cross-talk
  // bug. An unassigned manifest is safe only while no competing ownership is
  // known; otherwise fail clearly instead of downloading the wrong media.
  if (!owned.length && assignedToOther) return undefined;
  const pool = owned.length ? owned : scoped.filter((item) => !item.playerKey);
  return chooseFromPool(pool);
}
