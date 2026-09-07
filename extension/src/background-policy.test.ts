// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_POLICY } from './shared';

function chromeMock(storageSet: ReturnType<typeof vi.fn>, storageGet: ReturnType<typeof vi.fn> = vi.fn().mockResolvedValue({}), sendNativeMessage: ReturnType<typeof vi.fn> = vi.fn().mockResolvedValue({})) {
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
      sendNativeMessage,
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

  it('waits for policy before forwarding capture and honors an excluded page', async () => {
    let resolveStored: (value: unknown) => void = () => undefined;
    const stored = new Promise((resolve) => { resolveStored = resolve; });
    const storageGet = vi.fn().mockReturnValue(stored);
    const chrome = chromeMock(vi.fn().mockResolvedValue(undefined), storageGet);
    vi.stubGlobal('chrome', chrome);
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
    expect(chrome.runtime.sendNativeMessage).not.toHaveBeenCalledWith(
      'com.downloadmanager.host',
      expect.objectContaining({ type: 'capture-acquisition' }),
    );

    resolveStored({ ["dm-policy"]: { interceptDownloads: true, showMediaButtons: true, excludedSites: ['example.test'] } });
    const response = await reply;
    expect(response.ok).toBe(false);
    expect(response.error).toContain('site excluded');
    expect(chrome.runtime.sendNativeMessage).not.toHaveBeenCalledWith(
      'com.downloadmanager.host',
      expect.objectContaining({ type: 'capture-acquisition' }),
    );
  });

  it('waits for policy before forwarding browser fallback and honors an excluded referrer', async () => {
    let resolveStored: (value: unknown) => void = () => undefined;
    const stored = new Promise((resolve) => { resolveStored = resolve; });
    const storageGet = vi.fn().mockReturnValue(stored);
    const sendNativeMessage = vi.fn().mockResolvedValue({ ok: true });
    const chrome = chromeMock(vi.fn().mockResolvedValue(undefined), storageGet, sendNativeMessage);
    vi.stubGlobal('chrome', chrome);

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
    expect(sendNativeMessage).not.toHaveBeenCalledWith('com.downloadmanager.host', expect.objectContaining({ type: 'capture-acquisition' }));

    resolveStored({ ["dm-policy"]: { interceptDownloads: true, showMediaButtons: true, excludedSites: ['example.test'] } });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(suggestions).toBe(1);
    expect(sendNativeMessage).not.toHaveBeenCalledWith('com.downloadmanager.host', expect.objectContaining({ type: 'capture-acquisition' }));
  });

  it('still releases the browser download when native forwarding throws synchronously', async () => {
    const sendNativeMessage = vi.fn(() => {
      throw new Error('native bridge startup failed');
    });
    const chrome = chromeMock(vi.fn().mockResolvedValue(undefined), vi.fn().mockResolvedValue({}), sendNativeMessage);
    vi.stubGlobal('chrome', chrome);

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
