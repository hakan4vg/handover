export type MediaRole = 'unknown' | 'manifest' | 'segment';

export interface MediaCandidate {
  url: string;
  tabId: number;
  frameId: number;
  at: number;
  role: MediaRole;
}

export function roleFor(url: string, contentType = ''): MediaRole {
  const hint = `${contentType} ${url}`.toLowerCase();
  if (hint.includes('mpegurl') || hint.includes('dash+xml') || /\.(?:m3u8|mpd)(?:[?#]|$)/i.test(url)) return 'manifest';
  if (hint.includes('video/mp2t') || hint.includes('iso.segment') || /(?:^|[./_-])(?:m4s|ts|cmf[av])(?:[?#]|$)/i.test(url)) return 'segment';
  return 'unknown';
}

export function chooseMediaCandidate(candidates: MediaCandidate[], tabId: number, frameId: number): string | undefined {
  const scoped = candidates.filter((item) => item.tabId === tabId && (item.frameId === frameId || item.frameId === 0));
  const manifest = [...scoped].reverse().find((item) => item.role === 'manifest');
  if (manifest) return manifest.url;
  // A blob/MSE element cannot be safely reconstructed from an arbitrary
  // fragment. A non-segment full-object request is still a valid generic
  // fallback; known segment-only traffic must fail clearly instead.
  return [...scoped].reverse().find((item) => item.role !== 'segment')?.url;
}
