import { describe, expect, it } from 'vitest';
import { DEFAULT_POLICY, isHttp, siteOf } from './shared';
import {
  chooseMediaCandidate,
  choosePlayerEvidence,
  roleFor,
  type MediaCandidate,
  type MediaPlayerEvidence,
} from './media-candidates';

// Locks the URL-gating semantics the whole capture flow depends on:
// background.ts only remembers/forwards http(s) sources, and per-site
// exclusion matching is done on the normalized hostname.
describe('isHttp', () => {
  it('accepts http and https sources including fixture shapes', () => {
    expect(isHttp('http://127.0.0.1:8901/range.bin')).toBe(true);
    expect(isHttp('https://cdn.example.test/vod/index.m3u8?token=abc')).toBe(true);
    expect(isHttp('http://localhost:4173/')).toBe(true);
  });

  it('rejects blob, ftp, file, data, and unparseable input', () => {
    expect(isHttp('blob:https://x.test/abc')).toBe(false);
    expect(isHttp('ftp://cdn.example.test/file.zip')).toBe(false);
    expect(isHttp('file:///etc/passwd')).toBe(false);
    expect(isHttp('data:text/plain,hi')).toBe(false);
    expect(isHttp('')).toBe(false);
    expect(isHttp('not a url')).toBe(false);
    expect(isHttp('/relative/path.mp4')).toBe(false);
  });
});

describe('siteOf', () => {
  it('strips www and lowercases for exclusion matching', () => {
    expect(siteOf('https://www.Example.COM/video')).toBe('example.com');
    expect(siteOf('https://sub.cdn.example.test:8443/v/a.mp4?x=1')).toBe(
      'sub.cdn.example.test',
    );
    expect(siteOf('http://127.0.0.1:8901/range.bin')).toBe('127.0.0.1');
  });

  it('returns empty for anything without a hostname', () => {
    expect(siteOf('')).toBe('');
    expect(siteOf('not a url')).toBe('');
    expect(siteOf('blob:https://x.test/abc')).toBe('');
  });
});

describe('DEFAULT_POLICY', () => {
  it('starts with interception and media buttons on, nothing excluded', () => {
    expect(DEFAULT_POLICY).toEqual({
      interceptDownloads: true,
      showMediaButtons: true,
      excludedSites: [],
    });
  });
});

describe('media candidate selection', () => {
  it('classifies HLS/DASH manifests and fragment traffic', () => {
    expect(roleFor('https://cdn.test/vod/playlist.m3u8')).toBe('manifest');
    expect(roleFor('https://cdn.test/vod/manifest', 'application/dash+xml')).toBe('manifest');
    expect(roleFor('https://cdn.test/vod/part-04.m4s', 'video/mp4')).toBe('segment');
    expect(roleFor('https://cdn.test/vod/segment.ts', 'video/mp2t')).toBe('segment');
    expect(roleFor('https://cdn.test/vod/file.bin', 'application/octet-stream')).toBe('unknown');
  });

  it('keeps two players in one frame on their own manifest candidates', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/a/manifest.mpd', tabId: 4, frameId: 0, at: 1, role: 'manifest', playerKey: 'player-a' },
      { url: 'https://cdn.test/a/a-1.m4s', tabId: 4, frameId: 0, at: 2, role: 'segment', playerKey: 'player-a' },
      { url: 'https://cdn.test/b/manifest.mpd', tabId: 4, frameId: 0, at: 3, role: 'manifest', playerKey: 'player-b' },
      { url: 'https://cdn.test/b/b-1.m4s', tabId: 4, frameId: 0, at: 4, role: 'segment', playerKey: 'player-b' },
    ];
    expect(chooseMediaCandidate(candidates, 4, 0, 'player-a')).toBe('https://cdn.test/a/manifest.mpd');
    expect(chooseMediaCandidate(candidates, 4, 0, 'player-b')).toBe('https://cdn.test/b/manifest.mpd');
  });

  it('allows a manifest observed before player evidence when that player owns the segments', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/vod/index.m3u8', tabId: 4, frameId: 0, at: 1, role: 'manifest' },
      { url: 'https://cdn.test/vod/segment-1.ts', tabId: 4, frameId: 0, at: 2, role: 'segment', playerKey: 'player-a' },
    ];
    expect(chooseMediaCandidate(candidates, 4, 0, 'player-a')).toBe('https://cdn.test/vod/index.m3u8');
  });

  it('refuses to cross-select another player when the clicked player has no evidence', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/a/manifest.mpd', tabId: 4, frameId: 0, at: 1, role: 'manifest', playerKey: 'player-a' },
    ];
    expect(chooseMediaCandidate(candidates, 4, 0, 'player-b')).toBeUndefined();
  });

  it('does not mistake an MSE initialization file for a complete download', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/vod/a-init.mp4', tabId: 4, frameId: 0, at: 1, role: 'unknown' },
      { url: 'https://cdn.test/vod/a-0.m4s', tabId: 4, frameId: 0, at: 2, role: 'segment' },
    ];
    expect(chooseMediaCandidate(candidates, 4, 0, 'player-a')).toBeUndefined();
  });

  it('does not reuse a candidate from a previous document in the same tab', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/old/manifest.mpd', tabId: 4, frameId: 0, documentId: 'doc-old', at: 1, role: 'manifest', playerKey: 'player-a' },
      { url: 'https://cdn.test/new/manifest.mpd', tabId: 4, frameId: 0, documentId: 'doc-new', at: 2, role: 'manifest', playerKey: 'player-b' },
    ];
    expect(chooseMediaCandidate(candidates, 4, 0, 'player-b', 'doc-new')).toBe('https://cdn.test/new/manifest.mpd');
    expect(chooseMediaCandidate(candidates, 4, 0, 'player-b', 'doc-old')).toBeUndefined();
  });

  it('keeps player evidence isolated by document identity', () => {
    const players: MediaPlayerEvidence[] = [
      { playerKey: 'old', tabId: 4, frameId: 0, documentId: 'doc-old', at: 999, active: true, hovered: true, playing: true, visible: true },
      { playerKey: 'new', tabId: 4, frameId: 0, documentId: 'doc-new', at: 998, active: false, hovered: false, playing: true, visible: true },
    ];
    expect(choosePlayerEvidence(players, 4, 0, 1000, 'doc-new')?.playerKey).toBe('new');
  });
});
