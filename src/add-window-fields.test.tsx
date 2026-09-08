// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AddDownloadWindow, effectiveDestination } from './App';
import type { AppSettings, DownloadJob } from './types';

vi.mock('@tauri-apps/plugin-dialog', () => ({ open: vi.fn() }));

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

function settings(): AppSettings {
  return {
    defaultFolder: '/tmp/dm-test-downloads',
    maxConnections: 8,
    perDownloadOverrides: false,
  } as AppSettings;
}

function job(overrides: Partial<DownloadJob> = {}): DownloadJob {
  return {
    id: 'provisional-fields-1',
    source: 'http://127.0.0.1:9/file/original.bin',
    name: 'original.bin',
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
    destination: '/tmp/dm-test-downloads/original.bin',
    tempPath: '/tmp/dm-test-downloads/provisional-fields-1.part',
    resumable: false,
    created: 'Just now',
    provisional: true,
    ...overrides,
  } as DownloadJob;
}

function renderWindow(current: DownloadJob | undefined, callbacks: {
  onCommit: (id: string, name: string, destination: string, maxConnections: number, bandwidthLimit: number | null) => void;
  onCancel: (id: string) => void;
  onCreate?: (source: string, name: string, maxConnections: number, bandwidthLimit: number | null) => Promise<void>;
}) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => {
    root.render(
      <AddDownloadWindow
        settings={settings()}
        job={current}
        onCommit={callbacks.onCommit}
        onCancel={callbacks.onCancel}
        onCreate={callbacks.onCreate}
        onClose={() => {}}
      />,
    );
  });
  return host;
}

function field(host: Element, label: string): HTMLInputElement {
  const input = host.querySelector(`input[aria-label="${label}"]`);
  if (!(input instanceof HTMLInputElement)) throw new Error(`${label} missing`);
  return input;
}

afterEach(() => {
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

describe('effectiveDestination', () => {
  it('tracks renames, appends to bare dirs, and respects manual edits', () => {
    expect(effectiveDestination('/tmp/dl/original.bin', 'renamed.bin', false)).toBe('/tmp/dl/renamed.bin');
    expect(effectiveDestination('/tmp/dl/original.bin', 'original.bin', false)).toBe('/tmp/dl/original.bin');
    expect(effectiveDestination('/tmp/dl', 'a.zip', false, '/tmp/dl')).toBe('/tmp/dl/a.zip');
    expect(effectiveDestination('/tmp/dl', 'a.zip', false)).toBe('/tmp/dl/a.zip');
    expect(effectiveDestination('/tmp/dl/typed.bin', 'a.zip', true)).toBe('/tmp/dl/typed.bin');
    expect(effectiveDestination('/tmp/dl', '', false)).toBe('/tmp/dl');
  });
});

describe('AddDownloadWindow field honesty', () => {
  it('sends the renamed destination on commit until the path is touched', () => {
    const seen: Array<[string, string, string]> = [];
    const host = renderWindow(job(), {
      onCommit: (id, name, destination) => { seen.push([id, name, destination]); },
      onCancel: () => {},
    });
    // React controlled inputs need native setter dispatch in jsdom; a bare
    // assignment never reaches the component.
    act(() => {
      const input = field(host, 'Filename');
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
      setter?.call(input, 'renamed.bin');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(field(host, 'Save destination').value).toBe('/tmp/dm-test-downloads/renamed.bin');
    act(() => {
      host.querySelector('.add-actions .button.primary')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    expect(seen).toEqual([['provisional-fields-1', 'renamed.bin', '/tmp/dm-test-downloads/renamed.bin']]);
  });

  it('locks the captured source and leaves manual URLs editable', () => {
    const captured = renderWindow(job(), { onCommit: () => {}, onCancel: () => {} });
    expect(field(captured, 'Source URL').readOnly).toBe(true);
    const manual = renderWindow(undefined, { onCommit: () => {}, onCancel: () => {}, onCreate: async () => {} });
    expect(field(manual, 'Source URL').readOnly).toBe(false);
  });

  it('shows the failure reason and disables Download for failed captures', () => {
    const host = renderWindow(job({ state: 'failed', error: 'source returned 404 Not Found' }), {
      onCommit: () => {},
      onCancel: () => {},
    });
    expect(host.textContent).toContain('source returned 404 Not Found');
    const button = host.querySelector('.add-actions .button.primary');
    if (!(button instanceof HTMLButtonElement)) throw new Error('Download button missing');
    expect(button.disabled).toBe(true);
  });

  it('distinguishes unknown from determined-non-resumable sources', () => {
    const connecting = renderWindow(job({ state: 'connecting', resumable: false }), { onCommit: () => {}, onCancel: () => {} });
    expect(connecting.textContent).toContain('Checking…');
    document.body.innerHTML = '';
    const determined = renderWindow(job({ state: 'downloading', resumable: false }), { onCommit: () => {}, onCancel: () => {} });
    expect(determined.textContent).toContain('No');
  });

  it('labels finalizing work Finalizing', () => {
    const host = renderWindow(job({ state: 'finalizing' }), { onCommit: () => {}, onCancel: () => {} });
    expect(host.textContent).toContain('Finalizing');
    expect(host.textContent).not.toContain('Merging');
  });
});
