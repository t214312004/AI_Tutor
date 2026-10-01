import { useEffect, useRef, useState } from 'react';
import { Search, BookOpen } from 'lucide-react';
import { request } from './api';

type Hit = { title: string; url: string; page: number; excerpt: string; sha256: string };
type Concept = {
  code: string;
  subject: string;
  kind: string;
  grade_min: number;
  grade_max: number;
  page: number;
  summary: string;
  quality: string;
  source_title: string;
};
export default function CurriculumPanel({ grade }: { grade: number }) {
  const [coverage, setCoverage] = useState<{
    indexed: number;
    pages?: number;
    mapping_status: string;
    concept_catalog?: { unique_codes: number; subjects: number; review: string };
  } | null>(null);
  const [query, setQuery] = useState('乘法'),
    [subject, setSubject] = useState('數學'),
    [hits, setHits] = useState<Hit[]>([]),
    [concepts, setConcepts] = useState<Concept[]>([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [searched, setSearched] = useState(false);
  const searchVersion = useRef(0);
  useEffect(() => {
    void request('/curriculum/coverage')
      .then(setCoverage)
      .catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    searchVersion.current++;
    setHits([]);
    setConcepts([]);
    setSearched(false);
    setBusy(false);
  }, [grade, subject, query]);
  async function search() {
    const version = ++searchVersion.current;
    setBusy(true);
    setError('');
    try {
      const [original, mapped] = await Promise.all([
        request(
          '/curriculum/search?q=' +
            encodeURIComponent(query) +
            '&subject=' +
            encodeURIComponent(subject),
        ),
        subject
          ? request(
              '/curriculum/concepts?grade=' +
                grade +
                '&subject=' +
                encodeURIComponent(subject) +
                '&q=' +
                encodeURIComponent(query),
            )
          : Promise.resolve({ results: [] }),
      ]);
      if (version !== searchVersion.current) return;
      setHits(original.results);
      setConcepts(mapped.results);
      setSearched(true);
    } catch (e) {
      if (version === searchVersion.current)
        setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (version === searchVersion.current) setBusy(false);
    }
  }
  return (
    <div className="curriculum-panel">
      <div className="school-title">
        <BookOpen />
        <div>
          <h3>108 課綱資料庫</h3>
          <p>
            {coverage
              ? `${coverage.indexed} 份官方文件 · ${coverage.pages ?? 0} 頁可追溯來源`
              : '正在讀取匯入狀態'}
          </p>
        </div>
      </div>
      <p className="muted-copy">
        已按課綱編碼整理學習階段；數學可對應單一年級，其餘多為兩年一階段。自動擷取條目仍待逐條人工審核，不代表課本或習作全文。
      </p>
      {coverage?.concept_catalog && (
        <p className="muted-copy">
          {coverage.concept_catalog.unique_codes} 個來源編碼 · {coverage.concept_catalog.subjects} 個有編碼的領域
        </p>
      )}
      <label className="field-label">
        科目
        <select value={subject} onChange={(e) => setSubject(e.target.value)}>
          {[
            '數學',
            '國語文',
            '英語文',
            '自然科學',
            '社會',
            '生活',
            '藝術',
            '健康與體育',
            '綜合活動',
            '科技',
            '',
          ].map((s) => (
            <option value={s} key={s}>
              {s || '所有科目'}
            </option>
          ))}
        </select>
      </label>
      <label className="field-label">
        想找的學習內容
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && query.trim().length >= 2) void search();
          }}
          placeholder="例如：乘法、分數、閱讀理解"
        />
      </label>
      <button className="primary" disabled={busy || query.trim().length < 2} onClick={search}>
        <Search size={16} />
        {busy ? '查詢中' : '查詢課綱'}
      </button>
      {error && (
        <p role="alert" className="muted-copy">
          {error}
        </p>
      )}
      {searched && hits.length === 0 && concepts.length === 0 && (
        <p className="muted-copy">沒有找到符合的原文。請換個詞或擴大科目範圍。</p>
      )}
      {searched && concepts.length > 0 && (
        <div className="curriculum-hits">
          <h4>{grade} 年級相關學習條目</h4>
          {concepts.map((item) => (
            <article key={item.source_title + item.code + item.page}>
              <h4>{item.code} · {item.subject} · {item.kind}</h4>
              <span>
                適用 {item.grade_min === item.grade_max ? `${item.grade_min} 年級` : `${item.grade_min}–${item.grade_max} 年級`}
                {' · '}{item.source_title}，PDF 第 {item.page} 頁
              </span>
              <p>{item.summary || '已擷取編碼；說明文字待人工核對。'}</p>
            </article>
          ))}
        </div>
      )}
      {searched && hits.length > 0 && <h4>官方原文搜尋</h4>}
      <div className="curriculum-hits">
        {hits.map((h) => (
          <article key={h.sha256 + ':' + h.page}>
            <h4>{h.title}</h4>
            <span>來源 PDF 第 {h.page} 頁</span>
            <p>{h.excerpt}</p>
          </article>
        ))}
      </div>
    </div>
  );
}
