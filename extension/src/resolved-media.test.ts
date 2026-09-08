// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

function testChrome() {
  const listeners: Record<string, Array<(...args: unknown[]) => unknown>> = {};
  const on = (name: string) => ({
    addListener: vi.fn((fn: (...args: unknown[]) => unknown) => {
      (listeners[name] ??= []).push(fn);
    }),
  });
  const sessionSet = vi.fn().mockResolvedValue(undefined);
  const sessionGet = vi.fn().mockResolvedValue({});
  return {
    listeners,
    sessionSet,
    chrome: {
      storage: {
        local: { get: vi.fn().mockResolvedValue({}), set: vi.fn().mockResolvedValue(undefined) },
        session: { get: sessionGet, set: sessionSet },
      },
      runtime: { id: 'extension-test', onMessage: on('message') },
      webRequest: {
        onResponseStarted: on('response'),
        onHeadersReceived: on('headers'),
        onBeforeRequest: on('beforeRequest'),
      },
      downloads: { onDeterminingFilename: on('filename'), download: vi.fn() },
    },
  };
}

async function flush(times = 5): Promise<void> {
  for (let i = 0; i < times; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('resolved media retention', () => {
  afterEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
  });

  it('reuses the scope record after traffic eviction and isolates tabs', async () => {
    const { listeners, sessionSet, chrome } = testChrome();
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue({ ok: true }) });
    vi.stubGlobal('chrome', chrome);
    vi.stubGlobal('fetch', fetchMock);
    await import('./background');
    await flush();

    const onMessage = listeners.message[0] as (
      message: unknown,
      sender: unknown,
      reply: (response: unknown) => void,
    ) => boolean;
    const sender = { tab: { id: 7 }, frameId: 0, documentId: 'doc1' };
    const manifest = 'https://cdn.example.test/vod/index.m3u8';

    // Direct capture resolves and records the scope source.
    const first = await new Promise<Record<string, unknown>>((resolve) => {
      onMessage({ type: 'media-capture', payload: { source: manifest, pageUrl: 'https://cdn.example.test/watch' } }, sender, resolve as (response: unknown) => void);
    });
    expect(first).toMatchObject({ ok: true });
    expect(sessionSet).toHaveBeenCalledWith({
      'dm-resolved-media': [expect.objectContaining({ scope: '7/0/doc1/', source: manifest })],
    });

    // Traffic ring is empty (evicted): blob capture falls back to the record.
    const second = await new Promise<Record<string, unknown>>((resolve) => {
      onMessage({ type: 'media-capture', payload: { source: 'blob:https://cdn.example.test/player', pageUrl: 'https://cdn.example.test/watch' } }, sender, resolve as (response: unknown) => void);
    });
    expect(second).toMatchObject({ ok: true });
    const bodies = fetchMock.mock.calls
      .filter(([url]) => String(url).endsWith('/v1/capture'))
      .map(([, init]) => JSON.parse(String((init as { body: string }).body)).payload as Record<string, unknown>);
    expect(bodies).toHaveLength(2);
    expect(bodies[1]?.source).toBe(manifest);

    // Another tab shares nothing.
    const third = await new Promise<Record<string, unknown>>((resolve) => {
      onMessage(
        { type: 'media-capture', payload: { source: 'blob:https://cdn.example.test/other', pageUrl: 'https://cdn.example.test/watch' } },
        { tab: { id: 8 }, frameId: 0, documentId: 'doc1' },
        resolve as (response: unknown) => void,
      );
    });
    expect(third).toMatchObject({ ok: false, error: 'no acquirable source for this media' });
  });
});
