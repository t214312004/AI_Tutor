import { useState } from 'react';
import { request } from './api';

type Concept = {
  code: string;
  subject: string;
  label: string;
  help_count: number;
  step_count: number;
  revision_count: number;
  parent_status: string;
  parent_note: string;
};
type Evidence = {
  id: string;
  code: string;
  kind: string;
  quote: string;
  origin: string;
  parent_corrected?: number;
  created: string;
};

function ConceptCard({ item, studentId, refresh, report }: {
  item: Concept; studentId: string; refresh: () => Promise<void>; report: (error: unknown) => void;
}) {
  const [status, setStatus] = useState(item.parent_status);
  const [note, setNote] = useState(item.parent_note);
  const [saving, setSaving] = useState(false);
  async function save() {
    setSaving(true);
    try {
      await request(`/students/${studentId}/learning/${encodeURIComponent(item.code)}`,
        { subject: item.subject, label: item.label, status, note }, 'PUT');
      await refresh();
    } catch (error) { report(error); }
    finally { setSaving(false); }
  }
  return <article className="memory-card">
    <b>{item.subject} · {item.label}</b>
    <small>{item.code} · 明確求助 {item.help_count} 次 · 可見進展 {item.step_count} 次 · 反覆修改 {item.revision_count} 次</small>
    <label>家長判斷
      <select value={status} onChange={e => setStatus(e.target.value)}>
        <option value="unassessed">尚未判定</option>
        <option value="practicing">正在練習</option>
        <option value="needs_review">需要多練</option>
        <option value="parent_confirmed">家長確認已熟悉</option>
      </select>
    </label>
    <label>適合的引導方式或補充說明
      <textarea value={note} maxLength={500} onChange={e => setNote(e.target.value)} placeholder="例如：先用實物示範，再畫圖。" />
    </label>
    <button disabled={saving} onClick={() => void save()}>{saving ? '儲存中' : '儲存教學註記'}</button>
  </article>;
}

function EvidenceRow({ item, studentId, refresh, report }: {
  item: Evidence; studentId: string; refresh: () => Promise<void>; report: (error: unknown) => void;
}) {
  const [quote, setQuote] = useState(item.quote);
  const [saving, setSaving] = useState(false);
  async function change(exclude: boolean) {
    if (exclude && !confirm('排除這筆證據？之後教學將不再使用它。')) return;
    setSaving(true);
    try {
      const route = `/students/${studentId}/learning/evidence/${item.id}`;
      if (exclude) await request(route, undefined, 'DELETE');
      else await request(route, { quote: quote.trim() }, 'PATCH');
      await refresh();
    } catch (error) { report(error); }
    finally { setSaving(false); }
  }
  const labels: Record<string,string> = {
    asked_for_help: '孩子明確求助', observed_step: '鏡頭觀察到進展',
    repeated_revision: '鏡頭觀察到反覆修改',
  };
  return <article className="memory-evidence">
    <b>{labels[item.kind] ?? '學習線索'} · {item.code}</b>
    <small>{new Date(item.created).toLocaleString('zh-TW')} · {item.origin === 'camera_observation' ? '鏡頭觀察' : item.origin === 'provider_transcript' ? '即時服務轉錄' : '孩子說話'}{item.parent_corrected ? ' · 家長已修正' : ''}</small>
    <textarea aria-label="修正證據內容" value={quote} maxLength={500} onChange={e => setQuote(e.target.value)} />
    <div className="button-row">
      <button disabled={saving || !quote.trim() || quote === item.quote} onClick={() => void change(false)}>儲存修正</button>
      <button disabled={saving} onClick={() => void change(true)}>排除這筆</button>
    </div>
  </article>;
}

export default function LearningMemory({ studentId, profile, evidence, refresh, report }: {
  studentId: string; profile: Concept[]; evidence: Evidence[];
  refresh: () => Promise<void>; report: (error: unknown) => void;
}) {
  return <section className="learning-memory">
    <h3>長期教學記憶</h3>
    <p className="muted-copy">以下整理求助與鏡頭觀察線索；影像判讀可能出錯。勾選作業完成不代表已學會，狀態由家長確認，並可隨時修正。</p>
    {profile.length ? profile.map(item =>
      <ConceptCard key={item.code} item={item} studentId={studentId} refresh={refresh} report={report}/>)
      : <p className="muted-copy">{studentId === 'student-test' ? '測試同學不累積長期概念紀錄與評估；課程摘要與工作區資料仍會保留。' : '目前還沒有可歸到課綱概念的長期紀錄。'}</p>}
    {evidence.length > 0 && <details className="memory-details">
      <summary>查看與修正原始證據（{evidence.length} 筆）</summary>
      {evidence.map(item => <EvidenceRow key={item.id} item={item} studentId={studentId} refresh={refresh} report={report}/>)}
    </details>}
  </section>;
}
