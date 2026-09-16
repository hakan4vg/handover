// @vitest-environment jsdom
import { act } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRoot } from 'react-dom/client';
import { ExtensionPopup } from './App';
import type { AppSettings, AppSnapshot, DownloadAdapter } from './types';

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => document.body.replaceChildren());

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

describe('ExtensionPopup feedback', () => {
  it('shows the native string when a browser policy update fails', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const adapter = {
      updateSettings: vi.fn().mockRejectedValue('browser policy store unavailable'),
    } as unknown as DownloadAdapter;

    await act(async () => {
      root.render(<ExtensionPopup adapter={adapter} snapshot={snapshot} />);
    });
    const exclude = host.querySelector('.popup-site button') as HTMLButtonElement;
    expect(exclude).not.toBeNull();

    await act(async () => {
      exclude.click();
      await Promise.resolve();
    });

    expect(host.querySelector('[role="alert"]')?.textContent).toContain('browser policy store unavailable');
    act(() => root.unmount());
    host.remove();
  });
});
