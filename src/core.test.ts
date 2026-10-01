import { describe, it, expect } from 'vitest';
import {
  appendTranscript,
  LatestQueue,
  Generation,
  MicState,
  playbackReceipt,
  DurationLedger,
} from './core';
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
  it('preserves manual mute after playback and permits click interruption', () => {
    const m = new MicState();
    m.userEnabled = false;
    m.playbackGate = true;
    m.finished();
    expect(m.effective).toBe(false);
    m.playbackGate = true;
    expect(m.click()).toBe('interrupt');
    expect(m.effective).toBe(true);
  });
  it('never claims a partially played word was heard', () => {
    const r = playbackReceipt(
      '一起試試看',
      [
        { text: '一起', offset: 0, duration: 1e7 },
        { text: '試試看', offset: 1e7, duration: 2e7 },
      ],
      1.5,
    );
    expect(r.fully_heard).toBe('一起');
    expect(r.possibly_partial).toBe('試試看');
  });
  it('keeps English spacing and explicitly records the unplayed tail', () => {
    const r = playbackReceipt(
      'Hello, try again.',
      [
        { text: 'Hello', offset: 0, duration: 1e7 },
        { text: 'try', offset: 1e7, duration: 1e7 },
        { text: 'again', offset: 2e7, duration: 1e7 },
      ],
      1.5,
    );
    expect(r.fully_heard).toBe('Hello');
    expect(r.possibly_partial).toBe('try');
    expect(r.not_yet_heard).toBe(' again.');
  });
  it('replaces cumulative duration and aggregates reconnects', () => {
    const l = new DurationLedger();
    l.update('a', 30);
    l.update('a', 60, true);
    l.update('a', 30);
    l.update('b', 60, true);
    expect(l.estimateUSD).toBeCloseTo(0.1);
    expect(l.complete).toBe(true);
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
