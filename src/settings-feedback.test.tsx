// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, it, vi } from 'vitest';
import { SettingsView } from './App';
import type { AppSettings, DownloadAdapter } from './types';

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

const settings: AppSettings = {
  startAtSignIn: false,
  showManagerAtSignIn: false,
  closeBehavior: 'tray',
  defaultFolder: '/tmp/downloads',
  tempFolder: '/tmp/downloads/.parts',
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

describe('SettingsView', () => {
  it('shows a visible error when saving a setting fails', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const adapter = {
      updateSettings: vi.fn().mockRejectedValue(new Error('settings store unavailable')),
    } as unknown as DownloadAdapter;

    await act(async () => {
      root.render(<SettingsView adapter={adapter} settings={settings} page="general" onPageChange={() => undefined} />);
    });
    const toggle = host.querySelector('button.toggle');
    expect(toggle).not.toBeNull();
    expect(toggle?.getAttribute('aria-label')).toContain('Start download service at sign-in');
    await act(async () => {
      (toggle as HTMLButtonElement).click();
      await Promise.resolve();
    });

    expect(host.querySelector('[role="alert"]')?.textContent).toContain('settings store unavailable');
    act(() => root.unmount());
    host.remove();
  });

  it('exposes bandwidth limit choices as a labelled radio group', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const adapter = {
      updateSettings: vi.fn().mockResolvedValue(undefined),
    } as unknown as DownloadAdapter;

    await act(async () => {
      root.render(<SettingsView adapter={adapter} settings={settings} page="network" onPageChange={() => undefined} />);
    });

    const group = host.querySelector('[role="radiogroup"]');
    expect(group?.getAttribute('aria-label')).toBe('Global bandwidth limit');
    const radios = Array.from(host.querySelectorAll('button.radio'));
    expect(radios).toHaveLength(2);
    expect(radios.map((radio) => radio.getAttribute('role'))).toEqual(['radio', 'radio']);
    expect(radios.map((radio) => radio.getAttribute('aria-checked'))).toEqual(['true', 'false']);

    act(() => root.unmount());
    host.remove();
  });
});
