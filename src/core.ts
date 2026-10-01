// Session-owned state. Late results must never affect a new student or turn.
export class Generation {
  private n = 0;
  next() {
    return ++this.n;
  }
  current() {
    return this.n;
  }
  accepts(n: number) {
    return n === this.n;
  }
}

export type ConversationMessage = { id: string; role: string; text: string; status?: string };
export function appendTranscript(
  items: ConversationMessage[],
  event: { role: string; text: string; transcript_id?: string; delivery?: string },
  teacherName: string,
): ConversationMessage[] {
  const text = (event.role === 'user' ? '你說：' : teacherName + '：') + event.text;
  const status = event.delivery === 'text_only' ? '僅文字' : '';
  if (event.transcript_id) {
    const index = items.findIndex((item) => item.id === event.transcript_id);
    if (index >= 0) {
      const updated = [...items];
      updated[index] = { ...updated[index], text, status };
      return updated;
    }
  }
  return [
    ...items,
    { id: event.transcript_id || crypto.randomUUID(), role: event.role, text, status },
  ].slice(-500);
}

export class LatestQueue<T> {
  private pending: T | undefined;
  private active = false;
  private closed = false;
  skipped = 0;
  constructor(
    private work: (value: T) => Promise<void>,
    private failure: (error: unknown) => void = () => {},
  ) {}
  push(value: T) {
    if (this.closed) return;
    if (this.pending !== undefined) this.skipped++;
    this.pending = value;
    if (!this.active) void this.drain();
  }
  close() {
    this.closed = true;
    this.pending = undefined;
  }
  private async drain() {
    this.active = true;
    try {
      while (!this.closed && this.pending !== undefined) {
        const value = this.pending;
        this.pending = undefined;
        try {
          await this.work(value);
        } catch (e) {
          this.failure(e);
        }
      }
    } finally {
      this.active = false;
    }
  }
}

export type Boundary = { offset: number; duration: number; text: string };
export function playbackReceipt(text: string, boundaries: Boundary[], seconds: number) {
  const ticks = Math.max(0, seconds) * 10_000_000;
  const completeTokens = boundaries.filter((b) => b.offset + b.duration <= ticks);
  let cursor = 0,
    aligned = true;
  for (const token of completeTokens) {
    const start = text.indexOf(token.text, cursor);
    if (start < 0) {
      aligned = false;
      break;
    }
    cursor = start + token.text.length;
  }
  const complete = aligned ? text.slice(0, cursor) : completeTokens.map((b) => b.text).join(' ');
  const partial =
    boundaries.find((b) => b.offset < ticks && b.offset + b.duration > ticks)?.text ?? '';
  // If alignment fails, conservatively retain the full unconfirmed text.
  const partialStart = partial ? text.indexOf(partial, cursor) : -1;
  const remainingStart = partialStart >= 0 ? partialStart + partial.length : cursor;
  return {
    fully_heard: complete,
    possibly_partial: partial,
    not_yet_heard: aligned ? text.slice(remainingStart) : text,
    full_planned_text: text,
    playback_seconds: seconds,
    warning: '只有 fully_heard 可視為已聽到；其餘內容不得假設學生已聽到。',
  };
}

export class MicState {
  userEnabled = true;
  playbackGate = false;
  get effective() {
    return this.userEnabled && !this.playbackGate;
  }
  click() {
    if (this.playbackGate) {
      this.playbackGate = false;
      this.userEnabled = true;
      return 'interrupt';
    }
    this.userEnabled = !this.userEnabled;
    return 'toggle';
  }
  finished() {
    this.playbackGate = false;
  }
}

export class DurationLedger {
  private sessions = new Map<string, { seconds: number; final: boolean }>();
  update(id: string, seconds: number, final = false) {
    if (!Number.isFinite(seconds) || seconds < 0) throw new Error('Invalid usage');
    const old = this.sessions.get(id);
    if (old?.final) return;
    this.sessions.set(id, { seconds: Math.max(old?.seconds ?? 0, seconds), final });
  }
  get estimateUSD() {
    return [...this.sessions.values()].reduce((sum, s) => sum + (s.seconds / 60) * 0.05, 0);
  }
  get complete() {
    return this.sessions.size > 0 && [...this.sessions.values()].every((s) => s.final);
  }
}
