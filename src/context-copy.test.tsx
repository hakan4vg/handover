// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { JobContextMenu } from './App';
import type { DownloadAdapter, DownloadJob } from './types';

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }));
(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

const job: DownloadJob = {
  id: 'job-1', name: 'finished.bin', source: 'https://example.com/finished.bin', domain: 'example.com', kind: 'archive',
  state: 'completed', progress: 100, downloaded: 42, total: 42, speed: 0, connections: 1, maxConnections: 4,
  mode: 'whole-object', media: false, destination: '/tmp/finished.bin', tempPath: '/tmp/finished.bin.part', resumable: true,
  created: '2026-09-07T00:00:00Z', events: [],
};

afterEach(() => {
  Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
});

describe('context-menu clipboard action', () => {
  it('reports clipboard rejection as an error notice', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('clipboard denied'));
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const onNotice = vi.fn();
    const onClose = vi.fn();
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);

    act(() => {
      root.render(<JobContextMenu job={job} adapter={{} as DownloadAdapter} onClose={onClose} onNotice={onNotice} />);
    });
    const copy = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Copy source URL')) as HTMLButtonElement;
    await act(async () => { copy.click(); await new Promise((resolve) => setTimeout(resolve, 10)); });

    expect(onNotice).toHaveBeenCalledWith('clipboard denied', 'error');
    expect(onClose).toHaveBeenCalledTimes(1);
    act(() => root.unmount());
    host.remove();
  });
});
