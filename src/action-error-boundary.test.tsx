// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { JobContextMenu, Manager } from './App';
import type { AppSnapshot, DownloadAdapter, DownloadJob } from './types';

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

const job: DownloadJob = {
  id: 'action-error-1',
  name: 'broken.iso',
  source: 'https://example.com/broken.iso',
  domain: 'example.com',
  kind: 'disk',
  state: 'failed',
  progress: 13,
  downloaded: 13,
  total: 100,
  speed: 0,
  connections: 0,
  maxConnections: 8,
  bandwidthLimit: null,
  mode: 'single-stream',
  media: false,
  destination: '/tmp/downloads/broken.iso',
  tempPath: '/tmp/downloads/broken.iso.part',
  resumable: true,
  created: 'Just now',
  events: [],
};

afterEach(() => document.body.replaceChildren());

describe('download action error boundary', () => {
  it('reports a synchronous adapter throw from a context-menu action', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const onNotice = vi.fn();
    const onClose = vi.fn();
    const adapter = {
      retryJob: vi.fn(() => { throw new Error('native retry unavailable'); }),
      removeJob: vi.fn(),
    } as unknown as DownloadAdapter;

    act(() => {
      root.render(<JobContextMenu job={job} adapter={adapter} onClose={onClose} onNotice={onNotice} />);
    });
    const retry = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Retry')) as HTMLButtonElement;

    await act(async () => {
      retry.click();
      await Promise.resolve();
    });

    expect(onNotice).toHaveBeenCalledWith('native retry unavailable', 'error');
    expect(onClose).not.toHaveBeenCalled();
    act(() => root.unmount());
    host.remove();
  });

  it('reports an asynchronous adapter rejection from the context menu', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const onNotice = vi.fn();
    const onClose = vi.fn();
    const adapter = {
      retryJob: vi.fn().mockRejectedValue(new Error('retry rejected')),
      removeJob: vi.fn(),
    } as unknown as DownloadAdapter;

    act(() => {
      root.render(<JobContextMenu job={job} adapter={adapter} onClose={onClose} onNotice={onNotice} />);
    });
    const retry = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Retry')) as HTMLButtonElement;

    await act(async () => {
      retry.click();
      await new Promise((resolve) => setTimeout(resolve, 10));
    });

    expect(onNotice).toHaveBeenCalledWith('retry rejected', 'error');
    expect(onClose).not.toHaveBeenCalled();
    act(() => root.unmount());
    host.remove();
  });

  it('preserves a plain string rejection from the native command boundary', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const onNotice = vi.fn();
    const onClose = vi.fn();
    const adapter = {
      retryJob: vi.fn().mockRejectedValue('native retry unavailable'),
      removeJob: vi.fn(),
    } as unknown as DownloadAdapter;

    act(() => {
      root.render(<JobContextMenu job={job} adapter={adapter} onClose={onClose} onNotice={onNotice} />);
    });
    const retry = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Retry')) as HTMLButtonElement;

    await act(async () => {
      retry.click();
      await Promise.resolve();
    });

    expect(onNotice).toHaveBeenCalledWith('native retry unavailable', 'error');
    expect(onClose).not.toHaveBeenCalled();
    act(() => root.unmount());
    host.remove();
  });

  it('does not offer reattach for an uncommitted provisional acquisition', () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const adapter = { reattachJob: vi.fn(), removeJob: vi.fn() } as unknown as DownloadAdapter;

    act(() => {
      root.render(<JobContextMenu job={{ ...job, id: 'provisional-action-1', provisional: true, state: 'downloading' }} adapter={adapter} onClose={() => {}} onNotice={() => {}} />);
    });

    expect(host.textContent).not.toContain('Reattach download');
    act(() => root.unmount());
    host.remove();
  });

  it('reports a synchronous adapter throw from an inline row action', async () => {
    const activeJob: DownloadJob = { ...job, id: 'active-action-error-1', name: 'active.bin', state: 'downloading', progress: 25 };
    const snapshot: AppSnapshot = {
      jobs: [activeJob],
      settings: {
        startAtSignIn: false, showManagerAtSignIn: false, closeBehavior: 'tray', defaultFolder: '/tmp/downloads', tempFolder: '/tmp/downloads/.parts',
        collisionBehavior: 'rename', interceptDownloads: true, showMediaButtons: true, excludedSites: [], bandwidthLimit: null, bandwidthUnit: 'MB/s',
        maxConnections: 8, perDownloadOverrides: true, retryAutomatically: true, maxRetries: 3, completionNotifications: true, failureNotifications: true,
        theme: 'light', accent: '#0878ed', density: 'comfortable',
      },
      connected: true,
      aggregateSpeed: 0,
      notifications: [],
    };
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const adapter = {
      pauseJob: vi.fn(() => { throw new Error('native pause unavailable'); }),
    } as unknown as DownloadAdapter;

    act(() => {
      root.render(<Manager adapter={adapter} snapshot={snapshot} />);
    });
    const pause = host.querySelector('button[aria-label^="Pause "]') as HTMLButtonElement;
    expect(pause).not.toBeNull();

    await act(async () => {
      pause.click();
      await new Promise((resolve) => setTimeout(resolve, 10));
    });

    expect(host.querySelector('[role="alert"]')?.textContent).toContain('native pause unavailable');
    act(() => root.unmount());
    host.remove();
  });
});
