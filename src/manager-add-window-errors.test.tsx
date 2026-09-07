// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import type { AppSnapshot, DownloadJob } from './types';

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

const harness = vi.hoisted(() => {
  const settings = {
    defaultFolder: '/tmp/dm-test-downloads',
    tempFolder: '/tmp/dm-test-downloads/.parts',
    maxConnections: 8,
    perDownloadOverrides: false,
    theme: 'light',
    accent: '#0878ed',
  };
  const seed: DownloadJob = {
    id: 'seed-1',
    name: 'seed.bin',
    source: 'http://127.0.0.1:8901/file/range.bin',
    domain: '127.0.0.1',
    kind: 'document',
    state: 'completed',
    progress: 100,
    downloaded: 1024,
    total: 1024,
    speed: 0,
    connections: 0,
    maxConnections: 8,
    bandwidthLimit: null,
    mode: 'whole-object',
    media: false,
    destination: '/tmp/dm-test-downloads/seed.bin',
    tempPath: '/tmp/dm-test-downloads/seed-1.part',
    resumable: true,
    created: 'Just now',
    events: [],
  } as DownloadJob;
  const provisional: DownloadJob = {
    ...seed,
    id: 'provisional-1',
    name: 'captured.bin',
    state: 'downloading',
    progress: 20,
    downloaded: 2048,
    total: 10240,
    destination: '/tmp/dm-test-downloads/captured.bin',
    tempPath: '/tmp/dm-test-downloads/provisional-1.part',
    provisional: true,
  };
  let current: AppSnapshot = {
    jobs: [seed],
    settings: settings as AppSnapshot['settings'],
    connected: true,
    aggregateSpeed: 0,
    notifications: [],
  };
  let notify: ((snapshot: AppSnapshot) => void) | undefined;
  const adapter = {
    getSnapshot: vi.fn(async () => structuredClone(current)),
    subscribe: vi.fn((listener: (snapshot: AppSnapshot) => void) => {
      notify = listener;
      return () => undefined;
    }),
    createProvisional: vi.fn(async () => {
      current = { ...current, jobs: [provisional, ...current.jobs] };
      notify?.(structuredClone(current));
      return provisional.id;
    }),
    commitProvisional: vi.fn(async () => {
      throw new Error('Destination is not writable');
    }),
    cancelJob: vi.fn(async () => {
      throw new Error('Temporary cleanup unavailable');
    }),
    pauseJob: vi.fn(async () => undefined),
    resumeJob: vi.fn(async () => undefined),
    retryJob: vi.fn(async () => undefined),
    removeJob: vi.fn(async () => undefined),
    pauseAll: vi.fn(async () => undefined),
    resumeAll: vi.fn(async () => undefined),
    updateSettings: vi.fn(async () => undefined),
    reattachJob: vi.fn(async () => undefined),
  };
  const reset = () => {
    current = { jobs: [seed], settings: settings as AppSnapshot['settings'], connected: true, aggregateSpeed: 0, notifications: [] };
    notify = undefined;
    vi.clearAllMocks();
  };
  return { adapter, reset };
});

vi.mock('./adapters', async () => {
  const actual = await vi.importActual<typeof import('./adapters')>('./adapters');
  return { ...actual, createAdapter: () => harness.adapter };
});

function setInputValue(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

afterEach(() => {
  document.body.replaceChildren();
  window.history.replaceState(null, '', '/');
  harness.reset();
});

describe('Manager Add Download error recovery', () => {
  it('keeps the captured manual download window open when commit fails', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);

    await act(async () => {
      root.render(<App />);
      await Promise.resolve();
      await Promise.resolve();
    });
    const addUrl = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Add URL'));
    if (!(addUrl instanceof HTMLButtonElement)) throw new Error('Add URL button missing');
    act(() => addUrl.click());

    const source = host.querySelector('input[placeholder="https://example.com/file.iso"]');
    const create = host.querySelector('.add-actions .button.primary');
    if (!(source instanceof HTMLInputElement) || !(create instanceof HTMLButtonElement)) throw new Error('manual Add Download controls missing');
    act(() => setInputValue(source, 'http://127.0.0.1:8901/file/range.bin'));
    await act(async () => {
      create.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    const captured = host.querySelector('.add-download-window.captured');
    expect(captured).not.toBeNull();
    const download = captured?.querySelector('.add-actions .button.primary');
    if (!(download instanceof HTMLButtonElement)) throw new Error('captured Download button missing');
    await act(async () => {
      download.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(harness.adapter.commitProvisional).toHaveBeenCalledWith(
      'provisional-1',
      expect.objectContaining({ name: 'captured.bin' }),
    );
    expect(host.querySelector('.add-download-window.captured')).not.toBeNull();
    expect(host.querySelector('.add-download-window [role="alert"]')?.textContent).toContain('Destination is not writable');
    act(() => root.unmount());
  });
});
