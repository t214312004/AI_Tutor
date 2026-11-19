import { afterEach, expect, it, vi } from 'vitest';
import { CodexVoicePeer } from './codex-voice';

function fixture() {
  const original = {clone:vi.fn(),stop:vi.fn(),enabled:true};
  const clone = {stop:vi.fn(),enabled:true};original.clone.mockReturnValue(clone);
  const pcs:any[]=[];
  class PC {
    connectionState='new';iceGatheringState='complete';localDescription:any=null;
    ontrack:any=null;onconnectionstatechange:any=null;
    close=vi.fn();addTrack=vi.fn();setRemoteDescription=vi.fn();
    channel={onopen:null as any};
    constructor() { pcs.push(this); }
    createDataChannel() { return this.channel; }
    async createOffer() { return {type:'offer',sdp:'v=0 synthetic'}; }
    async setLocalDescription(value:any) { this.localDescription=value; }
  }
  vi.stubGlobal('RTCPeerConnection',PC);
  vi.stubGlobal('MediaStream',class {constructor(public tracks:any[]) {} });
  vi.stubGlobal('Audio',class {muted=false;srcObject:any=null;play=vi.fn();pause=vi.fn();});
  const events=vi.fn(),errors=vi.fn();
  const peer=new CodexVoicePeer({getAudioTracks:()=>[original]} as any,events,errors);
  return {peer,original,clone,pcs,events,errors};
}
afterEach(()=>{vi.unstubAllGlobals();vi.useRealTimers();});

it('clones microphone tracks and closes only its own resources',async()=>{
  const {peer,original,clone,pcs}=fixture();
  expect(clone.enabled).toBe(false);
  peer.setMic(true);expect(clone.enabled).toBe(true);
  expect(await peer.offer()).toBe('v=0 synthetic');
  await peer.answer('v=0 answer');
  expect(pcs[0].setRemoteDescription).toHaveBeenCalledWith({type:'answer',sdp:'v=0 answer'});
  peer.close();peer.close();
  expect(clone.stop).toHaveBeenCalledTimes(1);
  expect(original.stop).not.toHaveBeenCalled();
  expect(pcs[0].close).toHaveBeenCalledTimes(1);
});

it('reports ready only after both media and data channel are connected',()=>{
  const {peer,pcs,events}=fixture();
  pcs[0].connectionState='connected';pcs[0].onconnectionstatechange();
  expect(events).not.toHaveBeenCalled();
  pcs[0].channel.onopen();
  expect(events).toHaveBeenCalledWith({type:'native_voice_status',stage:'connected'});
  peer.close();
});

it('connection timeout closes the peer and microphone clone',()=>{
  vi.useFakeTimers();
  const {peer,clone,pcs,events,errors}=fixture();
  vi.advanceTimersByTime(30000);
  expect(clone.stop).toHaveBeenCalledTimes(1);
  expect(pcs[0].close).toHaveBeenCalledTimes(1);
  expect(events).toHaveBeenCalledWith({type:'native_voice_status',stage:'error'});
  expect(errors).toHaveBeenCalledTimes(1);
  peer.close();
});
