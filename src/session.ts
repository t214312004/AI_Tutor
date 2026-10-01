import { pcm16, Resampler, SpeechGate } from './audio';
import { Boundary, playbackReceipt } from './core';

type Speech = {
  type: 'speech';
  session_id: string;
  generation: number;
  playback_id: string;
  text: string;
  audio: string;
  boundaries: Boundary[];
};
type Photo = string | { image: string; meta: Record<string, unknown> } | null;
type Callbacks = {
  event: (event: any) => void;
  error: (message: string) => void;
  capture: () => Photo | Promise<Photo>;
  board: (html: string, close: boolean) => Promise<void>;
  playbackGate: (value: boolean) => void;
  caption: (text: string) => void;
};
function encode(bytes: ArrayBuffer) {
  let text = '';
  for (const value of new Uint8Array(bytes)) text += String.fromCharCode(value);
  return btoa(text);
}

/** Owns only the transport and audio graph. The view remains the sole device owner. */
export class BasicConnection {
  private socket: WebSocket | null = null;
  private context: AudioContext | null = null;
  private captureNode: AudioWorkletNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private generation = 0;
  private boardRevision = 0;
  private userMic = false;
  private pendingUtterance: string | null = null;
  private inputEpoch = 0;
  private playback = false;
  private gate = new SpeechGate();
  private player: HTMLAudioElement | null = null;
  private speech: Speech | null = null;
  private audioURL = '';
  private closed = false;
  private outputContext: AudioContext | null = null;
  private outputNodes = new Set<AudioBufferSourceNode>();
  private outputUntil = 0;
  private outputEpoch = 0;
  private outputTask: Promise<void> = Promise.resolve();
  private closeAck: (() => void) | null = null;
  private closing: Promise<void> | null = null;
  private recovering = false;
  private photoTransport: { base: string; token: string } | null = null;
  private photoUploads = new Set<AbortController>();
  constructor(
    private id: string,
    private callbacks: Callbacks,
    private mode: 'basic' | 'gpt' | 'gemini' = 'basic',
  ) {}
  async connect(base: string, token: string, route = `/sessions/${this.id}/stream`) {
    this.photoTransport = { base: base.replace(/\/$/, ''), token };
    const socket = new WebSocket(base.replace(/^http/, 'ws') + route);
    this.socket = socket;
    return new Promise<void>((resolve, reject) => {
      let ready = false;
      const timer = setTimeout(() => {
        socket.close();
        reject(Error('教學連線逾時'));
      }, this.recovering ? 5000 : 30000);
      socket.onopen = () => socket.send(JSON.stringify({ type: 'authenticate', token }));
      socket.onerror = () => {
        clearTimeout(timer);
        reject(Error('無法建立教學連線'));
      };
      socket.onclose = (event) => {
        clearTimeout(timer);
        if (this.socket !== socket) return;
        this.socket = null;
        this.cancelPhotos();
        this.closeAck?.();
        if (!this.closed && ready) {
          this.stopPlayback(false);
          this.clearPCM();
          this.pauseCapture();
          this.callbacks.event({ type: 'transport_lost', code: event.code });
          void this.recover(base, token, route);
        }
        reject(Error('教學連線已關閉'));
      };
      socket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'closed') this.closeAck?.();
          if (data.type === 'ready') {
            clearTimeout(timer);
            ready = true;
            this.generation = data.generation;
            resolve();
          }
          void this.receive(data).catch((e) => this.callbacks.error(String(e)));
        } catch {
          this.callbacks.error('教學訊息格式錯誤');
        }
      };
    });
  }
  private async recover(base: string, token: string, route: string) {
    if (this.recovering || this.closed) return;
    this.recovering = true;
    const deadline = Date.now() + 35000;
    let attempt = 0;
    try {
      while (!this.closed && Date.now() < deadline) {
        try {
          this.callbacks.event({ type: 'transport_retry', attempt: ++attempt });
          await this.connect(base, token, route);
          if (this.closed) return;
          if (this.socket?.readyState !== WebSocket.OPEN) throw Error('重連後連線已關閉');
          this.callbacks.event({ type: 'transport_restored' });
          return;
        } catch {
          if (this.closed) return;
          await new Promise((resolve) => setTimeout(resolve, 1500));
        }
      }
      if (!this.closed) {
        this.callbacks.event({ type: 'media_stopped' });
        this.callbacks.error('教學連線無法恢復，請結束本次陪讀後重新開始。');
      }
    } finally {
      this.recovering = false;
    }
  }
  private send(message: Record<string, unknown>) {
    if (this.closed || this.socket?.readyState !== WebSocket.OPEN) {
      if (message.type === 'audio') throw Error('教學連線尚未就緒，這句沒有送出');
      return;
    }
    if (this.socket.bufferedAmount > 2_000_000 &&
        ['audio', 'frame', 'activity'].includes(String(message.type))) {
      this.pauseCapture();
      this.callbacks.event({ type: 'transport_backpressure', kind: message.type,
        buffered_bytes: this.socket.bufferedAmount });
      throw Error('網路傳送不及，收音已停止');
    }
    this.socket.send(JSON.stringify({ ...message, session_id: this.id }));
  }
  private async sendFrame(message: Record<string, unknown>) {
    if (this.mode !== 'gpt' || !String(message.image).startsWith('data:image/png;base64,')) {
      this.send(message);
      return;
    }
    if (this.closed || this.socket?.readyState !== WebSocket.OPEN || !this.photoTransport) return;
    if (this.photoUploads.size >= 2) throw Error('照片仍在傳送，請稍後重拍；語音可繼續使用');
    const controller = new AbortController();
    this.photoUploads.add(controller);
    const transport = this.photoTransport;
    const started = performance.now();
    const imageChars = String(message.image).length;
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 15000);
    this.callbacks.event({ type: 'photo_upload', stage: 'start', image_chars: imageChars });
    try {
      const response = await fetch(`${transport.base}/sessions/${this.id}/frame`, {
        method: 'POST', signal: controller.signal,
        headers: { Authorization: `Bearer ${transport.token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ image: message.image, request_id: message.request_id,
          capture_meta: message.capture_meta }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw Error(typeof body.detail === 'string' ? body.detail : `照片傳送失敗（HTTP ${response.status}）`);
      }
      this.callbacks.event({ type: 'photo_upload', stage: 'done', image_chars: imageChars,
        elapsed_ms: Math.round(performance.now() - started) });
    } catch (error) {
      if (controller.signal.aborted && !timedOut) return;
      this.callbacks.event({ type: 'photo_upload', stage: 'error', image_chars: imageChars,
        elapsed_ms: Math.round(performance.now() - started) });
      throw timedOut ? Error('照片傳送逾時，語音可繼續使用；請稍後重拍') : error;
    } finally {
      clearTimeout(timer);
      this.photoUploads.delete(controller);
    }
  }
  private cancelPhotos() {
    for (const controller of this.photoUploads) controller.abort();
  }
  async attachMicrophone(stream: MediaStream) {
    await this.detachMicrophone();
    if (this.closed) return;
    const rate = this.mode === 'gpt' ? 24000 : 16000;
    const context = new AudioContext({ sampleRate: rate });
    this.context = context;
    try {
      await context.audioWorklet.addModule(new URL('./capture.worklet.js', import.meta.url));
      if (this.closed || this.context !== context) return;
      this.source = context.createMediaStreamSource(stream);
      this.captureNode = new AudioWorkletNode(context, 'tutor-capture');
    } catch (error) {
      if (this.context === context) await this.detachMicrophone();
      throw error;
    }
    const resampler = new Resampler(context.sampleRate, rate);
    let pending: number[] = [];
    let inputEpoch = this.inputEpoch;
    let lastActivity = 0;
    this.captureNode.port.onmessage = (event) => {
      if (inputEpoch !== this.inputEpoch) {
        pending = [];
        resampler.clear();
        inputEpoch = this.inputEpoch;
      }
      if (!this.userMic || this.playback || this.pendingUtterance || this.closed) return;
      if (this.mode === 'basic') {
        const samples = event.data as Float32Array;
        const rms = Math.sqrt(
          samples.reduce((sum, x) => sum + x * x, 0) / Math.max(1, samples.length),
        );
        if (rms >= 0.015 && performance.now() - lastActivity > 1000) {
          lastActivity = performance.now();
          try {
            this.send({ type: 'activity' });
          } catch (e) {
            this.callbacks.error(String(e));
          }
        }
      }
      if (this.mode !== 'basic') {
        pending.push(...resampler.push(event.data));
        if (pending.length >= rate / 10) {
          try {
            this.send({ type: 'audio', pcm: encode(pcm16(new Float32Array(pending))) });
          } catch (e) {
            this.callbacks.error(String(e));
          }
          pending = [];
        }
        return;
      }
      const wasCapturing = this.gate.capturing;
      const segment = this.gate.push(resampler.push(event.data));
      if (!wasCapturing && this.gate.capturing)
        this.callbacks.event({ type: 'audio_stage', stage: 'capturing' });
      if (segment) {
        const utterance_id = crypto.randomUUID();
        this.pendingUtterance = utterance_id;
        this.inputEpoch++;
        this.syncMic();
        this.callbacks.event({ type: 'audio_stage', stage: 'sending' });
        try {
          this.send({ type: 'audio', pcm: encode(pcm16(segment.pcm)), utterance_id,
            audio_meta: { reason:segment.reason,
              duration_seconds:Math.round(segment.durationSeconds*100)/100,
              voiced_seconds:Math.round(segment.voicedSeconds*100)/100,
              peak_rms:Math.round(segment.peakRms*1000)/1000 } });
        } catch (e) {
          this.pendingUtterance = null;
          this.syncMic();
          this.callbacks.event({ type: 'audio_stage', stage: 'rejected' });
          this.callbacks.error(String(e));
        }
      } else if (wasCapturing && !this.gate.capturing)
        this.callbacks.event({ type: 'audio_stage', stage: 'ready' });
    };
    try {
      this.source.connect(this.captureNode);
      // Keep the graph scheduled, while never routing the microphone to speakers.
      const silent = context.createGain();
      silent.gain.value = 0;
      this.captureNode.connect(silent);
      silent.connect(context.destination);
      await context.resume();
      this.syncMic();
    } catch (error) {
      if (this.context === context) await this.detachMicrophone();
      throw error;
    }
  }
  setMic(enabled: boolean) {
    this.inputEpoch++;
    this.userMic = enabled;
    if (!enabled) this.pendingUtterance = null;
    this.gate.clear();
    this.syncMic();
    this.send({ type: 'mic', enabled });
    this.callbacks.event({ type: 'audio_stage', stage: enabled ? 'ready' : 'off' });
  }
  private syncMic() {
    this.captureNode?.port.postMessage(this.userMic && !this.playback && !this.pendingUtterance && !this.closed);
  }
  private pauseCapture() {
    this.inputEpoch++;
    this.userMic = false;
    this.pendingUtterance = null;
    this.gate.clear();
    this.syncMic();
  }
  setCamera(enabled: boolean) {
    if (!enabled) this.cancelPhotos();
    this.send({ type: 'camera', enabled });
  }
  async frame(image: string, capture_meta?: Record<string, unknown>) {
    await this.sendFrame({ type: 'frame', image, capture_meta });
  }
  begin() {
    if (this.mode === 'basic') this.send({ type: 'begin' });
  }
  text(text: string) {
    this.send({ type: 'text', text });
  }
  lesson(action: 'confirm' | 'done' | 'todo', task_id?: string) {
    this.send({ type: 'lesson', action, task_id });
  }
  async interrupt(enableMic = true) {
    const receipt =
      this.speech && this.player
        ? playbackReceipt(this.speech.text, this.speech.boundaries, this.player.currentTime)
        : {};
    const playback_id = this.speech?.playback_id;
    if (playback_id)
      this.callbacks.event({
        type: 'speech_stopped',
        playback_id,
        text: 'fully_heard' in receipt ? receipt.fully_heard : '',
      });
    this.generation++;
    this.inputEpoch++;
    this.pendingUtterance = null;
    this.stopPlayback(false);
    this.gate.clear();
    this.userMic = enableMic;
    this.syncMic();
    this.send({ type: 'interrupt', playback_id, receipt });
    this.send({ type: 'mic', enabled: this.userMic });
    this.callbacks.event({ type: 'audio_stage', stage: this.userMic ? 'ready' : 'off' });
  }
  private async receive(event: any) {
    if (this.closed || (event.session_id && event.session_id !== this.id)) return;
    if (typeof event.generation === 'number' && event.generation < this.generation) return;
    if (event.type === 'interrupted') this.generation = event.generation;
    if (event.type === 'audio_status') {
      if (event.utterance_id && event.utterance_id !== this.pendingUtterance) return;
      if (event.stage === 'ignored' || event.stage === 'rejected') {
        this.pendingUtterance = null;
        this.syncMic();
      }
      this.callbacks.event({ type: 'audio_stage', stage: event.stage });
      return;
    }
    if (event.type === 'capture_requested') {
      try {
        const capture = await this.callbacks.capture();
        if (!capture || this.closed) return;
        await this.sendFrame({ type: 'frame', request_id: event.request_id,
          image: typeof capture === 'string' ? capture : capture.image,
          capture_meta: typeof capture === 'string' ? undefined : capture.meta });
      } catch (e) {
        this.callbacks.error(String(e));
      }
    } else if (event.type === 'board' && event.revision > this.boardRevision) {
      this.boardRevision = event.revision;
      await this.callbacks.board(event.html, event.close);
    } else if (event.type === 'speech') await this.play(event);
    else if (event.type === 'idle') {
      this.pendingUtterance = null;
      this.syncMic();
      this.callbacks.event({ type: 'audio_stage', stage: 'ready' });
      this.callbacks.event(event);
    }
    else if (event.type === 'pcm') await this.playPCM(event.audio, event.rate);
    else if (event.type === 'audio_clear') this.clearPCM();
    else if (event.type === 'error') {
      if (this.pendingUtterance) {
        this.pendingUtterance = null;
        this.syncMic();
        this.callbacks.event({ type: 'audio_stage', stage: 'rejected' });
      }
      this.callbacks.event(event);
    }
    else if (event.type === 'media_stopped') {
      this.pauseCapture();
      this.clearPCM();
      this.callbacks.event(event);
    } else this.callbacks.event(event);
  }
  private async playPCM(encoded: string, rate: number) {
    const epoch = this.outputEpoch;
    const task = this.outputTask.then(() => this.schedulePCM(encoded, rate, epoch));
    this.outputTask = task.catch(() => {});
    return task;
  }
  private async schedulePCM(encoded: string, rate: number, epoch: number) {
    if (rate !== 24000) throw Error('不支援的 Live 音訊格式');
    const bytes = Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0));
    if (bytes.length % 2) throw Error('Live 音訊資料不完整');
    if (this.closed || epoch !== this.outputEpoch) return;
    const context = (this.outputContext ??= new AudioContext({ sampleRate: rate }));
    await context.resume();
    if (this.closed || epoch !== this.outputEpoch) return;
    if (this.outputUntil - context.currentTime > 8) {
      this.clearPCM();
      throw Error('語音播放落後，已清除過期播音');
    }
    const buffer = context.createBuffer(1, bytes.length / 2, rate);
    const view = new DataView(bytes.buffer),
      samples = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    const start = Math.max(context.currentTime + 0.025, this.outputUntil);
    this.outputUntil = start + buffer.duration;
    this.outputNodes.add(source);
    source.onended = () => {
      this.outputNodes.delete(source);
      source.disconnect();
    };
    source.start(start);
  }
  private clearPCM() {
    this.outputEpoch++;
    for (const source of this.outputNodes) {
      source.onended = null;
      source.stop();
      source.disconnect();
    }
    this.outputNodes.clear();
    this.outputUntil = 0;
  }
  private async play(event: Speech) {
    this.stopPlayback(true);
    this.callbacks.event({ type: 'speech_started', playback_id: event.playback_id, text: '' });
    this.speech = event;
    this.playback = true;
    this.inputEpoch++;
    this.gate.clear();
    this.syncMic();
    this.callbacks.playbackGate(true);
    const data = Uint8Array.from(atob(event.audio), (c) => c.charCodeAt(0));
    this.audioURL = URL.createObjectURL(new Blob([data], { type: 'audio/mpeg' }));
    const player = new Audio(this.audioURL);
    this.player = player;
    player.ontimeupdate = () => {
      if (this.player === player)
        this.callbacks.caption(
          playbackReceipt(event.text, event.boundaries, player.currentTime).fully_heard,
        );
    };
    player.onended = () => {
      if (this.player !== player) return;
      this.callbacks.caption(event.text);
      this.callbacks.event({ type: 'speech_finished', playback_id: event.playback_id });
      this.send({ type: 'played', playback_id: event.playback_id, complete: true });
      this.pendingUtterance = null;
      this.stopPlayback(false);
      this.callbacks.event({ type: 'audio_stage', stage: 'ready' });
    };
    player.onerror = () => {
      if (this.player === player) {
        this.pendingUtterance = null;
        this.stopPlayback(true);
        this.callbacks.error('語音播放失敗，這段內容尚未聽到');
      }
    };
    try {
      await player.play();
    } catch (e) {
      // A cancelled or replaced player can reject after the next speech starts.
      // Only the player that still owns playback may stop it or report failure.
      if (this.player === player) {
        this.pendingUtterance = null;
        this.stopPlayback(true);
        throw e;
      }
    }
  }
  private stopPlayback(report: boolean) {
    if (report && this.player && this.speech) {
      const receipt = playbackReceipt(
        this.speech.text,
        this.speech.boundaries,
        this.player.currentTime,
      );
      this.callbacks.event({
        type: 'speech_stopped',
        playback_id: this.speech.playback_id,
        text: receipt.fully_heard,
      });
      try {
        this.send({
          type: 'played',
          playback_id: this.speech.playback_id,
          receipt: playbackReceipt(this.speech.text, this.speech.boundaries, this.player.currentTime),
          complete: false,
        });
      } catch (error) { this.callbacks.error(String(error)); }
    }
    this.player?.pause();
    if (this.player) {
      this.player.src = '';
      this.player.load();
    }
    this.player = null;
    this.speech = null;
    if (this.audioURL) URL.revokeObjectURL(this.audioURL);
    this.audioURL = '';
    this.playback = false;
    this.callbacks.playbackGate(false);
    this.syncMic();
  }
  private async detachMicrophone() {
    this.captureNode?.disconnect();
    this.source?.disconnect();
    this.captureNode = null;
    this.source = null;
    if (this.context && this.context.state !== 'closed') await this.context.close();
    this.context = null;
    this.gate.clear();
  }
  async close() {
    if (this.closing) return this.closing;
    this.closing = this.finishClose();
    return this.closing;
  }
  finalizePlayback() {
    const value = this.speech && this.player
      ? { playback_id: this.speech.playback_id,
          heard: playbackReceipt(this.speech.text, this.speech.boundaries, this.player.currentTime).fully_heard }
      : null;
    this.stopPlayback(true);
    this.clearPCM();
    return value;
  }
  private async finishClose() {
    this.cancelPhotos();
    this.stopPlayback(true);
    this.clearPCM();
    const acknowledged = new Promise<void>((resolve) => {
      this.closeAck = resolve;
    });
    const open = this.socket?.readyState === WebSocket.OPEN;
    this.send({ type: 'close' });
    this.closed = true;
    this.pauseCapture();
    await this.detachMicrophone();
    if (this.outputContext) {
      await this.outputContext.close();
      this.outputContext = null;
    }
    if (open) {
      let timer: ReturnType<typeof setTimeout>;
      await Promise.race([
        acknowledged,
        new Promise<void>((resolve) => {
          timer = setTimeout(resolve, 20000);
        }),
      ]);
      clearTimeout(timer!);
    }
    this.socket?.close();
    this.socket = null;
  }
}
