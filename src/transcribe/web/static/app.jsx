const { useState, useEffect, useRef, useCallback, useMemo } = React;

// ─── 表示用の定義 ──────────────────────────────────────────────────────────

const STATUS_LABEL = {
  queued: '待機中',
  downloading: 'ダウンロード中',
  separating: '音声分離中',
  transcribing: '文字起こし中',
  formatting: '出力中',
  done: '完了',
  failed: '失敗',
};

const STATUS_CLASS = {
  queued: 'status-queued',
  downloading: 'status-running',
  separating: 'status-running',
  transcribing: 'status-running',
  formatting: 'status-running',
  done: 'status-done',
  failed: 'status-failed',
};

const STAGE_ORDER = ['download', 'separate', 'transcribe', 'format', 'summarize', 'docs_sync', 'notion_sync'];

const STAGE_LABELS = {
  download: 'ダウンロード',
  separate: '音声分離',
  transcribe: '文字起こし',
  format: '出力',
  summarize: 'まとめ',
  docs_sync: 'Docs',
  notion_sync: 'Notion',
};

const STAGE_STATUS_LABEL = { done: '完了', failed: '失敗', running: '実行中', skipped: 'スキップ' };

const SOURCE_LABEL = { youtube: '体育動画', local: '叡智講義' };

const POST_STAGE_KEYS = { summarize: 'summarize', docs_sync: 'docs', notion_sync: 'notion' };

// ─── ユーティリティ ────────────────────────────────────────────────────────

const OFFLINE_MESSAGE = 'サーバーに接続できません。Web UI のサーバー（uv run transcribe web）が起動しているか確認してください。';

async function apiFetch(path, options = {}) {
  let res;
  try {
    res = await fetch(path, options);
  } catch (_) {
    // fetch 自体の失敗（"Failed to fetch"）はサーバー停止・ネットワーク断
    throw new Error(OFFLINE_MESSAGE);
  }
  if (!res.ok) {
    let msg = await res.text();
    try {
      const data = JSON.parse(msg);
      if (data.detail) msg = typeof data.detail === 'string' ? data.detail : data.detail.map(d => d.msg).join(' / ');
    } catch (_) {}
    throw new Error(msg || `HTTP ${res.status}`);
  }
  return res.json();
}

function postJson(path, body) {
  return apiFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
}

function storageGet(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : JSON.parse(v);
  } catch (_) {
    return fallback;
  }
}

function storageSet(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) {}
}

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function renderMarkdown(text, { highlightReview = false, highlightTerm = '' } = {}) {
  let src = text || '';
  if (highlightReview) {
    // 要確認（低信頼・無音疑い・繰り返し圧縮）の行をハイライトする
    src = src.split('\n').map(line => (
      line.startsWith('⚠️') ? `<mark class="review">${line.replace(/</g, '&lt;')}</mark>` : line
    )).join('\n');
  }
  if (highlightTerm) {
    src = src.replace(new RegExp(escapeRegExp(highlightTerm), 'g'), m => `<mark class="hit">${m}</mark>`);
  }
  const html = marked.parse(src, { breaks: true, gfm: true });
  const clean = DOMPurify.sanitize(html, { ADD_ATTR: ['target'] });
  // 外部リンク（YouTube のタイムスタンプ等）は別タブで開く
  return clean.replace(/<a href="http/g, '<a target="_blank" rel="noopener" href="http');
}

function isInProgress(job) {
  return job.in_progress;
}

function formatRemaining(seconds) {
  if (!isFinite(seconds) || seconds <= 0) return null;
  const min = Math.round(seconds / 60);
  if (min < 1) return '1 分未満';
  if (min < 60) return `約 ${min} 分`;
  return `約 ${Math.floor(min / 60)} 時間 ${min % 60} 分`;
}

// 進捗の伸び方から残り時間を推定する（ジョブごとに前回の観測値を保持）
const etaSamples = new Map();

function estimateRemaining(job) {
  if (job.status !== 'transcribing' || job.progress == null) {
    etaSamples.delete(job.id);
    return null;
  }
  const now = Date.now();
  const prev = etaSamples.get(job.id);
  if (!prev || job.progress < prev.progress) {
    etaSamples.set(job.id, { progress: job.progress, at: now, eta: null });
    return null;
  }
  if (job.progress > prev.progress) {
    const rate = (job.progress - prev.progress) / ((now - prev.at) / 1000);
    const eta = rate > 0 ? (1 - job.progress) / rate : null;
    // 直前の推定と平均して細かい上下を抑える
    const smoothed = prev.eta && eta ? prev.eta * 0.5 + eta * 0.5 : eta;
    etaSamples.set(job.id, { progress: job.progress, at: now, eta: smoothed });
    return formatRemaining(smoothed);
  }
  return prev.eta ? formatRemaining(prev.eta - (now - prev.at) / 1000) : null;
}

// ─── 共通コンポーネント ────────────────────────────────────────────────────

function StatusLabel({ status }) {
  return <span className={`status-label ${STATUS_CLASS[status] || ''}`}>{STATUS_LABEL[status] || status}</span>;
}

function ProgressBar({ value }) {
  if (value == null) {
    return <div className="progress-bar indeterminate"><div /></div>;
  }
  return <div className="progress-bar"><div style={{ width: `${Math.round(value * 100)}%` }} /></div>;
}

function Modal({ children, onClose, wide }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className={`modal${wide ? ' wide' : ''}`}>{children}</div>
    </div>
  );
}

function ConfirmModal({ title, children, confirmLabel, danger, onConfirm, onClose }) {
  return (
    <Modal onClose={onClose}>
      <h2>{title}</h2>
      <div style={{ lineHeight: 1.7 }}>{children}</div>
      <div className="modal-actions">
        <button onClick={onClose}>キャンセル</button>
        <button className={danger ? 'primary danger' : 'primary'} onClick={() => { onConfirm(); onClose(); }}>
          {confirmLabel}
        </button>
      </div>
    </Modal>
  );
}

function ContextMenu({ x, y, items, onClose }) {
  const ref = useRef(null);
  const [pos, setPos] = useState({ left: x, top: y });

  useEffect(() => {
    // 画面からはみ出さない位置に寄せる
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    setPos({
      left: Math.max(4, Math.min(x, window.innerWidth - rect.width - 8)),
      top: Math.max(4, Math.min(y, window.innerHeight - rect.height - 8)),
    });
  }, [x, y]);

  useEffect(() => {
    const close = () => onClose();
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('click', close);
    window.addEventListener('resize', close);
    window.addEventListener('keydown', onKey);
    window.addEventListener('scroll', close, true);
    return () => {
      window.removeEventListener('click', close);
      window.removeEventListener('resize', close);
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', close, true);
    };
  }, [onClose]);

  return (
    <div ref={ref} className="menu context-menu" style={{ left: pos.left, top: pos.top }}
      onContextMenu={e => e.preventDefault()}>
      {items.map((item, i) => (
        item.separator
          ? <hr key={`sep-${i}`} />
          : (
            <button key={item.label} className={item.danger ? 'danger' : ''}
              onClick={() => { onClose(); item.onClick(); }}>
              {item.label}{item.sub && <small>{item.sub}</small>}
            </button>
          )
      ))}
    </div>
  );
}

function Toasts({ toasts }) {
  return (
    <div className="toasts">
      {toasts.map(t => <div key={t.id} className={`toast ${t.kind || ''}`}>{t.text}</div>)}
    </div>
  );
}

// ─── 使い方 ────────────────────────────────────────────────────────────────

function HelpModal({ onClose }) {
  return (
    <Modal onClose={onClose} wide>
      <h2>使い方</h2>
      <p>体育指導の YouTube 動画や叡智講義の録音ファイルを入れておくと、<b>文字起こし → まとめ作成 → Notion / Google Docs への登録</b>まで自動で進みます。やることは 3 つだけです。</p>
      <div className="flow" style={{ margin: '14px 0' }}>
        <div className="flow-step"><span className="num">1</span><b>入れる</b><p>「＋ 追加」から URL を貼るか、音声ファイル（mp3 / m4a）をドロップして「追加して開始」。</p></div>
        <div className="flow-step"><span className="num">2</span><b>待つ</b><p>1 件ずつ順番に自動処理されます。進み具合は左の「処理中」で確認できます。</p></div>
        <div className="flow-step"><span className="num">3</span><b>確認する</b><p>左の「要対応」に出たものだけ開いて対処します。何も出ていなければ作業は終わりです。</p></div>
      </div>
      <h3 style={{ fontSize: 13, marginTop: 8 }}>ことば</h3>
      <dl>
        <dt>体育動画 / 叡智講義</dt><dd>YouTube の URL は体育指導の動画、録音ファイル（mp3 / m4a）は叡智講義として扱います。まとめの形式と Notion の登録先（体育動画まとめDB / 叡智まとめDB）が分かれます。</dd>
        <dt>まとめ</dt><dd>文字起こしをもとに AI が作る構造化された要約。Notion / Docs に登録されるのはこれです。</dd>
        <dt>要対応</dt><dd>失敗した・まとめがないなど、あなたの操作が必要なジョブ。</dd>
        <dt>要確認箇所</dt><dd>聞き取りの自信が低い部分。文字起こし画面で黄色く表示されます。必要なら直してください。</dd>
        <dt>用語辞書</dt><dd>よく間違える言葉の正しい表記。文字起こしで言葉を選択するとその場で登録でき、次回から自動で直ります。</dd>
        <dt>再開</dt><dd>失敗した段階から続きを実行します（文字起こし済みならやり直しません）。</dd>
        <dt>最初からやり直す</dt><dd>ダウンロードから全部やり直します。設定を変えて作り直したいとき用です。</dd>
      </dl>
      <p style={{ marginTop: 12, color: 'var(--text-dim)' }}>設定が正しいかは右上の「設定状況」で確認できます。コマンドラインでは <code>uv run transcribe doctor</code> でも確認できます。</p>
      <div className="modal-actions"><button className="primary" onClick={onClose}>はじめる</button></div>
    </Modal>
  );
}

// ─── ヘッダー ──────────────────────────────────────────────────────────────

function QueueIndicator({ jobs, queue, onOpenJob, onCancel }) {
  const [open, setOpen] = useState(false);
  const running = jobs.find(j => ['downloading', 'separating', 'transcribing', 'formatting'].includes(j.status));
  const pending = queue.pending || [];
  const busy = Boolean(running) || Boolean(queue.running);

  if (!busy && pending.length === 0) {
    return <span className="queue-indicator">待機中のジョブはありません</span>;
  }
  const pct = running && running.progress != null ? ` ${Math.round(running.progress * 100)}%` : '';
  return (
    <span style={{ position: 'relative' }}>
      <span className={`queue-indicator${busy ? ' busy' : ''}`} style={{ cursor: 'pointer' }}
        onClick={() => (pending.length ? setOpen(o => !o) : running && onOpenJob(running.id))}
        title={pending.length ? 'クリックで待機中の一覧を開く' : 'クリックで処理中のジョブを開く'}>
        {busy && <span className="spinner" />}
        {running
          ? <>#{running.id} {STATUS_LABEL[running.status]}{pct}{running.eta ? ` ・ 残り ${running.eta}` : ''}</>
          : queue.running ? <>処理中: {queue.running.label}</> : '処理待ち'}
        {pending.length > 0 && <> ・ 待ち {pending.length} 件 ▾</>}
      </span>
      {open && pending.length > 0 && (
        <div className="menu" style={{ left: 0, right: 'auto', minWidth: 300 }} onMouseLeave={() => setOpen(false)}>
          {queue.running && (
            <div style={{ padding: '6px 10px', fontSize: 11.5, color: 'var(--text-dim)' }}>
              実行中: {queue.running.label}（取り消せません）
            </div>
          )}
          {pending.map((t, i) => (
            <div key={t.task_id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px' }}>
              <span style={{ flex: 1, fontSize: 12 }}>{i + 1}. {t.label}</span>
              <button onClick={() => { onCancel(t.task_id); setOpen(false); }}>取り消す</button>
            </div>
          ))}
        </div>
      )}
    </span>
  );
}

// ─── サイドバー: ジョブ一覧 ────────────────────────────────────────────────

function ResultIcons({ job, enabledPost }) {
  if (job.status !== 'done') return null;
  const items = [['summarize', 'まとめ', 'summarized_at'], ['notion_sync', 'Notion', 'notion_synced_at'], ['docs_sync', 'Docs', 'synced_at']]
    .filter(([stage]) => enabledPost.includes(stage));
  return (
    <span className="result-icons">
      {items.map(([stage, label, col]) => {
        const st = job.stages?.[stage]?.status;
        const ok = st === 'done' || (!st && job[col]);
        const cls = st === 'failed' ? 'failed' : ok ? 'done' : 'none';
        const mark = st === 'failed' ? '✗' : ok ? '✓' : '–';
        return <span key={stage} className={`result-icon ${cls}`} title={`${label}: ${cls === 'done' ? '完了' : cls === 'failed' ? '失敗' : '未実行'}`}>{label}{mark}</span>;
      })}
    </span>
  );
}

function JobRow({ job, selected, onSelect, onContextMenu, enabledPost }) {
  const attention = job.attention?.length > 0;
  return (
    <div className={`job-row${selected ? ' selected' : ''}${attention ? ' has-attention' : ''}`}
      onClick={() => onSelect(job.id)}
      onContextMenu={e => { e.preventDefault(); onContextMenu(job, e); }}
      title="右クリックで操作メニュー">
      <div className="job-row-title" title={job.title || job.url}>{job.title || job.url}</div>
      <div className="job-row-meta">
        <StatusLabel status={job.status} />
        <span>#{job.id}</span>
        <span className={`kind-label kind-${job.source_type}`}>{SOURCE_LABEL[job.source_type] || job.source_type}</span>
        {job.recording_date && job.recording_date !== '不明' && <span>{job.recording_date}</span>}
        <ResultIcons job={job} enabledPost={enabledPost} />
        {job.low_confidence_count > 0 && (
          <span className="review-badge" title="聞き取りの自信が低い箇所の数（文字起こし画面で黄色表示）">要確認 {job.low_confidence_count}</span>
        )}
      </div>
      {job.status === 'transcribing' && (
        <>
          <ProgressBar value={job.progress} />
          {job.eta && <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>残り {job.eta}</div>}
        </>
      )}
      {['downloading', 'separating', 'formatting'].includes(job.status) && <ProgressBar value={null} />}
      {attention && <div className="job-row-attention">⚠ {job.attention.join(' / ')}</div>}
    </div>
  );
}

const FILTERS = [
  { key: 'attention', label: '要対応', match: j => j.attention?.length > 0, empty: '対応が必要なジョブはありません 🎉' },
  { key: 'progress', label: '処理中', match: j => isInProgress(j), empty: '処理中のジョブはありません。「＋ 追加」から素材を入れてください。' },
  { key: 'done', label: '完了', match: j => j.status === 'done' && !(j.attention?.length > 0), empty: '完了したジョブはまだありません' },
  { key: 'all', label: 'すべて', match: () => true, empty: 'ジョブはまだありません。「＋ 追加」から始めましょう。' },
];

function Sidebar({ jobs, filter, setFilter, selectedId, onSelect, onJobContextMenu, enabledPost }) {
  const [query, setQuery] = useState('');
  const active = FILTERS.find(f => f.key === filter) || FILTERS[3];
  const q = query.trim().toLowerCase();
  const visible = jobs
    .filter(active.match)
    .filter(j => !q || (j.title || '').toLowerCase().includes(q) || (j.url || '').toLowerCase().includes(q));

  return (
    <aside className="sidebar">
      <div className="sidebar-top">
        <div className="filter-tabs">
          {FILTERS.map(f => {
            const count = jobs.filter(f.match).length;
            return (
              <button key={f.key} className={`filter-tab ${f.key}${filter === f.key ? ' active' : ''}`} onClick={() => setFilter(f.key)}>
                {f.label}{f.key !== 'all' && count > 0 && <span className="count">{count}</span>}
              </button>
            );
          })}
        </div>
        <input type="search" placeholder="タイトル・URL で検索" value={query} onChange={e => setQuery(e.target.value)} />
      </div>
      <div className="job-list">
        {visible.length === 0
          ? <div className="list-empty">{q ? '該当するジョブはありません' : active.empty}</div>
          : visible.map(job => (
            <JobRow key={job.id} job={job} selected={selectedId === job.id} onSelect={onSelect}
              onContextMenu={onJobContextMenu} enabledPost={enabledPost} />
          ))}
      </div>
    </aside>
  );
}

// ─── 追加画面 ──────────────────────────────────────────────────────────────

function AddView({ health, onAdded, notify, isFirstUse, onOpenHelp }) {
  const [urls, setUrls] = useState('');
  const [files, setFiles] = useState([]);
  const [dragOver, setDragOver] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const fileRef = useRef(null);

  const addFiles = (list) => {
    const accepted = Array.from(list).filter(f => /\.(mp3|m4a)$/i.test(f.name));
    const rejected = list.length - accepted.length;
    if (rejected > 0) notify(`${rejected} 件は対応していない形式のため除外しました（mp3 / m4a のみ）`, 'error');
    setFiles(prev => [...prev, ...accepted.map(f => ({ id: `${f.name}-${f.size}-${Math.random()}`, file: f }))]);
  };

  const urlList = urls.split('\n').map(s => s.trim()).filter(Boolean);
  const total = urlList.length + files.length;

  const submit = async () => {
    if (total === 0 || submitting) return;
    setSubmitting(true);
    let ok = 0;
    try {
      if (urlList.length) {
        await postJson('/api/run', { urls: urlList });
        ok += urlList.length;
        setUrls('');
      }
      for (const item of files) {
        const form = new FormData();
        form.append('audio', item.file);
        try {
          const res = await apiFetch('/api/run/file', { method: 'POST', body: form });
          setFiles(prev => prev.filter(f => f.id !== item.id));
          if (res.duplicate_job_id) {
            notify(`${item.file.name} は登録済みです（#${res.duplicate_job_id}「${res.duplicate_title}」）。重複しないよう追加しませんでした。`);
          } else {
            ok += 1;
          }
        } catch (e) {
          notify(`${item.file.name}: ${e.message}`, 'error');
        }
      }
      if (ok > 0) {
        notify(`${ok} 件を追加しました。順番に自動で処理されます。`, 'success');
        onAdded();
      } else {
        onAdded();
      }
    } catch (e) {
      notify(`追加できませんでした: ${e.message}`, 'error');
    } finally {
      setSubmitting(false);
    }
  };

  const status = key => health.find(c => c.key === key)?.status;
  const integrations = [
    ['まとめ作成', 'summarize'],
    ['Notion 登録', 'notion'],
    ['Google Docs 登録', 'docs'],
    ['完了メール', 'notification'],
  ];

  return (
    <div className="view">
      <div className="view-narrow">
        <h2>素材を追加</h2>
        <p className="lead">体育指導の YouTube URL を貼るか、叡智講義の録音ファイルをドロップして「追加して開始」を押すだけです。あとは自動で進みます。</p>

        <div className="card add-card section">
          <textarea
            placeholder={'体育指導の YouTube URL（1 行に 1 件、複数可）\nhttps://www.youtube.com/watch?v=...'}
            value={urls}
            onChange={e => setUrls(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submit(); }}
          />
          <div
            className={`drop-area${dragOver ? ' drag-over' : ''}`}
            onDragOver={e => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={e => { e.preventDefault(); setDragOver(false); addFiles(e.dataTransfer.files); }}
          >
            叡智講義の録音ファイル（mp3 / m4a）をここにドロップ、または
            <label className="file-btn">
              ファイルを選択
              <input ref={fileRef} type="file" accept=".mp3,.m4a" multiple style={{ display: 'none' }}
                onChange={e => { addFiles(e.target.files); e.target.value = ''; }} />
            </label>
          </div>
          {files.length > 0 && (
            <ul className="pending-files">
              {files.map(f => (
                <li key={f.id}>
                  🎵 <span>{f.file.name}</span>
                  <small style={{ color: 'var(--text-dim)' }}>{(f.file.size / 1024 / 1024).toFixed(1)} MB</small>
                  <button className="ghost" onClick={() => setFiles(prev => prev.filter(x => x.id !== f.id))} title="取り消す">✕</button>
                </li>
              ))}
            </ul>
          )}
          <div className="add-actions">
            <button className="primary large" onClick={submit} disabled={total === 0 || submitting}>
              {submitting ? '追加中…' : total > 0 ? `追加して開始（${total} 件）` : '追加して開始'}
            </button>
            <span className="hint">Ctrl + Enter でも追加できます。同じ動画を再度追加しても二重には処理されません。</span>
          </div>
        </div>

        <div className="section">
          <h3>追加したあとの流れ</h3>
          <div className="flow">
            <div className="flow-step">
              <span className="num">1</span><b>自動で処理</b>
              <p>ダウンロード → 文字起こし → 以下の有効な処理。1 件ずつ順番に進みます。</p>
              <p><b>YouTube</b> は体育指導、<b>録音ファイル</b>は叡智講義として、それぞれ専用の形式でまとめ、Notion の別の DB に登録します。</p>
              <div className="chips">
                {integrations.map(([label, key]) => (
                  <span key={key} className={`chip${['ok', 'warn'].includes(status(key)) ? ' on' : ''}`}
                    title={['ok', 'warn'].includes(status(key)) ? '有効' : '無効または未設定（設定状況を確認）'}>
                    {['ok', 'warn'].includes(status(key)) ? '✓' : '–'} {label}
                  </span>
                ))}
              </div>
            </div>
            <div className="flow-step">
              <span className="num">2</span><b>進み具合を見る</b>
              <p>左の「処理中」タブと右上の表示で確認できます。画面を閉じても処理は続きます。</p>
            </div>
            <div className="flow-step">
              <span className="num">3</span><b>要対応だけ確認</b>
              <p>失敗やまとめ漏れがあると左の「要対応」に出ます。誤認識は文字起こし画面で直せます。</p>
            </div>
          </div>
        </div>

        {isFirstUse && (
          <div className="banner info">
            <div className="banner-body">はじめて使う場合は、まず「使い方」と「設定状況」を確認してください。</div>
            <button onClick={onOpenHelp}>使い方を見る</button>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── ジョブ詳細 ────────────────────────────────────────────────────────────

function StageTrack({ stages, job }) {
  const byStage = Object.fromEntries((stages || []).map(s => [s.stage, s]));
  const shown = STAGE_ORDER.filter(s => byStage[s] && !(s === 'separate' && byStage[s].status === 'skipped' && !byStage.transcribe));
  if (shown.length === 0) {
    return <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>処理の記録はありません（この機能の導入前に処理されたジョブです）</span>;
  }
  return (
    <div className="stage-track">
      {shown.map((s, i) => {
        const st = byStage[s];
        const pct = st.status === 'running' && st.progress != null ? ` ${Math.round(st.progress * 100)}%` : '';
        return (
          <React.Fragment key={s}>
            {i > 0 && <span className="stage-sep">›</span>}
            <span className={`stage-step ${st.status}`}
              title={[`試行 ${st.attempts} 回`, st.finished_at ? `終了 ${st.finished_at}` : '', st.error || ''].filter(Boolean).join('\n')}>
              {st.status === 'running' && <span className="spinner" />}
              {STAGE_LABELS[s]}{st.status === 'done' ? ' ✓' : st.status === 'failed' ? ' ✗' : st.status === 'skipped' ? ' –' : ''}{pct}
              {st.attempts > 1 && <small>×{st.attempts}</small>}
            </span>
          </React.Fragment>
        );
      })}
    </div>
  );
}

function AddToGlossaryModal({ text, jobId, onClose, onSaved, notify }) {
  const [pattern, setPattern] = useState(text);
  const [replacement, setReplacement] = useState('');
  const [apply, setApply] = useState(true);
  const [saving, setSaving] = useState(false);

  const save = async () => {
    if (!pattern || !replacement) return;
    setSaving(true);
    try {
      const res = await postJson('/api/glossary/substitutions', {
        pattern, replacement, type: 'literal', apply_to_job_id: apply ? jobId : null,
      });
      notify(apply
        ? `用語辞書に登録し、この文字起こしの ${res.replaced} か所を修正しました。まとめに反映するには「まとめを作り直す」を実行してください。`
        : '用語辞書に登録しました。次回の文字起こしから自動で修正されます。', 'success');
      onSaved();
      onClose();
    } catch (e) {
      notify(`登録できませんでした: ${e.message}`, 'error');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal onClose={onClose}>
      <h2>誤認識を用語辞書に登録</h2>
      <p style={{ color: 'var(--text-dim)' }}>登録した言葉は、次回からの文字起こしで自動的に正しい表記に置き換わります。</p>
      <div className="field">
        <label>誤って認識された言葉</label>
        <input type="text" value={pattern} onChange={e => setPattern(e.target.value)} />
      </div>
      <div className="field">
        <label>正しい表記</label>
        <input type="text" value={replacement} autoFocus onChange={e => setReplacement(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') save(); }} placeholder="例: 臍下丹田" />
      </div>
      <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12 }}>
        <input type="checkbox" checked={apply} onChange={e => setApply(e.target.checked)} />
        この文字起こしにもすぐ反映する
      </label>
      <div className="modal-actions">
        <button onClick={onClose}>キャンセル</button>
        <button className="primary" onClick={save} disabled={!pattern || !replacement || saving}>{saving ? '登録中…' : '登録'}</button>
      </div>
    </Modal>
  );
}

function TranscriptTab({ jobId, version, notify }) {
  const [content, setContent] = useState(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [selection, setSelection] = useState(null);
  const [menu, setMenu] = useState(null);
  const [highlight, setHighlight] = useState('');
  const [glossaryText, setGlossaryText] = useState(null);
  const bodyRef = useRef(null);

  const load = useCallback(() => {
    apiFetch(`/api/jobs/${jobId}/transcript`).then(d => setContent(d.content)).catch(() => setContent(''));
  }, [jobId]);

  useEffect(() => { setEditing(false); setContent(null); load(); }, [load, version]);

  const selectedWord = () => {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || !bodyRef.current) return null;
    const range = sel.getRangeAt(0);
    // ウィンドウが非フォーカスのとき sel.toString() は空になるため range から取る
    const text = range.toString().trim();
    if (!text || text.length > 40 || text.includes('\n')) return null;
    if (!bodyRef.current.contains(range.commonAncestorContainer)) return null;
    return { range, text };
  };

  const showSelectionButton = () => {
    const picked = selectedWord();
    if (!picked) { setSelection(null); return; }
    const rect = picked.range.getBoundingClientRect();
    const scroller = bodyRef.current.closest('.tab-body');
    const base = scroller.getBoundingClientRect();
    // 画面下端で見切れないよう、下に余裕がなければ選択範囲の上に出す
    const below = base.bottom - rect.bottom > 48;
    setSelection({
      text: picked.text,
      top: (below ? rect.bottom + 6 : rect.top - 34) - base.top + scroller.scrollTop,
      left: Math.min(rect.left - base.left + scroller.scrollLeft, scroller.clientWidth - 240),
    });
  };

  const onContextMenu = (e) => {
    const picked = selectedWord();
    if (!picked || editing) return;  // 選択していないときはブラウザ標準のメニューを出す
    e.preventDefault();
    const text = picked.text;
    const hits = (content.split(text).length - 1);
    setSelection(null);
    setMenu({
      x: e.clientX, y: e.clientY,
      items: [
        { label: `「${text}」を用語辞書に登録`, sub: '次回以降の文字起こしでも自動で直ります', onClick: () => setGlossaryText(text) },
        {
          label: highlight === text ? 'この語の強調をやめる' : `この語を本文で探す（${hits} か所）`,
          onClick: () => setHighlight(highlight === text ? '' : text),
        },
        { label: 'コピー', onClick: () => navigator.clipboard?.writeText(text).catch(() => {}) },
      ],
    });
  };

  // Esc で登録ボタンを閉じる
  useEffect(() => {
    if (!selection) return;
    const onKey = (e) => { if (e.key === 'Escape') setSelection(null); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selection]);

  const save = async () => {
    try {
      await apiFetch(`/api/jobs/${jobId}/transcript`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: draft }),
      });
      setContent(draft);
      setEditing(false);
      notify('文字起こしを保存しました。まとめに反映するには「まとめを作り直す」を実行してください。', 'success');
    } catch (e) {
      notify(`保存できませんでした: ${e.message}`, 'error');
    }
  };

  const html = useMemo(
    () => renderMarkdown(content, { highlightReview: true, highlightTerm: highlight }),
    [content, highlight],
  );

  if (content === null) return <div className="empty-state">読み込み中…</div>;
  if (!content) return <div className="empty-state">文字起こしはまだありません。</div>;

  return (
    <>
      <div className="tab-toolbar">
        {editing ? (
          <>
            <button className="primary" onClick={save}>保存</button>
            <button onClick={() => setEditing(false)}>キャンセル</button>
            <span className="hint">Markdown をそのまま編集できます。</span>
          </>
        ) : (
          <>
            <button onClick={() => { setDraft(content); setEditing(true); setSelection(null); }}>直接編集</button>
            {highlight && <button onClick={() => setHighlight('')}>「{highlight}」の強調を消す</button>}
            <span className="hint">誤認識した言葉を<b>選択</b>すると、用語辞書に登録できます（<b>右クリック</b>で検索・コピーも）。<mark className="review" style={{ background: 'var(--warn-soft)', color: 'inherit' }}>黄色</mark>は要確認箇所です。</span>
          </>
        )}
      </div>
      {editing
        ? <textarea className="edit-area" value={draft} onChange={e => setDraft(e.target.value)} />
        : (
          <div ref={bodyRef} className="markdown"
            onMouseUp={showSelectionButton}
            onTouchEnd={() => setTimeout(showSelectionButton, 0)}
            onContextMenu={onContextMenu}
            dangerouslySetInnerHTML={{ __html: html }} />
        )}
      {menu && <ContextMenu {...menu} onClose={() => setMenu(null)} />}
      {selection && !editing && (
        <button className="selection-pop" style={{ top: selection.top, left: selection.left }}
          onMouseDown={e => e.preventDefault()}
          onClick={() => { setGlossaryText(selection.text); setSelection(null); }}>
          「{selection.text}」を用語辞書に登録
        </button>
      )}
      {glossaryText !== null && (
        <AddToGlossaryModal text={glossaryText} jobId={jobId} notify={notify}
          onClose={() => setGlossaryText(null)} onSaved={load} />
      )}
    </>
  );
}

function SummaryTab({ jobId, version, job, summarizeEnabled, onSummarize }) {
  const [content, setContent] = useState(null);

  useEffect(() => {
    setContent(null);
    if (!job?.has_summary) { setContent(''); return; }
    apiFetch(`/api/jobs/${jobId}/summary`).then(d => setContent(d.content)).catch(() => setContent(''));
  }, [jobId, version, job?.has_summary]);

  const html = useMemo(() => renderMarkdown(content), [content]);

  if (content === null) return <div className="empty-state">読み込み中…</div>;
  if (!content) {
    if (job?.summary_truncated) {
      return (
        <div className="empty-state">
          まとめが出力上限に達して途中で切れたため、保存されていません。<br />
          config.yaml の <code>summarize.max_output_tokens</code> を増やしてから作り直してください。
          {onSummarize && <div style={{ marginTop: 8 }}><button className="primary" onClick={onSummarize}>まとめを作り直す</button></div>}
        </div>
      );
    }
    if (job?.status !== 'done') return <div className="empty-state">文字起こしが終わると、自動でまとめが作成されます。</div>;
    return (
      <div className="empty-state">
        まとめはまだありません。<br />
        {summarizeEnabled
          ? <button className="primary" style={{ marginTop: 8 }} onClick={onSummarize}>まとめを作成</button>
          : <>まとめ生成が無効か未設定です。右上の「設定状況」を確認してください。</>}
      </div>
    );
  }
  return <div className="markdown" dangerouslySetInnerHTML={{ __html: html }} />;
}

function HistoryTab({ job, stages }) {
  return (
    <div>
      <div className="section">
        <h3>処理の記録</h3>
        {(stages || []).length === 0
          ? <p className="empty-state" style={{ padding: 0 }}>記録はありません（この機能の導入前に処理されたジョブです）。</p>
          : (
            <table className="history-table">
              <thead><tr><th>段階</th><th>状態</th><th>試行</th><th>開始</th><th>終了</th><th>エラー</th></tr></thead>
              <tbody>
                {STAGE_ORDER.filter(s => stages.some(x => x.stage === s)).map(s => {
                  const st = stages.find(x => x.stage === s);
                  return (
                    <tr key={s}>
                      <td>{STAGE_LABELS[s]}</td>
                      <td>{STAGE_STATUS_LABEL[st.status] || st.status}{st.status === 'running' && st.progress != null ? ` ${Math.round(st.progress * 100)}%` : ''}</td>
                      <td>{st.attempts}</td>
                      <td>{st.started_at || ''}</td>
                      <td>{st.finished_at || ''}</td>
                      <td className="err">{st.error || ''}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
      </div>
      <div className="section">
        <h3>ジョブ情報</h3>
        <table className="history-table">
          <tbody>
            <tr><th>ID</th><td>{job.id}</td></tr>
            <tr><th>ソース</th><td style={{ wordBreak: 'break-all' }}>{job.url}</td></tr>
            <tr><th>追加日時</th><td>{job.created_at}</td></tr>
            <tr><th>最終更新</th><td>{job.updated_at}</td></tr>
            <tr><th>出力フォルダ</th><td style={{ wordBreak: 'break-all' }}>{job.output_dir || '-'}</td></tr>
            <tr><th>リトライ回数</th><td>{job.retry_count}</td></tr>
          </tbody>
        </table>
        {job.error_message && (
          <>
            <h3 style={{ marginTop: 12 }}>エラー詳細</h3>
            <pre style={{ whiteSpace: 'pre-wrap', fontSize: 11.5, color: 'var(--failed)', background: 'var(--surface)', padding: 10, borderRadius: 6 }}>{job.error_message}</pre>
          </>
        )}
      </div>
    </div>
  );
}

function JobView({ jobId, listJob, enabledPost, notify, onChanged, onClosed }) {
  const [job, setJob] = useState(null);
  const [stages, setStages] = useState([]);
  const [tab, setTab] = useState(() => storageGet('transcribe.detailTab', 'summary'));
  const [menuOpen, setMenuOpen] = useState(false);
  const [confirm, setConfirm] = useState(null);
  const [deleteFiles, setDeleteFiles] = useState(false);
  const version = listJob?.updated_at;

  useEffect(() => { storageSet('transcribe.detailTab', tab); }, [tab]);

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/api/jobs/${jobId}`).then(j => { if (!cancelled) setJob(j); }).catch(() => {});
    apiFetch(`/api/jobs/${jobId}/stages`).then(s => { if (!cancelled) setStages(s); }).catch(() => {});
    return () => { cancelled = true; };
  }, [jobId, version, listJob?.progress]);

  useEffect(() => { setMenuOpen(false); }, [jobId]);

  if (!job || !listJob) return <div className="view"><div className="empty-state">読み込み中…</div></div>;

  const run = async (path, body, message) => {
    setMenuOpen(false);
    try {
      await postJson(path, body);
      notify(message, 'success');
      onChanged();
    } catch (e) {
      notify(`実行できませんでした: ${e.message}`, 'error');
    }
  };

  const attention = listJob.attention || [];
  const failedPost = attention.length > 0 && job.status === 'done';
  const inProgress = listJob.in_progress;
  const summarizeEnabled = enabledPost.includes('summarize');
  const isYoutube = /^https?:\/\//.test(job.url);

  let primary = null;
  if (job.status === 'failed') {
    primary = <button className="primary" onClick={() => run('/api/retry', { job_id: job.id }, '失敗したところから再開します')}>失敗したところから再開</button>;
  } else if (failedPost) {
    primary = <button className="primary" onClick={() => run('/api/resume-post', { job_id: job.id }, '後処理をやり直します')}>後処理をやり直す</button>;
  }

  const coreError = job.error_message ? job.error_message.split('\n')[0] : '';

  return (
    <div className="detail">
      <div className="detail-head">
        <div className="detail-title-row">
          <StatusLabel status={job.status} />
          <div className="detail-title" title={job.title || job.url}>{job.title || job.url}</div>
          <div className="detail-actions">
            {primary}
            <button onClick={() => setMenuOpen(o => !o)} disabled={inProgress} title={inProgress ? '処理中は操作できません' : 'その他の操作'}>その他 ▾</button>
            {menuOpen && (
              <div className="menu" onMouseLeave={() => setMenuOpen(false)}>
                {job.status === 'done' && summarizeEnabled && (
                  <button onClick={() => { setMenuOpen(false); setConfirm('summarize'); }}>
                    まとめを作り直す<small>文字起こしを直したあとに。AI の API を再度呼び出します</small>
                  </button>
                )}
                {job.status === 'done' && enabledPost.some(s => s !== 'summarize') && (
                  <button onClick={async () => {
                    setMenuOpen(false);
                    if (enabledPost.includes('notion_sync')) await run('/api/sync-notion', { job_id: job.id }, 'Notion に登録し直します');
                    if (enabledPost.includes('docs_sync')) await run('/api/sync', { job_id: job.id }, 'Google Docs に登録し直します');
                  }}>
                    Notion / Docs に登録し直す<small>まとめを作り直したあとに。既存ページを最新の内容で上書きします</small>
                  </button>
                )}
                {job.status === 'done' && (
                  <button onClick={() => run('/api/reformat', { job_id: job.id }, '書き起こしを作り直します')}>
                    書き起こしの表示を作り直す<small>文字起こしはやり直さず、区切りや見出しだけ作り直します</small>
                  </button>
                )}
                <button onClick={() => { setMenuOpen(false); setConfirm('rerun'); }}>
                  最初からやり直す<small>ダウンロードから全部やり直します（既存の出力はバックアップ）</small>
                </button>
                <hr />
                <button className="danger" onClick={() => { setMenuOpen(false); setConfirm('delete'); }}>
                  削除<small>一覧から削除します</small>
                </button>
              </div>
            )}
          </div>
          <button className="ghost" onClick={onClosed} title="閉じる">✕</button>
        </div>
        <div className="detail-sub">
          <span>#{job.id}</span>
          <span className={`kind-label kind-${job.source_type}`}>{SOURCE_LABEL[job.source_type] || job.source_type}</span>
          {listJob.recording_date && listJob.recording_date !== '不明' && <span>録画日 {listJob.recording_date}</span>}
          {isYoutube && <a href={job.url} target="_blank" rel="noopener">▶ 元の動画</a>}
          {job.notion_url && <a href={job.notion_url} target="_blank" rel="noopener">Notion で開く</a>}
          {listJob.low_confidence_count > 0 && <span className="review-badge">要確認 {listJob.low_confidence_count} 箇所</span>}
        </div>
        <StageTrack stages={stages} job={job} />
        {job.status === 'failed' && (
          <div className="banner error">
            <div className="banner-body"><b>処理に失敗しました。</b> {coreError}<br />
              <small>一時的な通信エラーなら「再開」で直ります。繰り返し失敗する場合は「処理の記録」タブでエラー詳細を確認してください。</small></div>
          </div>
        )}
        {failedPost && (
          <div className="banner error">
            <div className="banner-body">
              <b>文字起こしは完了していますが、対応が必要です。</b>
              <ul>{attention.map(a => <li key={a}>{a}</li>)}</ul>
            </div>
          </div>
        )}
        {job.summary_truncated && (
          <div className="banner error">
            <div className="banner-body">
              <b>まとめが途中で切れています。</b> AI の出力上限に達しました。<br />
              <small>config.yaml の summarize.max_output_tokens を増やしてから「その他 → まとめを作り直す」を実行してください。途中までの出力は出力フォルダの summary.truncated.md にあります。</small>
            </div>
          </div>
        )}
        {inProgress && (
          <div className="banner info">
            <div className="banner-body">
              {STATUS_LABEL[job.status]}{listJob.progress != null && job.status === 'transcribing' ? `（${Math.round(listJob.progress * 100)}%）` : ''}{listJob.eta ? ` ・ 残り ${listJob.eta}` : ''}。完了まで自動で進みます。
              <div style={{ marginTop: 6 }}><ProgressBar value={job.status === 'transcribing' ? listJob.progress : null} /></div>
            </div>
          </div>
        )}
      </div>

      <div className="tabs">
        <button className={`tab${tab === 'summary' ? ' active' : ''}`} onClick={() => setTab('summary')}>まとめ</button>
        <button className={`tab${tab === 'transcript' ? ' active' : ''}`} onClick={() => setTab('transcript')}>文字起こし</button>
        <button className={`tab${tab === 'history' ? ' active' : ''}`} onClick={() => setTab('history')}>処理の記録</button>
      </div>
      <div className="tab-body">
        {tab === 'summary' && (
          <SummaryTab jobId={job.id} version={version} job={job} summarizeEnabled={summarizeEnabled}
            onSummarize={() => run('/api/summarize', { job_id: job.id }, 'まとめを作成します')} />
        )}
        {tab === 'transcript' && (job.has_transcript
          ? <TranscriptTab jobId={job.id} version={version} notify={notify} />
          : <div className="empty-state">文字起こしが終わるとここに表示されます。</div>)}
        {tab === 'history' && <HistoryTab job={job} stages={stages} />}
      </div>

      {confirm === 'summarize' && (
        <ConfirmModal title="まとめを作り直しますか？" confirmLabel="作り直す" onClose={() => setConfirm(null)}
          onConfirm={() => run('/api/summarize', { job_id: job.id }, 'まとめを作り直します')}>
          現在の文字起こしから、まとめを作り直します。AI の API を呼び出すため、利用料金が発生します。<br />
          完了後、Notion / Docs にも登録し直す場合は「Notion / Docs に登録し直す」を実行してください。
        </ConfirmModal>
      )}
      {confirm === 'rerun' && (
        <ConfirmModal title="最初からやり直しますか？" confirmLabel="やり直す" onClose={() => setConfirm(null)}
          onConfirm={() => run('/api/rerun', { url_or_id: String(job.id) }, '最初からやり直します')}>
          ダウンロード・文字起こしからすべてやり直します。長い動画では時間がかかります。<br />
          今の出力フォルダはバックアップとして残ります。手で直した文字起こしは新しい結果に置き換わります。
        </ConfirmModal>
      )}
      {confirm === 'delete' && (
        <ConfirmModal title={`ジョブ #${job.id} を削除しますか？`} confirmLabel="削除" danger onClose={() => setConfirm(null)}
          onConfirm={async () => { await run('/api/delete', { job_id: job.id, files: deleteFiles }, '削除しました'); onClosed(); }}>
          一覧から削除します。Notion / Docs に登録済みのページは削除されません。
          <label style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 10 }}>
            <input type="checkbox" checked={deleteFiles} onChange={e => setDeleteFiles(e.target.checked)} />
            文字起こし・まとめのファイルも削除する
          </label>
        </ConfirmModal>
      )}
    </div>
  );
}

// ─── 用語辞書 ──────────────────────────────────────────────────────────────

function GlossaryView({ notify }) {
  const [data, setData] = useState(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [query, setQuery] = useState('');
  const [newSub, setNewSub] = useState({ pattern: '', replacement: '', type: 'literal' });
  const [newTerm, setNewTerm] = useState('');

  useEffect(() => { apiFetch('/api/glossary').then(setData).catch(e => notify(`用語辞書を読み込めません: ${e.message}`, 'error')); }, []);

  const change = (fn) => { setData(fn); setDirty(true); };

  const save = async () => {
    setSaving(true);
    try {
      await apiFetch('/api/glossary', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
      setDirty(false);
      notify('用語辞書を保存しました。次回の文字起こしから反映されます。', 'success');
    } catch (e) {
      notify(`保存できませんでした: ${e.message}`, 'error');
    } finally {
      setSaving(false);
    }
  };

  if (!data) return <div className="view"><div className="empty-state">読み込み中…</div></div>;

  const q = query.trim();
  const subs = data.substitutions.map((s, i) => [s, i]).filter(([s]) => !q || s.pattern.includes(q) || s.replacement.includes(q));

  return (
    <div className="view">
      <div className="view-narrow">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <h2 style={{ flex: 1 }}>用語辞書</h2>
          {dirty && <span style={{ color: 'var(--warn)', fontSize: 12 }}>未保存の変更があります</span>}
          <button className="primary" onClick={save} disabled={saving || !dirty}>{saving ? '保存中…' : '保存'}</button>
        </div>
        <p className="lead">よく間違って聞き取られる言葉と正しい表記を登録します。文字起こし画面で言葉を選択して登録することもできます。</p>

        <div className="section">
          <h3>話題のヒント</h3>
          <p className="lead" style={{ marginBottom: 6 }}>動画でよく出る専門用語を書いておくと、最初から正しく聞き取られやすくなります。</p>
          <textarea style={{ width: '100%', minHeight: 70, resize: 'vertical' }} value={data.context}
            onChange={e => { const v = e.target.value; change(d => ({ ...d, context: v })); }} />
        </div>

        <div className="section">
          <h3>誤認識の置き換え（{data.substitutions.length} 件）</h3>
          <div style={{ display: 'flex', gap: 6, marginBottom: 8, flexWrap: 'wrap' }}>
            <input type="text" style={{ flex: 1 }} placeholder="誤って認識された言葉" value={newSub.pattern} onChange={e => setNewSub(s => ({ ...s, pattern: e.target.value }))} />
            <input type="text" style={{ flex: 1 }} placeholder="正しい表記" value={newSub.replacement} onChange={e => setNewSub(s => ({ ...s, replacement: e.target.value }))} />
            <select value={newSub.type} onChange={e => setNewSub(s => ({ ...s, type: e.target.value }))} title="正規表現は上級者向け">
              <option value="literal">そのまま</option>
              <option value="regex">正規表現</option>
            </select>
            <button onClick={() => {
              if (!newSub.pattern) return;
              change(d => ({ ...d, substitutions: [{ ...newSub }, ...d.substitutions] }));
              setNewSub({ pattern: '', replacement: '', type: 'literal' });
            }}>追加</button>
          </div>
          <input type="search" style={{ width: '100%', marginBottom: 6 }} placeholder="登録済みの言葉を検索" value={query} onChange={e => setQuery(e.target.value)} />
          <table className="glossary-table">
            <thead><tr><th>誤って認識された言葉</th><th>正しい表記</th><th>種類</th><th /></tr></thead>
            <tbody>
              {subs.map(([s, i]) => (
                <tr key={i}>
                  <td><input type="text" value={s.pattern} onChange={e => { const v = e.target.value; change(d => { const a = [...d.substitutions]; a[i] = { ...a[i], pattern: v }; return { ...d, substitutions: a }; }); }} /></td>
                  <td><input type="text" value={s.replacement} onChange={e => { const v = e.target.value; change(d => { const a = [...d.substitutions]; a[i] = { ...a[i], replacement: v }; return { ...d, substitutions: a }; }); }} /></td>
                  <td>
                    <select value={s.type} onChange={e => { const v = e.target.value; change(d => { const a = [...d.substitutions]; a[i] = { ...a[i], type: v }; return { ...d, substitutions: a }; }); }}>
                      <option value="literal">そのまま</option>
                      <option value="regex">正規表現</option>
                    </select>
                  </td>
                  <td><button className="ghost" title="削除" onClick={() => change(d => ({ ...d, substitutions: d.substitutions.filter((_, idx) => idx !== i) }))}>✕</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="section">
          <h3>重要語</h3>
          <p className="lead" style={{ marginBottom: 6 }}>含まれる箇所に目印を付けたい言葉です。</p>
          <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
            <input type="text" style={{ flex: 1 }} placeholder="重要語を追加" value={newTerm} onChange={e => setNewTerm(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && newTerm.trim()) { change(d => ({ ...d, important_terms: [newTerm.trim(), ...d.important_terms] })); setNewTerm(''); } }} />
            <button onClick={() => { if (!newTerm.trim()) return; change(d => ({ ...d, important_terms: [newTerm.trim(), ...d.important_terms] })); setNewTerm(''); }}>追加</button>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {data.important_terms.map((term, i) => (
              <span key={`${term}-${i}`} className="term-chip">{term}
                <button onClick={() => change(d => ({ ...d, important_terms: d.important_terms.filter((_, idx) => idx !== i) }))}>✕</button>
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── 設定状況 ──────────────────────────────────────────────────────────────

function SettingsView({ health, reload, notify }) {
  const [testing, setTesting] = useState({});
  const [results, setResults] = useState({});
  const icons = { ok: '✓', warn: '!', error: '✗', off: '–' };

  const test = async (key) => {
    setTesting(t => ({ ...t, [key]: true }));
    try {
      const r = await postJson(`/api/health/test/${key}`);
      setResults(x => ({ ...x, [key]: r }));
    } catch (e) {
      notify(`接続テストに失敗しました: ${e.message}`, 'error');
    } finally {
      setTesting(t => ({ ...t, [key]: false }));
    }
  };

  const errors = health.filter(c => c.status === 'error').length;

  return (
    <div className="view">
      <div className="view-narrow">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <h2 style={{ flex: 1 }}>設定状況</h2>
          <button onClick={reload}>再確認</button>
        </div>
        <p className="lead">
          各機能が使える状態かを確認できます。設定はプロジェクト直下の <code>config.yaml</code> で変更します（保存すると自動で反映されます）。
          {errors > 0 && <><br /><b style={{ color: 'var(--failed)' }}>{errors} 件の設定に問題があります。</b></>}
        </p>
        <div className="check-list">
          {health.map(c => {
            const r = results[c.key];
            const shown = r || c;
            return (
              <div key={c.key} className="check-row">
                <span className={`check-icon ${shown.status}`}>{icons[shown.status]}</span>
                <div className="check-body">
                  <b>{c.label}</b>
                  <span className="detail">{shown.detail}</span>
                  {(r?.hint || (!r && c.hint)) && <span className="hint">{r?.hint || c.hint}</span>}
                </div>
                {c.testable && <button onClick={() => test(c.key)} disabled={testing[c.key]}>{testing[c.key] ? '確認中…' : '接続テスト'}</button>}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ─── ツール ────────────────────────────────────────────────────────────────

const IDB_NAME = 'transcribe-ui';
const IDB_STORE = 'handles';

function idbPut(handle) {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(IDB_NAME, 1);
    req.onupgradeneeded = e => e.target.result.createObjectStore(IDB_STORE);
    req.onsuccess = e => {
      const tx = e.target.result.transaction(IDB_STORE, 'readwrite');
      tx.objectStore(IDB_STORE).put(handle, 'convertDir');
      tx.oncomplete = resolve;
      tx.onerror = reject;
    };
    req.onerror = reject;
  });
}

function idbGet() {
  return new Promise(resolve => {
    const req = indexedDB.open(IDB_NAME, 1);
    req.onupgradeneeded = e => e.target.result.createObjectStore(IDB_STORE);
    req.onsuccess = e => {
      const get = e.target.result.transaction(IDB_STORE, 'readonly').objectStore(IDB_STORE).get('convertDir');
      get.onsuccess = () => resolve(get.result || null);
      get.onerror = () => resolve(null);
    };
    req.onerror = () => resolve(null);
  });
}

function M4aConverter({ notify }) {
  const [items, setItems] = useState([]);
  const [dirName, setDirName] = useState('');
  const [busy, setBusy] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const dirRef = useRef(null);
  const pickingRef = useRef(false);

  useEffect(() => { idbGet().then(h => { if (h) { dirRef.current = h; setDirName(h.name); } }); }, []);

  const pickDir = async () => {
    if (pickingRef.current) return;
    pickingRef.current = true;
    try {
      const h = await window.showDirectoryPicker({ mode: 'readwrite', ...(dirRef.current ? { startIn: dirRef.current } : {}) });
      dirRef.current = h;
      setDirName(h.name);
      await idbPut(h);
    } catch (e) {
      if (e.name !== 'AbortError') notify(`フォルダを選択できません: ${e.message}`, 'error');
    } finally {
      pickingRef.current = false;
    }
  };

  const convert = async () => {
    const targets = items.filter(i => i.status === 'pending');
    if (!targets.length) return;
    setBusy(true);
    try {
      if (!dirRef.current) {
        await pickDir();
        if (!dirRef.current) return;
      }
      for (const item of targets) {
        setItems(prev => prev.map(f => f.id === item.id ? { ...f, status: 'converting' } : f));
        try {
          const form = new FormData();
          form.append('files', item.file);
          const res = await fetch('/api/convert', { method: 'POST', body: form });
          if (!res.ok) throw new Error(await res.text());
          const blob = await res.blob();
          const name = item.file.name.replace(/\.m4a$/i, '.mp3');
          const fh = await dirRef.current.getFileHandle(name, { create: true });
          const w = await fh.createWritable();
          await w.write(blob);
          await w.close();
          setItems(prev => prev.map(f => f.id === item.id ? { ...f, status: 'done' } : f));
        } catch (e) {
          setItems(prev => prev.map(f => f.id === item.id ? { ...f, status: 'error' } : f));
          notify(`${item.file.name}: ${e.message}`, 'error');
        }
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card tool-item">
      <b>m4a → mp3 変換</b>
      <p>録音アプリの m4a を mp3 にして手元のフォルダに保存します。文字起こしだけが目的なら変換は不要です（m4a のまま追加できます）。</p>
      <div className={`drop-area${dragOver ? ' drag-over' : ''}`}
        onDragOver={e => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={e => {
          e.preventDefault(); setDragOver(false);
          const files = Array.from(e.dataTransfer.files).filter(f => /\.m4a$/i.test(f.name));
          setItems(prev => [...prev, ...files.map(f => ({ id: Math.random(), file: f, status: 'pending' }))]);
        }}>
        m4a をここにドロップ
      </div>
      {items.length > 0 && (
        <ul className="convert-list">
          {items.map(i => (
            <li key={i.id} className={i.status}>
              {{ pending: '○', converting: '⏳', done: '✓', error: '✗' }[i.status]} {i.file.name}
            </li>
          ))}
        </ul>
      )}
      <div className="btns">
        <button className="primary" onClick={convert} disabled={busy || !items.some(i => i.status === 'pending')}>変換して保存</button>
        <button onClick={pickDir} disabled={busy}>📁 {dirName || '保存先を選択'}</button>
        {items.length > 0 && <button onClick={() => setItems([])} disabled={busy}>クリア</button>}
      </div>
    </div>
  );
}

function ToolsView({ notify, onStarted, enabledPost }) {
  const [confirm, setConfirm] = useState(null);

  const run = async (path, body, message) => {
    try {
      await postJson(path, body);
      notify(message, 'success');
      onStarted();
    } catch (e) {
      notify(`実行できませんでした: ${e.message}`, 'error');
    }
  };

  const tools = [
    {
      key: 'summarize', stage: 'summarize', title: 'まとめを一括作成',
      desc: 'まとめがないジョブのまとめを作成します。',
      pending: () => run('/api/summarize', { all: false }, '未作成のまとめを作成します'),
      all: { label: '全件作り直す', confirm: 'すべての完了ジョブのまとめを作り直します。件数分の AI 利用料金が発生します。', action: () => run('/api/summarize', { all: true }, 'すべてのまとめを作り直します') },
    },
    {
      key: 'notion', stage: 'notion_sync', title: 'Notion に一括登録',
      desc: 'まだ Notion に登録していないジョブのまとめを登録します。',
      pending: () => run('/api/sync-notion', { all: false }, '未登録のジョブを Notion に登録します'),
      all: { label: '全件登録し直す', confirm: 'すべてのジョブを Notion に登録し直します（既存ページは上書き更新）。', action: () => run('/api/sync-notion', { all: true }, 'すべてのジョブを Notion に登録し直します') },
    },
    {
      key: 'docs', stage: 'docs_sync', title: 'Google Docs に一括登録',
      desc: 'まだ Google Docs に登録していないジョブを登録します。',
      pending: () => run('/api/sync', { all: false }, '未登録のジョブを Google Docs に登録します'),
      all: { label: '全件登録し直す', confirm: 'すべてのジョブを Google Docs に登録し直します。', action: () => run('/api/sync', { all: true }, 'すべてのジョブを Google Docs に登録し直します') },
    },
  ];

  return (
    <div className="view">
      <div className="view-narrow">
        <h2>ツール</h2>
        <p className="lead">通常は使う必要はありません。設定を後から有効にした場合や、まとめ方を変えて作り直したいときに使います。</p>

        <div className="section">
          <h3>一括処理</h3>
          <div className="tool-grid">
            {tools.map(t => {
              const enabled = enabledPost.includes(t.stage);
              return (
                <div key={t.key} className="card tool-item">
                  <b>{t.title}</b>
                  <p>{enabled ? t.desc : '無効または未設定です（設定状況を確認してください）。'}</p>
                  <div className="btns">
                    <button onClick={t.pending} disabled={!enabled}>未処理分を実行</button>
                    <button onClick={() => setConfirm(t.all)} disabled={!enabled}>{t.all.label}</button>
                  </div>
                </div>
              );
            })}
            <div className="card tool-item">
              <b>一時ファイルを削除</b>
              <p>作業フォルダ（data/work）のダウンロード済み音声と、処理が完了したジョブのアップロードファイル（data/uploads）を削除して容量を空けます。処理中は実行されません。</p>
              <div className="btns">
                <button onClick={() => setConfirm({ confirm: '一時ファイルを削除します。完了したジョブの「最初からやり直す」は、YouTube は再ダウンロード、アップロードした録音ファイルは再アップロードが必要になります。', action: () => run('/api/clean', {}, '一時ファイルを削除します') })}>削除する</button>
              </div>
            </div>
          </div>
        </div>

        <div className="section">
          <h3>ファイル変換</h3>
          <div className="tool-grid"><M4aConverter notify={notify} /></div>
        </div>
      </div>
      {confirm && (
        <ConfirmModal title="実行しますか？" confirmLabel="実行" onClose={() => setConfirm(null)} onConfirm={confirm.action}>
          {confirm.confirm}
        </ConfirmModal>
      )}
    </div>
  );
}

// ─── ログ ──────────────────────────────────────────────────────────────────

function LogPanel({ logs }) {
  const [open, setOpen] = useState(() => storageGet('transcribe.logOpen', false));
  const [height, setHeight] = useState(() => storageGet('transcribe.logHeight', 220));
  const endRef = useRef(null);

  useEffect(() => { storageSet('transcribe.logOpen', open); }, [open]);
  useEffect(() => { storageSet('transcribe.logHeight', height); }, [height]);
  useEffect(() => { if (open) endRef.current?.scrollIntoView({ behavior: 'auto' }); }, [logs, open]);

  const onDrag = (e) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = height;
    const move = ev => setHeight(Math.max(80, Math.min(window.innerHeight - 200, startH - (ev.clientY - startY))));
    const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  };

  const last = logs[logs.length - 1] || '処理ログはまだありません';

  return (
    <>
      {open && <div className="log-resizer" onMouseDown={onDrag} />}
      {open && (
        <div className="log-viewer" style={{ height }}>
          {logs.length === 0
            ? <span className="log-empty">処理を開始すると、ここに詳細なログが表示されます</span>
            : logs.map((line, i) => {
              const cls = /\[完了 \(exit=0\)\]/.test(line) ? ' done' : /exit=[1-9]|ERROR|エラー|失敗/.test(line) ? ' error' : '';
              return <div key={i} className={`log-line${cls}`}>{line}</div>;
            })}
          <div ref={endRef} />
        </div>
      )}
      <div className="log-bar" onClick={() => setOpen(o => !o)} title="クリックで詳細ログを開閉">
        <span>{open ? '▼' : '▲'} 詳細ログ</span>
        <span className="last-line">{last}</span>
      </div>
    </>
  );
}

// ─── アプリ本体 ────────────────────────────────────────────────────────────

function App() {
  const [jobs, setJobs] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [offline, setOffline] = useState(false);
  const [queue, setQueue] = useState({ running: null, pending: [] });
  const [health, setHealth] = useState([]);
  const [view, setView] = useState('add');
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [filter, setFilter] = useState('all');
  const [logs, setLogs] = useState([]);
  const [toasts, setToasts] = useState([]);
  const [showHelp, setShowHelp] = useState(() => !storageGet('transcribe.helpSeen', false));
  const [jobMenu, setJobMenu] = useState(null);
  const [confirmAction, setConfirmAction] = useState(null);
  const [darkMode, setDarkMode] = useState(() => storageGet('transcribe.dark', true));
  const filterInitialized = useRef(false);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', darkMode ? 'dark' : '');
    storageSet('transcribe.dark', darkMode);
  }, [darkMode]);

  const notify = useCallback((text, kind = 'info') => {
    const id = Math.random();
    setToasts(t => [...t, { id, text, kind }]);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), kind === 'error' ? 8000 : 5000);
  }, []);

  const fetchJobs = useCallback(async () => {
    try {
      const [data, q] = await Promise.all([apiFetch('/api/jobs'), apiFetch('/api/queue')]);
      setJobs([...data].reverse().map(j => ({ ...j, eta: estimateRemaining(j) })));
      setQueue(q);
      setLoaded(true);
      setOffline(false);
    } catch (e) {
      if (e.message === OFFLINE_MESSAGE) setOffline(true);
    }
  }, []);

  const fetchHealth = useCallback(() => {
    apiFetch('/api/health').then(setHealth).catch(() => {});
  }, []);

  useEffect(() => {
    fetchJobs();
    fetchHealth();
    const id = setInterval(fetchJobs, 3000);
    return () => clearInterval(id);
  }, [fetchJobs, fetchHealth]);

  // 初回表示時: 要対応があればそれを、なければすべてを表示
  useEffect(() => {
    if (!loaded || filterInitialized.current) return;
    filterInitialized.current = true;
    setFilter(jobs.some(j => j.attention?.length > 0) ? 'attention' : 'all');
  }, [loaded, jobs]);

  // 全タスクのログを 1 本の WebSocket で受け取る
  useEffect(() => {
    let ws;
    let retry;
    let closed = false;
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/ws/logs/global`);
      ws.onmessage = (e) => {
        setLogs(prev => [...prev.slice(-499), e.data]);
        const m = /\[完了 \(exit=(\d+)\)\]/.exec(e.data);
        if (m) {
          fetchJobs();
          if (m[1] !== '0') notify('処理の一部が失敗しました。「要対応」または詳細ログを確認してください。', 'error');
        }
      };
      ws.onclose = () => { if (!closed) retry = setTimeout(connect, 3000); };
    };
    connect();
    return () => { closed = true; clearTimeout(retry); ws && ws.close(); };
  }, [fetchJobs, notify]);

  const enabledPost = useMemo(() => {
    const ok = key => ['ok', 'warn'].includes(health.find(c => c.key === key)?.status);
    return Object.entries(POST_STAGE_KEYS).filter(([, key]) => ok(key)).map(([stage]) => stage);
  }, [health]);

  const healthErrors = health.filter(c => c.status === 'error').length;
  const selectedJob = jobs.find(j => j.id === selectedJobId);

  const openJob = (id) => { setSelectedJobId(id); setView('job'); };

  const runTask = async (path, body, message) => {
    try {
      await postJson(path, body);
      notify(message, 'success');
      fetchJobs();
    } catch (e) {
      notify(`実行できませんでした: ${e.message}`, 'error');
    }
  };

  // 一覧の右クリックメニュー（詳細を開かずに操作できる）
  const openJobMenu = (job, e) => {
    const attention = job.attention?.length > 0;
    const items = [{ label: '開く', onClick: () => openJob(job.id) }];

    if (job.status === 'failed') {
      items.push({ label: '失敗したところから再開', onClick: () => runTask('/api/retry', { job_id: job.id }, '失敗したところから再開します') });
    } else if (job.status === 'done' && attention) {
      items.push({ label: '後処理をやり直す', onClick: () => runTask('/api/resume-post', { job_id: job.id }, '後処理をやり直します') });
    }

    if (job.status === 'done') {
      if (enabledPost.includes('summarize')) {
        items.push({
          label: 'まとめを作り直す', sub: 'AI の API を再度呼び出します',
          onClick: () => setConfirmAction({
            title: `ジョブ #${job.id} のまとめを作り直しますか？`,
            message: '現在の文字起こしからまとめを作り直します。AI の利用料金が発生します。',
            confirmLabel: '作り直す',
            run: () => runTask('/api/summarize', { job_id: job.id }, 'まとめを作り直します'),
          }),
        });
      }
      if (job.notion_url) {
        items.push({ label: 'Notion で開く', onClick: () => window.open(job.notion_url, '_blank', 'noopener') });
      }
      if (/^https?:\/\//.test(job.url)) {
        items.push({ label: '元の動画を開く', onClick: () => window.open(job.url, '_blank', 'noopener') });
      }
    }

    items.push({ separator: true });
    items.push({
      label: '削除', danger: true,
      onClick: () => setConfirmAction({
        title: `ジョブ #${job.id} を削除しますか？`,
        message: '一覧から削除します。Notion / Docs に登録済みのページは削除されません。',
        confirmLabel: '削除', danger: true,
        run: async () => {
          await runTask('/api/delete', { job_id: job.id, files: false }, '削除しました');
          if (selectedJobId === job.id) { setSelectedJobId(null); setView('add'); }
        },
      }),
    });

    setJobMenu({ x: e.clientX, y: e.clientY, items });
  };
  const cancelTask = async (taskId) => {
    try {
      await postJson(`/api/tasks/${taskId}/cancel`);
      notify('待機中のタスクを取り消しました。', 'success');
      fetchJobs();
    } catch (e) {
      notify(`取り消せませんでした: ${e.message}`, 'error');
    }
  };
  const go = (v) => { setView(v); if (v !== 'job') setSelectedJobId(null); if (v === 'settings') fetchHealth(); };
  const closeHelp = () => { setShowHelp(false); storageSet('transcribe.helpSeen', true); };

  let content;
  if (view === 'job' && selectedJobId) {
    content = selectedJob
      ? <JobView key={selectedJobId} jobId={selectedJobId} listJob={selectedJob} enabledPost={enabledPost} notify={notify}
          onChanged={fetchJobs} onClosed={() => go('add')} />
      : <div className="view"><div className="empty-state">このジョブは見つかりません（削除された可能性があります）。</div></div>;
  } else if (view === 'glossary') {
    content = <GlossaryView notify={notify} />;
  } else if (view === 'settings') {
    content = <SettingsView health={health} reload={fetchHealth} notify={notify} />;
  } else if (view === 'tools') {
    content = <ToolsView notify={notify} onStarted={fetchJobs} enabledPost={enabledPost} />;
  } else {
    content = <AddView health={health} notify={notify} isFirstUse={loaded && jobs.length === 0} onOpenHelp={() => setShowHelp(true)}
      onAdded={() => { fetchJobs(); setFilter('progress'); }} />;
  }

  return (
    <div id="app" style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      <header className="app-header">
        <span className="app-title" onClick={() => go('add')}>transcribe<small>文字起こし・まとめ</small></span>
        <button className={`nav-btn${view === 'add' ? ' active' : ''}`} onClick={() => go('add')}>＋ 追加</button>
        <QueueIndicator jobs={jobs} queue={queue} onOpenJob={openJob} onCancel={cancelTask} />
        <span className="header-spacer" />
        <button className={`nav-btn${view === 'glossary' ? ' active' : ''}`} onClick={() => go('glossary')} title="誤認識しやすい言葉の登録">用語辞書</button>
        <button className={`nav-btn${view === 'tools' ? ' active' : ''}`} onClick={() => go('tools')} title="一括処理・変換など">ツール</button>
        <button className={`nav-btn${view === 'settings' ? ' active' : ''}`} onClick={() => go('settings')} title="各機能が使える状態か確認">
          {healthErrors > 0 && <span className="dot" />}設定状況
        </button>
        <button onClick={() => setShowHelp(true)} title="使い方">？ 使い方</button>
        <button className="ghost" onClick={() => setDarkMode(d => !d)} title="表示テーマの切り替え">{darkMode ? '☀️' : '🌙'}</button>
      </header>
      {offline && (
        <div className="offline-banner">
          ⚠ {OFFLINE_MESSAGE} 起動すると自動で再接続します。
        </div>
      )}
      <main className="app-main">
        <Sidebar jobs={jobs} filter={filter} setFilter={setFilter} selectedId={view === 'job' ? selectedJobId : null}
          onSelect={openJob} onJobContextMenu={openJobMenu} enabledPost={enabledPost} />
        <section className="main-content">
          {content}
          <LogPanel logs={logs} />
        </section>
      </main>
      <Toasts toasts={toasts} />
      {jobMenu && <ContextMenu {...jobMenu} onClose={() => setJobMenu(null)} />}
      {confirmAction && (
        <ConfirmModal title={confirmAction.title} confirmLabel={confirmAction.confirmLabel} danger={confirmAction.danger}
          onClose={() => setConfirmAction(null)} onConfirm={confirmAction.run}>
          {confirmAction.message}
        </ConfirmModal>
      )}
      {showHelp && <HelpModal onClose={closeHelp} />}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
