// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, it } from 'vitest';
import { NotificationsSurface } from './App';
import type { AppSnapshot, NotificationItem } from './types';

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

function notification(id: string, detail: string): NotificationItem {
  return { id, type: 'completed', title: 'Download completed', detail, time: 'Just now', jobId: `job-${id}` };
}

function snapshot(notifications: NotificationItem[]): AppSnapshot {
  return {
    jobs: [],
    settings: { theme: 'light', accent: '#0878ed' } as AppSnapshot['settings'],
    connected: true,
    aggregateSpeed: 0,
    notifications,
  };
}

describe('NotificationsSurface live state', () => {
  it('renders notifications added by a later application snapshot', () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const initial = notification('one', 'first.bin · 1 KB');
    const later = notification('two', 'second.bin · 2 KB');

    act(() => {
      root.render(<NotificationsSurface snapshot={snapshot([initial])} />);
    });
    expect(host.textContent).toContain('first.bin · 1 KB');
    expect(host.textContent).not.toContain('second.bin · 2 KB');

    act(() => {
      root.render(<NotificationsSurface snapshot={snapshot([later, initial])} />);
    });
    expect(host.textContent).toContain('second.bin · 2 KB');

    act(() => root.unmount());
    host.remove();
  });

  it('gives each notification dismissal a distinct accessible name', () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const first = notification('one', 'first.bin · 1 KB');
    const second = notification('two', 'second.bin · 2 KB');

    act(() => {
      root.render(<NotificationsSurface snapshot={snapshot([first, second])} />);
    });
    const dismissals = Array.from(host.querySelectorAll('.notification-close')) as HTMLButtonElement[];
    expect(dismissals.map((button) => button.getAttribute('aria-label'))).toEqual([
      'Dismiss Download completed: first.bin · 1 KB',
      'Dismiss Download completed: second.bin · 2 KB',
    ]);

    act(() => root.unmount());
    host.remove();
  });

  it('keeps explicit dismissals hidden while showing later notifications', () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const initial = notification('one', 'first.bin · 1 KB');
    const later = notification('two', 'second.bin · 2 KB');

    act(() => {
      root.render(<NotificationsSurface snapshot={snapshot([initial])} />);
    });
    const dismiss = host.querySelector('.notification-close');
    expect(dismiss).toBeInstanceOf(HTMLButtonElement);
    expect(dismiss?.getAttribute('aria-label')).toBe('Dismiss Download completed: first.bin · 1 KB');
    act(() => {
      (dismiss as HTMLButtonElement).click();
    });
    expect(host.textContent).toContain("You're all caught up");

    act(() => {
      root.render(<NotificationsSurface snapshot={snapshot([later, initial])} />);
    });
    expect(host.textContent).toContain('second.bin · 2 KB');
    expect(host.textContent).not.toContain('first.bin · 1 KB');

    act(() => root.unmount());
    host.remove();
  });
});
