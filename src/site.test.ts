import { describe, expect, it } from 'vitest';
import { normalizeSite } from './site';

describe('normalizeSite', () => {
  it('stores the hostname used by browser policy matching', () => {
    expect(normalizeSite('https://www.Example.com/watch/')).toBe('example.com');
    expect(normalizeSite('example.com:8443/path')).toBe('example.com');
  });
});
