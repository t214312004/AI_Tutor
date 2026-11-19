import { describe, it, expect } from 'vitest';
import { appendTranscript, Generation, LatestQueue } from './core';
import { homography, defaultCorners } from './camera';
describe('session contracts', () => {
  it('keeps only the latest queued frame without cancelling active work', async () => {
    const seen: number[] = [];
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const q = new LatestQueue<number>(async (n) => {
      seen.push(n);
      if (n === 1) await gate;
    });
    q.push(1);
    q.push(2);
    q.push(3);
    expect(seen).toEqual([1]);
    expect(q.skipped).toBe(1);
    release();
    await new Promise((r) => setTimeout(r, 0));
    expect(seen).toEqual([1, 3]);
    q.close();
    q.push(4);
    expect(seen).toEqual([1, 3]);
  });
  it('isolates late results', () => {
    const g = new Generation();
    const before = g.next();
    g.next();
    expect(g.accepts(before)).toBe(false);
  });
  it('updates one live transcript message as cumulative text arrives', () => {
    const first = appendTranscript(
      [],
      { role: 'user', text: '七加', transcript_id: 'turn-1' },
      '伴讀老師',
    );
    const second = appendTranscript(
      first,
      { role: 'user', text: '七加五？', transcript_id: 'turn-1' },
      '伴讀老師',
    );
    expect(second).toEqual([{ id: 'turn-1', role: 'user', text: '你說：七加五？', status: '' }]);
  });
  it('rectifies identity and rejects crossed calibration', () => {
    expect(homography(defaultCorners)).toEqual([1, 0, 0, 0, 1, 0, 0, 0]);
    expect(() =>
      homography([
        [0, 0],
        [1, 1],
        [1, 0],
        [0, 1],
      ]),
    ).toThrow();
  });
});
