import { useEffect, useRef, useState } from 'react';
import { request } from './api';

const assistance = { none:'無即時提示', read_prompt:'只協助讀題', hint:'給過提示', worked_example:'看過同型示範', full_solution:'看過本題完整解法' };
const phases = { initial:'練習前', practice:'練習中', immediate_check:'同次短檢查', delayed_check:'隔日或之後的檢查' };
const results = { unreviewed:'待核對', correct:'核對正確', incorrect:'核對有誤', not_observed:'未觀察／無法判定' };
type AttemptData = { subject:string; concept:string; question:string; response:string;
  assistance:keyof typeof assistance; phase:keyof typeof phases; previous_attempt_id:string; answer_basis:string };
type Attempt = AttemptData & { id:string; session_id:string; origin:string; created:string; result:keyof typeof results;
  review_note:string; revision:number; reviewed_at:string | null; independent_verified:boolean; elapsed_days:number | null; context_included:number };
type Card = { id:string; subject:string; goal:string; prerequisite:string; misconception:string;
  representation:string; example:string; check:{question:string; answer:string} };
type Practice = { settings:{enabled:boolean; reviewed_cards:string[]}; attempts:Attempt[]; cards:Card[]; voice_evidence_note:string };
const blank:AttemptData = {subject:'數學',concept:'',question:'',response:'',assistance:'none',phase:'practice',previous_attempt_id:'',answer_basis:''};
const fields = (value:AttemptData):AttemptData => ({subject:value.subject,concept:value.concept,question:value.question,
  response:value.response,assistance:value.assistance,phase:value.phase,previous_attempt_id:value.previous_attempt_id,answer_basis:value.answer_basis});

function ReviewAttempt({ attempt, studentId, refresh, report }: {attempt:Attempt;studentId:string;refresh:()=>Promise<void>;report:(e:unknown)=>void}) {
  const [draft,setDraft]=useState(fields(attempt));
  const [result,setResult]=useState(attempt.result);
  const [note,setNote]=useState(attempt.review_note);
  const [saving,setSaving]=useState(false);
  async function save(exclude=false) {
    if (exclude && !confirm('排除這筆練習紀錄？之後不再提供給老師。')) return;
    setSaving(true);
    try {
      await request(`/students/${studentId}/practice/attempts/${attempt.id}`,
        {...(exclude?fields(attempt):draft),result:exclude?'unreviewed':result,note:exclude?attempt.review_note:note,revision:attempt.revision,exclude},'PUT');
      await refresh();
    } catch(e) { report(e); } finally { setSaving(false); }
  }
  return <article className="practice-attempt">
    <div className="practice-attempt-heading"><b>{attempt.subject} · {attempt.concept}</b><span>{results[attempt.result]}</span></div>
    <small>{new Date(attempt.created).toLocaleString('zh-TW')} · {phases[attempt.phase]} · {assistance[attempt.assistance]}
      {attempt.elapsed_days !== null ? ` · 兩筆紀錄相隔 ${attempt.elapsed_days.toFixed(1)} 天` : ''}</small>
    <p>{attempt.question}</p>
    <p className="practice-answer">作答：{attempt.response || '未觀察到作答'}</p>
    <small>{attempt.origin==='parent' ? '家長補記' : attempt.origin==='typed_text' ? '學生文字' : '服務轉錄'} · {attempt.result==='unreviewed'?'待家長核對':'家長已複核'}
      {!attempt.context_included ? ' · 所屬課程已排除於老師背景' : ''}</small>
    {attempt.independent_verified && <p className="practice-confirmed">家長已核對本次無即時提示的正確作答；單次結果不代表已掌握。</p>}
    <details><summary>核對、修正或排除</summary>
      <fieldset disabled={saving}>
        <label>題目原文<textarea value={draft.question} maxLength={1000} onChange={e=>setDraft({...draft,question:e.target.value})}/></label>
        <label>學生作答原文<textarea value={draft.response} maxLength={1000} onChange={e=>setDraft({...draft,response:e.target.value})}/></label>
        <label>實際協助程度<select value={draft.assistance} onChange={e=>setDraft({...draft,assistance:e.target.value as AttemptData['assistance']})}>
          {Object.entries(assistance).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
        <label>核對依據<textarea value={draft.answer_basis} maxLength={1000} placeholder="例如：依課本例題核對算式，並確認孩子沒有看解答。" onChange={e=>setDraft({...draft,answer_basis:e.target.value})}/></label>
        <label>家長核對結果<select value={result} onChange={e=>setResult(e.target.value as Attempt['result'])}>
          {Object.entries(results).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
        <label>複核補充<textarea value={note} maxLength={1000} onChange={e=>setNote(e.target.value)}/></label>
        <div className="button-row"><button onClick={()=>void save()}>儲存練習複核</button><button onClick={()=>void save(true)}>排除練習紀錄</button></div>
      </fieldset>
    </details>
  </article>;
}

export default function PracticePanel({studentId,sessions,report}:{studentId:string;sessions:{id:string;started:string;status:string;context_included?:number}[];report:(e:unknown)=>void}) {
  const [data,setData]=useState<Practice|null>(null);
  const [draft,setDraft]=useState<AttemptData>({...blank});
  const [sessionId,setSessionId]=useState('');
  const [saving,setSaving]=useState(false);
  const [notice,setNotice]=useState('');
  const addDetails=useRef<HTMLDetailsElement>(null);
  const sessionsVersion = sessions.map(s => `${s.id}:${s.status}:${s.context_included ?? 1}`).join('|');
  async function refresh() { setData(await request<Practice>(`/students/${studentId}/practice`)); }
  useEffect(()=>{ let current=true;void request<Practice>(`/students/${studentId}/practice`).then(value=>{if(current)setData(value);}).catch(report);return()=>{current=false;}; },[studentId,sessionsVersion]);
  const eligibleSessions=sessions.filter(s=>s.status!=='finalizing');
  useEffect(() => {
    if (sessionId && !eligibleSessions.some(s => s.id === sessionId)) setSessionId('');
  }, [sessionId, sessionsVersion]);
  async function settings(enabled:boolean,reviewed_cards:string[]) {
    const previous=data;
    if (data) setData({...data,settings:{enabled,reviewed_cards}});
    setSaving(true);setNotice('');
    let saved = false;
    try {
      await request(`/students/${studentId}/practice/settings`,{enabled,reviewed_cards},'PUT');
      saved = true;
      await refresh();
      setNotice('設定已儲存');
    } catch(e) {
      if (!saved) setData(previous);
      report(e);
    } finally { setSaving(false); }
  }
  async function add(e:React.FormEvent) {
    e.preventDefault();setSaving(true);setNotice('');
    try {
      await request(`/students/${studentId}/practice/attempts`,{...draft,session_id:sessionId||eligibleSessions[0]?.id});
      setDraft({...blank});await refresh();setNotice('已保存，請在下方核對這筆作答');
      if (addDetails.current) addDetails.current.open=false;
    } catch(error) { report(error); } finally { setSaving(false); }
  }
  if (!data) return <p role="status">正在載入練習紀錄…</p>;
  return <section className="practice-panel" aria-label="練習與獨立作答">
    <h3>練習與獨立作答</h3>
    <p className="muted-copy">分開記錄提示後完成、獨立完成與隔日檢查。沒有觀察到不算答錯；每筆由家長核對，與「家長確認已熟悉」分開保存。</p>
    <label className="practice-toggle"><input type="checkbox" checked={data.settings.enabled} disabled={saving}
      onChange={e=>void settings(e.target.checked,data.settings.reviewed_cards)}/>允許老師邀請短檢查</label>
    <p className="muted-copy">每次先徵求孩子同意，最多一兩題；拒絕或結束就停止。數日後可再檢查，不自動增加作業。</p>
    <details className="parent-guide"><summary>家長與孩子的使用方法</summary>
      <p>開始前確認今天的作業與一個學習目標。孩子可以說：「第二題我試過這一步，請給我一個提示。」</p>
      <p>聽錯或看錯時說：「請重新確認。」持續卡住、看不清楚或孩子不舒服時由家長接手；每週挑少量紀錄，補上學校老師的教法。</p>
      <p>{data.voice_evidence_note}</p>
    </details>
    <details className="practice-cards"><summary>核對教學示例（{data.settings.reviewed_cards.length} 張已確認）</summary>
      <p className="muted-copy">以下為原創草稿，尚未經教師審定。請先核對適用程度與學校教法，勾選後才會提供給老師。</p>
      {data.cards.map(card=><article key={card.id} className="teaching-card">
        <b>{card.subject} · {card.goal}</b><p>先備：{card.prerequisite}</p><p>常見困難：{card.misconception}</p>
        <p>可以用：{card.representation}</p><p>例子：{card.example}</p>
        <p>短檢查：{card.check.question}</p><p>核對參考：{card.check.answer}</p>
        <label><input type="checkbox" disabled={saving} checked={data.settings.reviewed_cards.includes(card.id)}
          onChange={e=>void settings(data.settings.enabled,e.target.checked?[...data.settings.reviewed_cards,card.id]:data.settings.reviewed_cards.filter(id=>id!==card.id))}/>
          我已核對這張示例適合孩子</label>
      </article>)}
    </details>
    {studentId==='student-test' ? <p className="muted-copy">測試同學不累積長期練習紀錄。</p> : <>
      <details className="practice-add" ref={addDetails}><summary>補記一次作答</summary>
        <form onSubmit={e=>void add(e)}><fieldset disabled={saving||!eligibleSessions.length}>
          <label>對應課程<select value={sessionId||eligibleSessions[0]?.id||''} onChange={e=>setSessionId(e.target.value)}>
            {eligibleSessions.map(s=><option key={s.id} value={s.id}>{new Date(s.started).toLocaleString('zh-TW')}</option>)}</select></label>
          <div className="practice-form-grid">
            <label>科目<input value={draft.subject} maxLength={40} required onChange={e=>setDraft({...draft,subject:e.target.value})}/></label>
            <label>概念<input value={draft.concept} maxLength={120} placeholder="例如：進位加法" required onChange={e=>setDraft({...draft,concept:e.target.value})}/></label>
          </div>
          <label>題目<textarea value={draft.question} maxLength={1000} required onChange={e=>setDraft({...draft,question:e.target.value})}/></label>
          <label>作答原文<textarea value={draft.response} maxLength={1000} placeholder="未觀察到可留空，不替孩子補答案" onChange={e=>setDraft({...draft,response:e.target.value})}/></label>
          <div className="practice-form-grid">
            <label>協助程度<select value={draft.assistance} onChange={e=>setDraft({...draft,assistance:e.target.value as AttemptData['assistance']})}>
              {Object.entries(assistance).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
            <label>檢查階段<select value={draft.phase} onChange={e=>setDraft({...draft,phase:e.target.value as AttemptData['phase'],previous_attempt_id:''})}>
              {Object.entries(phases).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
          </div>
          {draft.phase==='delayed_check'&&<label>先前同概念練習<select required value={draft.previous_attempt_id} onChange={e=>{
            const prior=data.attempts.find(a=>a.id===e.target.value);
            setDraft({...draft,previous_attempt_id:e.target.value,...(prior?{subject:prior.subject,concept:prior.concept}:{})});
          }}><option value="">選擇先前練習（須跨堂且相隔至少一天）</option>
            {data.attempts.map(a=><option key={a.id} value={a.id}>{a.concept} · {new Date(a.created).toLocaleDateString('zh-TW')}</option>)}</select></label>}
          <label>核對依據<textarea value={draft.answer_basis} maxLength={1000} onChange={e=>setDraft({...draft,answer_basis:e.target.value})}/></label>
          <button type="submit">保存待核對作答</button>
        </fieldset></form>
        {!eligibleSessions.length&&<p>開始並保存一堂課後，就能在這裡補記作答。</p>}
      </details>
      <p className="practice-status" role="status">{notice}</p>
      {data.attempts.length ? data.attempts.map(attempt=><ReviewAttempt key={`${attempt.id}:${attempt.revision}`} attempt={attempt} studentId={studentId} refresh={refresh} report={report}/>) : <p className="muted-copy">尚無練習紀錄；這不表示孩子不會。</p>}
    </>}
  </section>;
}
