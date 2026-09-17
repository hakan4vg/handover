// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { diffSnapshots, elementKey, hashString, playerStateSummary, signatureOf, snapshotElement, wrapperChain } from './trace-page';

function element(html: string): Element {
  document.body.innerHTML = html;
  return document.body.firstElementChild as Element;
}

describe('trace snapshots', () => {
  it('captures structure, clamps classes and strips URL tokens', () => {
    const wrap = element('<div class="player-wrap"><video id="v" class="c1 c2 c3 c4 c5 c6 c7" src="https://cdn.example.test/video.mp4?token=secret&exp=123"></video></div>');
    const video = wrap.querySelector('video') as HTMLVideoElement;
    const source = document.createElement('source');
    source.setAttribute('src', 'https://cdn.example.test/alt.mp4?x=1');
    source.setAttribute('type', 'video/mp4');
    video.appendChild(source);

    const tree = snapshotElement(video);
    expect(tree?.tag).toBe('video');
    expect(tree?.path).toBe('video');
    expect(tree?.children[0]?.tag).toBe('source');
    expect(tree?.children[0]?.path).toBe('video>source');
    // class lists are capped
    expect(tree?.cls?.length).toBe(5);
    // URL attributes keep origin+path but never the one-use query
    expect(tree?.attrs?.src).toContain('cdn.example.test/video.mp4');
    expect(tree?.attrs?.src).not.toContain('token');
  });

  it('bounds node count so a huge wrapper cannot blow up a record', () => {
    const big = element(`<div>${'<span>x</span>'.repeat(40)}</div>`);
    const tree = snapshotElement(big, 20);
    const count = (node: typeof tree): number => (node ? 1 + node.children.reduce((sum, child) => sum + count(child), 0) : 0);
    expect(count(tree)).toBeLessThanOrEqual(21);
  });

  it('treats animated style numbers and rotating URL tokens as no change', () => {
    const a = element('<div id="x" style="transform: translateX(12.5px)"></div>');
    a.innerHTML = '<b>hello</b>';
    const b = element('<div id="x" style="transform: translateX(432.75px)"></div>');
    b.innerHTML = '<b>hello</b>';
    expect(signatureOf(snapshotElement(a))).toBe(signatureOf(snapshotElement(b)));

    const c = element('<div id="y" src="https://cdn.test/a.ts?token=one"></div>');
    const d = element('<div id="y" src="https://cdn.test/a.ts?token=two"></div>');
    expect(signatureOf(snapshotElement(c))).toBe(signatureOf(snapshotElement(d)));
  });

  it('reports a structural change and names the paths that moved', () => {
    const before = element('<div id="p"><video></video></div>');
    const snapBefore = snapshotElement(before);
    const after = element('<div id="p"><video></video><button class="cc"></button></div>');
    const snapAfter = snapshotElement(after);
    const diff = diffSnapshots(snapBefore, snapAfter);
    expect(diff.addedCount).toBeGreaterThan(0);
    expect(diff.added.some((path) => path.includes('button'))).toBe(true);
    expect(diff.removedCount).toBe(0);

    const diffBack = diffSnapshots(snapAfter, snapBefore);
    expect(diffBack.removedCount).toBe(1);
    expect(diffBack.addedCount).toBe(0);
  });

  it('summarizes player state and the wrapper chain', () => {
    const wrap = element('<div class="html5-video-player" id="player"><div class="inner"><video id="v" muted></video></div></div>');
    const video = wrap.querySelector('video') as HTMLVideoElement;
    video.muted = true;
    const state = playerStateSummary(video);
    expect(state.tag).toBe('video');
    expect(state.muted).toBe(true);
    expect(state.currentSrcKind).toBe('none');
    expect(state.ownShadow).toBe('none');

    const chain = wrapperChain(video, 4);
    expect(chain.map((item) => item.tag)).toEqual(['div', 'div']);
    expect(chain[1]?.id).toBe('player');
    expect(chain[1]?.cls).toContain('html5-video-player');
  });

  it('gives every element a stable identity within the page world', () => {
    const one = element('<div id="q"></div>');
    const two = element('<div id="r"></div>');
    expect(elementKey(one)).toBe(elementKey(one));
    expect(elementKey(one)).not.toBe(elementKey(two));
    expect(hashString('abc')).toBe(hashString('abc'));
    expect(hashString('abc')).not.toBe(hashString('abd'));
  });
});
