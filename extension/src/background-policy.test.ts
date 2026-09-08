// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_POLICY } from './shared';

function chromeMock(storageSet: ReturnType<typeof vi.fn>, storageGet: ReturnType<typeof vi.fn> = vi.fn().mockResolvedValue({})) {
  const event = () => ({ addListener: vi.fn() });
  const onMessage = { addListener: vi.fn() };
  return {
    storage: {
      local: {
        get: storageGet,
        set: storageSet,
      },
    },
    runtime: {
      id: 'extension-test',
      onMessage,
    },
    webRequest: {
      onResponseStarted: event(),
      onHeadersReceived: event(),
      onBeforeRequest: event(),
    },
    downloads: {
      onDeterminingFilename: event(),
      download: vi.fn(),
    },
    __onMessage: onMessage,
  };
}

function bridgeMock(payload: unknown = { ok: true }): ReturnType<typeof vi.fn> {
  return vi.fn().mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue(payload) });
}

function bridgeCalls(fetchBridge: ReturnType<typeof vi.fn>, route: string): unknown[][] {
  return fetchBridge.mock.calls.filter(([url]) => String(url).endsWith(route));
}

afterEach(() => {
  vi.resetModules();
  vi.unstubAllGlobals();
});

describe('background policy persistence', () => {
  it('reports storage failure and rolls back the in-memory policy', async () => {
    const storageSet = vi.fn().mockRejectedValue(new Error('storage unavailable'));
    const chrome = chromeMock(storageSet);
    vi.stubGlobal('chrome', chrome);
    await import('./background');
    await new Promise((resolve) => setTimeout(resolve, 0));

    const listener = chrome.__onMessage.addListener.mock.calls.find(([candidate]) => typeof candidate === 'function')?.[0];
    expect(listener).toBeTypeOf('function');
    const response = await new Promise<Record<string, unknown>>((resolve) => {
      listener({ type: 'update-policy', patch: { showMediaButtons: !DEFAULT_POLICY.showMediaButtons } }, {}, resolve);
    });

    expect(response.ok).toBe(false);
    expect(response.error).toContain('storage unavailable');
    expect(response.policy).toEqual(DEFAULT_POLICY);
  });

  it('reports storage failure when loading the policy', async () => {
    const storageGet = vi.fn().mockRejectedValue(new Error('storage read unavailable'));
    const chrome = chromeMock(vi.fn().mockResolvedValue(undefined), storageGet);
    vi.stubGlobal('chrome', chrome);
    await import('./background');
    await new Promise((resolve) => setTimeout(resolve, 0));

    const listener = chrome.__onMessage.addListener.mock.calls.find(([candidate]) => typeof candidate === 'function')?.[0];
    expect(listener).toBeTypeOf('function');
    const response = await new Promise<Record<string, unknown>>((resolve) => {
      listener({ type: 'get-policy' }, {}, resolve);
    });

    expect(response.ok).toBe(false);
    expect(response.error).toContain('storage read unavailable');
    expect(response.policy).toEqual(DEFAULT_POLICY);
  });

  it('refreshes the cached policy from the resident app on each policy read', async () => {
    const storageSet = vi.fn().mockResolvedValue(undefined);
    const chrome = chromeMock(storageSet);
    const fetchBridge = bridgeMock({
      ok: true,
      policy: { interceptDownloads: false, showMediaButtons: true, excludedSites: ['example.test'] },
    });
    vi.stubGlobal('chrome', chrome);
    vi.stubGlobal('fetch', fetchBridge);
    await import('./background');
    await new Promise((resolve) => setTimeout(resolve, 0));

    const listener = chrome.__onMessage.addListener.mock.calls.find(([candidate]) => typeof candidate === 'function')?.[0];
    expect(listener).toBeTypeOf('function');
    const response = await new Promise<Record<string, unknown>>((resolve) => {
      listener({ type: 'get-policy' }, {}, resolve);
    });

    expect(response).toEqual({
      ok: true,
      policy: { interceptDownloads: false, showMediaButtons: true, excludedSites: ['example.test'] },
    });
    expect(bridgeCalls(fetchBridge, '/v1/policy').length).toBeGreaterThanOrEqual(2);
    expect(storageSet).toHaveBeenCalledWith({
      ['dm-policy']: { interceptDownloads: false, showMediaButtons: true, excludedSites: ['example.test'] },
    });
  });

  it('waits for policy before forwarding capture and honors an excluded page', async () => {
    let resolveStored: (value: unknown) => void = () => undefined;
    const stored = new Promise((resolve) => { resolveStored = resolve; });
    const storageGet = vi.fn().mockReturnValue(stored);
    const chrome = chromeMock(vi.fn().mockResolvedValue(undefined), storageGet);
    const fetchBridge = bridgeMock();
    vi.stubGlobal('chrome', chrome);
    vi.stubGlobal('fetch', fetchBridge);
    await import('./background');

    const listener = chrome.__onMessage.addListener.mock.calls.find(([candidate]) => typeof candidate === 'function')?.[0];
    expect(listener).toBeTypeOf('function');
    let resolveReply: (value: Record<string, unknown>) => void = () => undefined;
    const reply = new Promise<Record<string, unknown>>((resolve) => { resolveReply = resolve; });
    listener({
      type: 'ordinary-capture',
      payload: { source: 'https://cdn.example.test/file.zip', pageUrl: 'https://www.example.test/downloads' },
    }, {}, resolveReply);
    await Promise.resolve();
    expect(bridgeCalls(fetchBridge, '/v1/capture')).toHaveLength(0);

    resolveStored({ ["dm-policy"]: { interceptDownloads: true, showMediaButtons: true, excludedSites: ['example.test'] } });
    const response = await reply;
    expect(response.ok).toBe(false);
    expect(response.error).toContain('site excluded');
    expect(bridgeCalls(fetchBridge, '/v1/capture')).toHaveLength(0);
  });

  it('waits for policy before forwarding browser fallback and honors an excluded referrer', async () => {
    let resolveStored: (value: unknown) => void = () => undefined;
    const stored = new Promise((resolve) => { resolveStored = resolve; });
    const storageGet = vi.fn().mockReturnValue(stored);
    const chrome = chromeMock(vi.fn().mockResolvedValue(undefined), storageGet);
    const fetchBridge = bridgeMock();
    vi.stubGlobal('chrome', chrome);
    vi.stubGlobal('fetch', fetchBridge);

    await import('./background');
    const determine = chrome.downloads.onDeterminingFilename.addListener.mock.calls[0]?.[0];
    expect(determine).toBeTypeOf('function');
    let suggestions = 0;
    const result = determine({
      id: 7,
      url: 'https://cdn.example.test/file.zip',
      finalUrl: 'https://cdn.example.test/file.zip',
      filename: 'file.zip',
      referrer: 'https://www.example.test/page',
      byExtensionId: undefined,
    }, () => { suggestions += 1; });
    expect(result).toBe(true);
    await Promise.resolve();
    expect(bridgeCalls(fetchBridge, '/v1/capture')).toHaveLength(0);

    resolveStored({ ["dm-policy"]: { interceptDownloads: true, showMediaButtons: true, excludedSites: ['example.test'] } });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(suggestions).toBe(1);
    expect(bridgeCalls(fetchBridge, '/v1/capture')).toHaveLength(0);
  });

  it('waits for policy before forwarding media and keeps media independent from ordinary interception', async () => {
    let resolveStored: (value: unknown) => void = () => undefined;
    const stored = new Promise((resolve) => { resolveStored = resolve; });
    const chrome = chromeMock(vi.fn().mockResolvedValue(undefined), vi.fn().mockReturnValue(stored));
    const fetchBridge = bridgeMock();
    vi.stubGlobal('chrome', chrome);
    vi.stubGlobal('fetch', fetchBridge);

    await import('./background');
    const listener = chrome.__onMessage.addListener.mock.calls.find(([candidate]) => typeof candidate === 'function')?.[0];
    expect(listener).toBeTypeOf('function');
    let resolveReply: (value: Record<string, unknown>) => void = () => undefined;
    const excludedReply = new Promise<Record<string, unknown>>((resolve) => { resolveReply = resolve; });
    listener({ type: 'media-capture', payload: { source: 'https://cdn.example.test/video.mp4', pageUrl: 'https://www.example.test/watch', media: true } }, { tab: { id: 12 }, frameId: 0 }, resolveReply);
    await Promise.resolve();
    expect(bridgeCalls(fetchBridge, '/v1/capture')).toHaveLength(0);

    resolveStored({ ['dm-policy']: { interceptDownloads: false, showMediaButtons: true, excludedSites: ['example.test'] } });
    const excluded = await excludedReply;
    expect(excluded.ok).toBe(false);
    expect(excluded.error).toContain('site excluded');
    expect(bridgeCalls(fetchBridge, '/v1/capture')).toHaveLength(0);

    fetchBridge.mockClear();
    const allowed = await new Promise<Record<string, unknown>>((resolve) => {
      listener({ type: 'media-capture', payload: { source: 'https://cdn.example.test/video.mp4', pageUrl: 'https://other.example.test/watch', media: true } }, { tab: { id: 12 }, frameId: 0 }, resolve);
    });
    expect(allowed.ok).toBe(true);
    expect(bridgeCalls(fetchBridge, '/v1/capture')).toHaveLength(1);
  });

  it('rejects media capture when media buttons are disabled', async () => {
    const fetchBridge = bridgeMock();
    const storageGet = vi.fn().mockResolvedValue({ ['dm-policy']: { interceptDownloads: true, showMediaButtons: false, excludedSites: [] } });
    const chrome = chromeMock(vi.fn().mockResolvedValue(undefined), storageGet);
    vi.stubGlobal('chrome', chrome);
    vi.stubGlobal('fetch', fetchBridge);

    await import('./background');
    const listener = chrome.__onMessage.addListener.mock.calls.find(([candidate]) => typeof candidate === 'function')?.[0];
    expect(listener).toBeTypeOf('function');
    const response = await new Promise<Record<string, unknown>>((resolve) => {
      listener({ type: 'media-capture', payload: { source: 'https://cdn.example.test/video.mp4', pageUrl: 'https://www.example.test/watch', media: true } }, { tab: { id: 13 }, frameId: 0 }, resolve);
    });
    expect(response.ok).toBe(false);
    expect(response.error).toContain('media buttons disabled');
    expect(bridgeCalls(fetchBridge, '/v1/capture')).toHaveLength(0);
  });

  it('still releases the browser download when resident forwarding throws synchronously', async () => {
    const fetchBridge = vi.fn(() => {
      throw new Error('resident bridge startup failed');
    });
    const chrome = chromeMock(vi.fn().mockResolvedValue(undefined), vi.fn().mockResolvedValue({}));
    vi.stubGlobal('chrome', chrome);
    vi.stubGlobal('fetch', fetchBridge);

    await import('./background');
    const determine = chrome.downloads.onDeterminingFilename.addListener.mock.calls[0]?.[0];
    expect(determine).toBeTypeOf('function');
    let suggestions = 0;
    const result = determine({
      id: 8,
      url: 'https://cdn.example.test/file.zip',
      finalUrl: 'https://cdn.example.test/file.zip',
      filename: 'file.zip',
      referrer: 'https://www.example.test/page',
      byExtensionId: undefined,
    }, () => { suggestions += 1; });
    expect(result).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(suggestions).toBe(1);
  });
});
