import { it, expect } from 'vitest';
import { Resampler, pcm16 } from './audio';
it('produces equivalent resampling across block boundaries', () => {
  const signal = Float32Array.from({ length: 4800 }, (_, i) => Math.sin(i / 20));
  const one = new Resampler(48000, 16000).push(signal);
  const streaming = new Resampler(48000, 16000),
    parts: number[] = [];
  for (let i = 0; i < signal.length; i += 128)
    parts.push(...streaming.push(signal.slice(i, i + 128)));
  expect(parts).toEqual(Array.from(one));
});
it('does not carry a sample across a mute boundary', () => {
  const resampler = new Resampler(48000, 16000);
  resampler.push(new Float32Array(4).fill(1));
  resampler.clear();
  expect(Array.from(resampler.push(new Float32Array(4)))).toEqual([0]);
});
it('clips PCM16 and uses little endian', () => {
  const view = new DataView(pcm16(new Float32Array([-2, 0, 2])));
  expect(view.getInt16(0, true)).toBe(-32768);
  expect(view.getInt16(2, true)).toBe(0);
  expect(view.getInt16(4, true)).toBe(32767);
});
