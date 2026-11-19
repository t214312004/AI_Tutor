/** Browser audio for native subscription voice; the app still owns its devices. */
export class CodexVoicePeer {
  readonly id = crypto.randomUUID();
  private pc: RTCPeerConnection;
  private output = new Audio();
  private tracks: MediaStreamTrack[] = [];
  private context: AudioContext | null = null;
  private meter: ReturnType<typeof setInterval> | null = null;
  private timeout: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  private quiet = false;
  private speaking = false;
  private connected = false;
  private channelReady = false;
  private readySent = false;
  private cancelOffer: (() => void) | null = null;
  constructor(stream: MediaStream, private event: (event: any) => void,
    private error: (message: string) => void) {
    this.pc = new RTCPeerConnection();
    for (const track of stream.getAudioTracks()) {
      const clone = track.clone();
      clone.enabled = false;
      this.tracks.push(clone);
      this.pc.addTrack(clone, new MediaStream([clone]));
    }
    const channel = this.pc.createDataChannel('oai-events');
    channel.onopen = () => { this.channelReady = true; this.ready(); };
    this.pc.onconnectionstatechange = () => {
      if (this.closed) return;
      this.connected = this.pc.connectionState === 'connected';
      if (this.pc.connectionState === 'failed' || this.pc.connectionState === 'disconnected')
        this.fail('訂閱語音連線中斷，請重新連接語音。');
      else this.ready();
    };
    this.pc.ontrack = event => {
      if (this.closed) return;
      if (this.meter) { clearInterval(this.meter); this.meter = null; }
      if (this.context) { void this.context.close().catch(() => {}); this.context = null; }
      const remote = event.streams[0] ?? new MediaStream([event.track]);
      this.output.srcObject = remote;
      this.output.muted = this.quiet;
      void this.output.play().catch(() => this.fail('語音播放未啟動，請重新連接語音。'));
      this.context = new AudioContext();
      const source = this.context.createMediaStreamSource(remote);
      const analyser = this.context.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      const silent = this.context.createGain(); silent.gain.value = 0;
      analyser.connect(silent); silent.connect(this.context.destination);
      void this.context.resume().catch(() => {});
      const samples = new Float32Array(analyser.fftSize);
      this.meter = setInterval(() => {
        analyser.getFloatTimeDomainData(samples);
        const active = !this.quiet && Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length) > .008;
        if (active !== this.speaking) {
          this.speaking = active;
          this.event({ type: 'native_voice_speaking', speaking: active });
        }
      }, 150);
    };
    this.timeout = setTimeout(() => this.fail('訂閱語音連線逾時，請重新連接語音。'), 30000);
  }
  private ready() {
    if (this.closed || this.readySent || !this.connected || !this.channelReady) return;
    this.readySent = true;
    if (this.timeout) clearTimeout(this.timeout);
    this.timeout = null;
    this.event({ type: 'native_voice_status', stage: 'connected' });
  }
  private fail(message: string) {
    if (this.closed) return;
    this.close();
    this.event({ type: 'native_voice_status', stage: 'error' });
    this.error(message);
  }
  async offer() {
    await this.pc.setLocalDescription(await this.pc.createOffer());
    if (!this.closed && this.pc.iceGatheringState !== 'complete') {
      await new Promise<void>(resolve => {
        const finish = () => {
          clearTimeout(timer);
          this.pc.removeEventListener('icegatheringstatechange', changed);
          this.cancelOffer = null;
          resolve();
        };
        const changed = () => { if (this.pc.iceGatheringState === 'complete') finish(); };
        const timer = setTimeout(finish, 5000);
        this.cancelOffer = finish;
        this.pc.addEventListener('icegatheringstatechange', changed);
      });
    }
    if (this.closed || !this.pc.localDescription?.sdp) throw Error('語音連線已取消');
    return this.pc.localDescription.sdp;
  }
  async answer(sdp: string) {
    if (!this.closed) await this.pc.setRemoteDescription({ type: 'answer', sdp });
  }
  setMic(enabled: boolean) { this.tracks.forEach(track => { track.enabled = enabled && !this.closed; }); }
  setQuiet(quiet: boolean) {
    this.quiet = quiet;
    this.output.muted = quiet;
    if (quiet && this.speaking) { this.speaking = false; this.event({ type: 'native_voice_speaking', speaking: false }); }
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.cancelOffer?.();
    if (this.timeout) clearTimeout(this.timeout);
    if (this.meter) clearInterval(this.meter);
    this.tracks.forEach(track => track.stop());
    this.pc.ontrack = this.pc.onconnectionstatechange = null;
    this.pc.close();
    this.output.pause(); this.output.srcObject = null;
    void this.context?.close().catch(() => {});
    this.context = null;
    this.event({ type: 'native_voice_speaking', speaking: false });
  }
}
