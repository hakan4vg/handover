// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TrayMenu } from './App';
import type { AppSettings, AppSnapshot, DownloadAdapter } from './types';

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

const settings: AppSettings = {
  startAtSignIn: false,
  showManagerAtSignIn: false,
  closeBehavior: 'tray',
  defaultFolder: '/tmp/downloads',
  collisionBehavior: 'rename',
  interceptDownloads: true,
  showMediaButtons: true,
  excludedSites: [],
  bandwidthLimit: null,
  bandwidthUnit: 'MB/s',
  maxConnections: 8,
  perDownloadOverrides: true,
  retryAutomatically: true,
  maxRetries: 3,
  completionNotifications: true,
  failureNotifications: true,
  theme: 'light',
  accent: '#0878ed',
  density: 'comfortable',
};

const snapshot: AppSnapshot = {
  jobs: [],
  settings,
  connected: true,
  aggregateSpeed: 0,
  notifications: [],
};

afterEach(() => {
  document.body.replaceChildren();
});

describe('TrayMenu action feedback', () => {
  it('reports a failed settings update instead of leaving the toggle silent', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const adapter = {
      updateSettings: vi.fn().mockRejectedValue(new Error('tray settings unavailable')),
      pauseAll: vi.fn().mockResolvedValue(undefined),
      resumeAll: vi.fn().mockResolvedValue(undefined),
    } as unknown as DownloadAdapter;

    act(() => {
      root.render(<TrayMenu adapter={adapter} snapshot={snapshot} />);
    });
    const toggle = host.querySelector('button.toggle') as HTMLButtonElement;
    expect(toggle).not.toBeNull();
    await act(async () => {
      toggle.click();
      await Promise.resolve();
    });

    expect(host.querySelector('[role="alert"]')?.textContent).toContain('tray settings unavailable');
    act(() => root.unmount());
    host.remove();
  });

  it('reports a synchronous tray settings failure without throwing from the click', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const adapter = {
      updateSettings: vi.fn(() => {
        throw new Error('tray bridge unavailable');
      }),
      pauseAll: vi.fn().mockResolvedValue(undefined),
      resumeAll: vi.fn().mockResolvedValue(undefined),
    } as unknown as DownloadAdapter;

    act(() => {
      root.render(<TrayMenu adapter={adapter} snapshot={snapshot} />);
    });
    const toggle = host.querySelector('button.toggle') as HTMLButtonElement;
    expect(toggle).not.toBeNull();
    await act(async () => {
      toggle.click();
      await Promise.resolve();
    });

    expect(host.querySelector('[role="alert"]')?.textContent).toContain('tray bridge unavailable');
    act(() => root.unmount());
    host.remove();
  });
});
