// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { JobContextMenu, SortMenu } from './App';
import type { DownloadAdapter, DownloadJob } from './types';

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

const job: DownloadJob = {
  id: 'menu-semantics-1',
  name: 'project-assets.zip',
  source: 'https://example.com/project-assets.zip',
  domain: 'example.com',
  kind: 'archive',
  state: 'failed',
  progress: 42,
  downloaded: 420,
  total: 1000,
  speed: 0,
  connections: 0,
  maxConnections: 8,
  bandwidthLimit: null,
  mode: 'segments',
  media: false,
  destination: '/tmp/downloads/project-assets.zip',
  tempPath: '/tmp/downloads/menu-semantics-1.part',
  resumable: true,
  created: 'Just now',
  events: [],
};

const adapter = {
  retryJob: vi.fn(),
  openPath: vi.fn(),
  removeJob: vi.fn(),
} as unknown as DownloadAdapter;

afterEach(() => {
  document.body.replaceChildren();
});

describe('transient menu semantics', () => {
  it('marks sort choices as a named menu with menu items', () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    act(() => root.render(<SortMenu value="created" onChange={() => undefined} />));

    const menu = host.querySelector('.sort-menu');
    expect(menu?.getAttribute('role')).toBe('menu');
    expect(menu?.getAttribute('aria-label')).toBe('Sort downloads');
    expect(menu?.querySelectorAll('[role="menuitemradio"]')).toHaveLength(4);
    expect(Array.from(menu?.querySelectorAll('[role="menuitemradio"]') ?? []).filter((item) => item.getAttribute('aria-checked') === 'true')).toHaveLength(1);
    act(() => root.unmount());
  });

  it('names row context actions and exposes menu items', () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    act(() => {
      root.render(<JobContextMenu job={job} adapter={adapter} onClose={() => undefined} onNotice={() => undefined} />);
    });

    const menu = host.querySelector('.context-menu');
    expect(menu?.getAttribute('role')).toBe('menu');
    expect(menu?.getAttribute('aria-label')).toBe('Actions for project-assets.zip');
    expect(menu?.querySelectorAll('[role="menuitem"]')).toHaveLength(5);
    act(() => root.unmount());
  });
});
