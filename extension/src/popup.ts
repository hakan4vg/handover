import { DEFAULT_POLICY, siteOf, type BrowserPolicy } from './shared';

let policy: BrowserPolicy = { ...DEFAULT_POLICY };
let site = '';

function paint(): void {
  const setSwitch = (id: string, on: boolean) => {
    const element = document.getElementById(id);
    element?.classList.toggle('on', on);
    element?.setAttribute('aria-pressed', String(on));
  };
  setSwitch('intercept', policy.interceptDownloads);
  setSwitch('media', policy.showMediaButtons);
  document.getElementById('site')!.textContent = site || 'This page';
  const excluded = site !== '' && policy.excludedSites.includes(site);
  document.getElementById('site-state')!.textContent = excluded
    ? 'Media buttons excluded on this site'
    : 'Media buttons enabled on this site';
  document.getElementById('site-toggle')!.textContent = excluded ? 'Enable on this site' : 'Exclude this site';
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

async function init(): Promise<void> {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    site = tab?.url ? siteOf(tab.url) : '';
  } catch {
    site = '';
  }
  try {
    const response = (await chrome.runtime.sendMessage({ type: 'get-policy' })) as {
      ok?: boolean;
      policy?: BrowserPolicy;
      error?: string;
    };
    if (response?.policy) policy = response.policy;
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
  paint();
}

void init();
