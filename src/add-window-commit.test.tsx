// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, it, vi } from 'vitest';
import { AddDownloadWindow } from './App';
import type { AppSettings, DownloadJob } from './types';

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

function settings(): AppSettings {
  return {
    defaultFolder: '/tmp/dm-test-downloads',
    maxConnections: 8,
    perDownloadOverrides: false,
  } as AppSettings;
}

function job(): DownloadJob {
  return {
    id: 'provisional-wiring-1',
    source: 'http://127.0.0.1:9/file/range.bin',
    name: 'range.bin',
    domain: '127.0.0.1',
    kind: 'document',
    state: 'connecting',
    progress: 0,
    downloaded: 0,
    speed: 0,
    connections: 0,
    maxConnections: 8,
    bandwidthLimit: null,
    mode: 'single-stream',
    media: false,
    destination: '/tmp/dm-test-downloads/range.bin',
    tempPath: '/tmp/dm-test-downloads/provisional-wiring-1.part',
    resumable: false,
    created: 'Just now',
    provisional: true,
  } as DownloadJob;
}

function renderCaptured(callbacks: {
  onCommit: (id: string, name: string, destination: string, maxConnections: number, bandwidthLimit: number | null) => void;
  onCancel: (id: string) => void;
}) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => {
    root.render(
      <AddDownloadWindow
        settings={settings()}
        job={job()}
        onCommit={callbacks.onCommit}
        onCancel={callbacks.onCancel}
        onClose={() => {}}
      />,
    );
  });
  return host;
}

function primaryButton(host: Element): HTMLButtonElement {
  const button = host.querySelector('.add-actions .button.primary');
  if (!(button instanceof HTMLButtonElement)) throw new Error('Download button missing');
  return button;
}

describe('AddDownloadWindow captured commit wiring', () => {
  it('commits the captured job id with the shown name and destination', () => {
    const onCommit = vi.fn();
    const host = renderCaptured({ onCommit, onCancel: () => {} });
    expect(primaryButton(host).textContent).toContain('Save');
    act(() => {
      primaryButton(host).click();
    });
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith(
      'provisional-wiring-1',
      'range.bin',
      '/tmp/dm-test-downloads/range.bin',
      8,
      null,
    );
  });

  it('routes Cancel to the captured job, never another row', () => {
    const onCancel = vi.fn();
    const host = renderCaptured({ onCommit: () => {}, onCancel });
    const buttons = Array.from(host.querySelectorAll('.add-actions .button'));
    const cancel = buttons.find((item) => item.textContent === 'Cancel');
    if (!(cancel instanceof HTMLButtonElement)) throw new Error('Cancel button missing');
    act(() => {
      cancel.click();
    });
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onCancel).toHaveBeenCalledWith('provisional-wiring-1');
  });

  it('shows the live provisional progress, size, and resumability', () => {
    const live = {
      ...job(),
      state: 'downloading',
      downloaded: 5 * 1024 ** 2,
      total: 10 * 1024 ** 2,
      connections: 4,
      resumable: true,
    } as DownloadJob;
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    act(() => {
      root.render(
        <AddDownloadWindow
          settings={settings()}
          job={live}
          onCommit={() => {}}
          onCancel={() => {}}
          onClose={() => {}}
        />,
      );
    });
    const text = host.querySelector('.provisional-panel')?.textContent ?? '';
    expect(text).toContain('5.0 MB / 10.0 MB');
    expect(text).toContain('Downloading');
    expect(text).toContain('4 active');
    expect(text).toContain('Yes');
  });
});
