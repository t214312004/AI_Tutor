import { afterEach, expect, it, vi } from 'vitest';
import { BasicConnection } from './session';

function connection() {
  return new BasicConnection('test', {
    event: () => {},
    error: () => {},
    capture: () => '',
    board: async () => {},
    playbackGate: () => {},
    caption: () => {},
  }, 'gpt');
}

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

it('awaits a native photo without delaying audio messages and ignores it after lesson close', async () => {
  vi.stubGlobal('WebSocket', { OPEN: 1 });
  let complete!: (photo: string) => void;
  const send = vi.fn();
  const client = new BasicConnection('test', {
    event: () => {}, error: () => {},
    capture: () => new Promise<string>(resolve => { complete = resolve; }),
    board: async () => {}, playbackGate: () => {}, caption: () => {},
  });
  Object.assign(client, { socket: { readyState: 1, bufferedAmount: 0, send } });
  const pending = (client as any).receive({ type: 'capture_requested', request_id: 'fresh' });
  (client as any).send({ type: 'audio', pcm: 'AAA=' });
  expect(send.mock.calls.map(([raw]) => JSON.parse(raw).type)).toEqual(['audio']);
  (client as any).closed = true;
  complete('data:image/jpeg;base64,AA==');
  await pending;
  expect(send).toHaveBeenCalledTimes(1);
});

it('sends the awaited native photo with the requesting identifier and metadata', async () => {
  vi.stubGlobal('WebSocket', { OPEN: 1 });
  const send = vi.fn();
  const client = new BasicConnection('test', {
    event: () => {}, error: () => {},
    capture: async () => ({ image: 'data:image/jpeg;base64,AA==', meta: { capture_method: 'windows-native-photo' } }),
    board: async () => {}, playbackGate: () => {}, caption: () => {},
  });
  Object.assign(client, { socket: { readyState: 1, bufferedAmount: 0, send } });
  await (client as any).receive({ type: 'capture_requested', request_id: 'fresh' });
  expect(JSON.parse(send.mock.calls[0][0])).toMatchObject({ type: 'frame', request_id: 'fresh',
    capture_meta: { capture_method: 'windows-native-photo' } });
});

it('does not play a PCM chunk whose resume finishes after audio_clear', async () => {
  let resume!: () => void;
  let starts = 0;
  class AudioContextStub {
    currentTime = 0;
    destination = {};
    resume() { return new Promise<void>((resolve) => { resume = resolve; }); }
    createBuffer() { return { duration: 0.1, getChannelData: () => new Float32Array(2) }; }
    createBufferSource() {
      return { connect: () => {}, disconnect: () => {}, start: () => { starts++; }, stop: () => {}, onended: null };
    }
  }
  vi.stubGlobal('AudioContext', AudioContextStub);
  const client = connection();
  const pending = (client as any).playPCM('AAAAAA==', 24000);
  await vi.waitFor(() => expect(resume).toBeDefined());
  (client as any).clearPCM();
  resume();
  await pending;
  expect(starts).toBe(0);
});

it('closes the microphone AudioContext when its worklet cannot load', async () => {
  let closes = 0;
  class AudioContextStub {
    state = 'running';
    audioWorklet = { addModule: async () => { throw Error('worklet failed'); } };
    close() { closes++; this.state = 'closed'; return Promise.resolve(); }
  }
  vi.stubGlobal('AudioContext', AudioContextStub);
  await expect(connection().attachMicrophone({} as MediaStream)).rejects.toThrow('worklet failed');
  expect(closes).toBe(1);
});

it('ignores a late play rejection from speech that was already replaced', async () => {
  const players: any[] = [];
  class AudioStub {
    currentTime = 0;
    src = '';
    ontimeupdate: (() => void) | null = null;
    onended: (() => void) | null = null;
    onerror: (() => void) | null = null;
    resolve!: () => void;
    reject!: (error: Error) => void;
    constructor() { players.push(this); }
    play() {
      return new Promise<void>((resolve, reject) => {
        this.resolve = resolve;
        this.reject = reject;
      });
    }
    pause() {}
    load() {}
  }
  vi.stubGlobal('Audio', AudioStub);
  vi.stubGlobal('URL', { createObjectURL: () => 'blob:test', revokeObjectURL: () => {} });
  const client = connection();
  const speech = (id: string) => ({
    type: 'speech', session_id: 'test', generation: 0,
    playback_id: id, text: id, audio: 'AA==', boundaries: [],
  });
  const first = (client as any).play(speech('first'));
  const second = (client as any).play(speech('second'));
  players[0].reject(Error('old playback was cancelled'));
  await expect(first).resolves.toBeUndefined();
  expect((client as any).player).toBe(players[1]);
  players[1].resolve();
  await second;
});

it('still sends mute and camera-off controls when upload is backlogged', async () => {
  const send = vi.fn();
  vi.stubGlobal('WebSocket', { OPEN: 1 });
  const client = connection();
  (client as any).socket = { readyState: 1, bufferedAmount: 3_000_000, send };
  expect(() => client.setMic(false)).not.toThrow();
  expect(() => client.setCamera(false)).not.toThrow();
  expect(send.mock.calls.map(([raw]) => JSON.parse(raw).type)).toEqual(['mic', 'camera']);
  await expect(client.frame('data:image/jpeg;base64,AA==')).rejects.toThrow('網路傳送不及');
});

it('keeps audio flowing while a large PNG HTTP upload is pending', async () => {
  const events: any[] = [];
  const send = vi.fn((raw: string) => {
    const message = JSON.parse(raw);
    if (message.type === 'frame') socket.bufferedAmount += raw.length;
  });
  const socket = { readyState: 1, bufferedAmount: 0, send };
  vi.stubGlobal('WebSocket', { OPEN: 1 });
  let complete!: (response: any) => void;
  const fetchMock = vi.fn((_url: string, _options: any) => new Promise(resolve => { complete = resolve; }));
  vi.stubGlobal('fetch', fetchMock);
  const client = new BasicConnection('test', {
    event: event => events.push(event), error: () => {}, capture: () => '',
    board: async () => {}, playbackGate: () => {}, caption: () => {},
  }, 'gpt');
  Object.assign(client, { socket, userMic: true,
    photoTransport: { base: 'http://127.0.0.1:1234', token: 'test-token' } });
  const image = 'data:image/png;base64,' + 'A'.repeat(2_300_000);
  const uploading = client.frame(image, { source_width: 1920 });
  expect(fetchMock.mock.calls[0][0]).toBe('http://127.0.0.1:1234/sessions/test/frame');
  expect(JSON.parse((fetchMock.mock.calls[0] as any)[1].body).image).toBe(image);
  expect(() => (client as any).send({ type: 'audio', pcm: 'AAA=' })).not.toThrow();
  expect(() => (client as any).send({ type: 'audio', pcm: 'AAA=' })).not.toThrow();
  expect((client as any).userMic).toBe(true);
  expect(socket.bufferedAmount).toBe(0);
  expect(send.mock.calls.map(([raw]) => JSON.parse(raw).type)).toEqual(['audio', 'audio']);
  complete({ ok: true });
  await uploading;
  expect(events.map(e => e.stage)).toEqual(['start', 'done']);
});

it('reports PNG upload failure without muting audio', async () => {
  vi.stubGlobal('WebSocket', { OPEN: 1 });
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 422,
    json: async () => ({ detail: '照片格式無效' }) })));
  const client = connection();
  Object.assign(client, { socket: { readyState: 1, bufferedAmount: 0, send: vi.fn() },
    userMic: true, photoTransport: { base: 'http://localhost', token: 'test-token' } });
  await expect(client.frame('data:image/png;base64,AA==')).rejects.toThrow('照片格式無效');
  expect((client as any).userMic).toBe(true);
  expect((client as any).photoUploads.size).toBe(0);
});

it('cancels pending PNG upload on camera-off without muting audio', async () => {
  vi.stubGlobal('WebSocket', { OPEN: 1 });
  let signal!: AbortSignal;
  vi.stubGlobal('fetch', vi.fn((_url, options) => new Promise((_resolve, reject) => {
    signal = options.signal;
    signal.addEventListener('abort', () => reject(Error('cancelled')), { once: true });
  })));
  const client = connection();
  Object.assign(client, { socket: { readyState: 1, bufferedAmount: 0, send: vi.fn() },
    userMic: true, photoTransport: { base: 'http://localhost', token: 'test-token' } });
  const uploading = client.frame('data:image/png;base64,AA==');
  client.setCamera(false);
  expect(signal.aborted).toBe(true);
  await expect(uploading).resolves.toBeUndefined();
  expect((client as any).userMic).toBe(true);
});

it('times out a stuck PNG upload and releases its slot without muting audio', async () => {
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', { OPEN: 1 });
  vi.stubGlobal('fetch', vi.fn((_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(Error('aborted')), { once: true });
  })));
  const client = connection();
  Object.assign(client, { socket: { readyState: 1, bufferedAmount: 0, send: vi.fn() },
    userMic: true, photoTransport: { base: 'http://localhost', token: 'test-token' } });
  const checked = expect(client.frame('data:image/png;base64,AA==')).rejects.toThrow('照片傳送逾時');
  await vi.advanceTimersByTimeAsync(15000);
  await checked;
  expect((client as any).userMic).toBe(true);
  expect((client as any).photoUploads.size).toBe(0);
});

it('aborts PNG uploads at lesson close and ignores subsequent frames', async () => {
  vi.stubGlobal('WebSocket', { OPEN: 1 });
  let signal!: AbortSignal;
  const fetchMock = vi.fn((_url, options) => new Promise((_resolve, reject) => {
    signal = options.signal;
    signal.addEventListener('abort', () => reject(Error('cancelled')), { once: true });
  }));
  vi.stubGlobal('fetch', fetchMock);
  const client = connection();
  const socket = { readyState: 1, bufferedAmount: 0, close: vi.fn(),
    send: (raw: string) => { if (JSON.parse(raw).type === 'close') (client as any).closeAck?.(); } };
  Object.assign(client, { socket, photoTransport: { base: 'http://localhost', token: 'test-token' } });
  const uploading = client.frame('data:image/png;base64,AA==');
  await client.close();
  expect(signal.aborted).toBe(true);
  await uploading;
  await client.frame('data:image/png;base64,AA==');
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(socket.close).toHaveBeenCalledTimes(1);
});

it('reopens capture only after the matching audio segment is ignored', async () => {
  const stages: string[] = [];
  const gate = vi.fn();
  const client = new BasicConnection('test', {
    event: (event) => { if (event.type === 'audio_stage') stages.push(event.stage); },
    error: () => {}, capture: () => '', board: async () => {},
    playbackGate: () => {}, caption: () => {},
  });
  (client as any).userMic = true;
  (client as any).pendingUtterance = 'current';
  (client as any).captureNode = { port: { postMessage: gate } };
  await (client as any).receive({ type:'audio_status',session_id:'test',generation:0,
    utterance_id:'old',stage:'ignored' });
  expect((client as any).pendingUtterance).toBe('current');
  await (client as any).receive({ type:'audio_status',session_id:'test',generation:0,
    utterance_id:'current',stage:'ignored' });
  expect((client as any).pendingUtterance).toBeNull();
  expect(gate).toHaveBeenCalledWith(true);
  expect(stages).toEqual(['ignored']);
});

it('reconnects after a transient socket close and keeps the same lesson connection', async () => {
  const sockets: any[] = [];
  const events: string[] = [];
  class SocketStub {
    static OPEN = 1;
    readyState = 1;
    onopen: (() => void) | null = null;
    onclose: ((event: { code: number }) => void) | null = null;
    onerror: (() => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    constructor() { sockets.push(this); }
    send() {}
    close() { this.readyState = 3; }
  }
  vi.stubGlobal('WebSocket', SocketStub);
  const client = new BasicConnection('lesson-1', {
    event: (event) => events.push(event.type),
    error: () => {}, capture: () => '', board: async () => {},
    playbackGate: () => {}, caption: () => {},
  });
  const initial = client.connect('http://127.0.0.1:1234', 'secret');
  sockets[0].onopen();
  sockets[0].onmessage({ data: JSON.stringify({ type: 'ready', generation: 0 }) });
  await initial;
  sockets[0].onclose({ code: 1006 });
  await vi.waitFor(() => expect(sockets).toHaveLength(2));
  sockets[1].onopen();
  sockets[1].onmessage({ data: JSON.stringify({ type: 'ready', generation: 1, resumed: true }) });
  await vi.waitFor(() => expect(events).toContain('transport_restored'));
  expect(events).toContain('transport_lost');
  expect(events).not.toContain('media_stopped');
});
