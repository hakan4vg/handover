import { DEFAULT_MEDIA_FILTERS, DEFAULT_POLICY, normalizeMediaFilterSettings, siteOf, type BrowserPolicy, type MediaFilterSettings } from './shared';

let policy: BrowserPolicy = { ...DEFAULT_POLICY };
let mediaFilters: MediaFilterSettings = { ...DEFAULT_MEDIA_FILTERS, excludedFileTypes: [...DEFAULT_MEDIA_FILTERS.excludedFileTypes] };
let site = '';
let minimumSizeDraftDirty = false;
let excludedTypesDraftDirty = false;
let mediaFilterDraftRevision = 0;

function mediaSizeMb(): string {
  const value = mediaFilters.minimumSizeBytes / 1_000_000;
  return value === 0 ? '0' : value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
}

function paintMediaFilters(): void {
  const minimum = document.getElementById('minimum-size') as HTMLInputElement | null;
  if (minimum && !minimumSizeDraftDirty && document.activeElement !== minimum) minimum.value = mediaSizeMb();
  const excluded = document.getElementById('excluded-types') as HTMLInputElement | null;
  if (excluded && !excludedTypesDraftDirty && document.activeElement !== excluded) excluded.value = mediaFilters.excludedFileTypes.join(', ');
}

function paint(): void {
  const setSwitch = (id: string, on: boolean) => {
    const element = document.getElementById(id);
    element?.classList.toggle('on', on);
    element?.setAttribute('aria-checked', String(on));
  };
  setSwitch('intercept', policy.interceptDownloads);
  setSwitch('media', policy.showMediaButtons);
  document.getElementById('site')!.textContent = site || 'This page';
  const excluded = site !== '' && policy.excludedSites.includes(site);
  const siteState = document.getElementById('site-state');
  if (siteState) {
    siteState.textContent = !policy.showMediaButtons
      ? 'Media buttons are off globally'
      : excluded
      ? 'Media buttons excluded on this site'
      : 'Media buttons enabled on this site';
    siteState.classList.toggle('excluded-copy', excluded);
    siteState.classList.toggle('enabled-copy', policy.showMediaButtons && !excluded);
  }
  document.getElementById('site-toggle')!.textContent = excluded ? 'Enable on this site' : 'Exclude this site';
  paintMediaFilters();
}

function setStatus(message: string): void {
  const element = document.getElementById('status');
  if (!element) return;
  element.textContent = message;
  element.hidden = message === '';
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

async function push(): Promise<void> {
  paint();
  setStatus('');
  try {
    const response = (await chrome.runtime.sendMessage({ type: 'update-policy', patch: policy })) as {
      ok?: boolean;
      policy?: BrowserPolicy;
      error?: string;
    };
    if (response?.policy) policy = response.policy;
    if (response?.ok === false || !response?.policy) setStatus(response?.error ?? 'Could not save browser integration settings.');
  } catch (reason) {
    setStatus(errorText(reason, 'Could not save browser integration settings.'));
  }
  paint();
}

async function pushMediaFilters(): Promise<void> {
  const minimum = document.getElementById('minimum-size') as HTMLInputElement | null;
  const excluded = document.getElementById('excluded-types') as HTMLInputElement | null;
  const draftRevision = mediaFilterDraftRevision;
  const minimumMb = minimum ? Number(minimum.value) : mediaFilters.minimumSizeBytes / 1_000_000;
  if (!Number.isFinite(minimumMb) || minimumMb < 0) {
    setStatus('Minimum media size must be 0 MB or greater.');
    return;
  }
  const next = normalizeMediaFilterSettings({
    minimumSizeBytes: Math.round(minimumMb * 1_000_000),
    excludedFileTypes: (excluded?.value ?? mediaFilters.excludedFileTypes.join(',')).split(','),
  });
  setStatus('');
  try {
    const response = (await chrome.runtime.sendMessage({ type: 'update-media-filters', filters: next })) as {
      ok?: boolean;
      mediaFilters?: MediaFilterSettings;
      error?: string;
    };
    if (response?.mediaFilters) mediaFilters = normalizeMediaFilterSettings(response.mediaFilters);
    if (response?.ok === false || !response?.mediaFilters) {
      setStatus(response?.error ?? 'Could not save media filter settings.');
      return;
    }
    if (draftRevision !== mediaFilterDraftRevision) return;
    minimumSizeDraftDirty = false;
    excludedTypesDraftDirty = false;
    paintMediaFilters();
  } catch (reason) {
    setStatus(errorText(reason, 'Could not save media filter settings.'));
  }
}

async function pull(): Promise<void> {
  try {
    const response = (await chrome.runtime.sendMessage({ type: 'get-policy', includeMediaFilters: true })) as { policy?: BrowserPolicy; mediaFilters?: MediaFilterSettings };
    if (response?.policy) {
      policy = response.policy;
      if (response.mediaFilters && !minimumSizeDraftDirty && !excludedTypesDraftDirty) mediaFilters = normalizeMediaFilterSettings(response.mediaFilters);
      paint();
    }
  } catch {
    // Keep the last visible state while the service worker restarts.
  }
}

async function init(): Promise<void> {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    site = tab?.url ? siteOf(tab.url) : '';
  } catch {
    site = '';
  }
  try {
    const response = (await chrome.runtime.sendMessage({ type: 'get-policy', includeMediaFilters: true })) as {
      ok?: boolean;
      policy?: BrowserPolicy;
      mediaFilters?: MediaFilterSettings;
      error?: string;
    };
    if (response?.policy) policy = response.policy;
    if (response?.mediaFilters) mediaFilters = normalizeMediaFilterSettings(response.mediaFilters);
    if (response?.ok === false || !response?.policy) setStatus(response?.error ?? 'Could not load browser integration settings.');
  } catch (reason) {
    policy = { ...DEFAULT_POLICY };
    setStatus(errorText(reason, 'Could not load browser integration settings.'));
  }
  document.getElementById('intercept')!.addEventListener('click', () => {
    policy.interceptDownloads = !policy.interceptDownloads;
    void push();
  });
  document.getElementById('media')!.addEventListener('click', () => {
    policy.showMediaButtons = !policy.showMediaButtons;
    void push();
  });
  document.getElementById('site-toggle')!.addEventListener('click', () => {
    if (!site) return;
    policy.excludedSites = policy.excludedSites.includes(site)
      ? policy.excludedSites.filter((item) => item !== site)
      : [...policy.excludedSites, site];
    void push();
  });
  document.getElementById('open')!.addEventListener('click', () => {
    setStatus('');
    void chrome.runtime.sendMessage({ type: 'open-manager' }).then((response: { ok?: boolean; error?: string } | undefined) => {
      if (response?.ok === false) setStatus(response.error ?? 'Could not open Download Manager.');
    }).catch((reason: unknown) => setStatus(errorText(reason, 'Could not open Download Manager.')));
  });
  const minimum = document.getElementById('minimum-size') as HTMLInputElement | null;
  minimum?.addEventListener('input', () => { minimumSizeDraftDirty = true; mediaFilterDraftRevision += 1; });
  minimum?.addEventListener('change', () => { void pushMediaFilters(); });
  minimum?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      void pushMediaFilters();
    }
  });
  const excluded = document.getElementById('excluded-types') as HTMLInputElement | null;
  excluded?.addEventListener('input', () => { excludedTypesDraftDirty = true; mediaFilterDraftRevision += 1; });
  excluded?.addEventListener('change', () => { void pushMediaFilters(); });
  excluded?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      void pushMediaFilters();
    }
  });
  paint();
  window.setInterval(() => void pull(), 1000);
}

void init();
