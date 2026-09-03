import { describe, expect, it } from 'vitest';
import { bandwidthToBps, bpsToParts, sanitizeCapBps } from './bandwidth';

describe('bandwidthToBps', () => {
  it('converts each unit to bytes/sec', () => {
    expect(bandwidthToBps(1, 'KB/s')).toBe(1024);
    expect(bandwidthToBps(0.5, 'MB/s')).toBe(524288);
    expect(bandwidthToBps(2, 'GB/s')).toBe(2 * 1024 ** 3);
  });

  it('maps non-positive and non-finite input to no cap', () => {
    expect(bandwidthToBps(0, 'MB/s')).toBeNull();
    expect(bandwidthToBps(-5, 'MB/s')).toBeNull();
    expect(bandwidthToBps(Number.NaN, 'MB/s')).toBeNull();
    expect(bandwidthToBps(Number.POSITIVE_INFINITY, 'MB/s')).toBeNull();
  });
});

describe('bpsToParts', () => {
  it('round-trips exact caps', () => {
    expect(bpsToParts(524288)).toEqual({ value: 0.5, unit: 'MB/s' });
    expect(bpsToParts(10 * 1024 ** 2)).toEqual({ value: 10, unit: 'MB/s' });
    expect(bpsToParts(2 * 1024 ** 3)).toEqual({ value: 2, unit: 'GB/s' });
    expect(bpsToParts(2048)).toEqual({ value: 2, unit: 'KB/s' });
  });

  it('falls back to a sane default for garbage', () => {
    expect(bpsToParts(0)).toEqual({ value: 50, unit: 'MB/s' });
    expect(bpsToParts(Number.NaN)).toEqual({ value: 50, unit: 'MB/s' });
  });
});

describe('sanitizeCapBps', () => {
  it('passes positive numbers through floored', () => {
    expect(sanitizeCapBps(524288.9)).toBe(524288);
  });

  it('drops zero, negatives, NaN, and non-numbers', () => {
    expect(sanitizeCapBps(0)).toBeUndefined();
    expect(sanitizeCapBps(-1)).toBeUndefined();
    expect(sanitizeCapBps(Number.NaN)).toBeUndefined();
    expect(sanitizeCapBps('fast')).toBeUndefined();
    expect(sanitizeCapBps(undefined)).toBeUndefined();
  });
});
