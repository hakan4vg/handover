// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

function chromeMock(stored: Record<string, unknown>) {
  const listeners: Record<string, Array<(...args: unknown[]) => unknown>> = {};
  const on = (name: string) => ({
    addListener: vi.fn((listener: (...args: unknown[]) => unknown) => {
      (listeners[name] ??= []).push(listener);
    }),
  });
  return {
    listeners,
    chrome: {
      storage: {
        local: {
          get: vi.fn().mockResolvedValue(stored),
          set: vi.fn().mockResolvedValue(undefined),
        },
        session: {
          get: vi.fn().mockResolvedValue({}),
          set: vi.fn().mockResolvedValue(undefined),
        },
      },
      runtime: { id: 'extension-test', onMessage: on('message') },
      tabs: { sendMessage: vi.fn().mockResolvedValue(null) },
      webRequest: {
        onResponseStarted: on('response'),
        onHeadersReceived: on('headers'),
        onBeforeRequest: on('beforeRequest'),
      },
      downloads: {
        onDeterminingFilename: on('filename'),
        download: vi.fn(),
        pause: vi.fn().mockResolvedValue(undefined),
        resume: vi.fn().mockResolvedValue(undefined),
        cancel: vi.fn().mockResolvedValue(undefined),
      },
    },
  };
}

async function flush(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

function handoffPayload(fetchMock: ReturnType<typeof vi.fn>): Record<string, unknown> | undefined {
  for (const call of fetchMock.mock.calls) {
    const init = call[1] as { body?: string } | undefined;
    if (!init?.body || !init.body.includes('"media-capture"')) continue;
    return (JSON.parse(init.body) as { payload: Record<string, unknown> }).payload;
  }
  return undefined;
}

afterEach(() => {
  vi.resetModules();
  vi.unstubAllGlobals();
});

describe('background media evidence', () => {
  it('drops unusable hints instead of discarding the evidence they arrived with', async () => {
    const { chrome, listeners } = chromeMock({});
    vi.stubGlobal('chrome', chrome);
    const fetchMock = vi.fn().mockImplementation((url: string) => Promise.resolve({
      ok: true,
      json: vi.fn().mockResolvedValue(url.includes('/v1/capture') ? { ok: true, id: 'provisional-1' } : { ok: true }),
    }));
    vi.stubGlobal('fetch', fetchMock);
    await import('./background');
    await flush();

    const onMessage = listeners.message[0] as (message: unknown, sender: unknown, reply: (value: unknown) => void) => boolean;
    const reply = await new Promise<Record<string, unknown>>((resolve) => {
      onMessage(
        {
          type: 'media-capture',
          payload: {
            source: 'https://cdn.test/hls/master.m3u8',
            currentSrc: 'blob:https://page.test/9f0c',
            mediaIdentity: 'media-7:blob:https://page.test/9f0c',
            pageUrl: 'https://page.test/watch',
            playerKind: 'video',
            playerKey: 'player-1',
            pageEvidence: {
              currentSrc: 'blob:https://page.test/9f0c',
              sourceIdentity: 'source-3',
              source: 'https://cdn.test/hls/master.m3u8',
              playerKind: 'video',
              // Steady-state playback fills the hint list with segments; one
              // unusable entry must not invalidate the whole evidence.
              selectedSegments: [
                'https://cdn.test/hls/seg-0001.ts',
                'https://cdn.test/hls/720p/index.m3u8',
              ],
            },
          },
        },
        { tab: { id: 7 }, frameId: 0, documentId: 'doc-1' },
        resolve as (value: unknown) => void,
      );
    });
    await flush();

    expect(reply).toMatchObject({ ok: true });
    const payload = handoffPayload(fetchMock);
    expect(payload).toMatchObject({
      source: 'https://cdn.test/hls/master.m3u8',
      selectedSegments: ['https://cdn.test/hls/720p/index.m3u8'],
    });
  });
});
