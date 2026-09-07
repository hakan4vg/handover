// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { invoke } from '@tauri-apps/api/core';
import { Inspector, openLocalPath } from './App';
import type { DownloadAdapter, DownloadJob } from './types';

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }));
(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  vi.mocked(invoke).mockReset();
  delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});

describe('openLocalPath', () => {
  it('returns a visible error when the native opener fails', async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    vi.mocked(invoke).mockRejectedValueOnce(new Error('launcher unavailable'));

    await expect(openLocalPath('/tmp/missing.bin')).resolves.toBe('launcher unavailable');
  });

  it('shows the native opener failure in the inspector', async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    vi.mocked(invoke).mockRejectedValueOnce(new Error('launcher unavailable'));
    const job: DownloadJob = {
      id: 'job-1', name: 'finished.bin', source: 'https://example.com/finished.bin', domain: 'example.com', kind: 'archive',
      state: 'completed', progress: 100, downloaded: 42, total: 42, speed: 0, connections: 1, maxConnections: 4,
      mode: 'whole-object', media: false, destination: '/tmp/finished.bin', tempPath: '/tmp/finished.bin.part', resumable: true,
      created: '2026-09-07T00:00:00Z', events: [],
    };
    const adapter = { removeJob: vi.fn().mockResolvedValue(undefined) } as unknown as DownloadAdapter;
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => { root.render(<Inspector job={job} adapter={adapter} onClose={() => undefined} />); });
    const openFile = Array.from(container.querySelectorAll('button')).find((button) => button.textContent?.includes('Open file')) as HTMLButtonElement;
    await act(async () => { openFile.click(); await new Promise((resolve) => setTimeout(resolve, 10)); });

    expect(container.querySelector('[role="alert"]')?.textContent).toContain('launcher unavailable');
    act(() => root.unmount());
    container.remove();
  });
});
