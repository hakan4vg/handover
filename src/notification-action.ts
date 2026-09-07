/** Parse a `notification-action` event payload into a job id, or null. */
export function notificationActionJobId(payload: unknown): string | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const jobId = (payload as { jobId?: unknown }).jobId;
  return typeof jobId === 'string' && jobId.length > 0 ? jobId : null;
}
