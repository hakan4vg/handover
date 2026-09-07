// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, it, vi } from 'vitest';
import { useAppSnapshot } from './App';
import type { AppSnapshot, DownloadAdapter } from './types';

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => { resolve = settle; });
  return { promise, resolve };
}

function snapshot(connected: boolean): AppSnapshot {
  return {
    jobs: [],
    settings: { theme: 'light', accent: '#0878ed' } as AppSnapshot['settings'],
    connected,
    aggregateSpeed: 0,
    notifications: [],
  };
}

function Probe({ adapter }: { adapter: DownloadAdapter }) {
  const { snapshot: current, error } = useAppSnapshot(adapter);
  return <output>{error || (current?.connected ? 'connected' : current ? 'disconnected' : 'loading')}</output>;
}

describe('useAppSnapshot startup ordering', () => {
  it('does not overwrite a live update with a stale initial response', async () => {
    const initial = deferred<AppSnapshot>();
    let notify!: (value: AppSnapshot) => void;
    const adapter = {
      subscribe: vi.fn((listener: (value: AppSnapshot) => void) => { notify = listener; return () => undefined; }),
      getSnapshot: vi.fn(() => initial.promise),
    } as unknown as DownloadAdapter;
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);

    act(() => { root.render(<Probe adapter={adapter} />); });
    await act(async () => {
      notify(snapshot(true));
      initial.resolve(snapshot(false));
      await initial.promise;
    });

    expect(host.querySelector('output')?.textContent).toBe('connected');
    act(() => root.unmount());
    host.remove();
  });

  it('surfaces a live subscription failure after the initial snapshot', async () => {
    let reportError: ((reason: unknown) => void) | undefined;
    const adapter = {
      subscribe: vi.fn((_listener: (value: AppSnapshot) => void, onError?: (reason: unknown) => void) => {
        reportError = onError;
        return () => undefined;
      }),
      getSnapshot: vi.fn(async () => snapshot(true)),
    } as unknown as DownloadAdapter;
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);

    await act(async () => { root.render(<Probe adapter={adapter} />); });
    expect(reportError).toEqual(expect.any(Function));
    await act(async () => { reportError?.(new Error('live state unavailable')); });

    expect(host.querySelector('output')?.textContent).toBe('live state unavailable');
    act(() => root.unmount());
    host.remove();
  });

  it('surfaces a synchronous subscription setup failure', async () => {
    const adapter = {
      subscribe: vi.fn(() => {
        throw new Error('live subscription setup failed');
      }),
      getSnapshot: vi.fn(async () => snapshot(true)),
    } as unknown as DownloadAdapter;
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);

    await act(async () => {
      root.render(<Probe adapter={adapter} />);
      await Promise.resolve();
    });

    expect(host.querySelector('output')?.textContent).toBe('live subscription setup failed');
    act(() => root.unmount());
    host.remove();
  });
});
