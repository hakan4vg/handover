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

  it('exposes theme choices as a labelled radio group', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const adapter = {
      updateSettings: vi.fn().mockResolvedValue(undefined),
    } as unknown as DownloadAdapter;

    await act(async () => {
      root.render(<SettingsView adapter={adapter} settings={settings} page="appearance" onPageChange={() => undefined} />);
    });

    const group = host.querySelector('.theme-options');
    expect(group?.getAttribute('role')).toBe('radiogroup');
    expect(group?.getAttribute('aria-label')).toBe('Theme');
    const options = Array.from(host.querySelectorAll('button.theme-option'));
    expect(options).toHaveLength(3);
    expect(options.map((option) => option.getAttribute('role'))).toEqual(['radio', 'radio', 'radio']);
    expect(options.map((option) => option.getAttribute('aria-checked'))).toEqual(['false', 'true', 'false']);

    act(() => root.unmount());
    host.remove();
  });

  it('exposes accent choices as a labelled radio group', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const adapter = {
      updateSettings: vi.fn().mockResolvedValue(undefined),
    } as unknown as DownloadAdapter;

    await act(async () => {
      root.render(<SettingsView adapter={adapter} settings={settings} page="appearance" onPageChange={() => undefined} />);
    });

    const group = host.querySelector('.accent-options');
    expect(group?.getAttribute('role')).toBe('radiogroup');
    expect(group?.getAttribute('aria-label')).toBe('Accent color');
    const options = Array.from(host.querySelectorAll('button.accent-swatch'));
    expect(options).toHaveLength(8);
    expect(options.map((option) => option.getAttribute('role'))).toEqual(Array(8).fill('radio'));
    expect(options.map((option) => option.getAttribute('aria-checked'))).toEqual(['true', ...Array(7).fill('false')]);

    act(() => root.unmount());
    host.remove();
  });

  it('gives editable settings fields specific accessible names', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const adapter = {
      updateSettings: vi.fn().mockResolvedValue(undefined),
    } as unknown as DownloadAdapter;

    const renderPage = async (page: 'downloads' | 'network' | 'appearance' | 'browser') => {
      await act(async () => {
        root.render(<SettingsView adapter={adapter} settings={settings} page={page} onPageChange={() => undefined} />);
      });
    };

    await renderPage('downloads');
    expect(Array.from(host.querySelectorAll('.path-field input')).map((input) => input.getAttribute('aria-label'))).toEqual([
      'Default download folder',
      'Temporary / cache folder',
    ]);
    expect(host.querySelector('select')?.getAttribute('aria-label')).toBe('File name collisions');

    await renderPage('network');
    expect(Array.from(host.querySelectorAll('input.number-input')).map((input) => input.getAttribute('aria-label'))).toEqual([
      'Global bandwidth limit',
      'Default maximum connections per download',
      'Max retry attempts',
    ]);
    expect(host.querySelector('select')?.getAttribute('aria-label')).toBe('Bandwidth unit');

    await renderPage('appearance');
    expect(host.querySelector('select')?.getAttribute('aria-label')).toBe('App density');

    await renderPage('browser');
    expect(host.querySelector('.add-site input')?.getAttribute('aria-label')).toBe('Add excluded media site');

    act(() => root.unmount());
    host.remove();
  });

  it('commits folder edits after editing finishes instead of on every keystroke', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const updateSettings = vi.fn().mockResolvedValue(undefined);
    const adapter = { updateSettings } as unknown as DownloadAdapter;

    await act(async () => {
      root.render(<SettingsView adapter={adapter} settings={settings} page="downloads" onPageChange={() => undefined} />);
    });
    const input = host.querySelector('input[aria-label="Temporary / cache folder"]') as HTMLInputElement;
    await act(async () => {
      input.focus();
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(input, '/tmp/downloads/.part');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(updateSettings).not.toHaveBeenCalled();

    await act(async () => {
      input.blur();
      await Promise.resolve();
    });
    expect(updateSettings).toHaveBeenCalledTimes(1);
    expect(updateSettings).toHaveBeenCalledWith({ tempFolder: '/tmp/downloads/.part' });

    act(() => root.unmount());
    host.remove();
  });
});
