import { describe, expect, it } from 'vitest';
import { notificationActionJobId } from './notification-action';

describe('notification-action payload', () => {
  it('extracts the job id from a valid payload', () => {
    expect(notificationActionJobId({ jobId: 'job-7' })).toBe('job-7');
  });

  it('rejects missing, empty, and mistyped job ids', () => {
    expect(notificationActionJobId({})).toBeNull();
    expect(notificationActionJobId({ jobId: '' })).toBeNull();
    expect(notificationActionJobId({ jobId: 42 })).toBeNull();
    expect(notificationActionJobId(null)).toBeNull();
  });
});
