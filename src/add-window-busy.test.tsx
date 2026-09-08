// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AddDownloadWindow } from './App';
import type { AppSettings, DownloadJob } from './types';

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

const settings = {
  defaultFolder: '/tmp/downloads',
  tempFolder: '/tmp/downloads/.parts',
  maxConnections: 8,
  perDownloadOverrides: false,
  theme: 'light',
  accent: '#0878ed',
} as AppSettings;

const job = {
  id: 'provisional-1',
  name: 'capture.bin',
  source: 'http://127.0.0.1:8901/file/range.bin',
  domain: '127.0.0.1',
  kind: 'document',
  state: 'downloading',
  progress: 10,
  downloaded: 1024,
  total: 10240,
  speed: 0,
  connections: 1,
  maxConnections: 8,
  bandwidthLimit: null,
  mode: 'whole-object',
  media: false,
  destination: '/tmp/downloads/capture.bin',
  tempPath: '/tmp/downloads/.parts/provisional-1.part',
  resumable: true,
  provisional: true,
  created: 'Just now',
  events: [],
} as DownloadJob;

function setInputValue(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

function renderWindow(props: Partial<React.ComponentProps<typeof AddDownloadWindow>> = {}) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => {
    root.render(<AddDownloadWindow settings={settings} onCancel={vi.fn()} onClose={vi.fn()} {...props} />);
  });
  return { host, root };
}

afterEach(() => {
  document.body.replaceChildren();
});

describe('Add Download pending actions', () => {
  it('locks manual create controls while provisional creation is pending', async () => {
    let resolveCreate: () => void = () => undefined;
    const create = vi.fn(() => new Promise<void>((resolve) => { resolveCreate = resolve; }));
    const onCancel = vi.fn();
    const onClose = vi.fn();
    const { host, root } = renderWindow({ onCreate: create, onCancel, onClose });
    const source = host.querySelector('input[aria-label="Source URL"]');
    const start = host.querySelector('.add-actions .button.primary');
    const cancel = host.querySelector('.add-actions .button:not(.primary)');
    const close = host.querySelector('.add-titlebar button');
    if (!(source instanceof HTMLInputElement) || !(start instanceof HTMLButtonElement) || !(cancel instanceof HTMLButtonElement) || !(close instanceof HTMLButtonElement)) throw new Error('manual controls missing');
    act(() => setInputValue(source, 'http://127.0.0.1:8901/file/range.bin'));
    await act(async () => {
      start.click();
      await Promise.resolve();
    });
    expect(create).toHaveBeenCalledTimes(1);
    expect(host.querySelector('.add-download-window')?.getAttribute('aria-busy')).toBe('true');
    expect(start.disabled).toBe(true);
    expect(start.textContent).toContain('Starting…');
    expect(cancel.disabled).toBe(true);
    expect(close.disabled).toBe(true);
    await act(async () => {
      resolveCreate();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    act(() => root.unmount());
  });

  it('uses commit feedback and locks captured controls while commit is pending', async () => {
    let resolveCommit: () => void = () => undefined;
    const commit = vi.fn(() => new Promise<void>((resolve) => { resolveCommit = resolve; }));
    const onCancel = vi.fn();
    const onClose = vi.fn();
    const { host, root } = renderWindow({ job, onCommit: commit, onCancel, onClose });
    const download = host.querySelector('.add-actions .button.primary');
    const cancel = host.querySelector('.add-actions .button:not(.primary)');
    const close = host.querySelector('.add-titlebar button');
    if (!(download instanceof HTMLButtonElement) || !(cancel instanceof HTMLButtonElement) || !(close instanceof HTMLButtonElement)) throw new Error('captured controls missing');
    await act(async () => {
      download.click();
      await Promise.resolve();
    });
    expect(commit).toHaveBeenCalledTimes(1);
    expect(host.querySelector('.add-download-window')?.getAttribute('aria-busy')).toBe('true');
    expect(download.disabled).toBe(true);
    expect(download.textContent).toContain('Adding…');
    expect(cancel.disabled).toBe(true);
    expect(close.disabled).toBe(true);
    await act(async () => {
      resolveCommit();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    act(() => root.unmount());
  });

  it('keeps captured details stable while cancellation removes the live job', async () => {
    let resolveCancel: () => void = () => undefined;
    const onCancel = vi.fn(() => new Promise<void>((resolve) => { resolveCancel = resolve; }));
    const onClose = vi.fn();
    const { host, root } = renderWindow({ job, onCancel, onClose });
    const cancel = host.querySelector('.add-actions .button:not(.primary)');
    if (!(cancel instanceof HTMLButtonElement)) throw new Error('cancel control missing');

    await act(async () => {
      cancel.click();
      await Promise.resolve();
    });
    act(() => {
      root.render(<AddDownloadWindow settings={settings} onCancel={onCancel} onClose={onClose} />);
    });

    expect(host.querySelector('.add-download-window')?.classList.contains('captured')).toBe(true);
    expect((host.querySelector('input[aria-label="Source URL"]') as HTMLInputElement).readOnly).toBe(true);
    expect(host.querySelector('.add-actions .button.primary')?.textContent).toContain('Download');

    await act(async () => {
      resolveCancel();
      await Promise.resolve();
      await Promise.resolve();
    });
    act(() => root.unmount());
  });
});
