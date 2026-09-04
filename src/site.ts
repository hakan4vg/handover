export function normalizeSite(value: string): string {
  const input = value.trim();
  if (!input) return '';
  const candidate = input.startsWith('//')
    ? `https:${input}`
    : /^[a-z][a-z\d+.-]*:\/\//i.test(input)
      ? input
      : `https://${input}`;
  try {
    return new URL(candidate).hostname.replace(/^www\./i, '').toLowerCase();
  } catch {
    return input
      .replace(/^[a-z][a-z\d+.-]*:\/\//i, '')
      .split(/[/?#]/, 1)[0]
      .replace(/^www\./i, '')
      .toLowerCase();
  }
}
