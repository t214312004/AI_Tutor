import { afterEach, expect, it, vi } from 'vitest';
import { BasicConnection } from './session';

const peers=vi.hoisted(()=>[] as any[]);
vi.mock('./codex-voice',()=>({CodexVoicePeer:class {
  id=`peer-${peers.length}`;closed=false;
  answer=vi.fn();setMic=vi.fn();setQuiet=vi.fn();close=vi.fn(()=>{this.closed=true;});
  offer=vi.fn(async()=>'v=0 synthetic');
  constructor(_stream:any,public event:any,public error:any) { peers.push(this); }
}}));
function fixture() {
  vi.stubGlobal('WebSocket',{OPEN:1});
  const events=vi.fn(),error=vi.fn(),send=vi.fn();
  const client=new BasicConnection('synthetic',{
    event:events,error,capture:()=>null,board:async()=>{},
  });
  Object.assign(client,{voiceEngine:"codex",userMic:true,socket:{readyState:1,bufferedAmount:0,send}});
  const stream={getAudioTracks:()=>[{readyState:'live'}]} as any;
  return {client,stream,events,error,send};
}
afterEach(()=>{vi.unstubAllGlobals();peers.length=0;});

it('native microphone sends only SDP and remains open during teacher speech',async()=>{
  const {client,stream,send}=fixture();
  await client.attachMicrophone(stream);
  expect(JSON.parse(send.mock.calls[0][0])).toMatchObject({type:'native_voice_offer',rtc_id:'peer-0'});
  Object.assign(client,{playback:true,pendingUtterance:'ignored'});
  client.setMic(true);
  expect(peers[0].setMic).toHaveBeenLastCalledWith(true);
  expect(send.mock.calls.map(([raw])=>JSON.parse(raw).type)).not.toContain('audio');
  (client as any).stopNativeVoice();
});

it('ignores an SDP answer for a retired connection and closes it before retry',async()=>{
  const {client,stream}=fixture();
  await client.attachMicrophone(stream);
  await client.restartNativeVoice();
  expect(peers[0].close).toHaveBeenCalledOnce();
  await (client as any).receive({type:'native_voice_answer',rtc_id:'peer-0',sdp:'old'});
  expect(peers[0].answer).not.toHaveBeenCalled();
  expect(peers[1].answer).not.toHaveBeenCalled();
  await (client as any).receive({type:'native_voice_answer',rtc_id:'peer-1',sdp:'new'});
  expect(peers[1].answer).toHaveBeenCalledWith('new');
  (client as any).stopNativeVoice();
});

it('listen policy mutes immediately and resume creates a fresh audio stream',async()=>{
  const {client,stream}=fixture();
  await client.attachMicrophone(stream);
  client.interaction('listening');
  expect(peers[0].setQuiet).toHaveBeenLastCalledWith(true);
  expect(peers[0].setMic).toHaveBeenLastCalledWith(true);
  await (client as any).receive({type:'interaction_policy',mode:'normal'});
  await (client as any).receive({type:'native_voice_policy',mode:'normal'});
  expect(peers[0].close).toHaveBeenCalledOnce();
  expect(peers[1].setQuiet).toHaveBeenCalledWith(false);
  (client as any).stopNativeVoice();
});

it('browser voice failure requests server shutdown to stop subscription media',async()=>{
  const {client,stream,send}=fixture();
  await client.attachMicrophone(stream);
  peers[0].event({type:'native_voice_status',stage:'error'});
  expect(JSON.parse(send.mock.calls.at(-1)![0])).toMatchObject({type:'native_voice_stop'});
  (client as any).stopNativeVoice();
});

it('only the active connected peer unlocks server speech and a mic-off survives restart',async()=>{
  const {client,stream,send}=fixture();
  await client.attachMicrophone(stream);
  client.setMic(false);
  expect(peers[0].setMic).toHaveBeenLastCalledWith(false);
  await client.restartNativeVoice();
  expect(peers[1].setMic).toHaveBeenLastCalledWith(false);
  peers[0].event({type:'native_voice_status',stage:'connected'});
  expect(send.mock.calls.map(([raw])=>JSON.parse(raw).type)).not.toContain('native_voice_connected');
  peers[1].event({type:'native_voice_status',stage:'connected'});
  expect(JSON.parse(send.mock.calls.at(-1)![0])).toMatchObject({type:'native_voice_connected',rtc_id:'peer-1'});
  (client as any).stopNativeVoice();
});

it('voice selection closes old audio before requesting a new voice and keeps the mic preference',async()=>{
  const {client,stream,send,events}=fixture();
  await client.attachMicrophone(stream);
  client.setMic(false);
  client.selectNativeVoice('arbor');
  expect(peers[0].close).toHaveBeenCalledOnce();
  expect(JSON.parse(send.mock.calls.at(-1)![0])).toMatchObject({type:'native_voice_select',voice:'arbor'});
  expect(events).toHaveBeenCalledWith({type:'native_voice_status',stage:'connecting'});
  await (client as any).receive({type:'native_voice_restart'});
  expect(peers[1].setMic).toHaveBeenLastCalledWith(false);
  (client as any).stopNativeVoice();
});
