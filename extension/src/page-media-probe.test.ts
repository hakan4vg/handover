// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

/** jsdom's window.postMessage leaves MessageEvent.source null, which the
 *  bridge (correctly) rejects; dispatch the event with an explicit source so
 *  the test exercises the same guard production uses. */
function sendProbe(requestId: string, url: string, depth: number, range?: 'ranged' | 'none'): void {
  window.dispatchEvent(new MessageEvent('message', {
    data: { marker: 'download-manager-media-v1', type: 'dm-trace-probe', requestId, url, depth, ...(range ? { range } : {}) },
    origin: location.origin,
    source: window,
  }));
}

function awaitProbeAnswer(requestId: string): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('bridge never answered')), 2000);
    window.addEventListener('message', (event) => {
      const data = event.data as Record<string, unknown> | null;
      if (!data || data.type !== 'dm-trace-probe-response' || data.requestId !== requestId) return;
      clearTimeout(timer);
      resolve(data);
    });
  });
}

/** The page bridge's probe channel: content script posts a request, the MAIN
 *  world answers with the raw fetch result. This pins the contract that broke
 *  silently in the first live trace (a message-type mismatch made every page
 *  probe resolve as "no response"). */
describe('page bridge trace probes', () => {
  afterEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
  });

  it('answers a dm-trace-probe request with a bounded, classified sample', async () => {
    const body = new TextEncoder().encode('#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:4,\nseg1.ts\n');
    const fetchMock = vi.fn().mockResolvedValue(new Response(body, {
      status: 206,
      headers: { 'content-type': 'application/x-mpegURL', 'content-range': 'bytes 0-39/40' },
    }));
    vi.stubGlobal('fetch', fetchMock);

    await import('./page-media');

    const answered = awaitProbeAnswer('probe-1');
    sendProbe('probe-1', 'https://cdn.example.test/master.m3u8', 0, 'ranged');
    const data = await answered;
    const result = data.result as Record<string, unknown>;
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.status).toBe(206);
    expect(result.ok).toBe(true);
    expect(result.requestRange).toBe('ranged');
    expect(result.bytesRead).toBe(body.byteLength);
    // The raw sample travels with the answer; classification happens in the worker.
    expect(typeof result.sampleText).toBe('string');
    expect(String(result.sampleText)).toContain('#EXTM3U');

    const headers = fetchMock.mock.calls[0]?.[1] as RequestInit | undefined;
    expect((headers?.headers as Record<string, string> | undefined)?.Range).toBe('bytes=0-65535');
  });

  it('omits the Range header when the probe asks for a plain GET', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('ok', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await import('./page-media');

    const answered = awaitProbeAnswer('probe-2');
    sendProbe('probe-2', 'https://cdn.example.test/video.mp4', 0, 'none');
    const data = await answered;
    expect((data.result as Record<string, unknown>).requestRange).toBe('plain');
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit | undefined;
    expect(init?.headers).toBeUndefined();
  });

  it('answers even when the URL itself is rejected', async () => {
    vi.stubGlobal('fetch', vi.fn());
    await import('./page-media');

    const answered = awaitProbeAnswer('probe-3');
    sendProbe('probe-3', 'file:///etc/passwd', 0);
    const data = await answered;
    expect((data.result as Record<string, unknown>).stage).toBe('validate');
  });
});
