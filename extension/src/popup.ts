import { DEFAULT_POLICY, siteOf, type BrowserPolicy } from './shared';

let policy: BrowserPolicy = { ...DEFAULT_POLICY };
let site = '';

function paint(): void {
  const setSwitch = (id: string, on: boolean) => document.getElementById(id)?.classList.toggle('on', on);
  setSwitch('intercept', policy.interceptDownloads);
  setSwitch('media', policy.showMediaButtons);
  document.getElementById('site')!.textContent = site || 'This page';
  const excluded = site !== '' && policy.excludedSites.includes(site);
  document.getElementById('site-state')!.textContent = excluded
    ? 'Media buttons excluded on this site'
    : 'Media buttons enabled on this site';
  document.getElementById('site-toggle')!.textContent = excluded ? 'Enable on this site' : 'Exclude this site';
}

async function push(): Promise<void> {
  paint();
  try {
    const response = (await chrome.runtime.sendMessage({ type: 'update-policy', patch: policy })) as {
      policy?: BrowserPolicy;
    };
    if (response?.policy) policy = response.policy;
  } catch {
    // Background unreachable (e.g. popup open in a plain tab during dev).
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
      policy?: BrowserPolicy;
    };
    if (response?.policy) policy = response.policy;
  } catch {
    policy = { ...DEFAULT_POLICY };
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
    void chrome.runtime.sendMessage({ type: 'open-manager' }).catch(() => undefined);
  });
  paint();
}

void init();
