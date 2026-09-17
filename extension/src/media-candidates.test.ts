import { describe, expect, it } from 'vitest';
import {
  acquireableSource,
  chooseMediaSelection,
  isByteRangeFragment,
  planMediaCapture,
  rankedMediaCandidates,
  type MediaCandidate,
  type MediaPlayerEvidence,
} from './media-candidates';

const T = 1_000_000;

function candidate(over: Partial<MediaCandidate>): MediaCandidate {
  return { url: 'https://cdn.test/a.mp4', tabId: 1, frameId: 0, at: T, role: 'unknown', ...over };
}

function player(over: Partial<MediaPlayerEvidence>): MediaPlayerEvidence {
  return {
    playerKey: 'player-1',
    tabId: 1,
    frameId: 0,
    at: T,
    active: true,
    hovered: true,
    playing: true,
    visible: true,
    ...over,
  };
}

describe('byte-range fragments are not acquire-able', () => {
  it('detects signed range parameters and path segments', () => {
    // YouTube signs the range into the URL (`sparams` lists it): the URL names
    // a slice and cannot be turned back into the object.
    expect(isByteRangeFragment('https://rr1---sn-x.googlevideo.com/videoplayback?expire=1&rn=2&range=0-62000&sparams=expire%2Cid%2Crange&mime=video%2Fmp4')).toBe(true);
    // Vimeo's fragments carry the range as a path segment.
    expect(isByteRangeFragment('https://vod-adaptive-ak.vimeocdn.com/exp=1~acl=%2Fx~hmac=ab/uuid/psid=1/v2/range/prot/cHI9NTQw/avf/x.mp4?pathsig=1~a')).toBe(true);
    // An unsigned range can simply be stripped, so it is not treated as a fragment.
    expect(isByteRangeFragment('https://cdn.test/video.mp4?range=0-1000')).toBe(false);
    expect(isByteRangeFragment('https://cdn.test/video.mp4')).toBe(false);
    expect(isByteRangeFragment('https://cdn.test/hls/master.m3u8')).toBe(false);
    expect(isByteRangeFragment('not a url')).toBe(false);
  });

  it('keeps whole objects and manifests acquire-able', () => {
    expect(acquireableSource('https://cdn.test/video.mp4')).toBe(true);
    expect(acquireableSource('https://cdn.test/vod/manifest.mpd')).toBe(true);
    // Unsigned ranges normalize into a whole-object URL…
    expect(acquireableSource('https://cdn.test/video.mp4?range=0-1000')).toBe(true);
    // …signed ones cannot.
    expect(acquireableSource('https://rr1---sn-x.googlevideo.com/videoplayback?range=0-1&sparams=range')).toBe(false);
    expect(acquireableSource('blob:https://x.test/abc')).toBe(false);
  });

  it('never picks a fragment as a source, even when it is the newest observation', () => {
    const fragment = candidate({ url: 'https://cdn.test/v2/range/prot/cHI9NTQw/avf/x.mp4', kind: 'video' });
    const whole = candidate({ url: 'https://cdn.test/whole.mp4', kind: 'video', at: T - 1_000 });
    expect(chooseMediaSelection([fragment, whole], 1, 0, undefined, undefined, 'video')?.source).toBe('https://cdn.test/whole.mp4');
    expect(chooseMediaSelection([fragment], 1, 0, undefined, undefined, 'video')).toBeUndefined();
  });

  it('prefers the manifest over any representation', () => {
    const manifest = candidate({ url: 'https://cdn.test/hls/master.m3u8', role: 'manifest', at: T - 5_000 });
    const whole = candidate({ url: 'https://cdn.test/whole.mp4', kind: 'video', at: T });
    expect(chooseMediaSelection([whole, manifest], 1, 0, undefined, undefined, 'video')?.source).toBe('https://cdn.test/hls/master.m3u8');
  });
});

describe('ownership is frame- and source-scoped', () => {
  it('never attributes another frame\'s media to an embedded player', () => {
    const hostMedia = candidate({ url: 'https://cdn.host/post-video.mp4', frameId: 0, kind: 'video', documentId: 'doc-host' });
    const embed = player({ frameId: 7, documentId: 'doc-embed', currentSrc: 'blob:https://embed.test/x', srcAt: T - 5_000 });
    // The host's video must not be handed to the embed's capture…
    expect(planMediaCapture([hostMedia], [embed], 1, 7, 'player-1', 'doc-embed', T, 'video')).toBeUndefined();
    // …while a top-frame player still sees its own frame's traffic.
    const top = player({ frameId: 0, documentId: 'doc-host', currentSrc: 'blob:https://host.test/y', srcAt: T - 5_000 });
    expect(planMediaCapture([hostMedia], [top], 1, 0, 'player-1', 'doc-host', T, 'video')?.source).toBe('https://cdn.host/post-video.mp4');
  });

  it('credits only traffic newer than the player\'s current source', () => {
    const players = [player({ currentSrc: 'blob:new', srcAt: T - 1_000 })];
    const previousVideo = candidate({ url: 'https://cdn.test/previous-video.mp4', kind: 'video', at: T - 60_000 });
    const currentVideo = candidate({ url: 'https://cdn.test/current-video.mp4', kind: 'video', at: T - 500 });
    expect(planMediaCapture([previousVideo, currentVideo], players, 1, 0, 'player-1', undefined, T, 'video')?.source).toBe('https://cdn.test/current-video.mp4');
    // With only the previous video's URL in the ring there is nothing to hand over.
    expect(planMediaCapture([previousVideo], players, 1, 0, 'player-1', undefined, T, 'video')).toBeUndefined();
  });

  it('ranks candidates inside one frame only', () => {
    const sameFrame = candidate({ url: 'https://cdn.test/a.mp4', kind: 'video', frameId: 3 });
    const topFrame = candidate({ url: 'https://cdn.test/b.mp4', kind: 'video', frameId: 0 });
    expect(rankedMediaCandidates([sameFrame, topFrame], 1, 3, undefined, 'video', T)).toEqual(['https://cdn.test/a.mp4']);
    expect(rankedMediaCandidates([sameFrame, topFrame], 1, 0, undefined, 'video', T)).toEqual(['https://cdn.test/b.mp4']);
  });
});
