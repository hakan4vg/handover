// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
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
    id: 'cancel-error-1',
    source: 'http://127.0.0.1:9/file.bin',
    name: 'file.bin',
    domain: '127.0.0.1',
    kind: 'document',
    state: 'connecting',
    progress: 0,
    downloaded: 0,
    total: 256,
    speed: 0,
    connections: 0,
    maxConnections: 8,
    bandwidthLimit: null,
    mode: 'single-stream',
    media: false,
    destination: '/tmp/dm-test-downloads/file.bin',
    tempPath: '/tmp/dm-test-downloads/cancel-error-1.part',
    resumable: false,
    created: 'Just now',
    provisional: true,
  } as DownloadJob;
}

afterEach(() => {
  document.body.replaceChildren();
});

describe('AddDownloadWindow cancellation feedback', () => {
  it('shows a form error when captured-job cancellation fails', async () => {
    const onCancel = vi.fn().mockRejectedValue(new Error('Native core unavailable'));
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    act(() => {
      root.render(
        <AddDownloadWindow
          settings={settings()}
          job={job()}
          onCommit={() => {}}
          onCancel={onCancel}
          onClose={() => {}}
        />,
      );
    });

    const actions = Array.from(host.querySelectorAll('.add-actions .button'));
    const cancel = actions.find((button) => button.textContent === 'Cancel');
    if (!(cancel instanceof HTMLButtonElement)) throw new Error('cancel button missing');
    await act(async () => {
      cancel.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(onCancel).toHaveBeenCalledWith('cancel-error-1');
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('Native core unavailable');
    expect(host.querySelector('.add-download-window')).not.toBeNull();
    act(() => root.unmount());
  });
});
