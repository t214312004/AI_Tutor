import { it, expect } from 'vitest';
import { Resampler, pcm16, SpeechGate, AudioBuffer } from './audio';
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
it('keeps pre-roll and tolerates brief pauses', () => {
  const gate = new SpeechGate(1000, 0.015, 1.2);
  const silence = new Float32Array(100),
    voice = new Float32Array(100).fill(0.1);
  for (let i = 0; i < 5; i++) expect(gate.push(silence)).toBeNull();
  for (let i = 0; i < 6; i++) expect(gate.push(voice)).toBeNull();
  for (let i = 0; i < 5; i++) expect(gate.push(silence)).toBeNull();
  expect(gate.push(voice)).toBeNull();
  let segment = null;
  for (let i = 0; i < 14; i++) {
    const result = gate.push(silence);
    if (result) segment = result;
  }
  expect(segment?.reason).toBe('silence');
  expect(segment!.pcm.length).toBeGreaterThan(1800);
  expect(segment!.voicedSeconds).toBeGreaterThan(0.6);
  expect(segment!.peakRms).toBeCloseTo(0.1);
});
it('mute clears unsent reconnect audio and overflow is visible', () => {
  const q = new AudioBuffer(4);
  q.push(new ArrayBuffer(4));
  expect(() => q.push(new ArrayBuffer(1))).toThrow();
  q.clear();
  expect(q.drain()).toEqual([]);
});
it('rejects transient noise shorter than minVoicedSeconds', () => {
  const gate = new SpeechGate(1000, 0.015, 1.2, 0.25);
  const silence = new Float32Array(100);
  const noise = new Float32Array(100).fill(0.1); // 0.1s noise (< 0.25s)
  for (let i = 0; i < 5; i++) expect(gate.push(silence)).toBeNull();
  expect(gate.push(noise)).toBeNull();
  let segment = null;
  for (let i = 0; i < 15; i++) {
    const result = gate.push(silence);
    if (result) segment = result;
  }
  expect(segment).toBeNull();
});
