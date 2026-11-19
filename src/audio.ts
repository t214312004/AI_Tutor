/** PCM utilities for live audio transport. No provider keys in this module. */
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
    if (this.inputRate === this.outputRate && this.samples.length === 0 && this.position === 0)
      return input;
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
