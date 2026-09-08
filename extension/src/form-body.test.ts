// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

function testChrome() {
  const listeners: Record<string, Array<(...args: unknown[]) => unknown>> = {};
  const on = (name: string) => ({
    addListener: vi.fn((fn: (...args: unknown[]) => unknown) => {
      (listeners[name] ??= []).push(fn);
    }),
  });
  return {
    listeners,
    chrome: {
      storage: { local: { get: vi.fn().mockResolvedValue({}), set: vi.fn().mockResolvedValue(undefined) } },
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

function bridge() {
  return vi.fn().mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue({ ok: true }) });
}

function capturePayloads(fetchMock: ReturnType<typeof vi.fn>): Array<Record<string, unknown>> {
  return fetchMock.mock.calls
    .filter(([url]) => String(url).endsWith('/v1/capture'))
    .map(([, init]) => JSON.parse(String((init as { body: string }).body)).payload as Record<string, unknown>);
}

async function flush(times = 5): Promise<void> {
  for (let i = 0; i < times; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

function postDetails(url: string, fields: Record<string, string>, tabId = 7) {
  const formData: Record<string, string[]> = {};
  for (const [key, value] of Object.entries(fields)) formData[key] = [value];
  return { tabId, frameId: 0, method: 'POST', url, requestBody: { formData } };
}

function fileItem(url: string) {
  return { url, finalUrl: url, filename: 'out/export.zip', referrer: 'https://shop.example.test/cart' };
}

describe('form body observation', () => {
  afterEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
  });

  it('queues concurrent same-URL posts FIFO instead of overwriting', async () => {
    const { listeners, chrome } = testChrome();
    const fetchMock = bridge();
    vi.stubGlobal('chrome', chrome);
    vi.stubGlobal('fetch', fetchMock);
    await import('./background');
    await flush();

    const beforeRequest = listeners.beforeRequest[0] as (details: unknown) => undefined;
    beforeRequest(postDetails('https://shop.example.test/export', { order: 'one' }));
    beforeRequest(postDetails('https://shop.example.test/export', { order: 'two' }));

    const onFilename = listeners.filename[0] as (item: unknown, suggest: () => void) => boolean;
    const suggest = vi.fn();
    onFilename(fileItem('https://shop.example.test/export'), suggest);
    await flush();
    onFilename(fileItem('https://shop.example.test/export'), suggest);
    await flush();

    const payloads = capturePayloads(fetchMock);
    expect(payloads).toHaveLength(2);
    expect(payloads[0]).toMatchObject({ method: 'POST', postBody: 'order=one' });
    expect(payloads[1]).toMatchObject({ method: 'POST', postBody: 'order=two' });
  });

  it('ignores non-POST observations and sends GET captures without a body', async () => {
    const { listeners, chrome } = testChrome();
    const fetchMock = bridge();
    vi.stubGlobal('chrome', chrome);
    vi.stubGlobal('fetch', fetchMock);
    await import('./background');
    await flush();

    const beforeRequest = listeners.beforeRequest[0] as (details: unknown) => undefined;
    beforeRequest({ ...postDetails('https://shop.example.test/export', { order: 'one' }), method: 'GET', requestBody: undefined });

    const onFilename = listeners.filename[0] as (item: unknown, suggest: () => void) => boolean;
    onFilename(fileItem('https://shop.example.test/export'), vi.fn());
    await flush();

    const payloads = capturePayloads(fetchMock);
    expect(payloads).toHaveLength(1);
    expect(payloads[0]).not.toHaveProperty('postBody');
    expect(payloads[0]).not.toHaveProperty('method');
  });
});
