// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_POLICY } from './shared';

function chromeMock(storageSet: ReturnType<typeof vi.fn>) {
  const event = () => ({ addListener: vi.fn() });
  const onMessage = { addListener: vi.fn() };
  return {
    storage: {
      local: {
        get: vi.fn().mockResolvedValue({}),
        set: storageSet,
      },
    },
    runtime: {
      id: 'extension-test',
      onMessage,
      sendNativeMessage: vi.fn().mockResolvedValue({}),
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
});
