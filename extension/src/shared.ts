export const NATIVE_HOST = 'com.downloadmanager.host';

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
  const raw = currentSrc?.trim() || elementSrc?.trim() || childSrc?.trim() || '';
  if (!raw) return '';
  try {
    return new URL(raw, baseUrl).href;
  } catch {
    return raw;
  }
}

export function isHttp(url: string): boolean {
  try {
    const scheme = new URL(url).protocol;
    return scheme === 'http:' || scheme === 'https:';
  } catch {
    return false;
  }
}
