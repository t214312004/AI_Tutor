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
  const status = event.delivery === 'text_only' ? '僅文字' : event.delivery === 'withheld' ? '未播放' :
    event.role === 'assistant' && event.delivery === 'provider_transcript_not_playback_receipt' ? '語音轉錄，未核對播放' : '';
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
