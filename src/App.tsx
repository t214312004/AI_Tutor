import { useEffect, useRef, useState } from 'react';
import {
  ArrowRight,
  BookOpen,
  CalendarDays,
  Camera,
  CameraOff,
  Check,
  ChevronRight,
  CircleHelp,
  Leaf,
  Maximize,
  Mic,
  MicOff,
  Settings,
  ShieldCheck,
  Square,
  Volume2,
  X,
} from 'lucide-react';
import { request } from './api';
import {
  appendTranscript,
  Boundary,
  Generation,
  LatestQueue,
  MicState,
  playbackReceipt,
} from './core';
import {
  capture,
  captureMeta,
  CameraSettings,
  defaultCameraSettings,
  defaultCorners,
  focusCameraSettings,
  homography,
  isFullFrameCorners,
  Point,
  renderFrame,
  rotatedSize,
  Rotation,
} from './camera';
import { loadCameraSettings, preferredCameraDevice, saveCameraSettings } from './camera-settings';
import CurriculumPanel from './CurriculumPanel';
import LearningMemory from './LearningMemory';
import { BasicConnection } from './session';
import {
  defaultGptConfig, defaultRoute, GptLiveConfig, GptOptions, GptPreferences, GptProvider,
  loadPreferences, reconcileDefaults, savePreferences, updatePreferences,
} from './gpt-models';

type Student = {
  id: string;
  name: string;
  grade: number;
  preferences: string;
  school_id: string | null;
  school_name: string | null;
};
type School = {
  school: string;
  year: number | null;
  source: string;
  notice: string;
  textbooks: Record<string, Record<string, string>>;
  events: { date: string; end?: string; name: string }[];
};
type VoiceOption = { id: string; label: string };
const initialGptOptions: GptOptions = {
  openai: { models: [
    { id: 'gpt-6.1-sol', label: 'GPT-6.1 Sol', efforts: ['low','medium','high','xhigh','max'], image: true },
    { id: 'gpt-6-luna', label: 'GPT-6 Luna', efforts: ['none','low','medium','high','xhigh','max'], image: true },
  ], error: null },
  codex: { models: [], error: null },
  agy: { models: [], error: null },
};
type Finalization = {
  session_id: string;
  state: string;
  stage: string;
  snapshot_saved: boolean;
  summary_available: boolean;
  online: boolean;
  error?: string | null;
};
type Boot = {
  students: Student[];
  schools: Record<string, School>;
  teacher_defaults: Record<string, { teacher_name: string; edge_voice: string }>;
  voice_options: VoiceOption[];
  capabilities: Record<
    string,
    { ready: boolean; reason: string; groq_key?: boolean; key?: boolean }
  >;
};
const modes = [
  ['basic', '基本陪讀', 'Groq · Codex / agy'],
  ['gpt', 'GPT Live', 'GPT-Live-1'],
  ['gemini', 'Gemini Live', 'Gemini 3.8 Live'],
];
function drawFrame(target: HTMLCanvasElement | null, frame: HTMLCanvasElement) {
  if (!target) return;
  if (target.width !== frame.width) target.width = frame.width;
  if (target.height !== frame.height) target.height = frame.height;
  target.getContext('2d')?.drawImage(frame, 0, 0);
}
const preview: Boot = {
  students: [], schools: {}, teacher_defaults: {},
  voice_options: [
    { id: 'zh-TW-YunJheNeural', label: '男生聲音（YunJhe）' },
    { id: 'zh-TW-HsiaoYuNeural', label: '女生聲音（HsiaoYu）' },
    { id: 'zh-TW-HsiaoChenNeural', label: '女生聲音（HsiaoChen）' },
  ],
  capabilities: {},
};
const emptyStudent: Student = {
  id: '', name: '', grade: 3, preferences: '', school_id: null, school_name: null,
};

export default function App() {
  const [boot, setBoot] = useState<Boot>(preview),
    [selected, setSelected] = useState(''),
    [mode, setMode] = useState('basic'),
    [notes, setNotes] = useState('');
  const [teacherName, setTeacherName] = useState('');
  const [edgeVoice, setEdgeVoice] = useState('');
  const [sid, setSid] = useState(''),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [parent, setParent] = useState(false),
    [tab, setTab] = useState('profile');
  const [profileDraft, setProfileDraft] = useState<Student | null>(null);
  const [profileSaving, setProfileSaving] = useState(false);
  const [newStudentName, setNewStudentName] = useState('');
  const [newStudentGrade, setNewStudentGrade] = useState(3);
  const [showStudentForm, setShowStudentForm] = useState(false);
  const parentTrigger = useRef<HTMLButtonElement>(null);
  const parentDialog = useRef<HTMLElement>(null);
  const [cameraPreviewExpanded, setCameraPreviewExpanded] = useState(false);
  const [full, setFull] = useState(false),
    [cameraOn, setCameraOn] = useState(false),
    [cameraReady, setCameraReady] = useState(false),
    [micOn, setMicOn] = useState(false),
    [audioStage, setAudioStage] = useState('off'),
    [speaking, setSpeaking] = useState(false),
    [captured, setCaptured] = useState(0),
    [lastFrame, setLastFrame] = useState('');
  const [cameraSettings, setCameraSettings] = useState<CameraSettings>(defaultCameraSettings);
  const cameraSettingsRef = useRef(cameraSettings);
  cameraSettingsRef.current = cameraSettings;
  const [focusRect, setFocusRect] = useState<[number,number,number,number] | null>(null);
  const [focusPicking, setFocusPicking] = useState(false);
  const focusStart = useRef<Point | null>(null);
  const activeCameraSettings = focusRect ? focusCameraSettings(cameraSettings,focusRect) : cameraSettings;
  const activeSettingsRef = useRef(activeCameraSettings);
  activeSettingsRef.current = activeCameraSettings;
  const [cameraDeviceId, setCameraDeviceId] = useState(preferredCameraDevice);
  const [cameraDevices, setCameraDevices] = useState<MediaDeviceInfo[]>([]);
  const [cameraSize, setCameraSize] = useState({ width: 0, height: 0 });
  const [cameraWanted, setCameraWanted] = useState(true);
  const [cameraLoading, setCameraLoading] = useState(false);
  const cameraWantedRef = useRef(true);
  const [cameraEditing, setCameraEditing] = useState(false);
  const [draftCorners, setDraftCorners] = useState<Point[]>(defaultCorners);
  const [cameraMessage, setCameraMessage] = useState('正在開啟鏡頭預覽…');
  const [previewRatio, setPreviewRatio] = useState(16 / 9);
  const [voiceStatus, setVoiceStatus] = useState('');
  const [voiceTesting, setVoiceTesting] = useState(false);
  const [micLevel, setMicLevel] = useState(0);
  const [micDevices, setMicDevices] = useState<MediaDeviceInfo[]>([]);
  const [micDeviceId, setMicDeviceId] = useState('');
  const [micChecking, setMicChecking] = useState(false);
  const [history, setHistory] = useState<any>(null),
    [recording, setRecording] = useState(false),
    [heard, setHeard] = useState(''),
    [elapsed, setElapsed] = useState(0);
  const [keyValues, setKeyValues] = useState({
      GROQ_API_KEY: '',
      OPENAI_API_KEY: '',
      GEMINI_API_KEY: '',
    }),
    [saved, setSaved] = useState(false);
  const [keyStatus, setKeyStatus] = useState<
    Record<string, { has_key: boolean; valid: boolean | null }>
  >({});
  const [keyChecking, setKeyChecking] = useState(false);
  const video = useRef<HTMLVideoElement>(null),
    homeCanvas = useRef<HTMLCanvasElement>(null),
    lessonCanvas = useRef<HTMLCanvasElement>(null),
    correctedCanvas = useRef<HTMLCanvasElement>(null),
    cam = useRef<MediaStream | null>(null),
    mic = useRef<MediaStream | null>(null),
    recorder = useRef<MediaRecorder | null>(null),
    audio = useRef<HTMLAudioElement | null>(null),
    audioURL = useRef('');
  const micState = useRef(new MicState()),
    epoch = useRef(new Generation()),
    sidRef = useRef(''),
    selectedRef = useRef(selected),
    historyRequest = useRef(0),
    speakingData = useRef<{ text: string; boundaries: Boundary[] }>({ text: '', boundaries: [] }),
    boardRef = useRef<HTMLDivElement>(null);
  selectedRef.current = selected;
  const voiceGeneration = useRef(0);
  const cameraRequest = useRef(0);
  const cameraPromise = useRef<Promise<void> | null>(null);
  const cameraDrain = useRef<Promise<void> | null>(null);
  const nativePhotoTask = useRef<Promise<{ image: string; meta: Record<string, unknown> } | null> | null>(null);
  const dragCorner = useRef<number | null>(null);
  const micMeter = useRef<{ stream: MediaStream; context: AudioContext; timer: number } | null>(
    null,
  );
  const micCheckRequest = useRef(0);
  const micCheckPending = useRef(false);
  const micRequest = useRef(0);
  const micPending = useRef(false);
  const noteRequest = useRef(0);
  const notePending = useRef(false);
  const startRequest = useRef(0);
  const startPending = useRef(false);
  const teaching = useRef<BasicConnection | null>(null);
  const recoverDevices = useRef({ camera: false, mic: false });
  const syntheticAudio = useRef<AudioContext | null>(null);
  const [online, setOnline] = useState(false);
  const [finalization, setFinalization] = useState<Finalization | null>(null);
  const [finalizationWaitSeconds, setFinalizationWaitSeconds] = useState(0);
  const [startupChecked, setStartupChecked] = useState(false);
  const [startupError, setStartupError] = useState(false);
  const finalizationRequest = useRef<{ captured: number; submitted_to_runtime: number; last_playback: { playback_id: string; heard: string } | null } | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [messages, setMessages] = useState<
    { id: string; role: string; text: string; status?: string }[]
  >([]);
  const [lesson, setLesson] = useState<{
    tasks: { id: string; title: string; status: string }[];
    confirmed: boolean;
    all_done: boolean;
  }>({ tasks: [], confirmed: false, all_done: false });
  const [typed, setTyped] = useState('');
  const speechMessage = useRef('');
  const messagesEnd = useRef<HTMLDivElement>(null);
  useEffect(() => {
    messagesEnd.current?.scrollIntoView({ block: 'nearest' });
  }, [messages]);
  const [backend, setBackend] = useState<'codex' | 'agy'>('codex');
  const [gptPreferences, setGptPreferences] = useState<GptPreferences>({
    student: '', config: defaultGptConfig, modelDefaults: { delegation: true, vision: true },
  });
  const gptConfig = gptPreferences.config;
  const setGptConfig = (update: (config: GptLiveConfig) => GptLiveConfig) =>
    setGptPreferences(current => updatePreferences(current, update(current.config)));
  const photoFormatRef = useRef<'jpeg' | 'png'>('jpeg');
  const [gptOptions, setGptOptions] = useState<GptOptions>(initialGptOptions);
  const [gptOptionsLoading, setGptOptionsLoading] = useState(false);
  const student = boot.students.find((s) => s.id === selected) ?? boot.students[0] ?? emptyStudent;
  const draft = profileDraft?.id === selected ? profileDraft : student;
  const profileDirty = !!draft && !!student && (
    draft.name !== student.name || draft.grade !== student.grade ||
    (draft.school_name ?? '') !== (student.school_name ?? '') || draft.preferences !== student.preferences
  );
  const school = student.school_id ? boot.schools[student.school_id] : undefined;
  const activeTeacherName =
    teacherName || boot.teacher_defaults?.[selected]?.teacher_name || '老師';
  const report = (e: unknown) => setError(e instanceof Error ? e.message : String(e));
  const modelChoices = (provider: GptProvider, image: boolean) =>
    gptOptions[provider].models.filter((item) => !image || item.image);
  const selectedTeaching = modelChoices(gptConfig.delegation.provider, false)
    .find((item) => item.id === gptConfig.delegation.model);
  const selectedVision = modelChoices(gptConfig.vision.provider, true)
    .find((item) => item.id === gptConfig.vision.model);
  const gptSelectionReady = !!selectedTeaching && !!selectedVision &&
    selectedTeaching.efforts.includes(gptConfig.delegation.effort) &&
    selectedVision.efforts.includes(gptConfig.vision.effort);
  const firstRoute = (provider: GptProvider, image: boolean) => defaultRoute(gptOptions, provider, image);
  const trace = (event: string, detail?: string | number) => {
    void window.desktop?.diagnostic(event, detail).catch(() => {});
    if (selected === 'student-test' && sidRef.current)
      void request(`/sessions/${sidRef.current}/events`, {
        kind: 'client_diagnostic', payload: { event, detail: String(detail ?? '') },
      }).catch(() => {});
  };
  const refresh = () => request<Boot>('/bootstrap').then((value) => {
    setBoot(value);
    setSelected(current => value.students.some(s => s.id === current) ? current : value.students[0]?.id ?? '');
    return value;
  });
  useEffect(() => {
    setHistory(null);
    setSaved(false);
    const defaults = boot.teacher_defaults?.[selected];
    if (defaults) {
      setTeacherName(defaults.teacher_name);
      setEdgeVoice(defaults.edge_voice);
    }
  }, [
    selected,
    boot.teacher_defaults?.[selected]?.teacher_name,
    boot.teacher_defaults?.[selected]?.edge_voice,
  ]);
  useEffect(() => {
    if (window.desktop) void refresh().catch(report);
  }, []);
  useEffect(() => {
    let saved: string | null = null;
    try {
      saved = window.localStorage.getItem(`gpt-live-config:${selected}`);
    } catch { /* Use defaults if browser storage is unavailable. */ }
    setGptPreferences(loadPreferences(selected, saved));
  }, [selected]);
  useEffect(() => {
    if (gptPreferences.student !== selected) return;
    try { window.localStorage.setItem(`gpt-live-config:${selected}`, savePreferences(gptPreferences)); }
    catch { /* Preferences are optional; the session still receives the selected config. */ }
  }, [gptPreferences, selected]);
  useEffect(() => {
    setGptPreferences(current => reconcileDefaults(current, gptOptions));
  }, [gptOptions, gptPreferences]);
  useEffect(() => {
    if (mode !== 'gpt' || !window.desktop) return;
    setGptOptionsLoading(true);
    void request<GptOptions>('/gpt-options')
      .then(setGptOptions)
      .catch(report)
      .finally(() => setGptOptionsLoading(false));
  }, [mode]);
  useEffect(() => {
    if (!window.desktop) { setStartupChecked(true); return; }
    request<{items: Finalization[]}>('/finalizations/pending')
      .then(async ({items}) => {
        if (items.length) {
          setFinalization(items[0]);
          await window.desktop?.setFinalizing(true);
        }
      })
      .catch((error) => { report(error); setStartupError(true); })
      .finally(() => setStartupChecked(true));
  }, []);
  useEffect(() => {
    if (!finalization) return;
    const id = finalization.session_id;
    if (finalization.state === 'completed') {
      const timer = window.setTimeout(() => {
        void request<{items: Finalization[]}>('/finalizations/pending')
          .then(async ({items}) => {
            if (items.length) setFinalization(items[0]);
            else {
              await window.desktop?.setFinalizing(false);
              setFinalization(null);
            }
          })
          .catch(report);
      }, 800);
      return () => window.clearTimeout(timer);
    }
    const timer = window.setInterval(() => {
      void request<Finalization>(`/sessions/${id}/finalization`)
        .then((value) => {
          setFinalization((current) => current?.session_id === id ? value : current);
        })
        .catch(() => {});
    }, 1600);
    return () => window.clearInterval(timer);
  }, [finalization?.session_id, finalization?.state]);
  useEffect(() => {
    if (!parent || tab !== 'connection' || !window.desktop) return;
    setKeyChecking(true);
    request<Record<string, { has_key: boolean; valid: boolean | null }>>('/credentials/status')
      .then(setKeyStatus)
      .catch(() => {})
      .finally(() => setKeyChecking(false));
  }, [parent, tab]);
  useEffect(() => {
    if (!parent) return;
    setProfileDraft({ ...student });
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const frame = requestAnimationFrame(() => parentDialog.current?.querySelector<HTMLElement>('button')?.focus());
    return () => {
      cancelAnimationFrame(frame);
      document.body.style.overflow = previousOverflow;
    };
  }, [parent]);
  useEffect(() => {
    if (!parent) return;
    setProfileDraft({ ...student });
    setSaved(false);
  }, [selected]);
  useEffect(() => {
    if (!finalization) {
      setFinalizationWaitSeconds(0);
      return;
    }
    const started = Date.now();
    setFinalizationWaitSeconds(0);
    const timer = window.setInterval(() => setFinalizationWaitSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [finalization?.session_id]);
  useEffect(() => {
    if (!parent || tab !== 'history' || !window.desktop) {
      historyRequest.current++;
      return;
    }
    const requestId = ++historyRequest.current;
    const studentId = selected;
    setHistory(null);
    request(`/students/${studentId}/history`)
      .then((value) => {
        if (requestId === historyRequest.current && selectedRef.current === studentId)
          setHistory(value);
      })
      .catch((error) => {
        if (requestId === historyRequest.current) report(error);
      });
    return () => {
      historyRequest.current++;
    };
  }, [parent, tab, selected]);
  async function reloadHistory(studentId: string) {
    const requestId = ++historyRequest.current;
    const value = await request(`/students/${studentId}/history`);
    if (requestId === historyRequest.current && selectedRef.current === studentId)
      setHistory(value);
  }
  function chime() {
    try {
      const ctx = new AudioContext();
      const gain = ctx.createGain();
      gain.gain.setValueAtTime(0.12, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.8);
      gain.connect(ctx.destination);
      const o = ctx.createOscillator();
      o.frequency.value = 660;
      o.connect(gain);
      o.start();
      o.stop(ctx.currentTime + 0.8);
      o.onended = () => void ctx.close();
    } catch {}
  }
  useEffect(
    () =>
      window.desktop?.onFocus((value) => {
        setFull(value);
        if (!value) {
          chime();
          if (sidRef.current)
            void request(`/sessions/${sidRef.current}/events`, {
              kind: 'focus_exit',
              payload: { at: new Date().toISOString() },
            }).catch(report);
        }
      }),
    [],
  );
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (parent) {
        if (e.key === 'Escape') {
          e.preventDefault();
          void closeParent();
        } else if (e.key === 'Tab') {
          const dialog = parentDialog.current;
          if (!dialog) return;
          const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
            'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), summary, a[href]',
          )).filter((element) => element.getClientRects().length > 0);
          if (!focusable.length) return;
          const first = focusable[0], last = focusable[focusable.length - 1];
          if (!dialog.contains(document.activeElement) || (e.shiftKey && document.activeElement === first)) {
            e.preventDefault();
            (e.shiftKey ? last : first).focus();
          } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first.focus();
          }
        }
        return;
      }
      if (e.key === 'Escape' || e.key === 'F11') {
        e.preventDefault();
        void window.desktop?.focus(e.key === 'Escape' ? false : !full);
      }
    };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, [full, parent, profileDirty, profileSaving, tab]);
  useEffect(
    () =>
      window.desktop?.onSuspend((reason) => {
        trace('media_suspend', reason);
        epoch.current.next();
        startRequest.current++;
        startPending.current = false;
        cameraWantedRef.current = false;
        setCameraWanted(false);
        stopDevices();
        setCameraMessage('裝置已停止，請手動重新開啟鏡頭');
        stopAudio(false);
        setVoiceTesting(false);
        setVoiceStatus('');
        const interruptedTeaching = teaching.current;
        teaching.current = null;
        setOnline(false);
        setReconnecting(false);
        setBusy(false);
        void interruptedTeaching?.close().catch(report);
        if (syntheticAudio.current && syntheticAudio.current.state !== 'closed')
          void syntheticAudio.current.close();
        if (reason === 'core') {
          sidRef.current = '';
          setSid('');
          setLastFrame('');
          setError('本機核心已中斷，裝置已停止。請重新啟動伴讀；本次紀錄會標記為中斷。');
        } else {
          setError('裝置已因鎖定或休眠而停止。若原本正在 AI 陪讀，請結束本次後重新開始。');
        }
      }),
    [],
  );
  useEffect(() => {
    const target = boardRef.current;
    if (!sid || !target) return;
    const observer = new ResizeObserver(() => {
      const r = target.getBoundingClientRect();
      void window.desktop
        ?.resizeBoard({
          x: r.x + 1,
          y: r.y + 1,
          width: Math.max(0, r.width - 2),
          height: Math.max(0, r.height - 2),
        })
        .catch(report);
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [sid]);
  useEffect(() => {
    if (!sid) return;
    const start = Date.now();
    setElapsed(0);
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 1000);
    return () => clearInterval(id);
  }, [sid]);
  useEffect(() => {
    if (!sid || !cameraOn) return;
    const generation = epoch.current.current();
    const queue = new LatestQueue<CameraSettings>(async (settings) => {
      if (!epoch.current.accepts(generation)) return;
      const frame = await takeNativePhoto(settings, photoFormatRef.current);
      if (!frame || !epoch.current.accepts(generation)) return;
      setLastFrame(frame.image);
      setCaptured((n) => n + 1);
      await teaching.current?.frame(frame.image, frame.meta);
    }, (error) => { if (epoch.current.accepts(generation)) report(error); });
    const snap = () => {
      try {
        queue.push(activeSettingsRef.current);
      } catch (e) {
        report(e);
      }
    };
    const timer = setInterval(snap, 10000);
    if (focusRect) snap();
    return () => {
      clearInterval(timer);
      queue.close();
    };
  }, [sid, cameraOn, focusRect]);
  useEffect(() => {
    if (video.current) video.current.srcObject = cam.current;
  }, [cameraOn]);
  useEffect(() => {
    if (!cameraOn) return;
    let frameId = 0;
    let lastDraw = 0;
    const draw = (time: number) => {
      if (time - lastDraw >= 80 && video.current?.readyState && video.current.readyState >= 2) {
        lastDraw = time;
        try {
          const active =
            cameraEditing && !sid ? { ...cameraSettings, corners: draftCorners }
              : sid ? activeCameraSettings : cameraSettings;
          const frame = renderFrame(video.current, active, sid ? 340 : 720, cameraEditing && !sid);
          drawFrame(sid ? lessonCanvas.current : homeCanvas.current, frame);
          setCameraReady(true);
          if (!sid && cameraEditing)
            drawFrame(correctedCanvas.current, renderFrame(video.current, active, 320));
          const ratio = frame.width / frame.height;
          setPreviewRatio((old) => (Math.abs(old - ratio) > 0.005 ? ratio : old));
        } catch (e) {
          if (!cameraEditing) report(e);
          const target = correctedCanvas.current;
          if (target) target.getContext('2d')?.clearRect(0, 0, target.width, target.height);
        }
      }
      frameId = window.requestAnimationFrame(draw);
    };
    frameId = window.requestAnimationFrame(draw);
    return () => window.cancelAnimationFrame(frameId);
  }, [cameraOn, cameraSettings, cameraEditing, draftCorners, sid, focusRect]);
  useEffect(() => {
    if (!window.desktop) {
      setCameraMessage('桌面程式中可預覽鏡頭');
      return;
    }
    const refreshDevices = () => {
      void listDevices();
    };
    navigator.mediaDevices?.addEventListener('devicechange', refreshDevices);
    return () => {
      navigator.mediaDevices?.removeEventListener('devicechange', refreshDevices);
      releaseCamera();
      stopMicCheck();
    };
  }, []);
  useEffect(() => {
    if (window.desktop && startupChecked && !finalization && !sid && cameraWantedRef.current)
      void ensureCamera();
  }, [startupChecked, finalization?.session_id, sid]);
  useEffect(
    () => () => {
      cam.current?.getTracks().forEach((t) => t.stop());
      mic.current?.getTracks().forEach((t) => t.stop());
      audio.current?.pause();
    },
    [],
  );
  function stopDevices() {
    micRequest.current++;
    noteRequest.current++;
    micPending.current = false;
    notePending.current = false;
    try { teaching.current?.setMic(false); } catch (e) { report(e); }
    releaseCamera();
    stopMicCheck();
    mic.current?.getTracks().forEach((t) => t.stop());
    mic.current = null;
    setMicOn(false);
    setAudioStage('off');
    micState.current.userEnabled = false;
    if (recorder.current?.state === 'recording') recorder.current.stop();
  }
  async function listDevices() {
    try {
      const all = await navigator.mediaDevices.enumerateDevices();
      setCameraDevices(all.filter((d) => d.kind === 'videoinput'));
      setMicDevices(all.filter((d) => d.kind === 'audioinput'));
    } catch {
      /* device list is optional */
    }
  }
  function releaseCamera() {
    cameraRequest.current++;
    if (cameraPromise.current || nativePhotoTask.current) {
      cameraDrain.current = Promise.allSettled([cameraPromise.current, nativePhotoTask.current]).then(() => {});
      void window.desktop?.cancelPhoto().catch(report);
    }
    cameraPromise.current = null;
    setCameraLoading(false);
    try { teaching.current?.setCamera(false); } catch (e) { report(e); }
    cam.current?.getTracks().forEach((t) => t.stop());
    cam.current = null;
    if (video.current) video.current.srcObject = null;
    setCameraOn(false);
    setCameraReady(false);
    setLastFrame('');
  }
  async function ensureCamera(deviceId = cameraDeviceId) {
    if (cameraDrain.current) {
      const drain = cameraDrain.current;
      const requestId = cameraRequest.current;
      await drain;
      if (cameraDrain.current === drain) cameraDrain.current = null;
      if (requestId !== cameraRequest.current || !cameraWantedRef.current) return;
    }
    if (nativePhotoTask.current) { await nativePhotoTask.current; return; }
    if (cam.current?.getVideoTracks().some((t) => t.readyState === 'live')) return;
    if (cameraPromise.current) return cameraPromise.current;
    const requestId = ++cameraRequest.current;
    setCameraLoading(true);
    setCameraMessage('正在開啟鏡頭預覽…');
    const pending = (async () => {
      try {
        const requestCamera = (id: string) =>
          navigator.mediaDevices.getUserMedia({
            video: {
              ...(id ? { deviceId: { exact: id } } : {}),
              width: { ideal: 1920 },
              height: { ideal: 1080 },
            },
            audio: false,
          });
        let stream: MediaStream;
        let usedFallback = false;
        try {
          stream = await requestCamera(deviceId);
        } catch (error) {
          if (
            !deviceId ||
            !['NotFoundError', 'OverconstrainedError'].includes((error as DOMException).name)
          )
            throw error;
          stream = await requestCamera('');
          usedFallback = true;
        }
        if (requestId !== cameraRequest.current) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        cam.current = stream;
        const track = stream.getVideoTracks()[0];
        const actual = track.getSettings();
        const id = actual.deviceId ?? deviceId;
        const width = actual.width ?? 1920,
          height = actual.height ?? 1080;
        setCameraDeviceId(id);
        if (id) localStorage.setItem('camera-device-id', id);
        setCameraSize({ width, height });
        setCameraSettings(loadCameraSettings(id, width, height));
        setCameraEditing(false);
        setCameraReady(false);
        setCameraOn(true);
        setCameraMessage(
          usedFallback ? '原相機未連接，已改用預設相機；畫面只在本機預覽' : '鏡頭預覽只在本機處理',
        );
        teaching.current?.setCamera(true);
        if (video.current) {
          video.current.srcObject = stream;
          void video.current.play().catch(() => {});
        }
        track.onended = () => {
          if (cam.current === stream) {
            releaseCamera();
            setCameraMessage('鏡頭已中斷，請重新開啟或選擇其他鏡頭');
          }
        };
        void listDevices();
      } catch (e) {
        if (requestId === cameraRequest.current) {
          setCameraMessage('無法開啟鏡頭；請檢查連接與權限');
          report(e);
          void listDevices();
        }
        trace('camera_error', e instanceof Error ? e.name : 'unknown');
      }
    })();
    cameraPromise.current = pending;
    try {
      await pending;
    } finally {
      if (cameraPromise.current === pending) {
        cameraPromise.current = null;
        setCameraLoading(false);
      }
    }
  }
  async function takeNativePhoto(settings: CameraSettings, format: 'jpeg' | 'png') {
    if (nativePhotoTask.current) return nativePhotoTask.current;
    const stream = cam.current;
    const track = stream?.getVideoTracks()[0];
    if (!track || !window.desktop || !cameraWantedRef.current) return null;
    const requestId = cameraRequest.current;
    const generation = epoch.current.current();
    const device = track.getSettings();
    const previewWidth = device.width ?? video.current?.videoWidth ?? 1920;
    const previewHeight = device.height ?? video.current?.videoHeight ?? 1080;
    const accepts = () => requestId === cameraRequest.current && cameraWantedRef.current &&
      epoch.current.accepts(generation);
    // MediaCapture needs exclusive access on devices such as Link 2C. Keep the
    // logical camera and audio connection active while releasing only preview tracks.
    stream!.getTracks().forEach((item) => item.stop());
    cam.current = null;
    if (video.current) video.current.srcObject = null;
    setCameraMessage('正在拍照，預覽稍後恢復');
    const pending = (async () => {
      try {
        const photo = await window.desktop!.takePhoto({ label: track.label,
          aspectRatio: previewWidth / previewHeight });
        if (!accepts()) return null;
        const image = new Image();
        image.src = photo.image;
        await image.decode();
        if (!accepts()) return null;
        if (image.naturalWidth !== photo.width || image.naturalHeight !== photo.height)
          throw Error('原生照片解析度資訊不一致');
        return { image: capture(image, settings, format), meta: {
          ...captureMeta(image, settings), capture_method: photo.method,
          captured_at: photo.captured_at,
        } };
      } catch (error) {
        if (accepts()) throw error;
        return null;
      } finally {
        if (accepts()) {
          try {
            const restored = await navigator.mediaDevices.getUserMedia({ audio: false, video: {
              ...(device.deviceId ? { deviceId: { exact: device.deviceId } } : {}),
              width: { ideal: previewWidth }, height: { ideal: previewHeight },
            } });
            if (!accepts()) restored.getTracks().forEach((item) => item.stop());
            else {
              cam.current = restored;
              if (video.current) {
                video.current.srcObject = restored;
                void video.current.play().catch(() => {});
              }
              restored.getVideoTracks()[0].onended = () => {
                if (cam.current === restored) {
                  releaseCamera();
                  setCameraMessage('鏡頭已中斷，請重新開啟或選擇其他鏡頭');
                }
              };
              setCameraMessage('原生拍照完成；預覽已恢復');
            }
          } catch (error) {
            if (accepts()) {
              releaseCamera();
              setCameraMessage('照片拍攝後無法恢復預覽，請重新開啟鏡頭');
              report(error);
            }
          }
        }
      }
    })();
    // Camera-off or lesson-end may occur while preview restoration is awaiting
    // device access. Discard the photo after that await as well.
    const guarded = pending.then((frame) => accepts() ? frame : null, (error) => {
      if (accepts()) throw error;
      return null;
    });
    nativePhotoTask.current = guarded;
    try { return await guarded; }
    finally { if (nativePhotoTask.current === guarded) nativePhotoTask.current = null; }
  }
  async function switchCamera(deviceId: string) {
    setFocusRect(null);
    setFocusPicking(false);
    releaseCamera();
    cameraWantedRef.current = true;
    setCameraWanted(true);
    setCameraDeviceId(deviceId);
    localStorage.setItem('camera-device-id', deviceId);
    await ensureCamera(deviceId);
  }
  async function cameraToggle() {
    if (cam.current || cameraPromise.current || nativePhotoTask.current) {
      cameraWantedRef.current = false;
      setCameraWanted(false);
      releaseCamera();
      setCameraMessage('鏡頭已關閉');
    } else {
      cameraWantedRef.current = true;
      setCameraWanted(true);
      await ensureCamera();
    }
  }
  function updateCameraSettings(changes: Partial<CameraSettings>) {
    const next = { ...cameraSettingsRef.current, ...changes };
    cameraSettingsRef.current = next;
    setCameraSettings(next);
    if (cameraSize.width && cameraSize.height)
      saveCameraSettings(cameraDeviceId, cameraSize.width, cameraSize.height, next);
  }
  function stopMicCheck() {
    micCheckRequest.current++;
    micCheckPending.current = false;
    const meter = micMeter.current;
    setMicChecking(false);
    if (!meter) return;
    window.clearInterval(meter.timer);
    meter.stream.getTracks().forEach((t) => t.stop());
    void meter.context.close().catch(() => {});
    micMeter.current = null;
    setMicLevel(0);
  }
  async function toggleMicCheck() {
    if (micMeter.current || micCheckPending.current) {
      stopMicCheck();
      return;
    }
    const requestId = ++micCheckRequest.current;
    micCheckPending.current = true;
    setMicChecking(true);
    let stream: MediaStream | null = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: micDeviceId ? { deviceId: { exact: micDeviceId } } : true,
        video: false,
      });
      if (requestId !== micCheckRequest.current) {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }
      const context = new AudioContext();
      const source = context.createMediaStreamSource(stream);
      const analyser = context.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      const samples = new Uint8Array(analyser.fftSize);
      const timer = window.setInterval(() => {
        analyser.getByteTimeDomainData(samples);
        const rms = Math.sqrt(
          samples.reduce((sum, x) => sum + ((x - 128) / 128) ** 2, 0) / samples.length,
        );
        setMicLevel(Math.min(100, Math.round(rms * 500)));
      }, 120);
      micMeter.current = { stream, context, timer };
      stream.getAudioTracks()[0].onended = () => {
        if (micMeter.current?.stream === stream) {
          stopMicCheck();
          setError('麥克風已中斷，請重新選擇或連接裝置。');
        }
      };
      micCheckPending.current = false;
      void listDevices();
    } catch (e) {
      stream?.getTracks().forEach((t) => t.stop());
      if (requestId === micCheckRequest.current) {
        micCheckPending.current = false;
        setMicChecking(false);
        report(e);
      }
    }
  }
  async function enableMic() {
    if (micPending.current) return;
    micPending.current = true;
    setAudioStage('starting');
    const requestId = ++micRequest.current;
    const ticket = epoch.current.current();
    try {
      const stream =
        mic.current ??
        (await navigator.mediaDevices.getUserMedia({
          audio: {
            ...(micDeviceId ? { deviceId: { exact: micDeviceId } } : {}),
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
          video: false,
        }));
      if (!epoch.current.accepts(ticket) || requestId !== micRequest.current) {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }
      mic.current = stream;
      stream.getTracks().forEach((t) => (t.enabled = true));
      micState.current.userEnabled = true;
      const client = teaching.current;
      if (client) {
        await client.attachMicrophone(stream);
        if (
          !epoch.current.accepts(ticket) ||
          requestId !== micRequest.current ||
          client !== teaching.current
        ) {
          client.setMic(false);
          return;
        }
        client.setMic(true);
      }
      setMicOn(true);
      setAudioStage('ready');
    } catch (e) {
      if (epoch.current.accepts(ticket) && requestId === micRequest.current) {
        trace('microphone_error', e instanceof Error ? e.name : 'unknown');
        mic.current?.getTracks().forEach((t) => t.stop());
        mic.current = null;
        micState.current.userEnabled = false;
        setMicOn(false);
        setAudioStage('off');
        teaching.current?.setMic(false);
        report(e);
      }
    } finally {
      if (requestId === micRequest.current) micPending.current = false;
    }
  }
  function stopAudio(interrupted: boolean) {
    voiceGeneration.current++;
    const player = audio.current;
    if (player && interrupted) {
      const receipt = playbackReceipt(
        speakingData.current.text,
        speakingData.current.boundaries,
        player.currentTime,
      );
      setHeard(receipt.fully_heard + (receipt.possibly_partial ? ' …' : ''));
      if (sidRef.current)
        void request(`/sessions/${sidRef.current}/events`, {
          kind: 'playback_interrupted',
          payload: receipt,
        }).catch(report);
    }
    player?.pause();
    if (player) {
      player.removeAttribute('src');
      player.load();
    }
    audio.current = null;
    if (audioURL.current) URL.revokeObjectURL(audioURL.current);
    audioURL.current = '';
    micState.current.finished();
    setSpeaking(false);
  }
  async function toggleMic() {
    const pendingSpeech = micOn && mode === 'basic' &&
      (thinking || ['sending', 'received', 'transcribing', 'accepted'].includes(audioStage));
    if (micState.current.playbackGate || pendingSpeech) {
      if (teaching.current) {
        const client = teaching.current;
        micState.current.userEnabled = true;
        await client.interrupt();
        if (!sidRef.current) client.setMic(false);
        else if (!mic.current) await enableMic();
        else {
          mic.current.getTracks().forEach((t) => (t.enabled = true));
          setMicOn(true);
          setAudioStage('ready');
        }
        return;
      }
      stopAudio(true);
      if (sidRef.current) await request(`/sessions/${sidRef.current}/interrupt`, {}).catch(report);
      await enableMic();
      return;
    }
    if (micState.current.userEnabled && micOn) {
      micRequest.current++;
      micPending.current = false;
      micState.current.userEnabled = false;
      mic.current?.getTracks().forEach((t) => t.stop());
      mic.current = null;
      setMicOn(false);
      setAudioStage('off');
      teaching.current?.setMic(false);
    } else await enableMic();
  }
  async function start() {
    if (startPending.current || notePending.current || recorder.current?.state === 'recording')
      return;
    startPending.current = true;
    const requestId = ++startRequest.current;
    let createdId = '';
    setError('');
    setBusy(true);
    try {
      stopAudio(false);
      setVoiceTesting(false);
      setVoiceStatus('');
      stopMicCheck();
      setCameraEditing(false);
      await teaching.current?.close();
      teaching.current = null;
      if (syntheticAudio.current && syntheticAudio.current.state !== 'closed')
        await syntheticAudio.current.close();
      syntheticAudio.current = null;
      if (!window.desktop)
        throw Error('介面預覽無法開始陪讀。請使用 npm start 開啟 Windows 桌面程式。');
      const state = await request('/sessions', {
        student_id: selected,
        mode,
        notes,
        teacher_name: teacherName,
        edge_voice: edgeVoice,
        ...(mode === 'basic' ? { backend } : {}),
        ...(mode === 'gpt' ? { gpt_live: gptConfig } : {}),
      });
      createdId = state.id;
      if (requestId !== startRequest.current) {
        await request(`/sessions/${createdId}/end`, {}).catch(() => {});
        return;
      }
      epoch.current.next();
      sidRef.current = state.id;
      photoFormatRef.current = mode === 'gpt' && gptConfig.vision.provider === 'openai' ? 'png' : 'jpeg';
      setSid(state.id);
      trace('session_start', mode);
      setCaptured(0);
      setHeard('');
      setMessages([]);
      setFocusRect(null);
      setFocusPicking(false);
      setLesson({ tasks: [], confirmed: false, all_done: false });
      setTyped('');
      setOnline(false);
      setReconnecting(false);
      await window.desktop.focus(true);
      if (cameraWantedRef.current) await ensureCamera();
      if (requestId !== startRequest.current || sidRef.current !== state.id) return;
      if (state.online) {
        const connection = await window.desktop.connection();
        if (requestId !== startRequest.current || sidRef.current !== state.id) return;
        teaching.current = createTeaching(state.id, mode as 'basic' | 'gpt' | 'gemini');
        await teaching.current.connect(connection.base, connection.token);
        if (requestId !== startRequest.current || sidRef.current !== state.id) return;
        teaching.current.setCamera(!!cam.current);
        setOnline(true);
        await enableMic();
        if (requestId !== startRequest.current || sidRef.current !== state.id) return;
        teaching.current?.begin();
      } else micState.current.userEnabled = false;
    } catch (e) {
      if (requestId !== startRequest.current) return;
      report(e);
      if (createdId && sidRef.current === createdId) {
        stopDevices();
        setCameraMessage('裝置已停止，請手動重新開啟鏡頭');
        await teaching.current?.close().catch(() => {});
        teaching.current = null;
        await request(`/sessions/${createdId}/end`, {}).catch(() => {});
        sidRef.current = '';
        setSid('');
        setOnline(false);
        await window.desktop?.focus(false);
      }
    } finally {
      if (requestId === startRequest.current) {
        startPending.current = false;
        setBusy(false);
      }
    }
  }
  async function end() {
    const id = sidRef.current;
    if (!id) return;
    trace('session_end');
    setBusy(true);
    setFinalization({session_id:id,state:'requested',stage:'stopping',snapshot_saved:false,
      summary_available:false,online});
    void window.desktop?.setFinalizing(true);
    startRequest.current++;
    startPending.current = false;
    epoch.current.next();
    stopDevices();
    stopAudio(false);
    const endingTeaching = teaching.current;
    let lastPlayback: { playback_id: string; heard: string } | null = null;
    try { lastPlayback = endingTeaching?.finalizePlayback() ?? null; } catch (e) { report(e); }
    const finishPayload = {captured,submitted_to_runtime:online ? captured : 0,last_playback:lastPlayback};
    finalizationRequest.current = finishPayload;
    teaching.current = null;
    setOnline(false);
    setReconnecting(false);
    sidRef.current = '';
    setSid('');
    setLastFrame('');
    setFocusRect(null);
    setFocusPicking(false);
    setHeard('');
    try {
      try { await window.desktop?.closeBoard(); } catch (e) { report(e); }
      try { await window.desktop?.focus(false); } catch (e) { report(e); }
      const result = await request<Finalization>(`/sessions/${id}/finalize`,finishPayload);
      setFinalization(result);
    } catch (e) {
      report(e);
      setFinalization((current) => current?.session_id === id
        ? {...current,state:'failed',stage:'failed',error:'尚未確認本次紀錄已保存，請重試。'} : current);
    } finally {
      await endingTeaching?.close().catch(report);
      setBusy(false);
    }
  }
  async function retryFinalization() {
    if (!finalization) return;
    const id = finalization.session_id;
    setFinalization({...finalization,state:'requested',stage:'stopping'});
    try {
      const status = await request<Finalization>(`/sessions/${id}/finalization`).catch(() => null);
      const result = status
        ? await request<Finalization>(`/sessions/${id}/finalization/retry`,{})
        : await request<Finalization>(`/sessions/${id}/finalize`,finalizationRequest.current ?? {});
      setFinalization(result);
    } catch (e) {
      report(e);
      setFinalization((current) => current?.session_id === id
        ? {...current,state:'failed',stage:'failed',error:'重新整理失敗，請再試一次。'} : current);
    }
  }
  async function dismissFinalization() {
    if (!finalization) return;
    try {
      await request(`/sessions/${finalization.session_id}/finalization/dismiss`,{});
      const {items} = await request<{items: Finalization[]}>('/finalizations/pending');
      if (items.length) setFinalization(items[0]);
      else {
        await window.desktop?.setFinalizing(false);
        setFinalization(null);
      }
    } catch (e) { report(e); }
  }
  async function recordNote() {
    if (recording) {
      recorder.current?.stop();
      return;
    }
    if (notePending.current) return;
    stopMicCheck();
    notePending.current = true;
    const requestId = ++noteRequest.current;
    let started = false;
    setError('');
    setBusy(true);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          ...(micDeviceId ? { deviceId: { exact: micDeviceId } } : {}),
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      if (requestId !== noteRequest.current) {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }
      const rec = new MediaRecorder(stream, { mimeType: 'audio/webm' });
      recorder.current = rec;
      const chunks: BlobPart[] = [];
      rec.ondataavailable = (e) => chunks.push(e.data);
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        setRecording(false);
        if (recorder.current === rec) recorder.current = null;
        if (requestId !== noteRequest.current) return;
        const data = new FormData();
        data.append('file', new Blob(chunks, { type: 'audio/webm' }), 'speech.webm');
        setBusy(true);
        try {
          const r = await request('/transcribe', data);
          if (requestId === noteRequest.current && r.text && r.text.trim())
            setNotes((n) => n + (n ? '\n' : '') + r.text.trim());
        } catch (e) {
          if (requestId === noteRequest.current) report(e);
        } finally {
          if (requestId === noteRequest.current) {
            notePending.current = false;
            setBusy(false);
          }
        }
      };
      rec.start();
      started = true;
      setRecording(true);
      setTimeout(() => {
        if (rec.state === 'recording') rec.stop();
      }, 30000);
    } catch (e) {
      if (requestId === noteRequest.current) report(e);
    } finally {
      if (requestId === noteRequest.current) {
        if (!started) notePending.current = false;
        if (!recording) setBusy(false);
      }
    }
  }
  async function testVoice() {
    stopAudio(false);
    setVoiceTesting(false);
    setVoiceStatus('正在準備試聽…');
    setError('');
    const ticket = epoch.current.current(),
      voiceTicket = voiceGeneration.current;
    try {
      const text = '你好，我們一起把今天的作業，慢慢學會。';
      const result = await request('/diagnostics/tts', { text, voice: edgeVoice });
      if (!epoch.current.accepts(ticket) || voiceTicket !== voiceGeneration.current) return;
      const bytes = Uint8Array.from(atob(result.audio), (c) => c.charCodeAt(0));
      audioURL.current = URL.createObjectURL(new Blob([bytes], { type: 'audio/mpeg' }));
      const player = new Audio(audioURL.current);
      audio.current = player;
      speakingData.current = { text, boundaries: result.boundaries };
      micState.current.playbackGate = true;
      mic.current?.getTracks().forEach((t) => (t.enabled = false));
      setSpeaking(true);
      setVoiceTesting(true);
      setVoiceStatus('正在播放老師聲音');
      player.ontimeupdate = () =>
        setHeard(playbackReceipt(text, result.boundaries, player.currentTime).fully_heard);
      player.onended = () => {
        setHeard(text);
        stopAudio(false);
        setVoiceTesting(false);
        setVoiceStatus('試聽完成');
        mic.current?.getTracks().forEach((t) => (t.enabled = micState.current.userEnabled));
      };
      await player.play();
    } catch (e) {
      if (!epoch.current.accepts(ticket) || voiceTicket !== voiceGeneration.current) return;
      stopAudio(false);
      setVoiceTesting(false);
      setVoiceStatus('試聽失敗');
      report(e);
    }
  }
  function createTeaching(id: string, teachingMode: 'basic' | 'gpt' | 'gemini' = 'basic') {
    const photoFormat = teachingMode === 'gpt' && gptConfig.vision.provider === 'openai' ? 'png' : 'jpeg';
    photoFormatRef.current = photoFormat;
    return new BasicConnection(
      id,
      {
        event: (event) => {
          if (event.type === 'photo_upload') trace('photo_upload',
            `${event.stage} chars=${event.image_chars} ms=${event.elapsed_ms ?? 0}`);
          if (event.type === 'transport_backpressure') {
            trace('transport_backpressure', `${event.kind} bytes=${event.buffered_bytes}`);
            micState.current.userEnabled = false;
            setMicOn(false);
            setAudioStage('off');
            teaching.current?.setMic(false);
          }
          if (event.type === 'thinking') setThinking(true);
          if (event.type === 'audio_stage') setAudioStage(event.stage);
          if (['speech_started', 'error', 'idle', 'media_stopped', 'interrupted'].includes(event.type))
            setThinking(false);
          if (event.type === 'transcript') {
            setHeard((event.role === 'user' ? '你說：' : activeTeacherName + '：') + event.text);
            setMessages((items) => appendTranscript(items, event, activeTeacherName));
          }
          if (event.type === 'lesson') setLesson(event);
          if (event.type === 'speech_started') {
            speechMessage.current = event.playback_id;
            setMessages((items) =>
              [
                ...items,
                { id: event.playback_id, role: 'assistant', text: '', status: '播放中' },
              ].slice(-500),
            );
          }
          if (event.type === 'speech_finished')
            setMessages((items) =>
              items.map((item) => (item.id === event.playback_id ? { ...item, status: '' } : item)),
            );
          if (event.type === 'speech_stopped')
            setMessages((items) =>
              items.map((item) =>
                item.id === event.playback_id
                  ? { ...item, text: event.text, status: '已中斷，僅顯示已播放內容' }
                  : item,
              ),
            );
          if (event.type === 'error') setError(event.message);
          if (event.type === 'transport_lost') {
            trace('socket_lost', event.code);
            recoverDevices.current = { camera: !!cam.current, mic: micState.current.userEnabled };
            stopDevices();
            setOnline(false);
            setReconnecting(true);
            setThinking(false);
            setError(`教學連線暫時中斷（代碼 ${event.code}），正在重新連線…`);
          }
          if (event.type === 'transport_restored') {
            trace('socket_restored');
            if (sidRef.current !== id) return;
            setOnline(true);
            setReconnecting(false);
            setError('');
            const devices = recoverDevices.current;
            recoverDevices.current = { camera: false, mic: false };
            void (async () => {
              if (devices.camera) {
                await cameraToggle();
                const restoredVideo = video.current;
                if (restoredVideo && cam.current && restoredVideo.readyState < 2) {
                  await new Promise<void>((resolve) => {
                    const done = () => {
                      clearTimeout(timer);
                      restoredVideo.removeEventListener('loadeddata', done);
                      resolve();
                    };
                    const timer = setTimeout(done, 3000);
                    restoredVideo.addEventListener('loadeddata', done, { once: true });
                  });
                }
                if (
                  sidRef.current === id &&
                  restoredVideo?.readyState &&
                  restoredVideo.readyState >= 2
                ) {
                  try {
                    void takeNativePhoto(activeSettingsRef.current, photoFormat).then(async (frame) => {
                      if (!frame || sidRef.current !== id) return;
                      setLastFrame(frame.image);
                      setCaptured((count) => count + 1);
                      await teaching.current?.frame(frame.image, frame.meta);
                    }).catch(report);
                  } catch (e) {
                    report(e);
                  }
                }
              }
              if (devices.mic && sidRef.current === id) await enableMic();
            })();
          }
          if (event.type === 'transport_retry') trace('socket_retry', event.attempt);
          if (event.type === 'media_stopped') {
            stopDevices();
            setOnline(false);
            setReconnecting(false);
          }
        },
        error: (message) => {
          if (message.includes('無法恢復')) trace('socket_recovery_failed');
          setError(message);
          setBusy(false);
        },
        capture: () => takeNativePhoto(activeSettingsRef.current, photoFormat),
        board: async (html, close) => {
          if (close) {
            await window.desktop?.closeBoard();
            return;
          }
          const box = boardRef.current?.getBoundingClientRect();
          if (box)
            await window.desktop?.board(html, {
              x: box.x + 1,
              y: box.y + 1,
              width: box.width - 2,
              height: box.height - 2,
            });
        },
        playbackGate: (value) => {
          micState.current.playbackGate = value;
          setSpeaking(value);
          mic.current
            ?.getTracks()
            .forEach((t) => (t.enabled = micState.current.userEnabled && !value));
          if (value) setBusy(false);
        },
        caption: (text) => {
          setHeard(text);
          setMessages((items) =>
            items.map((item) => (item.id === speechMessage.current ? { ...item, text } : item)),
          );
        },
      },
      teachingMode,
    );
  }
  async function testPipeline() {
    setBusy(true);
    setError('');
    setHeard('正在準備合成語音測試…');
    const ticket = epoch.current.current();
    let client: BasicConnection | null = null;
    let context: AudioContext | null = null;
    const checkCurrent = () => {
      if (!epoch.current.accepts(ticket)) throw Error('診斷已取消');
    };
    try {
      await teaching.current?.close();
      teaching.current = null;
      const fixture = await request('/diagnostics/tts', {
        text: '七加五要怎麼算？請給我一點提示。',
      });
      if (!epoch.current.accepts(ticket)) return;
      const config = await window.desktop!.connection();
      checkCurrent();
      client = createTeaching('diagnostic');
      teaching.current = client;
      await client.connect(
        config.base,
        config.token,
        `/diagnostics/basic/stream?backend=${backend}`,
      );
      checkCurrent();
      client.setCamera(false);
      context = new AudioContext();
      syntheticAudio.current = context;
      const bytes = Uint8Array.from(atob(fixture.audio), (c) => c.charCodeAt(0));
      const decoded = await context.decodeAudioData(bytes.buffer);
      checkCurrent();
      const source = context.createBufferSource();
      source.buffer = decoded;
      const destination = context.createMediaStreamDestination();
      source.connect(destination);
      await client.attachMicrophone(destination.stream);
      checkCurrent();
      client.setMic(true);
      await context.resume();
      source.start();
      setHeard(`正在轉錄合成語音，接著由 ${backend === 'agy' ? 'agy' : 'Codex'} 提供提示…`);
    } catch (e) {
      if (epoch.current.accepts(ticket)) {
        report(e);
        setBusy(false);
      }
      await client?.close();
      if (teaching.current === client) teaching.current = null;
      if (context && context.state !== 'closed') await context.close();
      if (syntheticAudio.current === context) syntheticAudio.current = null;
    }
  }
  function openParent(nextTab?: string) {
    epoch.current.next();
    stopMicCheck();
    stopAudio(false);
    setVoiceTesting(false);
    setVoiceStatus('');
    const diagnostic = teaching.current;
    teaching.current = null;
    void diagnostic?.close().catch(report);
    if (syntheticAudio.current && syntheticAudio.current.state !== 'closed')
      void syntheticAudio.current.close().catch(report);
    syntheticAudio.current = null;
    setBusy(false);
    if (nextTab) setTab(nextTab);
    setParent(true);
    setSaved(false);
  }
  async function closeParent() {
    if (profileSaving) return;
    if (tab === 'profile' && profileDirty && !confirm('個人檔案有尚未儲存的修改。要放棄修改並關閉嗎？')) return;
    epoch.current.next();
    setParent(false);
    setProfileDraft(null);
    requestAnimationFrame(() => parentTrigger.current?.focus());
    stopAudio(false);
    setVoiceTesting(false);
    setBusy(false);
    await teaching.current?.close();
    teaching.current = null;
    if (syntheticAudio.current && syntheticAudio.current.state !== 'closed')
      await syntheticAudio.current.close();
    syntheticAudio.current = null;
  }
  function switchParentStudent(next: Student) {
    if (profileSaving) return;
    if (next.id === selected) return;
    if (tab === 'profile' && profileDirty && !confirm('個人檔案有尚未儲存的修改。要放棄修改並切換學生嗎？')) return;
    setSelected(next.id);
    setProfileDraft({ ...next });
    setHistory(null);
    setSaved(false);
  }
  function switchParentTab(next: string) {
    if (profileSaving) return;
    if (next === tab) return;
    if (tab === 'profile' && profileDirty && !confirm('個人檔案有尚未儲存的修改。要放棄修改並切換頁面嗎？')) return;
    setProfileDraft({ ...student });
    setTab(next);
    setHistory(null);
    setSaved(false);
  }
  async function addStudent() {
    if (!newStudentName.trim() || profileSaving) return;
    setProfileSaving(true);
    try {
      const created = await request<Student>('/students', { name: newStudentName.trim(), grade: newStudentGrade });
      await refresh();
      setSelected(created.id);
      setNewStudentName('');
      setShowStudentForm(false);
    } catch (e) { report(e); }
    finally { setProfileSaving(false); }
  }
  async function loadDemo() {
    setProfileSaving(true);
    try { await request('/demo', {}); await refresh(); }
    catch (e) { report(e); }
    finally { setProfileSaving(false); }
  }
  async function saveProfile() {
    if (!draft || profileSaving) return;
    setProfileSaving(true);
    setSaved(false);
    try {
      await request(`/students/${selected}`, {
        name: draft.name,
        preferences: draft.preferences,
        grade: draft.grade,
        school_name: draft.school_name ?? '',
      }, 'PUT');
      const updated = await refresh();
      const savedProfile = updated.students.find((item) => item.id === selected);
      if (savedProfile) setProfileDraft({ ...savedProfile });
      setSaved(true);
    } catch (e) {
      report(e);
    } finally {
      setProfileSaving(false);
    }
  }
  function beginCalibration() {
    if (!video.current || !cameraOn) {
      setError('請先開啟相機。');
      return;
    }
    setDraftCorners(cameraSettings.corners.map((p) => [...p] as Point));
    setCameraEditing(true);
  }
  function applyCalibration() {
    try {
      homography(draftCorners);
      updateCameraSettings({ corners: draftCorners.map((p) => [...p] as Point) });
      setCameraEditing(false);
    } catch (e) {
      report(e);
    }
  }
  function moveCorner(index: number, x: number, y: number) {
    setDraftCorners((old) =>
      old.map((p, i) =>
        i === index ? ([Math.max(0, Math.min(1, x)), Math.max(0, Math.min(1, y))] as Point) : p,
      ),
    );
  }
  function previewPoint(canvas: HTMLCanvasElement, clientX: number, clientY: number): Point {
    const box=canvas.getBoundingClientRect();
    const scale=Math.min(box.width/Math.max(1,canvas.width),box.height/Math.max(1,canvas.height));
    const width=canvas.width*scale,height=canvas.height*scale;
    const x=(clientX-box.left-(box.width-width)/2)/Math.max(1,width);
    const y=(clientY-box.top-(box.height-height)/2)/Math.max(1,height);
    return [Math.max(0,Math.min(1,x)),Math.max(0,Math.min(1,y))];
  }
  async function demoBoard() {
    const box = boardRef.current?.getBoundingClientRect();
    if (!box || !window.desktop) return;
    await window.desktop.board(
      '<h1>把一個大問題，分成小步驟。</h1><p>這是白板的 JavaScript 互動測試。</p><button id="step">我想一想</button><p id="hint"></p><script>document.getElementById("step").onclick=()=>document.getElementById("hint").textContent="先圈出題目已經告訴你的數字。";</script>',
      { x: box.x + 1, y: box.y + 1, width: box.width - 2, height: box.height - 2 },
    );
  }
  const events = (school?.events ?? [])
    .filter((e) => e.date >= new Date().toLocaleDateString('en-CA'))
    .slice(0, 3);
  let calibrationError = '';
  if (cameraEditing) {
    try {
      homography(draftCorners);
    } catch (e) {
      calibrationError = e instanceof Error ? e.message : String(e);
    }
  }
  const interruptAction = speaking || (mode === 'basic' && micOn &&
    (thinking || ['sending', 'received', 'transcribing', 'accepted'].includes(audioStage)));
  const voiceReady = online && micOn && !reconnecting && !interruptAction &&
    ['ready','ignored','rejected'].includes(audioStage);
  const listenText = reconnecting ? '正在重新連線，現在不能說話'
    : !online ? '教學服務尚未連線'
    : !micOn ? '麥克風已關閉'
    : speaking ? '老師說話中；按「打斷老師並說話」即可插話'
    : audioStage === 'rejected' ? '剛才那句沒有送出，請再說一次'
    : audioStage === 'ignored' ? '沒有聽清楚，請再說一次'
    : audioStage === 'capturing' ? '正在收你的話；說完請稍停一下'
    : audioStage === 'sending' || audioStage === 'received' || audioStage === 'transcribing'
      ? '已收到聲音，正在辨識；可按「我想補充」重說'
    : thinking || audioStage === 'accepted' ? '老師正在思考；可按「我想補充」改說'
    : voiceReady ? '現在可以說話' : '麥克風啟動中，請稍候';
  return (
    <div className="app">
      <video ref={video} autoPlay playsInline muted className="camera-source" aria-hidden="true" />
      <header className="topbar">
        <div className="brand">
          <span className="brand-icon">
            <Leaf size={23} />
          </span>
          伴讀<span className="brand-en">a little, every day</span>
        </div>
        <div className="top-actions">
          {finalization ? (
            <span className="pill"><span className="dot" />課後整理中</span>
          ) : sid ? (
            <>
              <span className="pill">
                <span className="dot" />
                {online ? 'AI 陪讀已連線' : '專注工作區 · AI 尚未連線'}
              </span>
              <span className="clock">
                {Math.floor(elapsed / 60)
                  .toString()
                  .padStart(2, '0')}
                :{(elapsed % 60).toString().padStart(2, '0')}
              </span>
              {!full && (
                <button
                  className="icon-button"
                  aria-label="返回全螢幕"
                  onClick={() => window.desktop?.focus(true)}
                >
                  <Maximize size={18} />
                </button>
              )}
            </>
          ) : (
            <>
              <span className="desktop-label">家庭陪讀</span>
              <button
                ref={parentTrigger}
                className="text-button"
                disabled={busy || !student.id}
                onClick={() => openParent()}
              >
                <Settings size={16} />
                家長設定
              </button>
            </>
          )}
        </div>
      </header>
      {error && (
        <div className="alert" role="alert">
          {error}
          <button aria-label="關閉訊息" onClick={() => setError('')}>
            <X size={16} />
          </button>
        </div>
      )}
      {!startupChecked ? (
        <main className="finalization-page" role="status">正在檢查上次課程狀態…</main>
      ) : startupError ? (
        <main className="finalization-page">
          <div className="finalization-card" role="alert">
            <h1>無法確認課程狀態</h1>
            <p>請重新連接本機服務，確認上次課後整理是否完成。</p>
            <button onClick={() => window.location.reload()}>重新檢查</button>
          </div>
        </main>
      ) : finalization ? (
        <main className="finalization-page" aria-live="polite">
          <div className="finalization-card" role="status">
            <div className="eyebrow">課後整理</div>
            <h1>{finalization.state === 'completed' ? '本次紀錄已整理完成'
              : finalization.state === 'failed' ? '課後整理尚未完成'
              : finalization.stage === 'stopping' ? '正在保存本次陪讀紀錄'
              : finalization.stage === 'saving' ? '正在核對並保存學習摘要'
              : finalization.online ? '老師正在課後整理學習資料'
              : '正在保存本次紀錄'}</h1>
            <p>{finalization.state === 'completed' ? '即將返回開始畫面。'
              : finalization.state === 'failed'
              ? finalization.snapshot_saved
                ? '本次紀錄已保存，AI 課後整理未完成。'
                : '尚未確認本次紀錄已保存，請重試保存。'
              : finalizationWaitSeconds >= 30
                ? finalization.snapshot_saved
                  ? '仍在整理。本次紀錄已保存，請保持程式開啟；完成後會自動返回開始畫面。'
                  : '仍在整理。收音、拍照與播音已停止；正在確認本次紀錄的保存狀態。'
              : finalization.snapshot_saved
                ? '紀錄已安全保存。請保持程式開啟，完成後會自動返回開始畫面。'
                : '收音、拍照與播音已停止；正在封存本次資料。'}</p>
            <ol className="finalization-steps">
              {['停止採集與封存','整理學習資料','核對並保存'].map((label,index) => {
                const current = finalization.state === 'failed' ? (finalization.snapshot_saved ? 1 : 0)
                  : finalization.stage === 'stopping' ? 0
                  : finalization.stage === 'summarizing' ? 1
                  : finalization.stage === 'saving' ? 2 : 3;
                return <li key={label} className={index < current ? 'done' : index === current ? 'current' : ''}>{label}</li>;
              })}
            </ol>
            {finalization.state === 'failed' ? (
              <div className="finalization-actions">
                <button onClick={() => void retryFinalization()}>重試整理</button>
                {finalization.snapshot_saved &&
                  <button className="text-button" onClick={() => void dismissFinalization()}>先返回開始畫面</button>}
              </div>
            ) : finalization.state !== 'completed' && <div className="finalization-spinner" aria-hidden="true" />}
          </div>
        </main>
      ) : !sid ? (
        <main className="home">
          <section className="preflight-camera" aria-label="課前鏡頭設定">
            <div className="preflight-heading">
              <div>
                <div className="eyebrow">課前檢查</div>
                <h1>鏡頭與拍攝範圍</h1>
              </div>
              <span className="preflight-local">
                <ShieldCheck size={15} /> 開始前的畫面只在本機預覽
              </span>
            </div>
            <div className="preflight-display">
              <div
                className="preflight-frame"
                style={{
                  aspectRatio: previewRatio,
                  maxWidth: `${Math.round(60 * previewRatio)}vh`,
                }}
              >
                <canvas
                  ref={homeCanvas}
                  className={cameraOn && cameraReady ? '' : 'hidden'}
                  aria-label={cameraEditing ? '旋轉後的原始鏡頭' : '老師看到的畫面'}
                />
                {(!cameraOn || !cameraReady) && (
                  <div className="camera-empty">
                    <CameraOff size={32} />
                    <span>{cameraOn ? '正在取得鏡頭畫面…' : cameraMessage}</span>
                  </div>
                )}
                {cameraOn && cameraEditing && (
                  <div className="corner-layer">
                    <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
                      <polygon
                        points={draftCorners.map(([x, y]) => `${x * 100},${y * 100}`).join(' ')}
                      />
                    </svg>
                    {draftCorners.map(([x, y], index) => (
                      <button
                        key={index}
                        className="corner-handle"
                        style={{
                          left: `clamp(17px, ${x * 100}%, calc(100% - 17px))`,
                          top: `clamp(17px, ${y * 100}%, calc(100% - 17px))`,
                        }}
                        aria-label={`拍攝範圍第 ${index + 1} 角`}
                        onPointerDown={(e) => {
                          dragCorner.current = index;
                          e.currentTarget.setPointerCapture(e.pointerId);
                        }}
                        onPointerMove={(e) => {
                          if (dragCorner.current !== index) return;
                          const box = e.currentTarget.parentElement!.getBoundingClientRect();
                          moveCorner(
                            index,
                            (e.clientX - box.left) / box.width,
                            (e.clientY - box.top) / box.height,
                          );
                        }}
                        onPointerUp={() => {
                          dragCorner.current = null;
                        }}
                        onPointerCancel={() => {
                          dragCorner.current = null;
                        }}
                        onKeyDown={(e) => {
                          const delta = e.shiftKey ? 0.02 : 0.005;
                          const offset: Record<string, Point> = {
                            ArrowLeft: [-delta, 0],
                            ArrowRight: [delta, 0],
                            ArrowUp: [0, -delta],
                            ArrowDown: [0, delta],
                          };
                          if (offset[e.key]) {
                            e.preventDefault();
                            moveCorner(index, x + offset[e.key][0], y + offset[e.key][1]);
                          }
                        }}
                      >
                        {index + 1}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <span className="camera-badge">
                {cameraEditing ? '原始鏡頭 · 拖曳四角' : '老師看到的畫面'}
              </span>
            </div>
            {cameraEditing && (
              <div className="calibration-tools">
                <div className="calibration-result">
                  <canvas ref={correctedCanvas} aria-label="校正結果預覽" />
                  <span>校正結果預覽</span>
                </div>
                <div className="calibration-actions">
                  <p>拖曳四角圈住作業書頁，讓要讀的字佔據畫面；也可用方向鍵微調，Shift 加快。</p>
                  {calibrationError && (
                    <p className="field-error" role="status">
                      {calibrationError}
                    </p>
                  )}
                  <div className="button-row">
                    <button
                      onClick={() => setDraftCorners(defaultCorners.map((p) => [...p] as Point))}
                    >
                      重選四角
                    </button>
                    <button onClick={() => setCameraEditing(false)}>取消</button>
                    <button
                      className="primary"
                      onClick={applyCalibration}
                      disabled={!!calibrationError}
                    >
                      套用範圍
                    </button>
                  </div>
                </div>
              </div>
            )}
            <div className="camera-setup">
              <label>
                相機
                <select
                  value={cameraDeviceId}
                  onChange={(e) => void switchCamera(e.target.value)}
                  disabled={!cameraDevices.length}
                >
                  {!cameraDevices.length && <option value="">預設相機</option>}
                  {cameraDeviceId && !cameraDevices.some((d) => d.deviceId === cameraDeviceId) && (
                    <option value={cameraDeviceId}>原相機未連接</option>
                  )}
                  {cameraDevices.map((device, index) => (
                    <option key={device.deviceId || index} value={device.deviceId}>
                      {device.label || `相機 ${index + 1}`}
                    </option>
                  ))}
                </select>
              </label>
              <button onClick={cameraToggle}>
                {cameraLoading ? '取消開啟' : cameraWanted && cameraOn ? '關閉鏡頭' : '開啟鏡頭'}
              </button>
              <label>
                旋轉角度
                <select
                  value={cameraSettings.rotation}
                  onChange={(e) => {
                    updateCameraSettings({
                      rotation: Number(e.target.value) as Rotation,
                      corners: defaultCorners.map((p) => [...p] as Point),
                    });
                    setDraftCorners(defaultCorners.map((p) => [...p] as Point));
                    setCameraMessage('方向已變更，請重新檢查拍攝範圍');
                  }}
                  disabled={!cameraOn}
                >
                  {[0, 90, 180, 270].map((n) => (
                    <option key={n} value={n}>
                      {n}°
                    </option>
                  ))}
                </select>
              </label>
              <label>
                微調 {cameraSettings.fineRotation}°
                <input
                  type="range"
                  min={-15}
                  max={15}
                  step={1}
                  value={cameraSettings.fineRotation}
                  onChange={(e) => updateCameraSettings({ fineRotation: Number(e.target.value) })}
                  disabled={!cameraOn}
                />
              </label>
              <label>
                輸出比例
                <select
                  value={cameraSettings.outputAspect}
                  onChange={(e) =>
                    updateCameraSettings({
                      outputAspect: e.target.value as CameraSettings['outputAspect'],
                    })
                  }
                  disabled={!cameraOn}
                >
                  <option value="auto">依拍攝範圍</option>
                  <option value="a4-portrait">A4 直式</option>
                  <option value="a4-landscape">A4 橫式</option>
                </select>
              </label>
              <label className="camera-check">
                <input
                  type="checkbox"
                  checked={cameraSettings.perspectiveEnabled}
                  onChange={(e) => updateCameraSettings({ perspectiveEnabled: e.target.checked })}
                  disabled={!cameraOn}
                />
                梯形修正
              </label>
            </div>
            <div className="camera-setup-footer">
              <button onClick={beginCalibration} disabled={!cameraOn || cameraEditing}>
                調整拍攝範圍
              </button>
              <button
                onClick={() =>
                  updateCameraSettings({ corners: defaultCorners.map((p) => [...p] as Point) })
                }
                disabled={!cameraOn}
              >
                還原完整畫面
              </button>
              <span role="status">
                {cameraOn ? cameraMessage : '鏡頭未開啟，不會拍攝或傳送影像'}
              </span>
            </div>
            {cameraOn && isFullFrameCorners(cameraSettings.corners) && (
              <p className="camera-quality-warning" role="status">
                目前仍是完整鏡頭畫面。若要讀題目小字，請用「調整拍攝範圍」框住書頁；老師收到的也是這張畫面。
              </p>
            )}
          </section>
          <section className="start-card">
            <div className="section-caption">
              <span>01 / 準備開始</span>
              <span>自己的步調，也很好。</span>
            </div>
            <h2 className="student-heading">今天陪誰一起學？
              {!!boot.students.length && <button className="student-add-toggle" aria-label="新增學生檔案"
                onClick={() => setShowStudentForm(value => !value)}>新增</button>}
            </h2>
            {!boot.students.length && <p role="status">{!startupChecked ? '正在載入學生資料…' : '歡迎使用伴讀，請先新增學生。'}</p>}
            {(!boot.students.length || showStudentForm) && <form className="student-create" onSubmit={event => { event.preventDefault(); void addStudent(); }}>
              <label>學生稱呼<input aria-label="新學生稱呼" maxLength={40} value={newStudentName}
                onChange={event => setNewStudentName(event.target.value)} placeholder="可使用暱稱" /></label>
              <label>年級<select aria-label="新學生年級" value={newStudentGrade}
                onChange={event => setNewStudentGrade(Number(event.target.value))}>
                {Array.from({length: 12}, (_, index) => <option key={index + 1} value={index + 1}>{index + 1} 年級</option>)}
              </select></label>
              <button type="submit" disabled={!window.desktop || !startupChecked || startupError || profileSaving || !newStudentName.trim()}>新增學生</button>
              {!boot.students.length && <button type="button" disabled={!window.desktop || !startupChecked || startupError || profileSaving}
                onClick={() => void loadDemo()}>載入合成展示資料</button>}
            </form>}
            <div className="students">
              {boot.students.map((s) => (
                <button
                  key={s.id}
                  className={'student ' + (s.id === selected ? 'selected' : '')}
                  aria-pressed={s.id === selected}
                  onClick={() => setSelected(s.id)}
                >
                  <span className="avatar">
                    {s.id === 'student-test' ? '測' : s.name.slice(-1)}
                  </span>
                  <span>
                    <b>{s.name}</b>
                    <small>
                      {s.id === 'student-test'
                        ? `國小 ${s.grade} 年級 · 測試用`
                        : `國小 ${s.grade} 年級`}
                    </small>
                    <small>{s.school_name ?? '尚未設定學校'}</small>
                  </span>
                  {s.id === selected && <Check size={17} />}
                </button>
              ))}
            </div>
            <label className="field-label" htmlFor="notes">
              今天，有什麼需要留意的嗎？<small>選填</small>
            </label>
            <textarea
              id="notes"
              value={notes}
              maxLength={4000}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="例如：先寫數學習作第 12 頁，今天有點累，請多給他一點時間。"
            />
            <div className="note-footer">
              <span>只用於這次陪讀的安排</span>
              <button
                className={'text-button ' + (recording ? 'recording' : '')}
                onClick={recordNote}
                disabled={busy}
              >
                {recording ? <Square size={14} /> : <Mic size={14} />}{' '}
                {recording ? '完成錄音' : '用說的'}
              </button>
            </div>
            <details className="preflight-optional">
              <summary>老師聲音與裝置檢查 <ChevronRight size={15} /></summary>
            <div className="teacher-config">
              <label className="field-label">
                老師名字
                <input
                  type="text"
                  value={teacherName}
                  maxLength={40}
                  onChange={(e) => setTeacherName(e.target.value)}
                  placeholder={boot.teacher_defaults?.[selected]?.teacher_name || '老師'}
                />
              </label>
              <div className="voice-choice">
                {mode === 'basic' ? (
                  <>
                    <label className="field-label">
                      說話聲音
                      <select
                        value={edgeVoice}
                        onChange={(e) => {
                          stopAudio(false);
                          setVoiceTesting(false);
                          setVoiceStatus('');
                          setEdgeVoice(e.target.value);
                        }}
                      >
                        {(boot.voice_options || []).map((v) => (
                          <option key={v.id} value={v.id}>
                            {v.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <button
                      onClick={() =>
                        voiceTesting || voiceStatus === '正在準備試聽…'
                          ? (stopAudio(false), setVoiceTesting(false), setVoiceStatus('已停止試聽'))
                          : void testVoice()
                      }
                      disabled={busy || recording}
                    >
                      <Volume2 size={16} />
                      {voiceTesting || voiceStatus === '正在準備試聽…'
                        ? '停止試聽'
                        : '試聽老師聲音'}
                    </button>
                  </>
                ) : (
                  <p className="live-voice-note">
                    此模式使用服務提供的即時語音；目前無課前音色選擇與試聽。
                  </p>
                )}
                {voiceStatus && mode === 'basic' && <span role="status">{voiceStatus}</span>}
              </div>
            </div>
            <div className="mic-check">
              <div>
                <b>麥克風檢查</b>
                <small>只顯示本機音量，不錄音或上傳</small>
              </div>
              <select
                aria-label="麥克風來源"
                value={micDeviceId}
                onChange={(e) => {
                  stopMicCheck();
                  setMicDeviceId(e.target.value);
                }}
              >
                <option value="">系統預設麥克風</option>
                {micDevices
                  .filter((d) => d.deviceId)
                  .map((device, index) => (
                    <option key={device.deviceId} value={device.deviceId}>
                      {device.label || `麥克風 ${index + 1}`}
                    </option>
                  ))}
              </select>
              <button onClick={toggleMicCheck} disabled={busy || recording}>
                {micChecking ? '停止檢查' : '測試麥克風'}
              </button>
              <meter min="0" max="100" value={micLevel} aria-label="麥克風音量" />
              <span>喇叭使用 Windows 預設輸出；老師試聽可確認是否聽得到。</span>
            </div>
            </details>
            <details className="advanced">
              <summary>
                對話方式：{modes.find(([id]) => id === mode)?.[1] ?? '基本陪讀'}
                {mode === 'basic' ? ` · ${backend === 'codex' ? 'Codex' : 'agy'}` : ''}
                <ChevronRight size={15} />
              </summary>
              <div className="modes">
                {modes.map(([id, name, sub]) => (
                  <button
                    key={id}
                    className={mode === id ? 'active' : ''}
                    onClick={() => {
                      stopAudio(false);
                      setVoiceTesting(false);
                      setVoiceStatus('');
                      setMode(id);
                    }}
                    aria-pressed={mode === id}
                  >
                    <b>{name}</b>
                    <small>{sub}</small>
                  </button>
                ))}
              </div>
              {mode === 'basic' && (
                <label className="backend-row">
                  教學與作業看圖
                  <select
                    value={backend}
                    onChange={(e) => setBackend(e.target.value as 'codex' | 'agy')}
                  >
                    <option value="codex">Codex 訂閱</option>
                    <option value="agy">agy 訂閱</option>
                  </select>
                </label>
              )}
              {mode === 'gpt' && (
                <div className="gpt-route-options">
                  <p>語音：GPT-LIVE-1</p>
                  <label className="backend-row">教學委派方式
                    <select value={gptConfig.delegation.type} onChange={(e) => {
                      const type = e.target.value as 'responses' | 'client';
                      setGptConfig((value) => ({ ...value, delegation: {
                        ...firstRoute(type === 'responses' ? 'openai' : 'codex', false), type,
                      } }));
                    }}>
                      <option value="responses">Responses API</option>
                      <option value="client">Client（Codex／agy）</option>
                    </select>
                  </label>
                  {gptConfig.delegation.type === 'client' && (
                    <label className="backend-row">教學服務
                      <select value={gptConfig.delegation.provider} onChange={(e) => {
                        const provider = e.target.value as GptProvider;
                        setGptConfig((value) => ({ ...value, delegation: {
                          ...firstRoute(provider, false), type: 'client',
                        } }));
                      }}>
                        <option value="codex">Codex</option>
                        <option value="agy">agy</option>
                      </select>
                    </label>
                  )}
                  <label className="backend-row">教學模型
                    <select value={gptConfig.delegation.model} onChange={(e) => {
                      const model = modelChoices(gptConfig.delegation.provider, false)
                        .find((item) => item.id === e.target.value);
                      setGptConfig((value) => ({ ...value, delegation: {
                        ...value.delegation, model: e.target.value,
                        effort: model?.efforts.includes(value.delegation.effort) ? value.delegation.effort :
                          model?.efforts.includes('medium') ? 'medium' : model?.efforts[0] ?? '',
                      } }));
                    }}>
                      {!selectedTeaching && <option value={gptConfig.delegation.model}>請選擇可用模型</option>}
                      {modelChoices(gptConfig.delegation.provider, false).map((item) =>
                        <option key={item.id} value={item.id}>{item.label}</option>)}
                    </select>
                  </label>
                  <label className="backend-row">教學 effort
                    <select value={gptConfig.delegation.effort} onChange={(e) =>
                      setGptConfig((value) => ({ ...value, delegation: { ...value.delegation, effort: e.target.value } }))}>
                      {(selectedTeaching?.efforts ?? []).map((effort) =>
                        <option key={effort} value={effort}>{effort}</option>)}
                    </select>
                  </label>
                  <label className="backend-row">圖片辨識服務
                    <select value={gptConfig.vision.provider} onChange={(e) => {
                      const provider = e.target.value as GptProvider;
                      setGptConfig((value) => ({ ...value, vision: firstRoute(provider, true) }));
                    }}>
                      <option value="openai">OpenAI Responses API</option>
                      <option value="codex">Codex</option>
                      <option value="agy">agy</option>
                    </select>
                  </label>
                  <label className="backend-row">圖片模型
                    <select value={gptConfig.vision.model} onChange={(e) => {
                      const model = modelChoices(gptConfig.vision.provider, true)
                        .find((item) => item.id === e.target.value);
                      setGptConfig((value) => ({ ...value, vision: {
                        ...value.vision, model: e.target.value,
                        effort: model?.efforts.includes(value.vision.effort) ? value.vision.effort :
                          model?.efforts.includes('medium') ? 'medium' : model?.efforts[0] ?? '',
                      } }));
                    }}>
                      {!selectedVision && <option value={gptConfig.vision.model}>請選擇可用模型</option>}
                      {modelChoices(gptConfig.vision.provider, true).map((item) =>
                        <option key={item.id} value={item.id}>{item.label}</option>)}
                    </select>
                  </label>
                  <label className="backend-row">圖片 effort
                    <select value={gptConfig.vision.effort} onChange={(e) =>
                      setGptConfig((value) => ({ ...value, vision: { ...value.vision, effort: e.target.value } }))}>
                      {(selectedVision?.efforts ?? []).map((effort) =>
                        <option key={effort} value={effort}>{effort}</option>)}
                    </select>
                  </label>
                  <small role="status">{gptOptionsLoading ? '正在檢查可用模型…' :
                    !gptSelectionReady ? (gptOptions[gptConfig.delegation.provider].error ||
                      gptOptions[gptConfig.vision.provider].error || '請選擇有效的模型與 effort') :
                    `教學 ${gptConfig.delegation.model}／${gptConfig.delegation.effort}；圖片 ${gptConfig.vision.model}／${gptConfig.vision.effort}`}</small>
                  {gptConfig.vision.effort === 'max' &&
                    <small>鏡頭約每 10 秒拍照；max 可能增加看圖等待與 API 用量，過期結果不會當成新照片。</small>}
                </div>
              )}
              <p className="readiness">
                {boot.capabilities[mode]?.reason ?? '目前為介面預覽；請從桌面程式開啟。'}
              </p>
              {mode === 'basic' && (
                <div className="pipeline-check">
                  <button
                    onClick={testPipeline}
                    disabled={
                      busy ||
                      voiceTesting ||
                      voiceStatus === '正在準備試聽…' ||
                      !boot.capabilities.basic?.groq_key
                    }
                  >
                    完整語音流程測試
                  </button>
                  <small>使用合成語音測試轉錄與教學服務，不會上傳鏡頭畫面。</small>
                  {speaking && !voiceTesting && teaching.current && (
                    <button onClick={toggleMic}>插話：停止播放</button>
                  )}
                  {heard && <span role="status">{heard}</span>}
                </div>
              )}
            </details>
            <div className="mode-status">
              <span>{modes.find(([id]) => id === mode)?.[1] ?? '基本陪讀'}
                {mode === 'basic' ? ` · ${backend === 'codex' ? 'Codex' : 'agy'}` : ''}</span>
              <b role="status">{boot.capabilities[mode]?.ready
                ? '金鑰已設定，開始時會檢查服務'
                : mode === 'gpt' ? 'GPT-LIVE-1 需要 OpenAI API 金鑰'
                : '尚無金鑰，將進入本機工作區'}</b>
              <button className="mode-status-action" disabled={busy}
                onClick={() => openParent('connection')}>服務設定</button>
            </div>
            <button className="primary start" disabled={!student.id || !startupChecked || startupError || busy || recording ||
              (mode === 'gpt' && (!boot.capabilities.gpt?.ready || gptOptionsLoading || !gptSelectionReady))} onClick={start}>
              {boot.capabilities[mode]?.ready
                ? '檢查並開始陪讀'
                : mode === 'gpt' ? '請先設定 OpenAI 金鑰' : '開始專注工作區'}{' '}
              <ArrowRight size={19} />
            </button>
            <p className="start-help">
              {boot.capabilities[mode]?.ready
                ? mode === 'basic'
                  ? '開始前會檢查所選教學後端'
                  : '開始前會檢查所選教學服務'
                : mode === 'gpt' ? '請到服務設定填入 OpenAI API 金鑰'
                : '此模式尚未設定金鑰，將使用本機工作區'}{' '}
              · 全螢幕啟動，ESC 可離開
            </p>
          </section>
          <footer className="home-footer">
            <span>把答案留給孩子，把陪伴交給我們。</span>
            <span>伴讀 0.1 · 開發預覽</span>
          </footer>
        </main>
      ) : (
        <main className="workspace">
          <section className="board-panel">
            <div className="board-heading">
              <div>
                <span className="eyebrow">一起想一想</span>
                <h2>{student.name}的學習白板</h2>
              </div>
              <span className="board-tag">
                <BookOpen size={15} />
                今天的練習
              </span>
            </div>
            <div className="board" ref={boardRef}>
              <div className="board-empty">
                <div className="board-sprout">
                  <Leaf size={40} strokeWidth={1} />
                </div>
                <h2>把作業放好，我們慢慢來。</h2>
                <p>
                  這裡會留給解題步驟、圖畫，
                  <br />
                  還有你自己想到的好方法。
                </p>
                {!online && (
                  <div className="offline-label">目前為本機工作區，沒有 AI 正在批改作業。</div>
                )}
              </div>
            </div>
            <div className="board-footer">
              <span>每一步思考，都值得被看見。</span>
              <button
                className="text-button"
                onClick={() => {
                  void window.desktop?.closeBoard();
                }}
              >
                收起白板內容
              </button>
              {selected === 'student-test' && <button className="text-button" onClick={demoBoard}>
                白板互動測試
              </button>}
            </div>
          </section>
          <aside className="side-panel">
            <section className={'camera-card' + (cameraPreviewExpanded ? ' expanded' : '')}>
              <div className="camera-heading">
                <span>
                  <span className={'dot ' + (!cameraOn ? 'off' : '')} />
                  老師看到的畫面
                </span>
                <button className="camera-preview-toggle" aria-expanded={cameraPreviewExpanded}
                  onClick={() => setCameraPreviewExpanded((value) => !value)}>
                  {cameraPreviewExpanded ? '收起預覽' : '顯示預覽'}
                </button>
              </div>
              <div className="camera-view">
                <canvas
                  ref={lessonCanvas}
                  className={(cameraOn && cameraReady ? '' : 'hidden') + (focusPicking ? ' focus-picking' : '')}
                  aria-label="老師看到的畫面"
                  onPointerDown={(event) => {
                    if (!focusPicking) return;
                    focusStart.current=previewPoint(event.currentTarget,event.clientX,event.clientY);
                    event.currentTarget.setPointerCapture(event.pointerId);
                  }}
                  onPointerUp={(event) => {
                    if (!focusPicking || !focusStart.current) return;
                    const end=previewPoint(event.currentTarget,event.clientX,event.clientY);
                    const start=focusStart.current;
                    focusStart.current=null;
                    const rect:[number,number,number,number]=[
                      Math.min(start[0],end[0]),Math.min(start[1],end[1]),
                      Math.max(start[0],end[0]),Math.max(start[1],end[1]),
                    ];
                    try {
                      focusCameraSettings(cameraSettings,rect);
                      setFocusRect(rect);
                      setFocusPicking(false);
                      setError('');
                    } catch (error) { report(error); }
                  }}
                />
                {(!cameraOn || !cameraReady) && (
                  <div className="camera-empty">
                    <CameraOff size={28} strokeWidth={1.3} />
                    <span>{cameraOn ? '正在取得畫面' : '鏡頭已關閉'}</span>
                  </div>
                )}
                <span className="camera-badge">{cameraOn
                  ? focusRect ? '已放大題目' : isFullFrameCorners(cameraSettings.corners) ? '完整桌面 · 題目可能太小' : '已套用書頁範圍'
                  : '等待相機'}</span>
              </div>
              <div className="camera-foot">
                <span>每 10 秒拍照 · 共 {captured} 張</span>
                <span>{cameraOn ? (online ? '送交 AI 觀察' : '僅本機') : '影像傳送已停止'}</span>
              </div>
              {cameraOn && (
                <div className="camera-focus-actions">
                  <button onClick={() => {
                    setFocusRect(null);
                    setFocusPicking(true);
                    setCameraPreviewExpanded(true);
                  }}>{focusRect ? '重新框這題' : '放大這題'}</button>
                  {focusRect && <button onClick={() => setFocusRect(null)}>返回整頁</button>}
                  {focusPicking && <span role="status">在預覽上拖曳，框住要讀的題目與選項。</span>}
                </div>
              )}
            </section>
            <section className="conversation">
              <div className="conversation-heading">
                陪讀紀錄
                <span aria-live="polite">
                  {thinking ? activeTeacherName + '正在整理提示…' : '本次'}
                </span>
              </div>
              {lesson.tasks.length > 0 && (
                <div className="lesson-list">
                  <div className="lesson-label">
                    {lesson.all_done ? '今天的清單已完成，可以按「結束」' : '今天的作業'}
                    {!lesson.confirmed && (
                      <button
                        disabled={reconnecting}
                        onClick={() => teaching.current?.lesson('confirm')}
                      >
                        確認這份清單
                      </button>
                    )}
                  </div>
                  {lesson.tasks.map((task) => (
                    <label key={task.id}>
                      <input
                        type="checkbox"
                        checked={task.status === 'done'}
                        disabled={!lesson.confirmed || reconnecting}
                        onChange={(e) => {
                          const status = e.target.checked ? 'done' : 'todo';
                          setLesson((current) => ({
                            ...current,
                            tasks: current.tasks.map((t) =>
                              t.id === task.id ? { ...t, status } : t,
                            ),
                          }));
                          teaching.current?.lesson(status, task.id);
                        }}
                      />
                      <span>{task.title}</span>
                    </label>
                  ))}
                </div>
              )}
              <div className="messages">
                <div className="system-note">{notes || '今天的注意事項會顯示在這裡。'}</div>
                <div className="assistant-row">
                  <span className="tiny-avatar">
                    <Leaf size={15} />
                  </span>
                  <div className="bubble">
                    {online
                      ? activeTeacherName + '已連線。先讓我們確認今天的作業範圍。'
                      : '工作區已準備好。AI 教學服務尚未啟用，這段期間不會自動判斷答案或產生學習評語。'}
                  </div>
                </div>
                {messages
                  .filter((message) => message.text)
                  .map((message) => (
                    <div
                      className={
                        'assistant-row ' + (message.role === 'user' ? 'student-message' : '')
                      }
                      key={message.id}
                    >
                      <span className="tiny-avatar">
                        <Volume2 size={15} />
                      </span>
                      <div className="bubble">
                        <small>{message.role === 'user' ? '你' : activeTeacherName}</small>
                        {message.text}
                        {message.status && <small>{message.status}</small>}
                      </div>
                    </div>
                  ))}
                <div ref={messagesEnd} />
              </div>
              {online && mode === 'basic' && (
                <form
                  className="text-reply"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (!typed.trim()) return;
                    const value = typed.trim();
                    setTyped('');
                    void (async () => {
                      await teaching.current?.interrupt(false);
                      micState.current.userEnabled=false;
                      mic.current?.getTracks().forEach((track) => (track.enabled=false));
                      setMicOn(false);
                      setAudioStage('off');
                      teaching.current?.text(value);
                    })().catch(report);
                  }}
                >
                  <input
                    aria-label="用文字求助"
                    placeholder={`也可以打字告訴${activeTeacherName}…`}
                    maxLength={4000}
                    value={typed}
                    onChange={(e) => setTyped(e.target.value)}
                  />
                  <button aria-label="送出文字" disabled={!typed.trim()}>
                    <ArrowRight size={16} />
                  </button>
                </form>
              )}
              <div className={'listen-status ' + (voiceReady ? 'ready' : 'busy')} role="status" aria-live="polite">
                <span className={'dot ' + (!voiceReady ? 'off' : '')} />
                {listenText}
              </div>
            </section>
            <div className="controls">
              <button
                className={!micOn || interruptAction ? 'muted' : ''}
                onClick={toggleMic}
                disabled={reconnecting}
                aria-label={speaking ? '打斷老師並說話' : interruptAction ? '我想補充' : micOn ? '關閉麥克風' : '開啟麥克風'}
              >
                {interruptAction ? <Mic /> : micOn ? <Mic /> : <MicOff />}
                <span>{speaking ? '打斷老師並說話' : interruptAction ? '我想補充' : micOn ? '關閉麥克風' : '開啟麥克風'}</span>
              </button>
              <button onClick={cameraToggle} disabled={reconnecting}>
                {cameraOn ? <Camera /> : <CameraOff />}
                <span>{cameraOn ? '關鏡頭' : '開鏡頭'}</span>
              </button>
              <button className="end" onClick={end}>
                <Square size={19} />
                <span>結束</span>
              </button>
            </div>
          </aside>
        </main>
      )}
      {parent && (
        <div className="modal-backdrop">
          <section ref={parentDialog} className="parent-modal" role="dialog" aria-modal="true" aria-label="家長設定">
            <header>
              <div>
                <div className="eyebrow">家長空間</div>
                <h2>為孩子，準備剛好的陪伴。</h2>
              </div>
              <button className="icon-button" aria-label="關閉設定" onClick={closeParent} disabled={profileSaving}>
                <X />
              </button>
            </header>
            <div className="profile-selector">
              {boot.students.map((s) => (
                <button
                  className={selected === s.id ? 'active' : ''}
                  key={s.id}
                  aria-pressed={selected === s.id}
                  onClick={() => switchParentStudent(s)}
                >
                  {s.name}
                </button>
              ))}
            </div>
            <nav role="tablist" aria-label="家長設定頁面">
              {[
                ['profile', '個人檔案'],
                ['history', '學習紀錄'],
                ['school', '學校與課綱'],
                ['connection', '服務設定'],
              ].map(([id, label]) => (
                <button
                  className={tab === id ? 'active' : ''}
                  key={id}
                  role="tab"
                  aria-selected={tab === id}
                  aria-controls="parent-tab-panel"
                  onClick={() => switchParentTab(id)}
                >
                  {label}
                </button>
              ))}
            </nav>
            <div className="modal-content" id="parent-tab-panel" role="tabpanel">
              {tab === 'profile' ? (
                <>
                  <label className="field-label">
                    稱呼
                    <input
                      value={draft.name}
                      onChange={(e) => {
                        setProfileDraft({ ...draft, name: e.target.value });
                        setSaved(false);
                      }}
                    />
                  </label>
                  {selected === 'student-test' && (
                    <label className="field-label">
                      測試目標年級
                      <select
                        value={draft.grade}
                        onChange={(e) => {
                          setProfileDraft({ ...draft, grade: Number(e.target.value) });
                          setSaved(false);
                        }}
                      >
                        {[1, 2, 3, 4, 5, 6].map((g) => (
                          <option key={g} value={g}>
                            國小 {g} 年級
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  <label className="field-label">
                    學校
                    <input
                      value={draft.school_name ?? ''}
                      onChange={(e) => {
                        setProfileDraft({ ...draft, school_name: e.target.value });
                        setSaved(false);
                      }}
                      placeholder="輸入學校名稱；留空表示尚未設定"
                    />
                  </label>
                  <label className="field-label">
                    家長提供的引導偏好
                    <textarea
                      value={draft.preferences}
                      onChange={(e) => {
                        setProfileDraft({ ...draft, preferences: e.target.value });
                        setSaved(false);
                      }}
                      placeholder="例如：先用具體圖形，再解釋算式。"
                    />
                  </label>
                  {selected === 'student-test' && (
                    <p className="muted-copy" style={{ marginTop: '8px', fontSize: '12px' }}>
                      💡
                      測試專用學生：此處設定的稱呼、目標年級與引導偏好會持續保存，並在每次測試時生效；測試過程不會累積長期課綱評估。
                    </p>
                  )}
                  <div className="button-row">
                    <button className="primary" onClick={() => void saveProfile()}
                      disabled={!profileDirty || profileSaving}>
                      {profileSaving ? '儲存中…' : '儲存檔案'}
                    </button>
                    {profileDirty && <button onClick={() => { setProfileDraft({ ...student }); setSaved(false); }}>
                      放棄修改
                    </button>}
                    {profileDirty && <span role="status">尚未儲存</span>}
                    {saved && <span>已儲存</span>}
                  </div>
                </>
              ) : tab === 'history' ? (
                <>
                  {selected === 'student-test' && (
                    <div
                      style={{
                        marginBottom: '16px',
                        background: '#f5f7f3',
                        border: '1px solid #d4ded0',
                        padding: '12px',
                        borderRadius: '8px',
                      }}
                    >
                      <b style={{ display: 'block', marginBottom: '4px' }}>測試模式</b>
                      <p className="muted-copy" style={{ margin: '0 0 10px 0', fontSize: '13px' }}>
                        測試同學會保留文字對話、課程摘要與工作區檔案。每堂課的 AI 輸入輸出與除錯紀錄保留 72 小時，Live 模式的復盤照片也會隨紀錄到期。不累積長期課綱概念評估。
                      </p>
                      <button
                        className="text-button"
                        onClick={() => {
                          if (
                            confirm(
                              '確定要清除測試同學的所有測試歷程、相片與工作區檔案嗎？（引導偏好仍會保留）',
                            )
                          ) {
                            request(`/students/${selected}/history`, undefined, 'DELETE')
                              .then(async () => {
                                await reloadHistory(selected);
                              })
                              .catch(report);
                          }
                        }}
                      >
                        清除測試紀錄與工作區
                      </button>
                    </div>
                  )}
                  {history ? (
                    <div className="history">
                      <p>共 {history.sessions.length} 次紀錄。</p>
                      <LearningMemory
                        studentId={selected}
                        profile={history.learning_profile ?? []}
                        evidence={history.learning_evidence ?? []}
                        refresh={() => reloadHistory(selected)}
                        report={report}
                      />
                      {history.learning_context?.map((summary: any) => (
                        <article className="learning-summary" key={summary.session_id}>
                          <b>
                            {new Date(summary.recorded_at).toLocaleDateString('zh-TW')} ·
                            下次陪讀參考
                          </b>
                          <p>
                            {summary.tasks?.length
                              ? summary.tasks
                                  .map(
                                    (task: any) =>
                                      `${task.status === 'done' ? '已回報完成' : '待完成'}：${task.title}`,
                                  )
                                  .join('；')
                              : '尚未確認作業清單'}
                          </p>
                          {summary.student_questions?.length > 0 && (
                            <p>最近求助／對話：{summary.student_questions.join('；')}</p>
                          )}
                          {summary.heard_guidance?.length > 0 && (
                            <p>已播放的引導：{summary.heard_guidance.join('；')}</p>
                          )}
                          {summary.analysis && ([
                            ['highlights','本次重點'],['difficulties','遇到的困難'],
                            ['guidance','引導與效果'],['next_steps','下次建議'],
                            ['uncertainties','仍待核對'],
                          ] as const).map(([key,label]) =>
                            summary.analysis[key]?.length > 0 && (
                              <div key={key} className="summary-section">
                                <b>{label}</b>
                                <ul>{summary.analysis[key].map((item: any, index: number) => (
                                  <li key={`${key}-${index}`}>
                                    {item.text}
                                    <details>
                                      <summary>查看依據</summary>
                                      {item.sources.map((id: string) => {
                                        const source = history.summary_sources?.[id];
                                        return <p key={id}>{source
                                          ? `${new Date(source.at).toLocaleString('zh-TW')} · ${source.text || '本次紀錄'}`
                                          : '來源紀錄目前無法顯示'}</p>;
                                      })}
                                    </details>
                                  </li>
                                ))}</ul>
                              </div>
                            ))}
                          <small>{summary.basis}</small>
                        </article>
                      ))}
                      {history.finalizations?.filter((item: any) => item.state === 'failed').map((item: any) => (
                        <article className="learning-summary" key={item.session_id}>
                          <b>課後整理未完成</b>
                          <p>本次紀錄仍可查看；AI 結論尚未產生。</p>
                          <button onClick={() => void request(`/sessions/${item.session_id}/finalization/retry`,{})
                            .then(() => reloadHistory(selected)).catch(report)}>重試整理</button>
                        </article>
                      ))}
                      {history.sessions.map((s: any) => (
                        <div className="history-row" key={s.id}>
                          <span>{new Date(s.started).toLocaleString('zh-TW')}</span>
                          <span>
                            {{ basic: '基本陪讀', gpt: 'GPT Live', gemini: 'Gemini Live' }[s.mode as 'basic' | 'gpt' | 'gemini'] ?? s.mode}
                            {' · '}
                            {{ ended: '已結束', interrupted: '中斷', finalizing: '整理中', active: '進行中' }[s.status as 'ended' | 'interrupted' | 'finalizing' | 'active'] ?? s.status}
                            {(s.status === 'ended' || s.status === 'interrupted') && (
                              <button className="text-button" onClick={() => void request(
                                `/students/${selected}/sessions/${s.id}/context`,
                                { include: !s.context_included,
                                  reason: s.context_included ? '家長標記待複核' : '' }, 'PUT',
                              ).then(() => reloadHistory(selected)).catch(report)}>
                                {s.context_included ? '下次不引用此課' : '恢復下次參考'}
                              </button>
                            )}
                          </span>
                          <span>
                            {(() => {
                              const cost = history.events.find(
                                (e: any) => e.session_id === s.id && e.kind === 'session_cost',
                              )?.payload;
                              if (!cost) return '費用尚未結算';
                              if (cost.status === 'no_api_calls') return '未呼叫 API';
                              if (cost.mode === 'gemini')
                                return `已記錄 ${cost.gemini_raw_usage_events} 筆用量，金額待核對`;
                              if (cost.mode === 'gpt') {
                                const voice = typeof cost.gpt_voice_estimate === 'number'
                                  ? `語音約 US$${cost.gpt_voice_estimate.toFixed(4)}` : '語音金額待核對';
                                const teaching = cost.gpt_responses_usage?.length ?? 0;
                                const vision = cost.gpt_vision_usage?.length ?? 0;
                                const postLesson = history.events.filter((e: any) =>
                                  e.session_id === s.id && e.kind === 'post_lesson_api_usage').length;
                                return `${voice}；教學 API ${teaching} 次、圖片 API ${vision} 次、課後 API ${postLesson} 次（另計）`;
                              }
                              const amount = cost.gpt_voice_estimate ?? cost.groq_estimate_usd;
                              return typeof amount === 'number'
                                ? `約 US$${amount.toFixed(4)}${cost.mode === 'gpt' && !cost.gpt_final ? '（用量未完整）' : ''}`
                                : '金額待核對';
                            })()}
                          </span>
                        </div>
                      ))}
                      <div className="button-row">
                        <button
                          onClick={() => {
                            const u = URL.createObjectURL(
                              new Blob([JSON.stringify(history, null, 2)], {
                                type: 'application/json',
                              }),
                            );
                            const a = document.createElement('a');
                            a.href = u;
                            a.download = 'learning-history.json';
                            a.click();
                            setTimeout(() => URL.revokeObjectURL(u), 1000);
                          }}
                        >
                          匯出紀錄
                        </button>
                        <button
                          className="danger"
                          onClick={() => {
                            if (confirm('確定刪除此學生所有陪讀紀錄？'))
                              request(`/students/${selected}/history`, undefined, 'DELETE')
                                .then(() => reloadHistory(selected))
                                .catch(report);
                          }}
                        >
                          刪除紀錄
                        </button>
                      </div>
                    </div>
                  ) : (
                    <p className="muted-copy">正在載入陪讀紀錄…</p>
                  )}
                </>
              ) : tab === 'school' ? (
                <>
                  <div className="school-title">
                    <CalendarDays />
                    <div>
                      <h3>{school?.school ?? '尚未設定學校'}</h3>
                      <p>
                        {school?.year ? `${school.year} 學年度 · ` : ''}
                        {student.name} · 國小 {student.grade} 年級
                      </p>
                    </div>
                  </div>
                  {(() => {
                    const books = Object.entries(school?.textbooks[String(student.grade)] ?? {});
                    return books.length > 0 ? (
                      <div className="textbooks">
                        {books.map(([subject, publisher]) => (
                          <div key={subject}>
                            <span>{subject}</span>
                            <b>{publisher}</b>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p className="muted-copy" style={{ marginBottom: '16px' }}>
                        目前尚無國小 {student.grade} 年級教科書版本資料。
                      </p>
                    );
                  })()}
                  <h3>接下來的校園日程</h3>
                  {events.length === 0 && (
                    <p className="muted-copy">目前沒有可用的學校行事曆資料。</p>
                  )}
                  {events.map((e) => (
                    <div className="calendar-row" key={e.date}>
                      <time>{e.date.slice(5).replace('-', ' / ')}</time>
                      <span>
                        {e.name}
                        {e.end ? '（至 ' + e.end.slice(5) + '）' : ''}
                      </span>
                    </div>
                  ))}
                  <p className="muted-copy">
                    {school?.notice ?? '請先到個人檔案設定這位學生的學校。'}
                  </p>
                  {school?.source && (
                    <p className="muted-copy">
                      資料來源：
                      <a href={school.source} target="_blank" rel="noreferrer">
                        學校課程計畫
                      </a>
                    </p>
                  )}
                  <hr />
                  <CurriculumPanel grade={student.grade} />
                </>
              ) : (
                <>
                  <div className="service-notice">
                    <CircleHelp size={20} />
                    <p>
                      三種模式依各自金鑰啟用；實際模型權限在連線時檢查。下列金鑰使用 Windows
                      安全儲存；填入金鑰不代表教學服務已通過驗收。
                    </p>
                  </div>
                  {Object.entries(keyValues).map(([key, value]) => {
                    const status = keyStatus[key];
                    return (
                      <label className="field-label" key={key}>
                        {key.replace('_API_KEY', '')} API 金鑰
                        <input
                          type="password"
                          autoComplete="off"
                          value={value}
                          placeholder="重新填入會取代既有金鑰"
                          onChange={(e) => setKeyValues((k) => ({ ...k, [key]: e.target.value }))}
                        />
                        <span
                          className={
                            'key-status' +
                            (status?.has_key
                              ? status.valid === true
                                ? ' valid'
                                : status.valid === false
                                  ? ' invalid'
                                  : ' provided'
                              : ' missing')
                          }
                        >
                          {keyChecking
                            ? '檢查中…'
                            : !status
                              ? ''
                              : !status.has_key
                                ? '⚠ 尚未提供金鑰'
                                : status.valid === true
                                  ? '✓ 已提供，驗證有效'
                                  : status.valid === false
                                    ? '✗ 已提供，但金鑰無效'
                                    : '● 已提供（未能驗證）'}
                        </span>
                      </label>
                    );
                  })}
                  <div className="button-row">
                    <button
                      className="primary"
                      onClick={async () => {
                        try {
                          await window.desktop?.saveKeys(keyValues);
                          if (!window.desktop) throw Error('需從桌面程式儲存');
                          setSaved(true);
                          setKeyValues({
                            GROQ_API_KEY: '',
                            OPENAI_API_KEY: '',
                            GEMINI_API_KEY: '',
                          });
                          await refresh();
                          setKeyChecking(true);
                          request<Record<string, { has_key: boolean; valid: boolean | null }>>(
                            '/credentials/status',
                          )
                            .then(setKeyStatus)
                            .catch(() => {})
                            .finally(() => setKeyChecking(false));
                        } catch (e) {
                          report(e);
                        }
                      }}
                    >
                      儲存金鑰
                    </button>
                    {saved && <span>已加密儲存</span>}
                  </div>
                </>
              )}
            </div>
          </section>
        </div>
      )}
      {lastFrame && false && <img src={lastFrame} alt="校正後畫面" />}
    </div>
  );
}
