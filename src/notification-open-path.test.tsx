// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { invoke } from '@tauri-apps/api/core';
import { NotificationsSurface } from './App';
import type { AppSnapshot, DownloadJob, NotificationItem } from './types';

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }));
(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

const job: DownloadJob = {
  id: 'job-1', name: 'finished.bin', source: 'https://example.com/finished.bin', domain: 'example.com', kind: 'archive',
  state: 'completed', progress: 100, downloaded: 42, total: 42, speed: 0, connections: 1, maxConnections: 4,
  mode: 'whole-object', media: false, destination: '/tmp/finished.bin', tempPath: '/tmp/finished.bin.part', resumable: true,
  created: '2026-09-07T00:00:00Z', events: [],
};
const item: NotificationItem = { id: 'notice-1', type: 'completed', title: 'Download completed', detail: 'finished.bin · 42 B', time: 'Just now', jobId: job.id };
const snapshot: AppSnapshot = {
  jobs: [job],
  settings: { theme: 'light', accent: '#0878ed' } as AppSnapshot['settings'],
  connected: true,
  aggregateSpeed: 0,
  notifications: [item],
};

afterEach(() => {
  vi.mocked(invoke).mockReset();
  delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});

describe('notification file actions', () => {
  it('shows a native opener failure instead of silently doing nothing', async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    vi.mocked(invoke).mockRejectedValueOnce(new Error('launcher unavailable'));
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);

    act(() => { root.render(<NotificationsSurface snapshot={snapshot} />); });
    const open = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Open') as HTMLButtonElement;
    await act(async () => { open.click(); await Promise.resolve(); });

    expect(host.querySelector('[role="alert"]')?.textContent).toContain('launcher unavailable');
    act(() => root.unmount());
    host.remove();
  });
});
