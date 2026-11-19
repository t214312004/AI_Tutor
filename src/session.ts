import { pcm16, Resampler } from './audio';
import { CodexVoicePeer } from './codex-voice';

type Photo = string | { image: string; meta: Record<string, unknown> } | null;
type Callbacks = {
  event: (event: any) => void;
  error: (message: string) => void;
  capture: () => Photo | Promise<Photo>;
  board: (html: string, close: boolean) => Promise<void>;
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
  private directionRevision = 0;
  private workSequence = 0;
  private interactionMode: 'normal' | 'listening' | 'closing' = 'normal';
  private lastDeliveryAt = 0;
  private boardRevision = 0;
  private userMic = false;
  private inputEpoch = 0;
  private closed = false;
  private outputContext: AudioContext | null = null;
  private outputNodes = new Set<AudioBufferSourceNode>();
  private outputUntil = 0;
  private outputEpoch = 0;
  private outputTask: Promise<void> = Promise.resolve();
  private closeAck: (() => void) | null = null;
  private cancelConnect: (() => void) | null = null;
  private closing: Promise<void> | null = null;
  private recovering = false;
  private photoTransport: { base: string; token: string } | null = null;
  private photoUploads = new Set<AbortController>();
  private voiceEngine: 'agy' | 'codex' | null = null;
  private agyMediaId: string | null = null;
  private agyReady = false;
  private nativePeer: CodexVoicePeer | null = null;
  private microphone: MediaStream | null = null;
  private nativeEpoch = 0;
  constructor(
    private id: string,
    private callbacks: Callbacks,
  ) {}
  async connect(base: string, token: string, route = `/sessions/${this.id}/stream`) {
    this.photoTransport = { base: base.replace(/\/$/, ''), token };
    const socket = new WebSocket(base.replace(/^http/, 'ws') + route);
    this.socket = socket;
    return new Promise<void>((resolve, reject) => {
      let ready = false;
      let settled = false;
      const finish = (err?: Error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (this.cancelConnect === abort) this.cancelConnect = null;
        if (err) reject(err);
        else resolve();
      };
      const abort = () => finish(Error('教學連線已關閉'));
      this.cancelConnect = abort;
      const timer = setTimeout(() => {
        socket.close();
        finish(Error('教學連線逾時'));
      }, this.recovering ? 5000 : 30000);
      socket.onopen = () => socket.send(JSON.stringify({ type: 'authenticate', token }));
      socket.onerror = () => {
        finish(Error('無法建立教學連線'));
      };
      socket.onclose = (event) => {
        clearTimeout(timer);
        if (this.socket !== socket) {
          finish(Error('教學連線已關閉'));
          return;
        }
        this.socket = null;
        this.cancelPhotos();
        this.closeAck?.();
        if (!this.closed && ready) {
          this.stopNativeVoice();
          this.clearPCM();
          this.pauseCapture();
          this.callbacks.event({ type: 'transport_lost', code: event.code });
          void this.recover(base, token, route);
        }
        finish(Error('教學連線已關閉'));
      };
      socket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'closed') this.closeAck?.();
          if (data.type === 'ready') {
            ready = true;
            this.generation = data.generation;
            this.directionRevision = data.direction_revision ?? 0;
            this.workSequence = data.work_sequence ?? 0;
            this.interactionMode = data.interaction_mode ?? 'normal';
            this.voiceEngine = data.voice_engine === 'agy' || data.voice_engine === 'codex' ? data.voice_engine : null;
            finish();
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
      if (message.type === 'text') throw Error('教學連線尚未就緒，文字沒有送出；請在重連後再試一次');
      return;
    }
    if (this.socket.bufferedAmount > (message.type === 'agy_audio' ? 32_768 : 2_000_000) &&
        ['audio', 'agy_audio', 'frame', 'activity'].includes(String(message.type))) {
      this.pauseCapture();
      this.callbacks.event({ type: 'transport_backpressure', kind: message.type,
        buffered_bytes: this.socket.bufferedAmount });
      throw Error('網路傳送不及，收音已停止');
    }
    this.socket.send(JSON.stringify({ ...message, session_id: this.id }));
  }
  private async sendFrame(message: Record<string, unknown>) {
    if (!this.voiceEngine && !String(message.image).startsWith('data:image/png;base64,')) {
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
      const { accepted } = await response.json();
      if (controller.signal.aborted) return;
      this.callbacks.event({ type: 'photo_upload', stage: 'done', image_chars: imageChars,
        accepted: accepted === true, elapsed_ms: Math.round(performance.now() - started) });
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
    this.microphone = stream;
    if (this.voiceEngine === 'codex') {
      await this.restartNativeVoice();
      return;
    }
    const rate = 16000;
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
    this.captureNode.port.onmessage = (event) => {
      if (inputEpoch !== this.inputEpoch) {
        pending = [];
        resampler.clear();
        inputEpoch = this.inputEpoch;
      }
      const rtc_id = this.agyMediaId;
      if (!this.userMic || this.closed || !rtc_id || !this.agyReady) return;
      pending.push(...resampler.push(event.data));
      while (pending.length >= 640) {
        try {
          this.send({ type: 'agy_audio', rtc_id, pcm: encode(pcm16(Float32Array.from(pending.splice(0, 640)))) });
        } catch (error) {
          this.callbacks.error(String(error));
          break;
        }
      }
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
      if (this.voiceEngine === 'agy') await this.restartNativeVoice();
    } catch (error) {
      if (this.context === context) await this.detachMicrophone();
      throw error;
    }
  }
  setMic(enabled: boolean) {
    this.inputEpoch++;
    this.userMic = enabled;
    this.syncMic();
    this.send({ type: 'mic', enabled });
    this.callbacks.event({ type: 'audio_stage', stage: enabled ? 'ready' : 'off' });
  }
  private syncMic() {
    this.nativePeer?.setMic(this.userMic && !this.closed);
    this.captureNode?.port.postMessage(this.userMic && !this.closed);
  }
  private pauseCapture() {
    this.inputEpoch++;
    this.userMic = false;
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
    this.send({ type: 'begin' });
  }
  text(text: string) {
    this.send({ type: 'text', text });
  }
  selectNativeVoice(voice: string) {
    if (this.voiceEngine !== 'codex' || this.closed) return;
    this.stopNativeVoice();
    this.callbacks.event({type:'native_voice_status',stage:'connecting'});
    this.send({type:'native_voice_select',voice});
  }
  confirmAgyTool(id: string, approved: boolean) {
    if (this.voiceEngine === 'agy') this.send({type: 'agy_tool_confirm', id, approved});
  }
  lesson(action: 'confirm' | 'done' | 'todo', task_id?: string) {
    this.send({ type: 'lesson', action, task_id });
  }
  interaction(mode: 'normal' | 'listening' | 'closing', speaker?: string) {
    // Close the local gate immediately, before the server/provider round trip.
    if (mode !== 'normal') {
      this.interactionMode = mode;
      this.clearPCM();
      this.syncMic();
      this.nativePeer?.setQuiet(true);
    }
    this.send({ type: 'interaction', mode, speaker });
  }
  private stopNativeVoice() {
    this.nativeEpoch++;
    this.nativePeer?.close();
    this.nativePeer = null;
    if (this.voiceEngine === 'agy') {
      this.agyReady = false;
      this.agyMediaId = null;
      this.inputEpoch++;
      this.clearPCM();
    }
  }
  async restartNativeVoice() {
    if (!this.voiceEngine || this.closed || !this.microphone?.getAudioTracks().some(track => track.readyState === 'live')) return;
    this.stopNativeVoice();
    const epoch = this.nativeEpoch;
    this.callbacks.event({ type: 'native_voice_status', stage: 'connecting' });
    if (this.voiceEngine === 'agy') {
      this.agyMediaId = crypto.randomUUID();
      this.send({ type: 'agy_voice_start', rtc_id: this.agyMediaId });
      return;
    }
    const peer = new CodexVoicePeer(this.microphone, event => {
      if (this.nativePeer === peer && !this.closed) {
        if (event.type === 'native_voice_status' && event.stage === 'connected') this.send({type:'native_voice_connected',rtc_id:peer.id});
        if (event.type === 'native_voice_status' && event.stage === 'error') this.send({type:'native_voice_stop'});
        this.callbacks.event(event);
      }
    }, message => {
      if (this.nativePeer === peer && !this.closed) this.callbacks.error(message);
    });
    this.nativePeer = peer;
    peer.setQuiet(this.interactionMode !== 'normal');
    peer.setMic(this.userMic);
    try {
      const sdp = await peer.offer();
      if (this.closed || epoch !== this.nativeEpoch) return;
      this.send({ type: 'native_voice_offer', sdp, rtc_id: peer.id });
    } catch (error) {
      if (!this.closed && epoch === this.nativeEpoch) {
        this.stopNativeVoice();
        this.callbacks.event({ type: 'native_voice_status', stage: 'error' });
        throw error;
      }
    }
  }
  async interrupt(enableMic = this.userMic) {
    this.generation++;
    this.inputEpoch++;
    this.stopNativeVoice();
    this.clearPCM();
    this.userMic = enableMic;
    this.syncMic();
    this.send({ type: 'interrupt', receipt: {} });
    this.send({ type: 'mic', enabled: this.userMic });
    this.callbacks.event({ type: 'audio_stage', stage: this.userMic ? 'ready' : 'off' });
  }
  private async receive(event: any) {
    if (this.closed || (event.session_id && event.session_id !== this.id)) return;
    if (this.voiceEngine === 'agy' && event.rtc_id && event.rtc_id !== this.agyMediaId) return;
    if (typeof event.generation === 'number' && event.generation < this.generation) return;
    if (typeof event.direction_revision === 'number' && event.direction_revision < this.directionRevision) return;
    if (['speech','board'].includes(event.type) && typeof event.work_sequence === 'number' && event.work_sequence < this.workSequence) return;
    if (typeof event.direction_revision === 'number') this.directionRevision = event.direction_revision;
    if (typeof event.work_sequence === 'number') this.workSequence = Math.max(this.workSequence,event.work_sequence);
    if (event.type === 'interrupted') this.generation = event.generation;
    if (event.type === 'native_voice_answer') {
      const peer = this.nativePeer;
      if (peer && event.rtc_id === peer.id) await peer.answer(event.sdp);
      return;
    }
    if (event.type === 'native_voice_restart') { await this.restartNativeVoice(); return; }
    if (event.type === 'native_voice_policy') {
      if (event.mode !== 'normal') this.nativePeer?.setQuiet(true);
      else { await this.restartNativeVoice(); }
      return;
    }
    if (event.type === 'native_voice_status') {
      if (this.voiceEngine === 'agy') this.agyReady = event.stage === 'connected';
      if (event.stage === 'error') this.stopNativeVoice();
    }
    if (event.type === 'agy_pcm') {
      if (this.agyReady && this.interactionMode === 'normal') await this.playPCM(event.audio, event.rate);
      return;
    }
    if (event.type === 'agy_clear') { this.clearPCM(); return; }
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
    } else if (event.type === 'interaction_policy') {
      this.interactionMode = event.mode;
      if (event.mode !== 'normal') {
        this.clearPCM();
        this.syncMic();
      }
      this.callbacks.event(event);
    } else if (event.type === 'board' && event.revision > this.boardRevision) {
      this.boardRevision = event.revision;
      await this.callbacks.board(event.html, event.close);
    } else if (event.type === 'pcm' && this.interactionMode === 'normal') await this.playPCM(event.audio, event.rate);
    else if (event.type === 'audio_clear') this.clearPCM();
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
    if (this.closed || epoch !== this.outputEpoch || this.interactionMode !== 'normal') return;
    if (rate !== 24000) throw Error('不支援的 Live 音訊格式');
    const bytes = Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0));
    if (bytes.length % 2) throw Error('Live 音訊資料不完整');
    if (bytes.length === 0) return;
    const context = (this.outputContext ??= new AudioContext({ sampleRate: rate }));
    await context.resume();
    if (this.closed || epoch !== this.outputEpoch || this.interactionMode !== 'normal') return;
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
    if (this.voiceEngine === 'agy' && this.outputNodes.size === 1)
      this.callbacks.event({ type: 'native_voice_speaking', speaking: true });
    source.onended = () => {
      this.outputNodes.delete(source);
      source.disconnect();
      if (this.voiceEngine === 'agy' && this.outputNodes.size === 0)
        this.callbacks.event({ type: 'native_voice_speaking', speaking: false });
      this.delivery('ended');
    };
    this.delivery('scheduled');
    source.start(start);
  }
  private delivery(delivery: 'scheduled' | 'ended' | 'cleared') {
    if (performance.now() - this.lastDeliveryAt < 5000) return;
    this.lastDeliveryAt = performance.now();
    this.send({ type: 'audio_delivery', delivery });
  }
  private clearPCM() {
    this.outputEpoch++;
    if (this.voiceEngine === 'agy' && this.outputNodes.size)
      this.callbacks.event({ type: 'native_voice_speaking', speaking: false });
    for (const source of this.outputNodes) {
      source.onended = null;
      try { source.stop(); } catch {}
      try { source.disconnect(); } catch {}
    }
    this.outputNodes.clear();
    this.outputUntil = 0;
  }
  private async detachMicrophone() {
    this.stopNativeVoice();
    this.microphone = null;
    this.captureNode?.disconnect();
    this.source?.disconnect();
    this.captureNode = null;
    this.source = null;
    const context = this.context;
    this.context = null;
    if (context && context.state !== 'closed') await context.close();
  }
  async close() {
    if (this.closing) return this.closing;
    this.closing = this.finishClose();
    return this.closing;
  }
  stopPlayback() {
    this.stopNativeVoice();
    this.clearPCM();
  }
  private async finishClose() {
    this.stopNativeVoice();
    this.cancelPhotos();
    this.clearPCM();
    const acknowledged = new Promise<void>((resolve) => {
      this.closeAck = resolve;
    });
    const open = this.socket?.readyState === WebSocket.OPEN;
    this.send({ type: 'close' });
    this.closed = true;
    this.cancelConnect?.();
    this.pauseCapture();
    try {
      const output = this.outputContext;
      this.outputContext = null;
      await Promise.all([this.detachMicrophone(), output?.close()]);
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
    } finally {
      this.socket?.close();
      this.socket = null;
      this.closeAck = null;
    }
  }
}
