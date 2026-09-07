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
    id: 'commit-error-1',
    source: 'http://127.0.0.1:9/file.bin',
    name: 'file.bin',
    domain: '127.0.0.1',
    kind: 'document',
    state: 'finalizing',
    progress: 100,
    downloaded: 256,
    total: 256,
    speed: 0,
    connections: 0,
    maxConnections: 8,
    bandwidthLimit: null,
    mode: 'single-stream',
    media: false,
    destination: '/tmp/dm-test-downloads/file.bin',
    tempPath: '/tmp/dm-test-downloads/commit-error-1.part',
    resumable: false,
    created: 'Just now',
    provisional: true,
  } as DownloadJob;
}

afterEach(() => {
  document.body.replaceChildren();
});

describe('AddDownloadWindow commit feedback', () => {
  it('shows a form error when captured-job commit fails', async () => {
    const onCommit = vi.fn().mockRejectedValue(new Error('Destination is not writable'));
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    act(() => {
      root.render(
        <AddDownloadWindow
          settings={settings()}
          job={job()}
          onCommit={onCommit}
          onCancel={() => {}}
          onClose={() => {}}
        />,
      );
    });

    const submit = host.querySelector('.add-actions .button.primary');
    if (!(submit instanceof HTMLButtonElement)) throw new Error('commit button missing');
    await act(async () => {
      submit.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(onCommit).toHaveBeenCalledWith(
      'commit-error-1',
      'file.bin',
      '/tmp/dm-test-downloads/file.bin',
      8,
      null,
    );
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('Destination is not writable');
    expect(host.querySelector('.add-download-window')).not.toBeNull();
    act(() => root.unmount());
  });
});
