export type BandwidthUnit = 'KB/s' | 'MB/s' | 'GB/s';

export const BANDWIDTH_UNITS: BandwidthUnit[] = ['KB/s', 'MB/s', 'GB/s'];

/** Convert a user-entered value+unit into bytes/sec. Non-positive or
 *  non-finite input means "no cap" (null), never zero — a zero cap would
 *  wedge the transfer pacing loop. */
export function bandwidthToBps(value: number, unit: BandwidthUnit): number | null {
  if (!Number.isFinite(value) || value <= 0) return null;
  const multiplier = unit === 'GB/s' ? 1024 ** 3 : unit === 'MB/s' ? 1024 ** 2 : 1024;
  return Math.floor(value * multiplier);
}

/** Split bytes/sec back into a value+unit pair for prefilling the cap field
 *  from an existing job. Prefers exact units, rounds MB to one decimal. */
export function bpsToParts(bps: number): { value: number; unit: BandwidthUnit } {
  if (!Number.isFinite(bps) || bps <= 0) return { value: 50, unit: 'MB/s' };
  if (bps >= 1024 ** 3 && bps % 1024 ** 3 === 0) return { value: bps / 1024 ** 3, unit: 'GB/s' };
  const mb = bps / 1024 ** 2;
  // Prefer MB at/above 1 MiB, or below it when the value is exact to one
  // decimal (0.5 MiB stays "0.5 MB/s" instead of degrading to "512 KB/s").
  if (mb >= 1 || (mb >= 0.1 && Math.abs(mb * 10 - Math.round(mb * 10)) < 1e-9)) {
    return { value: Math.round(mb * 10) / 10, unit: 'MB/s' };
  }
  return { value: Math.max(1, Math.round(bps / 1024)), unit: 'KB/s' };
}

/** Sanitize a raw bytes/sec cap from any input surface (adapter, messages).
 *  Positive integers pass through; everything else means no cap. */
export function sanitizeCapBps(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? Math.floor(value)
    : undefined;
}
