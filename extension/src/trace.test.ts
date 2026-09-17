import { describe, expect, it } from 'vitest';
import { classifySample, isRangeFragmentUrl, parseDashManifest, parseHlsManifest, toJsonl } from './trace';
import type { TraceEnvelope } from './trace-schema';

const encoder = new TextEncoder();

describe('range-fragment shape detection', () => {
  it('recognizes Vimeo-style range paths and range query parameters', () => {
    expect(isRangeFragmentUrl('https://vod-adaptive-ak.vimeocdn.com/exp=1~acl=%2Fx~hmac=ab/uuid/psid=1/v2/range/prot/cmFuZ2U9MjEyMTE0MjEtMjg1NzE4MjQ/avf/b92d2162.mp4?pathsig=1~abc')).toBe(true);
    expect(isRangeFragmentUrl('https://cdn.test/media.mp4?range=100-200&token=x')).toBe(true);
    expect(isRangeFragmentUrl('https://cdn.test/segments/seg-0001.ts')).toBe(true);
  });

  it('leaves manifests and plain objects alone', () => {
    expect(isRangeFragmentUrl('https://vod-adaptive-ak.vimeocdn.com/exp=1~acl=%2Fx~hmac=ab/uuid/psid=1/v2/playlist/av/primary/prot/cHI9NTQw/playlist.m3u8?pathsig=1~abc')).toBe(false);
    expect(isRangeFragmentUrl('https://packaged-media.redd.it/abc/pb/m2-res_720p.mp4?m=DASHPlaylist.mpd')).toBe(false);
    expect(isRangeFragmentUrl('https://rr1---sn-x.googlevideo.com/videoplayback?expire=1&mime=video%2Fmp4')).toBe(false);
    expect(isRangeFragmentUrl('not a url')).toBe(false);
  });
});

describe('probe sample classification', () => {
  it('recognizes an HLS master and resolves the first child variant', () => {
    const text = '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nvideo-800.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2000000\nvideo-2000.m3u8\n';
    const verdict = classifySample(encoder.encode(text), 'application/vnd.apple.mpegurl', 'https://cdn.test/hls/master.m3u8');
    expect(verdict.bodyKind).toBe('hls');
    expect(verdict.manifest?.format).toBe('hls');
    expect(verdict.manifest?.variants).toBe(2);
    expect(verdict.manifest?.firstChild).toBe('https://cdn.test/hls/video-800.m3u8');
  });

  it('recognizes a DASH manifest and counts representations', () => {
    const text = '<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><BaseURL>video-1080.mp4</BaseURL><Period><AdaptationSet><Representation id="1"/><Representation id="2"/></AdaptationSet></Period></MPD>';
    const verdict = classifySample(encoder.encode(text), 'application/dash+xml', 'https://cdn.test/dash/manifest.mpd');
    expect(verdict.bodyKind).toBe('dash');
    expect(verdict.manifest?.variants).toBe(2);
    expect(verdict.manifest?.firstChild).toBe('https://cdn.test/dash/video-1080.mp4');
  });

  it('recognizes a login page served with 200', () => {
    const verdict = classifySample(encoder.encode('<!doctype html><html><body>Please sign in</body></html>'), 'text/html', 'https://hoster.test/file');
    expect(verdict.bodyKind).toBe('html');
  });

  it('recognizes fMP4 (with moof/mdat detection) and MPEG-TS sync bytes', () => {
    const mp4 = new Uint8Array(24);
    mp4.set([0, 0, 0, 16], 0);
    mp4.set(encoder.encode('ftyp'), 4);
    mp4.set(encoder.encode('moof'), 12);
    const mp4Verdict = classifySample(mp4, 'video/mp4', 'https://cdn.test/v.mp4');
    expect(mp4Verdict.bodyKind).toBe('mp4');
    expect(mp4Verdict.sniff).toContain('moof:y');

    const ts = new Uint8Array(400);
    ts[0] = 0x47;
    ts[188] = 0x47;
    ts[376] = 0x47;
    expect(classifySample(ts, 'video/mp2t', 'https://cdn.test/s.ts').bodyKind).toBe('mpegts');
  });

  it('recognizes a JSON error body and an empty body', () => {
    expect(classifySample(encoder.encode('{"error": "forbidden"}'), 'application/json', 'https://cdn.test/x').bodyKind).toBe('json');
    expect(classifySample(new Uint8Array(0), '', 'https://cdn.test/x').bodyKind).toBe('empty');
  });
});

describe('manifest parsing helpers', () => {
  it('marks a live playlist (no ENDLIST) and a VOD playlist apart', () => {
    const live = parseHlsManifest('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:4,\nseg1.ts\n', 'https://cdn.test/live.m3u8');
    expect(live?.live).toBe(true);
    const vod = parseHlsManifest('#EXTM3U\n#EXTINF:4,\nseg1.ts\n#EXT-X-ENDLIST\n', 'https://cdn.test/vod.m3u8');
    expect(vod?.live).toBe(false);
    expect(vod?.segments).toBe(1);
  });

  it('returns undefined for text that is not a manifest', () => {
    expect(parseHlsManifest('hello', 'https://x.test/')).toBeUndefined();
    expect(parseDashManifest('hello', 'https://x.test/')).toBeUndefined();
  });
});

describe('jsonl export', () => {
  it('writes a header line followed by one line per event', () => {
    const event: TraceEnvelope = {
      t: 1,
      kind: 'capture.click',
      phase: 'acquirement',
      sw: 'sw-test',
      swStartedAt: 0,
      payload: { buttonAgeMs: 1200 },
    };
    const jsonl = toJsonl([event]);
    const lines = jsonl.split('\n');
    expect(lines).toHaveLength(2);
    expect(JSON.parse(lines[0] as string).kind).toBe('trace.header');
    expect(JSON.parse(lines[1] as string).payload.buttonAgeMs).toBe(1200);
  });
});
