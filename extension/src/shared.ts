export const APP_BRIDGE_ORIGIN = 'http://127.0.0.1:38217';
export const APP_BRIDGE_TIMEOUT_MS = 1500;

export interface BrowserPolicy {
  interceptDownloads: boolean;
  showMediaButtons: boolean;
  excludedSites: string[];
}

export const DEFAULT_POLICY: BrowserPolicy = {
  interceptDownloads: true,
  showMediaButtons: true,
  excludedSites: [],
};

export function siteOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '').toLowerCase();
  } catch {
    return '';
  }
}

export function siteOfDocument(url: string, referrer: string, ancestorOrigin = ''): string {
  return siteOf(url) || siteOf(referrer) || siteOf(ancestorOrigin);
}

export function mediaSourceFromValues(
  currentSrc: string | null | undefined,
  elementSrc: string | null | undefined,
  childSrc: string | null | undefined,
  baseUrl: string,
): string {
  const candidates = [currentSrc, elementSrc, childSrc]
    .map((value) => value?.trim() || '')
    .filter(Boolean)
    .map((raw) => {
      try {
        return new URL(raw, baseUrl).href;
      } catch {
        return raw;
      }
    });
  return candidates.find((candidate) => isHttp(candidate)) || candidates[0] || '';
}

export function isHttp(url: string): boolean {
  try {
    const scheme = new URL(url).protocol;
    return scheme === 'http:' || scheme === 'https:';
  } catch {
    return false;
  }
}
