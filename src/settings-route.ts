import type { SettingsPage } from './types';

const KNOWN_PAGES: ReadonlySet<string> = new Set([
  'general',
  'downloads',
  'browser',
  'network',
  'notifications',
  'appearance',
]);

// Validates the ?settings= deep link (used by the tray "Set Bandwidth Limit"
// item, which navigates to ?settings=network). Unknown, empty, or absent
// values fall back to 'general' so Settings never renders a blank page.
export function settingsPageFromSearch(search: string): SettingsPage {
  const page = new URLSearchParams(search).get('settings');
  if (page !== null && KNOWN_PAGES.has(page)) return page as SettingsPage;
  return 'general';
}
