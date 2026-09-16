import { describe, expect, it } from 'vitest';
import { DEFAULT_POLICY, isHttp, mediaSourceFromValues, siteOf, siteOfDocument } from './shared';
import {
  chooseMediaCandidate,
  chooseMediaSelection,
  choosePlayerEvidence,
  chooseWorkerMediaSelection,
  isLikelyRepresentation,
  isMediaCandidate,
  isSubtitlePlaylist,
  mediaKindFor,
  normalizeChunkUrl,
  roleFor,
  type MediaCandidate,
  type MediaPlayerEvidence,
  type MediaSelection,
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

describe('siteOfDocument', () => {
  it('uses the document URL first and the parent referrer for opaque frames', () => {
    expect(siteOfDocument('https://child.example.test/player', 'https://parent.example.test/page')).toBe('child.example.test');
    expect(siteOfDocument('about:blank', 'https://www.Parent.EXAMPLE.test/page')).toBe('parent.example.test');
    expect(siteOfDocument('about:srcdoc', '', 'https://www.Parent.EXAMPLE.test/page')).toBe('parent.example.test');
    expect(siteOfDocument('about:srcdoc', 'https://referrer.example.test/page', 'https://ancestor.example.test/page')).toBe('referrer.example.test');
    expect(siteOfDocument('blob:https://child.example.test/id', 'https://parent.example.test/page')).toBe('parent.example.test');
  });
});

describe('mediaSourceFromValues', () => {
  it('prefers the browser-selected source over element and child fallbacks', () => {
    expect(mediaSourceFromValues('https://cdn.test/current.webm', 'https://cdn.test/element.mp4', 'child.webm', 'https://page.test/watch')).toBe('https://cdn.test/current.webm');
    expect(mediaSourceFromValues('', 'https://cdn.test/element.mp4', 'child.webm', 'https://page.test/watch')).toBe('https://cdn.test/element.mp4');
  });

  it('falls back to an HTTP child when the browser-selected source is opaque', () => {
    expect(mediaSourceFromValues('blob:https://page.test/player/id', '', 'https://cdn.test/vod/index.m3u8', 'https://page.test/watch')).toBe('https://cdn.test/vod/index.m3u8');
    expect(mediaSourceFromValues('blob:https://page.test/player/id', '', '../vod/index.m3u8', 'https://page.test/player/index.html')).toBe('https://page.test/vod/index.m3u8');
  });

  it('resolves a source child relative to the player document', () => {
    expect(mediaSourceFromValues('', '', '../media/clip.webm', 'https://page.test/player/index.html')).toBe('https://page.test/media/clip.webm');
  });

  it('does not invent the page URL when no media source exists', () => {
    expect(mediaSourceFromValues('', '', '', 'https://page.test/watch')).toBe('');
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
  it('classifies media-shaped traffic without retaining generic page assets', () => {
    expect(isMediaCandidate({ url: 'https://cdn.test/assets/player.js', role: 'unknown', kind: 'unknown' })).toBe(false);
    expect(isMediaCandidate({ url: 'https://cdn.test/vod/asset.mp4', role: 'unknown', kind: 'unknown' })).toBe(true);
    expect(isMediaCandidate({ url: 'https://cdn.test/vod/asset', role: 'unknown', kind: 'video' })).toBe(true);
    expect(isMediaCandidate({ url: 'https://cdn.test/vod/segment', role: 'segment', kind: 'unknown' })).toBe(true);
  });

  it('classifies HLS/DASH manifests and fragment traffic', () => {
    expect(roleFor('https://cdn.test/vod/playlist.m3u8')).toBe('manifest');
    expect(roleFor('https://cdn.test/vod/manifest', 'application/dash+xml')).toBe('manifest');
    expect(roleFor('https://cdn.test/vod/part-04.m4s', 'video/mp4')).toBe('segment');
    expect(roleFor('https://cdn.test/vod/part-04.m4v', 'video/mp4')).toBe('segment');
    expect(roleFor('https://cdn.test/vod/part-04.m4a', 'audio/mp4')).toBe('segment');
    expect(roleFor('https://cdn.test/vod/segment.ts', 'video/mp2t')).toBe('segment');
    expect(roleFor('https://cdn.test/vod/file.bin', 'application/octet-stream')).toBe('unknown');
  });

  it('classifies representation tracks from response MIME before URL guesses', () => {
    expect(mediaKindFor('https://cdn.test/vod/representation.mp4', 'audio/mp4; codecs=mp4a.40.2')).toBe('audio');
    expect(mediaKindFor('https://cdn.test/vod/representation.mp4', 'video/mp4; codecs=avc1.64001f')).toBe('video');
    expect(mediaKindFor('https://cdn.test/vod/audio_en_2c_128k_aac.mp4')).toBe('audio');
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

  it('refuses an unowned manifest even when another owned player segment exists', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/vod/index.m3u8', tabId: 4, frameId: 0, at: 1, role: 'manifest' },
      { url: 'https://cdn.test/vod/segment-1.ts', tabId: 4, frameId: 0, at: 2, role: 'segment', playerKey: 'player-a' },
    ];
    expect(chooseMediaCandidate(candidates, 4, 0, 'player-a')).toBeUndefined();
  });

  it('keeps a multivariant master when a newer alternate audio playlist arrives', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/vod/playlist.m3u8', tabId: 4, frameId: 0, at: 10, role: 'manifest', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/768/chunklist_w1_vo.m3u8', tabId: 4, frameId: 0, at: 20, role: 'manifest', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/en/chunklist_w1_ao.m3u8', tabId: 4, frameId: 0, at: 30, role: 'manifest', playerKey: 'player-a' },
    ];
    expect(chooseMediaCandidate(candidates, 4, 0, 'player-a')).toBe('https://cdn.test/vod/playlist.m3u8');
  });

  it('chooses the most recently observed manifest, not the last inserted one', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/vod/selected.m3u8', tabId: 4, frameId: 0, at: 20, role: 'manifest', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/old.m3u8', tabId: 4, frameId: 0, at: 10, role: 'manifest', playerKey: 'player-a' },
    ];
    expect(chooseMediaCandidate(candidates, 4, 0, 'player-a')).toBe('https://cdn.test/vod/selected.m3u8');
  });

  it('recognizes subtitle and caption playlist names', () => {
    expect(isSubtitlePlaylist('https://cdn.test/subtitles.m3u8')).toBe(true);
    expect(isSubtitlePlaylist('https://cdn.test/cc/captions.m3u8')).toBe(true);
    expect(isSubtitlePlaylist('https://cdn.test/vod/rendition.m3u8')).toBe(false);
    expect(isSubtitlePlaylist('https://cdn.test/vod/index.m3u8')).toBe(false);
  });

  it('prefers the media manifest over a newer subtitle playlist', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/vod/rendition.m3u8', tabId: 4, frameId: 0, at: 10, role: 'manifest', playerKey: 'player-a' },
      { url: 'https://cdn.test/subtitles/subtitles.m3u8', tabId: 4, frameId: 0, at: 30, role: 'manifest', playerKey: 'player-a' },
    ];
    expect(chooseMediaCandidate(candidates, 4, 0, 'player-a')).toBe('https://cdn.test/vod/rendition.m3u8');
  });

  it('falls back to the subtitle playlist when it is the only manifest', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/subtitles.m3u8', tabId: 4, frameId: 0, at: 30, role: 'manifest', playerKey: 'player-a' },
    ];
    expect(chooseMediaCandidate(candidates, 4, 0, 'player-a')).toBe('https://cdn.test/subtitles.m3u8');
  });

  it('does not send raw browser segments as manifest selection hints', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/vod/manifest.mpd', tabId: 4, frameId: 0, at: 1, role: 'manifest', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/old-1.m4s', tabId: 4, frameId: 0, at: 2, role: 'segment', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/new-1.m4s', tabId: 4, frameId: 0, at: 3, role: 'segment', playerKey: 'player-a' },
    ];
    const selection: MediaSelection | undefined = chooseMediaSelection(candidates, 4, 0, 'player-a');
    expect(selection).toEqual({
      source: 'https://cdn.test/vod/manifest.mpd',
      selectedSegments: [],
    });
  });

  it('returns observed SegmentBase representation files with the selected manifest', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/vod/manifest.mpd', tabId: 4, frameId: 0, at: 1, role: 'manifest', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/video_576p.webm', tabId: 4, frameId: 0, at: 2, role: 'unknown', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/audio_en.mp4', tabId: 4, frameId: 0, at: 3, role: 'unknown', playerKey: 'player-a' },
    ];
    expect(chooseMediaSelection(candidates, 4, 0, 'player-a')).toEqual({
      source: 'https://cdn.test/vod/manifest.mpd',
      selectedSegments: ['https://cdn.test/vod/audio_en.mp4', 'https://cdn.test/vod/video_576p.webm'],
    });
  });

  it('keeps only the newest active representation for each media kind', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/vod/manifest.mpd', tabId: 4, frameId: 0, at: 1, role: 'manifest', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/video_480p.webm', tabId: 4, frameId: 0, at: 2, role: 'unknown', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/audio_en.m4a', tabId: 4, frameId: 0, at: 3, role: 'unknown', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/video_576p.webm', tabId: 4, frameId: 0, at: 4, role: 'unknown', playerKey: 'player-a' },
    ];
    expect(chooseMediaSelection(candidates, 4, 0, 'player-a')).toEqual({
      source: 'https://cdn.test/vod/manifest.mpd',
      selectedSegments: ['https://cdn.test/vod/video_576p.webm', 'https://cdn.test/vod/audio_en.m4a'],
    });
  });

  it('puts recent child manifests before segment hints for a likely master', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/vod/master.m3u8', tabId: 4, frameId: 0, at: 1, role: 'manifest', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/high.m3u8', tabId: 4, frameId: 0, at: 10, role: 'manifest', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/active.m3u8', tabId: 4, frameId: 0, at: 20, role: 'manifest', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/audio/en.m3u8', tabId: 4, frameId: 0, at: 30, role: 'manifest', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/segment-active.ts', tabId: 4, frameId: 0, at: 40, role: 'segment', playerKey: 'player-a' },
      { url: 'https://cdn.test/vod/segment-high.ts', tabId: 4, frameId: 0, at: 39, role: 'segment', playerKey: 'player-a' },
    ];
    expect(chooseMediaSelection(candidates, 4, 0, 'player-a')).toEqual({
      source: 'https://cdn.test/vod/master.m3u8',
      selectedSegments: [
        'https://cdn.test/vod/audio/en.m3u8',
        'https://cdn.test/vod/active.m3u8',
        'https://cdn.test/vod/high.m3u8',
      ],
    });
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

  it('uses one unowned frame-zero worker source for a sole child player', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/vod/xgplayer-demo.mp4', tabId: 4, frameId: 0, documentId: 'top-doc', at: 100, role: 'unknown' },
    ];
    const players: MediaPlayerEvidence[] = [
      { playerKey: 'child-player', tabId: 4, frameId: 3, documentId: 'child-doc', at: 100, active: true, hovered: false, playing: true, visible: true },
    ];
    expect(chooseWorkerMediaSelection(candidates, players, 4, 3, 'child-player', 'child-doc', 100)).toEqual({
      source: 'https://cdn.test/vod/xgplayer-demo.mp4',
      selectedSegments: [],
    });
  });

  it('refuses frame-zero worker fallback when another player is active', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/vod/xgplayer-demo.mp4', tabId: 4, frameId: 0, documentId: 'top-doc', at: 100, role: 'unknown' },
    ];
    const players: MediaPlayerEvidence[] = [
      { playerKey: 'child-player', tabId: 4, frameId: 3, documentId: 'child-doc', at: 100, active: true, hovered: false, playing: true, visible: true },
      { playerKey: 'other-player', tabId: 4, frameId: 5, documentId: 'other-doc', at: 100, active: false, hovered: false, playing: true, visible: true },
    ];
    expect(chooseWorkerMediaSelection(candidates, players, 4, 3, 'child-player', 'child-doc', 100)).toBeUndefined();
  });

  it('refuses frame-zero worker fallback when several unowned sources exist', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/vod/one.mp4', tabId: 4, frameId: 0, documentId: 'top-doc', at: 100, role: 'unknown' },
      { url: 'https://cdn.test/vod/two.mp4', tabId: 4, frameId: 0, documentId: 'top-doc', at: 101, role: 'unknown' },
    ];
    const players: MediaPlayerEvidence[] = [
      { playerKey: 'child-player', tabId: 4, frameId: 3, documentId: 'child-doc', at: 101, active: true, hovered: false, playing: true, visible: true },
    ];
    expect(chooseWorkerMediaSelection(candidates, players, 4, 3, 'child-player', 'child-doc', 101)).toBeUndefined();
  });

  it('ignores unclassified worker page assets when selecting a progressive media source', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://cdn.test/assets/player.js', tabId: 4, frameId: 0, documentId: 'top-doc', at: 100, role: 'unknown' },
      { url: 'https://cdn.test/vod/xgplayer-demo.mp4', tabId: 4, frameId: 0, documentId: 'top-doc', at: 101, role: 'unknown', kind: 'video' },
      { url: 'https://cdn.test/assets/chunk.js', tabId: 4, frameId: 0, documentId: 'top-doc', at: 102, role: 'unknown' },
    ];
    const players: MediaPlayerEvidence[] = [
      { playerKey: 'child-player', tabId: 4, frameId: 3, documentId: 'child-doc', at: 102, active: true, hovered: false, playing: true, visible: true },
    ];
    expect(chooseWorkerMediaSelection(candidates, players, 4, 3, 'child-player', 'child-doc', 102)).toEqual({
      source: 'https://cdn.test/vod/xgplayer-demo.mp4',
      selectedSegments: [],
    });
  });

  it('does not resolve an audio endpoint for a video player', () => {
    const candidates: MediaCandidate[] = [
      { url: 'https://www.example.test/s/search/audio/open.mp3', tabId: 4, frameId: 0, at: 100, role: 'unknown', kind: 'audio', playerKey: 'player-a' },
    ];
    expect(chooseMediaSelection(candidates, 4, 0, 'player-a', undefined, 'video')).toBeUndefined();
  });

  it('normalizes chunk URLs by stripping the range parameter', () => {
    const chunkUrl = 'https://rr1---sn-4g5ednle.googlevideo.com/videoplayback?expire=123&sparams=expire%2Cid&id=abc&range=1048576-2097151&rn=1';
    const normalized = normalizeChunkUrl(chunkUrl);
    expect(normalized).toBe('https://rr1---sn-4g5ednle.googlevideo.com/videoplayback?expire=123&sparams=expire%2Cid&id=abc&rn=1');
    expect(normalizeChunkUrl('https://example.com/video.mp4')).toBe('https://example.com/video.mp4');
  });

  it('detects representation tracks and kinds from query parameters and Content-Type', () => {
    const ytVideoUrl = 'https://rr1---sn-4g5ednle.googlevideo.com/videoplayback?expire=123&mime=video%2Fwebm&itag=248';
    const ytAudioUrl = 'https://rr1---sn-4g5ednle.googlevideo.com/videoplayback?expire=123&mime=audio%2Fwebm&itag=251';
    const extensionlessUrl = 'https://stream.example.com/stream/track-1002';

    expect(isLikelyRepresentation(ytVideoUrl)).toBe(true);
    expect(mediaKindFor(ytVideoUrl)).toBe('video');
    expect(isLikelyRepresentation(ytAudioUrl)).toBe(true);
    expect(mediaKindFor(ytAudioUrl)).toBe('audio');

    expect(isLikelyRepresentation(extensionlessUrl, 'video/mp4; codecs="avc1"')).toBe(true);
    expect(mediaKindFor(extensionlessUrl, 'video/mp4; codecs="avc1"')).toBe('video');
    expect(isLikelyRepresentation(extensionlessUrl, 'audio/webm; codecs="opus"')).toBe(true);
    expect(mediaKindFor(extensionlessUrl, 'audio/webm; codecs="opus"')).toBe('audio');
  });

  it('pairs video and companion audio into dual-track selection with normalized URLs', () => {
    const candidates: MediaCandidate[] = [
      {
        url: 'https://rr.googlevideo.com/videoplayback?id=yt1&mime=video%2Fwebm&range=0-1000',
        tabId: 4,
        frameId: 0,
        at: 100,
        role: 'unknown',
        kind: 'video',
        playerKey: 'player-a',
      },
      {
        url: 'https://rr.googlevideo.com/videoplayback?id=yt1&mime=audio%2Fwebm&range=0-500',
        tabId: 4,
        frameId: 0,
        at: 101,
        role: 'unknown',
        kind: 'audio',
        playerKey: 'player-a',
      },
    ];

    const selection = chooseMediaSelection(candidates, 4, 0, 'player-a', undefined, 'video');
    expect(selection).toEqual({
      source: 'https://rr.googlevideo.com/videoplayback?id=yt1&mime=video%2Fwebm',
      selectedSegments: [],
      companionAudio: 'https://rr.googlevideo.com/videoplayback?id=yt1&mime=audio%2Fwebm',
    });
  });

  it('strictly isolates dual-track media by playerKey when multiple players exist on the same page', () => {
    const candidates: MediaCandidate[] = [
      // Player A video + audio
      {
        url: 'https://cdn.example.com/stream-a-video?range=0-1000',
        tabId: 4,
        frameId: 0,
        at: 100,
        role: 'unknown',
        kind: 'video',
        playerKey: 'player-a',
      },
      {
        url: 'https://cdn.example.com/stream-a-audio?range=0-500',
        tabId: 4,
        frameId: 0,
        at: 101,
        role: 'unknown',
        kind: 'audio',
        playerKey: 'player-a',
      },
      // Player B video + audio
      {
        url: 'https://cdn.example.com/stream-b-video?range=0-2000',
        tabId: 4,
        frameId: 0,
        at: 102,
        role: 'unknown',
        kind: 'video',
        playerKey: 'player-b',
      },
      {
        url: 'https://cdn.example.com/stream-b-audio?range=0-800',
        tabId: 4,
        frameId: 0,
        at: 103,
        role: 'unknown',
        kind: 'audio',
        playerKey: 'player-b',
      },
    ];

    // Selecting Player A must yield only Player A's streams:
    const selectionA = chooseMediaSelection(candidates, 4, 0, 'player-a', undefined, 'video');
    expect(selectionA).toEqual({
      source: 'https://cdn.example.com/stream-a-video',
      selectedSegments: [],
      companionAudio: 'https://cdn.example.com/stream-a-audio',
    });

    // Selecting Player B must yield only Player B's streams:
    const selectionB = chooseMediaSelection(candidates, 4, 0, 'player-b', undefined, 'video');
    expect(selectionB).toEqual({
      source: 'https://cdn.example.com/stream-b-video',
      selectedSegments: [],
      companionAudio: 'https://cdn.example.com/stream-b-audio',
    });
  });
});
