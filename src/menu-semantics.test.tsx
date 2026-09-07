// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { JobContextMenu, Inspector, SortMenu } from './App';
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

  it('moves focus through menu items and dismisses on Escape', () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const onDismiss = vi.fn();
    act(() => root.render(<SortMenu value="created" onChange={() => undefined} onDismiss={onDismiss} />));

    const menu = host.querySelector('.sort-menu') as HTMLDivElement;
    const items = Array.from(menu.querySelectorAll('[role="menuitemradio"]')) as HTMLButtonElement[];
    expect(document.activeElement).toBe(items[0]);
    act(() => {
      items[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
    });
    expect(document.activeElement).toBe(items[1]);
    act(() => {
      items[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }));
    });
    expect(document.activeElement).toBe(items[3]);
    act(() => {
      menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    expect(onDismiss).toHaveBeenCalledOnce();

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

  it('exposes inspector tabs and their labelled panel relationship', () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    act(() => {
      root.render(<Inspector job={job} adapter={adapter} onClose={() => undefined} />);
    });

    const tablist = host.querySelector('.inspector-tabs');
    expect(tablist?.getAttribute('role')).toBe('tablist');
    expect(tablist?.getAttribute('aria-label')).toBe('Download details');
    const tabs = Array.from(tablist?.querySelectorAll('[role="tab"]') ?? []);
    expect(tabs.map((tab) => tab.textContent)).toEqual(['Overview', 'Network', 'Files', 'Log']);
    expect(tabs.map((tab) => tab.getAttribute('aria-selected'))).toEqual(['true', 'false', 'false', 'false']);
    expect(tabs.map((tab) => tab.getAttribute('tabindex'))).toEqual(['0', '-1', '-1', '-1']);
    const panel = host.querySelector('.inspector-scroll');
    expect(panel?.getAttribute('role')).toBe('tabpanel');
    expect(panel?.getAttribute('aria-labelledby')).toBe(tabs[0]?.id);
    expect(tabs.every((tab) => tab.getAttribute('aria-controls') === panel?.id)).toBe(true);
    act(() => {
      (tabs[0] as HTMLButtonElement).dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    });
    const selectedAfterArrow = Array.from(tablist?.querySelectorAll('[role="tab"]') ?? []).filter((tab) => tab.getAttribute('aria-selected') === 'true');
    expect(selectedAfterArrow.map((tab) => tab.textContent)).toEqual(['Network']);
    expect(panel?.getAttribute('aria-labelledby')).toBe(selectedAfterArrow[0]?.id);
    act(() => root.unmount());
  });
});
