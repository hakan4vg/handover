// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it } from 'vitest';
import { DownloadRow } from './App';
import type { DownloadJob } from './types';

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

const job: DownloadJob = {
  id: 'row-labels-1',
  name: 'project-assets.zip',
  source: 'https://example.com/project-assets.zip',
  domain: 'example.com',
  kind: 'archive',
  state: 'downloading',
  progress: 42,
  downloaded: 420,
  total: 1000,
  speed: 128,
  connections: 2,
  maxConnections: 8,
  bandwidthLimit: null,
  mode: 'segments',
  media: false,
  destination: '/tmp/downloads/project-assets.zip',
  tempPath: '/tmp/downloads/row-labels-1.part',
  resumable: true,
  created: 'Just now',
  events: [],
};

afterEach(() => {
  document.body.replaceChildren();
});

describe('DownloadRow action names', () => {
  it('identifies the job in state and overflow actions', () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    act(() => {
      root.render(
        <DownloadRow
          job={job}
          selected={false}
          onSelect={() => undefined}
          onPause={() => undefined}
          onResume={() => undefined}
          onRetry={() => undefined}
          onMenu={() => undefined}
        />,
      );
    });

    expect(host.querySelector('.row-action')?.getAttribute('aria-label')).toBe('Pause project-assets.zip');
    expect(host.querySelectorAll('.row-action')[1]?.getAttribute('aria-label')).toBe('More actions for project-assets.zip');
    act(() => root.unmount());
  });

  it('keeps row selection separate from nested action buttons', () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    act(() => {
      root.render(
        <DownloadRow
          job={job}
          selected
          onSelect={() => undefined}
          onPause={() => undefined}
          onResume={() => undefined}
          onRetry={() => undefined}
          onMenu={() => undefined}
        />,
      );
    });
    const row = host.querySelector('.download-row');
    expect(row?.getAttribute('role')).toBe('group');
    expect(row?.getAttribute('aria-label')).toBe('Select project-assets.zip');
    expect(row?.getAttribute('aria-current')).toBe('true');
    expect(row?.querySelectorAll('button')).toHaveLength(2);

    act(() => root.unmount());
  });

  it('keeps state-specific action names attached to the same job', () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const cases: Array<[DownloadJob['state'], string | undefined]> = [
      ['downloading', 'Pause project-assets.zip'],
      ['paused', 'Resume project-assets.zip'],
      ['pending', 'Resume project-assets.zip'],
      ['failed', 'Retry project-assets.zip'],
      ['completed', undefined],
    ];

    for (const [state, expectedAction] of cases) {
      act(() => {
        root.render(
          <DownloadRow
            job={{ ...job, state }}
            selected={false}
            onSelect={() => undefined}
            onPause={() => undefined}
            onResume={() => undefined}
            onRetry={() => undefined}
            onMenu={() => undefined}
          />,
        );
      });
      const actions = Array.from(host.querySelectorAll('.row-action'));
      const stateAction = actions.find((button) => !button.getAttribute('aria-label')?.startsWith('More actions for '));
      expect(stateAction?.getAttribute('aria-label')).toBe(expectedAction);
      expect(actions.at(-1)?.getAttribute('aria-label')).toBe('More actions for project-assets.zip');
    }

    act(() => root.unmount());
  });
});
