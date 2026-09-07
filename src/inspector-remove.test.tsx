// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Inspector } from './App';
import type { DownloadAdapter, DownloadJob } from './types';

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

const job: DownloadJob = {
  id: 'remove-error-1',
  source: 'https://example.com/file.bin',
  name: 'file.bin',
  domain: 'example.com',
  kind: 'document',
  state: 'completed',
  progress: 100,
  downloaded: 256,
  total: 256,
  speed: 0,
  connections: 0,
  maxConnections: 8,
  bandwidthLimit: null,
  mode: 'whole-object',
  media: false,
  destination: '/tmp/downloads/file.bin',
  tempPath: '/tmp/downloads/remove-error-1.part',
  resumable: true,
  created: 'Just now',
  events: [],
};

afterEach(() => {
  document.body.replaceChildren();
});

describe('Inspector removal feedback', () => {
  it('shows a visible error and keeps the inspector when removal fails', async () => {
    const removeJob = vi.fn().mockRejectedValue(new Error('Native core unavailable'));
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    act(() => {
      root.render(<Inspector job={job} adapter={{ removeJob } as unknown as DownloadAdapter} onClose={() => undefined} />);
    });

    const remove = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Remove'));
    if (!(remove instanceof HTMLButtonElement)) throw new Error('remove button missing');
    await act(async () => {
      remove.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(removeJob).toHaveBeenCalledWith('remove-error-1');
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('Native core unavailable');
    expect(host.querySelector('.inspector')).not.toBeNull();
    act(() => root.unmount());
  });
});
