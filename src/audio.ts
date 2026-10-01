/** PCM utilities for the forthcoming live transport. No provider keys in this module. */
export function pcm16(samples: Float32Array): ArrayBuffer {
  const buffer = new ArrayBuffer(samples.length * 2),
    view = new DataView(buffer);
  for (let i = 0; i < samples.length; i++) {
    const n = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, Math.round(n < 0 ? n * 32768 : n * 32767), true);
  }
  return buffer;
}

/** Stateful linear resampling: retains fractional position across callback blocks. */
export class Resampler {
  private samples: number[] = [];
  private position = 0;
  constructor(
    private inputRate: number,
    private outputRate: number,
  ) {
    if (inputRate <= 0 || outputRate <= 0) throw Error('Invalid sample rate');
  }
  push(input: Float32Array): Float32Array {
    this.samples.push(...input);
    const output: number[] = [],
      ratio = this.inputRate / this.outputRate;
    while (this.position + 1 < this.samples.length) {
      const i = Math.floor(this.position),
        t = this.position - i;
      output.push(this.samples[i] * (1 - t) + this.samples[i + 1] * t);
      this.position += ratio;
    }
    const consumed = Math.min(Math.floor(this.position), this.samples.length);
    this.samples.splice(0, consumed);
    this.position -= consumed;
    return new Float32Array(output);
  }
  clear() {
    this.samples = [];
    this.position = 0;
  }
}

export type SpeechSegment = { pcm: Float32Array; reason: 'silence' | 'max_duration';
  durationSeconds: number; voicedSeconds: number; peakRms: number };
/** Simple configurable energy gate, not a claim of validated child-speech VAD. */
export class SpeechGate {
  private preRoll: Float32Array[] = [];
  private frames: Float32Array[] = [];
  private active = false;
  private silence = 0;
  private duration = 0;
  private voiced = 0;
  private peakRms = 0;
  constructor(
    private sampleRate = 16000,
    private threshold = 0.015,
    private pauseSeconds = 1.2,
    private minVoicedSeconds = 0.25,
  ) {}
  get capturing() { return this.active; }
  push(frame: Float32Array): SpeechSegment | null {
    const seconds = frame.length / this.sampleRate;
    const rms = Math.sqrt(frame.reduce((sum, x) => sum + x * x, 0) / Math.max(1, frame.length));
    this.peakRms = Math.max(this.peakRms, rms);
    const speaking = rms >= this.threshold;
    if (!this.active) {
      this.preRoll.push(frame.slice());
      while (
        this.preRoll.reduce((sum, x) => sum + x.length, 0) > this.sampleRate * 0.35 &&
        this.preRoll.length > 1
      )
        this.preRoll.shift();
      if (!speaking) return null;
      this.active = true;
      this.frames = this.preRoll;
      this.preRoll = [];
      this.duration = this.frames.reduce((sum, x) => sum + x.length, 0) / this.sampleRate;
      this.voiced = seconds;
      return null;
    }
    this.frames.push(frame.slice());
    this.duration += seconds;
    this.silence = speaking ? 0 : this.silence + seconds;
    if (speaking) this.voiced += seconds;
    if (this.silence >= this.pauseSeconds || this.duration >= 25) {
      const reason = this.duration >= 25 ? 'max_duration' : 'silence';
      const pcm = new Float32Array(this.frames.reduce((sum, x) => sum + x.length, 0));
      let offset = 0;
      for (const part of this.frames) {
        pcm.set(part, offset);
        offset += part.length;
      }
      const valid = this.voiced >= this.minVoicedSeconds;
      const metadata = { durationSeconds: this.duration, voicedSeconds: this.voiced,
        peakRms: this.peakRms };
      this.clear();
      return valid ? { pcm, reason, ...metadata } : null;
    }
    return null;
  }
  clear() {
    this.preRoll = [];
    this.frames = [];
    this.active = false;
    this.silence = 0;
    this.duration = 0;
    this.voiced = 0;
    this.peakRms = 0;
  }
}

/** Bounded reconnect buffer. Overflow is explicit; it cannot silently drop a request. */
export class AudioBuffer {
  private chunks: ArrayBuffer[] = [];
  private bytes = 0;
  constructor(private maxBytes = 24000 * 2 * 12) {}
  push(chunk: ArrayBuffer) {
    if (this.bytes + chunk.byteLength > this.maxBytes) throw Error('重新連線過久，音訊緩衝已滿');
    this.chunks.push(chunk.slice(0));
    this.bytes += chunk.byteLength;
  }
  drain() {
    const chunks = this.chunks;
    this.clear();
    return chunks;
  }
  clear() {
    this.chunks = [];
    this.bytes = 0;
  }
}
