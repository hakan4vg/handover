import { describe, expect, it } from 'vitest';
import { formatTime } from './App';

describe('formatTime', () => {
  it('renders today’s instants as Today', () => {
    expect(formatTime(new Date().toISOString())).toMatch(/^Today, /);
  });

  it('renders yesterday’s instants as Yesterday', () => {
    const yesterday = new Date();
    yesterday.setDate(yesterday.getDate() - 1);
    expect(formatTime(yesterday.toISOString())).toMatch(/^Yesterday, /);
  });

  it('passes legacy and mock display strings through untouched', () => {
    expect(formatTime('Just now')).toBe('Just now');
    expect(formatTime('Today, 9:41 AM')).toBe('Today, 9:41 AM');
    expect(formatTime('')).toBe('');
  });

  it('shows the year for older stamps', () => {
    const rendered = formatTime('2020-01-02T03:04:05Z');
    expect(rendered).toContain('2020');
  });
});
