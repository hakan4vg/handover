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
      },
      runtime: { id: 'extension-test', onMessage: on('message') },
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

afterEach(() => {
  vi.resetModules();
  vi.unstubAllGlobals();
});

describe('background media filters', () => {
  it('uses complete-object headers while ignoring manifest and fragment lengths', async () => {
    const { chrome, listeners } = chromeMock({
      'dm-media-filters': { minimumSizeBytes: 1_000, excludedFileTypes: ['gif'] },
    });
    vi.stubGlobal('chrome', chrome);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue({ ok: true }) }));
    await import('./background');
    await flush();

    const headers = listeners.headers[0] as (details: unknown) => unknown;
    headers({
      tabId: 7,
      frameId: 0,
      documentId: 'doc-1',
      type: 'media',
      url: 'https://cdn.test/clip.mp4',
      statusCode: 206,
      responseHeaders: [
        { name: 'Content-Type', value: 'video/mp4' },
        { name: 'Content-Range', value: 'bytes 0-99/900' },
        { name: 'Content-Length', value: '100' },
      ],
    });
    headers({
      tabId: 7,
      frameId: 0,
      documentId: 'doc-1',
      type: 'media',
      url: 'https://cdn.test/clip.m4s',
      statusCode: 200,
      responseHeaders: [
        { name: 'Content-Type', value: 'video/mp4' },
        { name: 'Content-Length', value: '1' },
      ],
    });
    headers({
      tabId: 7,
      frameId: 0,
      documentId: 'doc-1',
      type: 'xmlhttprequest',
      url: 'https://cdn.test/index.m3u8',
      statusCode: 200,
      responseHeaders: [
        { name: 'Content-Type', value: 'application/vnd.apple.mpegurl' },
        { name: 'Content-Length', value: '1' },
      ],
    });
    headers({
      tabId: 7,
      frameId: 0,
      documentId: 'doc-1',
      type: 'media',
      url: 'https://cdn.test/animation.gif',
      statusCode: 200,
      responseHeaders: [
        { name: 'Content-Type', value: 'image/gif' },
        { name: 'Content-Length', value: '1' },
      ],
    });

    const onMessage = listeners.message[0] as (message: unknown, sender: unknown, reply: (value: unknown) => void) => boolean;
    const check = (source: string) => new Promise<Record<string, unknown>>((resolve) => {
      onMessage(
        { type: 'check-media-filters', payload: { source } },
        { tab: { id: 7 }, frameId: 0, documentId: 'doc-1' },
        resolve as (value: unknown) => void,
      );
    });

    expect(await check('https://cdn.test/clip.mp4')).toMatchObject({ ok: true, allowed: false, reason: 'below-minimum', totalBytes: 900 });
    expect(await check('https://cdn.test/clip.m4s')).toMatchObject({ ok: true, allowed: true });
    expect(await check('https://cdn.test/index.m3u8')).toMatchObject({ ok: true, allowed: true });
    expect(await check('https://cdn.test/animation.gif')).toMatchObject({ ok: true, allowed: false, type: 'gif' });
  });

  it('keeps media filters independent when resident policy refreshes', async () => {
    const { chrome, listeners } = chromeMock({
      'dm-media-filters': { minimumSizeBytes: 2_000, excludedFileTypes: ['gif'] },
    });
    const fetchBridge = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ policy: { interceptDownloads: false, showMediaButtons: true, excludedSites: ['example.test'] } }),
    });
    vi.stubGlobal('chrome', chrome);
    vi.stubGlobal('fetch', fetchBridge);
    await import('./background');
    await flush();

    const onMessage = listeners.message[0] as (message: unknown, sender: unknown, reply: (value: unknown) => void) => boolean;
    const policy = await new Promise<Record<string, unknown>>((resolve) => {
      onMessage({ type: 'get-policy', includeMediaFilters: true }, {}, resolve as (value: unknown) => void);
    });
    expect(policy.mediaFilters).toEqual({ minimumSizeBytes: 2_000, excludedFileTypes: ['gif'] });
    expect(chrome.storage.local.set).not.toHaveBeenCalledWith(expect.objectContaining({ 'dm-media-filters': expect.anything() }));
  });
});
