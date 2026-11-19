import { afterEach, expect, it, vi } from 'vitest';
import { BasicConnection } from './session';

it('rejects a typed question when the socket has disconnected', () => {
  vi.stubGlobal('WebSocket',{OPEN:1});
  const client=connection();
  Object.assign(client,{socket:{readyState:3}});
  expect(()=>client.text('這題怎麼想？')).toThrow('文字沒有送出');
});

function connection() {
  return new BasicConnection('test', {
    event: () => {},
    error: () => {},
    capture: () => '',
    board: async () => {},
  });
}

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

it('interrupt immediately stops scheduled Live audio and invalidates pending chunks', async () => {
  vi.stubGlobal('WebSocket', { OPEN: 1 });
  const client=connection();
  const source={stop:vi.fn(),disconnect:vi.fn(),onended:vi.fn()};
  Object.assign(client,{socket:{readyState:1,bufferedAmount:0,send:vi.fn()},
    outputNodes:new Set([source]),outputUntil:8});
  const epoch=(client as any).outputEpoch;
  await client.interrupt();
  expect(source.stop).toHaveBeenCalledOnce();
  expect(source.disconnect).toHaveBeenCalledOnce();
  expect((client as any).outputNodes.size).toBe(0);
  expect((client as any).outputUntil).toBe(0);
  expect((client as any).outputEpoch).toBeGreaterThan(epoch);
});

it('blocks queued PCM on listen policy without closing capture', async () => {
  vi.stubGlobal('WebSocket',{ OPEN:1 });
  const client=connection();
  const send=vi.fn(), gate=vi.fn();
  Object.assign(client,{ socket:{ readyState:1,bufferedAmount:0,send },userMic:true,
    pendingUtterance:'reading',captureNode:{port:{postMessage:gate}} });
  const play=vi.spyOn(client as any,'playPCM');
  client.interaction('listening');
  expect((client as any).userMic).toBe(true);
  expect(gate).toHaveBeenCalledWith(true);
  await (client as any).receive({ type:'pcm',audio:'AAAAAA==',rate:24000 });
  expect(play).not.toHaveBeenCalled();
  expect(JSON.parse(send.mock.calls[0][0])).toMatchObject({type:'interaction',mode:'listening'});
});

it('rejects old direction and work versions before board or audio side effects',async () => {
  const client=connection();
  Object.assign(client,{ directionRevision:3,workSequence:8 });
  const play=vi.spyOn(client as any,'playPCM');
  const board=vi.fn();(client as any).callbacks.board=board;
  await (client as any).receive({ type:'pcm',direction_revision:2,work_sequence:9 });
  await (client as any).receive({ type:'board',direction_revision:3,work_sequence:7,revision:5,html:'old' });
  expect(play).not.toHaveBeenCalled();expect(board).not.toHaveBeenCalled();
});


it('awaits a native photo without delaying audio messages and ignores it after lesson close', async () => {
  vi.stubGlobal('WebSocket', { OPEN: 1 });
  let complete!: (photo: string) => void;
  const send = vi.fn();
  const client = new BasicConnection('test', {
    event: () => {}, error: () => {},
    capture: () => new Promise<string>(resolve => { complete = resolve; }),
    board: async () => {},
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
    board: async () => {},
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
    board: async () => {},
  });
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
  complete({ ok: true, json: async () => ({ accepted: true }) });
  await uploading;
  expect(events.map(e => e.stage)).toEqual(['start', 'done']);
  expect(events[1]).toMatchObject({ accepted: true });
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
