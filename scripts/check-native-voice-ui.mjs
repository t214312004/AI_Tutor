// Synthetic desktop test: no inference services, microphone uploads, or account auth.
import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { electronTestFlags } from './electron-test-flags.mjs';

async function waitForMessage(predicate, description) {
  const deadline = Date.now() + 5000;
  while (!predicate() && Date.now() < deadline) await delay(25);
  assert.ok(predicate(), description);
}

await fs.mkdir('.local',{recursive:true});
const home=await fs.mkdtemp(path.resolve('.local/native-voice-ui-'));
const app=await electron.launch({args:['.','--test-mode',...electronTestFlags,
  '--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream'],cwd:process.cwd(),
  env:{...process.env,TUTOR_TEST_HOME:home}});
try {
  const page=await app.firstWindow();page.setDefaultTimeout(20000);
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const connection=await page.evaluate(()=>window.desktop.connection());
  await page.addInitScript(()=>{
    window.__nativeTracks=[];window.__sourceTracks=[];window.__teacherSpeaking=false;
    const getUserMedia=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia=async constraints=>{
      const stream=await getUserMedia(constraints);
      if(constraints.audio)window.__sourceTracks.push(...stream.getAudioTracks());
      return stream;
    };
    // Drive the real speaking callback while keeping playback and meters synthetic.
    window.Audio=class {muted=false;srcObject=null;play(){return Promise.resolve();}pause(){}};
    window.AudioWorkletNode=class {
      port={postMessage(){}};
      constructor(){window.__agyInput=this.port;}
      connect(){}
      disconnect(){}
    };
    window.AudioContext=class {
      sampleRate=16000;
      audioWorklet={addModule:async()=>{}};
      destination={};
      state='running';
      currentTime=0;
      createMediaStreamSource(){return {connect(){},disconnect(){}};}
      createAnalyser(){return {fftSize:512,connect(){},getFloatTimeDomainData(samples){
        samples.fill(window.__teacherSpeaking ? .1 : 0);
      }};}
      createGain(){return {gain:{value:0},connect(){}};}
      createBuffer(_channels,length,rate){return {duration:length/rate,getChannelData:()=>new Float32Array(length)};}
      createBufferSource(){return {connect(){},disconnect(){},start(){},stop(){}};}
      resume(){return Promise.resolve();}close(){return Promise.resolve();}
    };
    class Peer {
      iceGatheringState='complete';connectionState='new';localDescription=null;
      onconnectionstatechange=null;ontrack=null;channel={onopen:null};
      addTrack(track) {window.__nativeTracks.push(track);} createDataChannel(){return this.channel;}
      async createOffer(){return {type:'offer',sdp:'v=0 synthetic UI'};}
      async setLocalDescription(value){this.localDescription=value;}
      async setRemoteDescription(){
        this.connectionState='connected';this.onconnectionstatechange?.();this.channel.onopen?.();
        this.ontrack?.({streams:[new MediaStream()],track:null});
      }
      close(){this.connectionState='closed';window.__teacherSpeaking=false;}
    }
    window.RTCPeerConnection=Peer;
  });
  await page.route(connection.base+'/bootstrap', async route => {
    const response = await route.fetch();
    const boot = await response.json();
    boot.capabilities.agy = {ready:true};
    await route.fulfill({json:boot});
  });
  const id='d'.repeat(32);let generation=0,voice='',engine='codex',sendEvent,agyId;
  const wire=[],starts=[],selections=[],micStates=[],packets=[];
  await page.routeWebSocket(/\/sessions\/[^/]+\/stream$/,socket=>{
    socket.onMessage(raw=>{
      const e=JSON.parse(String(raw));wire.push(e.type);packets.push(e);
      const send=data=>socket.send(JSON.stringify({session_id:id,generation,...data}));
      sendEvent=send;
      if(e.type==='mic')micStates.push(e.enabled);
      if(e.type==='authenticate')send({type:'ready',voice_engine:engine,codex_voice:voice,interaction_mode:'normal'});
      if(e.type==='agy_voice_start'){
        agyId=e.rtc_id;
        send({type:'native_voice_status',stage:'connected',rtc_id:agyId});
      }
      if(e.type==='agy_tool_confirm')send({type:'agy_classroom_proposal',id:null});
      if(e.type==='native_voice_offer')send({type:'native_voice_answer',rtc_id:e.rtc_id,sdp:'v=0 synthetic answer'});
      if(e.type==='interaction'){
        send({type:'interaction_policy',mode:e.mode});send({type:'native_voice_policy',mode:e.mode});
      }
      if(e.type==='interrupt'){
        generation++;send({type:'interrupted'});send({type:'native_voice_restart'});
      }
      if(e.type==='text'){
        generation++;send({type:'interrupted'});send({type:'native_voice_restart'});
        send({type:'transcript',role:'assistant',text:'二十四加十八等於四十二。',delivery:'text_only'});
      }
      if(e.type==='native_voice_select'){
        voice=e.voice;selections.push(voice);generation++;
        send({type:'interrupted'});send({type:'native_voice_restart'});send({type:'native_voice_selected',voice});
      }
      if(e.type==='close')send({type:'closed',cost:{voice_engine:engine}});
    });
  });
  await page.route(connection.base+'/sessions',route=>{
    const body=route.request().postDataJSON();starts.push(body);
    voice=body.codex_voice;engine=body.basic_voice;generation=0;
    return route.fulfill({json:{id,online:true,mode:'basic',basic_voice:engine,backend:engine,
      codex_voice:voice,teacher_name:body.teacher_name,lesson:null}});
  });
  await page.route(/\/sessions\/[^/]+\/finalize$/,route=>route.fulfill({json:{session_id:id,state:'completed',stage:'saved',summary_available:false}}));
  await page.route(/\/sessions\/[^/]+\/finalization$/,route=>route.fulfill({json:{session_id:id,state:'completed',stage:'saved',summary_available:false}}));
  await page.route(/\/sessions\/[^/]+\/frame$/,route=>route.fulfill({json:{accepted:true}}));
  await page.route(/\/sessions\/[^/]+\/events$/,route=>route.fulfill({json:{saved:true}}));
  await page.reload();
  await page.getByRole('button',{name:/^Codex Live/}).click();
  const voiceSelect=page.getByLabel('Codex 老師聲線',{exact:true});
  const startLesson=page.getByRole('button',{name:'檢查訂閱並開始陪讀',exact:true}).and(page.locator(':enabled'));
  await startLesson.waitFor();
  const voiceChoices=await voiceSelect.locator('option').evaluateAll(nodes=>nodes.map(n=>n.value));
  assert.ok(voiceChoices.includes('arbor')&&voiceChoices.includes('cove')&&!voiceChoices.includes('marin'));
  await voiceSelect.selectOption('arbor');
  await page.waitForFunction(() => Object.entries(localStorage).some(([key, value]) => /^codex-voice:.+/.test(key) && value === 'arbor'));
  assert.equal(await page.getByRole('button',{name:'試聽老師聲音'}).count(),0);
  const modes = page.getByRole('group',{name:'對話方式'}).getByRole('button');
  assert.equal(await modes.count(),2);
  assert.ok((await modes.allTextContents()).every(text=>/^(Codex|agy) Live/.test(text)));
  await page.reload();
  assert.equal(await page.getByRole('button',{name:/^Codex Live/}).getAttribute('aria-pressed'),'true');
  await voiceSelect.and(page.locator('select:has(option[value="arbor"]:checked)')).waitFor();
  const screenshots=path.resolve('test-results/native-voice-ui');await fs.mkdir(screenshots,{recursive:true});
  await page.screenshot({path:path.join(screenshots,'settings.png'),fullPage:true});
  await page.setViewportSize({width:980,height:760});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'native settings must fit narrow screens');
  await page.screenshot({path:path.join(screenshots,'settings-narrow.png'),fullPage:true});
  await page.setViewportSize({width:1400,height:900});
  await startLesson.click();
  await page.getByText('訂閱語音：已連接',{exact:true}).waitFor();
  assert.ok((await page.locator('.messages').boundingBox()).height>=160,'native dialogue must remain readable');
  await page.getByRole('button',{name:'顯示預覽',exact:true}).click();
  await page.locator('.camera-view').waitFor({state:'visible'});
  assert.ok((await page.locator('.messages').boundingBox()).height>=160,'expanded camera must not collapse the dialogue');
  await page.getByRole('button',{name:'收起預覽',exact:true}).click();
  assert.equal(starts[0].basic_voice,'codex');
  assert.equal(starts[0].codex_voice,'arbor');
  assert.equal(await page.getByLabel('課中 Codex 老師聲線').inputValue(),'arbor');
  const microphoneState=()=>page.evaluate(()=>({
    liveSources:window.__sourceTracks.filter(track=>track.readyState==='live').length,
    enabledSources:window.__sourceTracks.filter(track=>track.readyState==='live'&&track.enabled).length,
    enabledPeers:window.__nativeTracks.filter(track=>track.readyState==='live'&&track.enabled).length,
  }));
  const micButton=page.locator('.controls button').first();
  const stopTeacher=page.getByRole('button',{name:'停止老師說話',exact:true});
  await page.evaluate(()=>{window.__teacherSpeaking=true;});
  await stopTeacher.waitFor();
  assert.equal(await micButton.getAttribute('aria-label'),'關閉麥克風');
  assert.equal(await micButton.getAttribute('class'),'');
  assert.equal(await page.getByRole('button',{name:'打斷老師並說話',exact:true}).count(),0);
  assert.deepEqual(await microphoneState(),{liveSources:1,enabledSources:1,enabledPeers:1});
  await page.screenshot({path:path.join(screenshots,'teacher-speaking.png'),fullPage:true});
  let offers=wire.filter(type=>type==='native_voice_offer').length;
  let interrupts=wire.filter(type=>type==='interrupt').length;
  await stopTeacher.click();
  await waitForMessage(() => wire.filter(type=>type==='native_voice_offer').length>offers, 'Stopping the teacher starts a new offer');
  await page.waitForFunction(()=>!window.__teacherSpeaking);
  await page.getByText('訂閱語音：已連接',{exact:true}).waitFor();
  await page.getByRole('button',{name:'關閉麥克風',exact:true}).waitFor();
  assert.equal(wire.filter(type=>type==='interrupt').length,interrupts+1);
  assert.equal(wire.filter(type=>type==='native_voice_offer').length,offers+1);
  assert.equal(micStates.at(-1),true,'stopping the teacher preserves an open mic');
  assert.deepEqual(await microphoneState(),{liveSources:1,enabledSources:1,enabledPeers:1});
  await page.evaluate(()=>{window.__teacherSpeaking=true;});
  await stopTeacher.waitFor();
  sendEvent({type:'thinking'});
  await page.locator('.conversation-heading span').filter({hasText:'正在整理提示'}).waitFor();
  offers=wire.filter(type=>type==='native_voice_offer').length;
  interrupts=wire.filter(type=>type==='interrupt').length;
  await page.getByRole('button',{name:'關閉麥克風',exact:true}).click();
  await waitForMessage(() => micStates.at(-1)===false, 'The core receives the mute control');
  await page.getByRole('button',{name:'開啟麥克風',exact:true}).waitFor();
  assert.equal(await micButton.getAttribute('class'),'muted');
  assert.ok(await micButton.locator('svg.lucide-mic-off').count());
  assert.deepEqual(await microphoneState(),{liveSources:1,enabledSources:0,enabledPeers:0});
  assert.equal(micStates.at(-1),false,'mute disables capture, including during native thinking');
  assert.equal(wire.filter(type=>type==='interrupt').length,interrupts,'mute must not interrupt the teacher');
  assert.equal(wire.filter(type=>type==='native_voice_offer').length,offers,'mute keeps teacher playback connected');
  assert.ok(await stopTeacher.isVisible(),'teacher playback remains independent of the mic');
  await page.screenshot({path:path.join(screenshots,'microphone-muted.png'),fullPage:true});
  await page.evaluate(()=>{window.__teacherSpeaking=false;});
  await stopTeacher.waitFor({state:'hidden'});
  assert.deepEqual(await microphoneState(),{liveSources:1,enabledSources:0,enabledPeers:0});
  await page.evaluate(()=>{window.__teacherSpeaking=true;});
  await stopTeacher.waitFor();
  await stopTeacher.click();
  await page.waitForFunction(count=>window.__nativeTracks.length>count,offers);
  await page.getByText('訂閱語音：已連接',{exact:true}).waitFor();
  assert.equal(micStates.at(-1),false,'stopping the teacher must not unmute');
  assert.equal(wire.filter(type=>type==='interrupt').length,interrupts+1);
  assert.deepEqual(await microphoneState(),{liveSources:1,enabledSources:0,enabledPeers:0});
  await page.getByRole('button',{name:'開啟麥克風',exact:true}).waitFor();
  await page.getByRole('button',{name:'重新連接語音',exact:true}).click();
  await page.getByText('訂閱語音：已連接',{exact:true}).waitFor();
  assert.deepEqual(await microphoneState(),{liveSources:1,enabledSources:0,enabledPeers:0});
  await page.evaluate(()=>{window.__teacherSpeaking=true;});
  await stopTeacher.waitFor();
  offers=wire.filter(type=>type==='native_voice_offer').length;
  interrupts=wire.filter(type=>type==='interrupt').length;
  await page.getByRole('button',{name:'開啟麥克風',exact:true}).click();
  await waitForMessage(() => micStates.at(-1)===true, 'The core receives the unmute control');
  await page.getByRole('button',{name:'關閉麥克風',exact:true}).waitFor();
  assert.equal(await micButton.getAttribute('class'),'');
  assert.equal(micStates.at(-1),true);
  assert.deepEqual(await microphoneState(),{liveSources:1,enabledSources:1,enabledPeers:1});
  assert.equal(wire.filter(type=>type==='native_voice_offer').length,offers,'unmute resumes the same call');
  assert.equal(wire.filter(type=>type==='interrupt').length,interrupts,'unmute does not interrupt playback');
  await page.getByLabel('課中 Codex 老師聲線').selectOption('cove');
  await waitForMessage(() => selections.length>0, 'The core receives the voice selection');
  await page.getByText('訂閱語音：已連接',{exact:true}).waitFor();
  assert.deepEqual(selections,['cove']);
  assert.equal(await page.evaluate(studentId=>localStorage.getItem('codex-voice:'+studentId),starts[0].student_id),'cove');
  assert.equal(starts[0].teacher_route,undefined);
  assert.equal(starts[0].vision_route,undefined);
  await page.getByRole('button',{name:'先聽我說完',exact:true}).click();
  await page.getByRole('button',{name:'繼續對談',exact:true}).waitFor();
  await page.getByRole('button',{name:'繼續對談',exact:true}).click();
  await page.getByText('訂閱語音：已連接',{exact:true}).waitFor();
  await page.getByRole('button',{name:'重新連接語音',exact:true}).click();
  await page.getByText('訂閱語音：已連接',{exact:true}).waitFor();
  assert.ok(wire.filter(e=>e==='native_voice_offer').length>=3);
  const tracks=await page.evaluate(()=>window.__nativeTracks.length);
  await page.getByRole('textbox',{name:'用文字求助'}).fill('二十四加十八？');
  await page.getByRole('button',{name:'送出文字'}).click();
  await page.getByText(/：二十四加十八等於四十二。/).waitFor();
  await page.waitForFunction(count=>window.__nativeTracks.length>count,tracks);
  await page.getByText('訂閱語音：已連接',{exact:true}).waitFor();
  assert.ok(await page.evaluate(()=>window.__nativeTracks.every(track=>track.readyState==='ended'||!track.enabled)),
    'typed help must disable cloned microphone tracks, including a restarted peer');
  assert.deepEqual(await microphoneState(),{liveSources:1,enabledSources:0,enabledPeers:0});
  assert.ok(wire.includes('native_voice_connected'),'server speech waits for browser media readiness');
  assert.ok(!wire.includes('audio'),'native must never upload Groq PCM');
  await page.screenshot({path:path.join(screenshots,'lesson.png'),fullPage:true});
  await page.evaluate(()=>{window.__teacherSpeaking=true;});
  await stopTeacher.waitFor();
  await page.evaluate(() => window.desktop.focus(false));
  for (const [width, height] of [[911, 512], [390, 844]]) {
    await page.setViewportSize({ width, height });
    const layout = await page.evaluate(() => ({
      controls: document.querySelector('.controls').getBoundingClientRect().bottom,
      stop: document.querySelector('.native-stop-speech').getBoundingClientRect().toJSON(),
      height: innerHeight, width: innerWidth, scroll: document.documentElement.scrollWidth,
    }));
    assert.ok(layout.controls <= layout.height && layout.scroll <= layout.width &&
      layout.stop.top>=0 && layout.stop.bottom<=layout.height && layout.stop.right<=layout.width,
      `Native voice controls must stay visible: ${JSON.stringify(layout)}`);
    await page.screenshot({path:path.join(screenshots,`lesson-${width}.png`)});
  }
  await page.getByRole('button',{name:'結束',exact:true}).click();
  await startLesson.waitFor();
  assert.ok(await page.evaluate(()=>[...window.__sourceTracks,...window.__nativeTracks]
    .every(track=>track.readyState==='ended')),'ending a muted lesson releases all microphone tracks');
  await page.setViewportSize({width:1400,height:900});
  await page.getByRole('button',{name:/^agy Live/}).click();
  await page.getByRole('button',{name:'agy 首次設定'}).waitFor();
  await startLesson.click();
  await page.getByText('訂閱語音：已連接',{exact:true}).waitFor();
  assert.equal(starts.at(-1).basic_voice,'agy');
  assert.equal(await page.getByLabel('課中 Codex 老師聲線').count(),0);
  await page.getByRole('button',{name:'關閉麥克風',exact:true}).waitFor();
  await page.waitForFunction(()=>Boolean(window.__agyInput?.onmessage));
  await page.evaluate(()=>window.__agyInput.onmessage({data:new Float32Array(1280).fill(.1)}));
  await waitForMessage(() => packets.some(e=>e.type==='agy_audio'&&e.rtc_id===agyId&&e.pcm.length>100), 'The core receives agy microphone audio');
  sendEvent({type:'agy_classroom_proposal',id:'proposal-1',lines:['2 + 3 = 5']});
  const review=page.getByRole('group',{name:'待確認白板'});
  await review.getByText('2 + 3 = 5').waitFor();
  await review.getByRole('button',{name:'確認寫入'}).click();
  await review.waitFor({state:'hidden'});
  assert.ok(packets.some(e=>e.type==='agy_tool_confirm'&&e.approved&&e.id==='proposal-1'));
  sendEvent({type:'agy_pcm',rtc_id:agyId,rate:24000,audio:'AQABAAEA'});
  await stopTeacher.waitFor();
  const oldAgyId=agyId;
  await stopTeacher.click();
  await waitForMessage(() => agyId!==oldAgyId, 'Stopping teacher playback starts a new agy session');
  await page.getByText('訂閱語音：已連接',{exact:true}).waitFor();
  assert.notEqual(agyId,oldAgyId);
  sendEvent({type:'agy_pcm',rtc_id:oldAgyId,rate:24000,audio:'AQABAAEA'});
  sendEvent({type:'transcript',role:'user',text:'舊音訊測試確認'});
  await page.getByRole('log',{name:'陪讀對話'}).getByText('舊音訊測試確認').waitFor();
  assert.ok(!(await stopTeacher.isVisible()),'retired agy audio must stay silent');
  await page.getByRole('button',{name:'結束',exact:true}).click();
  await startLesson.waitFor();
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:['no-key startup','native voice UI','two subscription modes','agy PCM and confirmation','retired agy PCM ignored',
    'preferences survive reload','voice choices match V3','prelesson voice saved','in-lesson voice switch saved',
    'teacher speech keeps mic open','separate stop preserves open mic','mute disables source and peer during speech and thinking',
    'teacher silence does not unmute','stop and reconnect preserve mute','unmute resumes the same call',
    'listen and resume','reconnect','typed help mutes cloned microphone','media readiness','readable dialogue and camera toggle',
    'stop stays visible in narrow windows','ending a muted lesson releases microphone','no PCM fallback'],screenshots,realServicesUsed:false}));
} finally {
  await app.close();
  await fs.rm(home, { recursive: true, force: true }).catch(() => {});
}
