import { describe, expect, it } from 'vitest';
import { resumePlan } from './adapters';

describe('resumePlan', () => {
  it('keeps a ready provisional in the save decision', () => {
    expect(resumePlan(true, 100)).toEqual({ state: 'finalizing', shouldStart: false });
  });

  it('starts ordinary paused jobs again', () => {
    expect(resumePlan(true, 99.9)).toEqual({ state: 'downloading', shouldStart: true });
    expect(resumePlan(false, 100)).toEqual({ state: 'downloading', shouldStart: true });
    expect(resumePlan(undefined, 0)).toEqual({ state: 'downloading', shouldStart: true });
  });
});
