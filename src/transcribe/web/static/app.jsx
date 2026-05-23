const { useState, useEffect, useRef, useCallback } = React;

const STATUS_STYLE = {
  done:         { background: '#3fb950', color: '#000' },
  failed:       { background: '#f85149', color: '#fff' },
  queued:       { background: '#d29922', color: '#000' },
  transcribing: { background: '#58a6ff', color: '#000' },
  downloading:  { background: '#9e6a03', color: '#fff' },
  separating:   { background: '#bc4c00', color: '#fff' },
  formatting:   { background: '#388bfd', color: '#fff' },
};

function StatusBadge({ status }) {
  const style = STATUS_STYLE[status] || { background: '#444', color: '#fff' };
  return (
    <span className="status-badge" style={style}>{status}</span>
  );
}

function JobList({ jobs, selectedId, onSelect }) {
  if (jobs.length === 0) {
    return <div style={{ padding: '12px', color: 'var(--text-dim)', fontSize: '12px' }}>ジョブがありません</div>;
  }
  return (
    <div className="job-list">
      {jobs.map(job => (
        <div
          key={job.id}
          className={`job-item${selectedId === job.id ? ' selected' : ''}`}
          onClick={() => onSelect(job.id === selectedId ? null : job.id)}
        >
          <div className="job-item-top">
            <span className="job-id">#{job.id}</span>
            <StatusBadge status={job.status} />
          </div>
          <div className="job-title">{job.title || job.url}</div>
        </div>
      ))}
    </div>
  );
}

function LogViewer({ logs, height = 200 }) {
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'auto' });
  }, [logs]);

  if (logs.length === 0) {
    return <div className="log-viewer" style={{ height: `${height}px`, overflowY: 'auto', flex: 'none' }}><span className="log-empty">コマンドを実行するとログがここに表示されます</span></div>;
  }

  return (
    <div className="log-viewer" style={{ height: `${height}px`, overflowY: 'auto', flex: 'none' }}>
      {logs.map((line, i) => {
        const cls = line.startsWith('[完了') ? ' done' : line.startsWith('[ERROR') ? ' error' : '';
        return <div key={i} className={`log-line${cls}`}>{line}</div>;
      })}
      <div ref={endRef} />
    </div>
  );
}

function JobDetail({ jobId, onClose, onTaskStart, onMessage, onRefresh }) {
  const [job, setJob] = useState(null);
  const [fileContent, setFileContent] = useState(null);
  const [fileType, setFileType] = useState(null);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    setJob(null);
    setFileContent(null);
    setFileType(null);
    if (!jobId) return;
    fetch(`/api/jobs/${jobId}`).then(r => r.json()).then(setJob);
  }, [jobId]);

  const postJson = async (endpoint, body) => {
    setLoading(true);
    try {
      const res = await fetch(`/api/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.text();
        onMessage(`[エラー] ${endpoint}: ${err}`);
        return null;
      }
      const data = await res.json();
      onTaskStart(data.task_id);
      return data;
    } catch (e) {
      onMessage(`[エラー] ${e.message}`);
      return null;
    } finally {
      setLoading(false);
    }
  };

  const showFile = async (type) => {
    if (loading) return;
    setEditing(false);
    setLoading(true);
    setFileType(type);
    try {
      const res = await fetch(`/api/jobs/${jobId}/${type}`);
      if (res.ok) {
        const data = await res.json();
        setFileContent(data.content);
      } else {
        setFileContent(`(${type}.md が見つかりません)`);
      }
    } finally {
      setLoading(false);
    }
  };

  if (!job) return null;

  return (
    <div className="job-detail">
      <div className="detail-header">
        <h3>Job #{job.id}</h3>
        <StatusBadge status={job.status} />
        <span className="detail-url">{job.title || job.url}</span>
        <div className="detail-actions">
          <button onClick={() => showFile('transcript')} disabled={loading}>transcript</button>
          <button onClick={() => showFile('summary')} disabled={loading}>summary</button>
          <button onClick={() => postJson('summarize', { job_id: jobId })} disabled={loading}>まとめ再生成</button>
          {fileType === 'transcript' && !editing && (
            <button onClick={() => setEditing(true)} disabled={loading}>編集</button>
          )}
          {fileType === 'transcript' && editing && (
            <>
              <button onClick={async () => {
                try {
                  const res = await fetch(`/api/jobs/${jobId}/transcript`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ content: fileContent }),
                  });
                  if (res.ok) {
                    setEditing(false);
                    onMessage('[完了] transcript.md を保存しました');
                  } else {
                    onMessage('[エラー] 保存に失敗しました');
                  }
                } catch {
                  onMessage('[エラー] 保存に失敗しました');
                }
              }} disabled={loading}>保存</button>
              <button onClick={() => { setEditing(false); showFile('transcript'); }} disabled={loading}>キャンセル</button>
            </>
          )}
          <button onClick={async () => {
            const ok = await postJson('rerun', { url_or_id: String(jobId) });
            if (ok) onClose();
          }} disabled={loading}>rerun</button>
          <button onClick={() => postJson('retry', { job_id: jobId })} disabled={loading}>retry</button>
          <button onClick={async () => {
            if (!window.confirm(`ジョブ #${jobId} を削除しますか？`)) return;
            const ok = await postJson('delete', { job_id: jobId, files: false });
            if (ok) { onClose(); onRefresh(); }
          }} disabled={loading}>delete</button>
          <button onClick={onClose}>✕</button>
        </div>
      </div>
      {fileContent !== null && (
        <div className="file-view">
          {editing ? (
            <textarea
              style={{ width: '100%', height: '100%', minHeight: '400px', fontFamily: 'monospace', fontSize: '13px', background: 'var(--bg)', color: 'var(--text)', border: '1px solid var(--border)', padding: '8px', boxSizing: 'border-box', resize: 'vertical' }}
              value={fileContent}
              onChange={e => setFileContent(e.target.value)}
            />
          ) : (
            <pre>{fileContent}</pre>
          )}
        </div>
      )}
    </div>
  );
}

function GlossaryEditor({ onClose }) {
  const [data, setData] = useState(null);
  const [saving, setSaving] = useState(false);
  const [newSub, setNewSub] = useState({ pattern: '', replacement: '', type: 'literal' });
  const [newTerm, setNewTerm] = useState('');

  useEffect(() => {
    fetch('/api/glossary').then(r => r.json()).then(setData);
  }, []);

  const save = async () => {
    setSaving(true);
    try {
      await fetch('/api/glossary', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
    } finally {
      setSaving(false);
    }
  };

  const updateSub = (i, field, value) => {
    setData(d => {
      const subs = [...d.substitutions];
      subs[i] = { ...subs[i], [field]: value };
      return { ...d, substitutions: subs };
    });
  };

  const removeSub = (i) => {
    setData(d => ({ ...d, substitutions: d.substitutions.filter((_, idx) => idx !== i) }));
  };

  const addSub = () => {
    if (!newSub.pattern) return;
    setData(d => ({ ...d, substitutions: [{ ...newSub }, ...d.substitutions] }));
    setNewSub({ pattern: '', replacement: '', type: 'literal' });
  };

  const removeTerm = (i) => {
    setData(d => ({ ...d, important_terms: d.important_terms.filter((_, idx) => idx !== i) }));
  };

  const addTerm = () => {
    if (!newTerm.trim()) return;
    setData(d => ({ ...d, important_terms: [newTerm.trim(), ...d.important_terms] }));
    setNewTerm('');
  };

  if (!data) return <div style={{ padding: '16px' }}>読み込み中...</div>;

  const inputStyle = { background: 'var(--bg)', color: 'var(--text)', border: '1px solid var(--border)', padding: '3px 6px', fontSize: '12px' };
  const btnStyle = { fontSize: '12px', padding: '3px 8px', cursor: 'pointer' };

  return (
    <div style={{ padding: '12px', overflowY: 'auto', flex: 1 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
        <button style={btnStyle} onClick={onClose}>用語辞書を閉じる</button>
        <button style={{ ...btnStyle, background: 'var(--accent, #388bfd)', color: '#fff' }} onClick={save} disabled={saving}>
          {saving ? '保存中...' : '保存'}
        </button>
      </div>

      <div style={{ marginBottom: '12px' }}>
        <label style={{ fontSize: '12px', display: 'block', marginBottom: '4px' }}>コンテキスト (initial_prompt)</label>
        <textarea
          style={{ ...inputStyle, width: '100%', minHeight: '60px', resize: 'vertical', boxSizing: 'border-box' }}
          value={data.context}
          onChange={e => setData(d => ({ ...d, context: e.target.value }))}
        />
      </div>

      <h3 style={{ fontSize: '13px', marginBottom: '6px' }}>誤認識パターン（substitutions）</h3>
      <div style={{ display: 'flex', gap: '4px', marginBottom: '8px', flexWrap: 'wrap' }}>
        <input style={{ ...inputStyle, flex: 1 }} placeholder="パターン" value={newSub.pattern} onChange={e => setNewSub(s => ({ ...s, pattern: e.target.value }))} />
        <input style={{ ...inputStyle, flex: 1 }} placeholder="置換後" value={newSub.replacement} onChange={e => setNewSub(s => ({ ...s, replacement: e.target.value }))} />
        <select style={inputStyle} value={newSub.type} onChange={e => setNewSub(s => ({ ...s, type: e.target.value }))}>
          <option value="literal">literal</option>
          <option value="regex">regex</option>
        </select>
        <button style={btnStyle} onClick={addSub}>追加</button>
      </div>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px', marginBottom: '16px' }}>
        <thead>
          <tr style={{ borderBottom: '1px solid var(--border)' }}>
            <th style={{ textAlign: 'left', padding: '4px 6px' }}>パターン</th>
            <th style={{ textAlign: 'left', padding: '4px 6px' }}>置換後</th>
            <th style={{ textAlign: 'left', padding: '4px 6px' }}>種別</th>
            <th style={{ padding: '4px 6px' }}>削除</th>
          </tr>
        </thead>
        <tbody>
          {data.substitutions.map((s, i) => (
            <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
              <td style={{ padding: '2px 4px' }}>
                <input style={{ ...inputStyle, width: '100%' }} value={s.pattern} onChange={e => updateSub(i, 'pattern', e.target.value)} />
              </td>
              <td style={{ padding: '2px 4px' }}>
                <input style={{ ...inputStyle, width: '100%' }} value={s.replacement} onChange={e => updateSub(i, 'replacement', e.target.value)} />
              </td>
              <td style={{ padding: '2px 4px' }}>
                <select style={inputStyle} value={s.type} onChange={e => updateSub(i, 'type', e.target.value)}>
                  <option value="literal">literal</option>
                  <option value="regex">regex</option>
                </select>
              </td>
              <td style={{ padding: '2px 4px', textAlign: 'center' }}>
                <button style={btnStyle} onClick={() => removeSub(i)}>×</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3 style={{ fontSize: '13px', marginBottom: '6px' }}>重要語リスト（important_terms）</h3>
      <div style={{ display: 'flex', gap: '4px', marginBottom: '8px' }}>
        <input
          style={{ ...inputStyle, flex: 1 }}
          placeholder="重要語を追加"
          value={newTerm}
          onChange={e => setNewTerm(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') addTerm(); }}
        />
        <button style={btnStyle} onClick={addTerm}>追加</button>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginBottom: '8px' }}>
        {data.important_terms.map((term, i) => (
          <span key={i} style={{ background: 'var(--border)', padding: '2px 6px', borderRadius: '4px', fontSize: '12px', display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
            {term}
            <button
              onClick={() => removeTerm(i)}
              style={{ background: 'none', border: 'none', color: 'inherit', cursor: 'pointer', padding: '0', fontSize: '11px', lineHeight: 1 }}
            >×</button>
          </span>
        ))}
      </div>
    </div>
  );
}

function CommandPanel({ onTaskStart, onMessage }) {
  const [urls, setUrls] = useState('');
  const [loading, setLoading] = useState(false);
  const fileRef = useRef(null);
  const convertRef = useRef(null);

  const postJson = async (endpoint, body) => {
    setLoading(true);
    try {
      const res = await fetch(`/api/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.text();
        onMessage(`[エラー] ${endpoint}: ${err}`);
        return;
      }
      const data = await res.json();
      onTaskStart(data.task_id);
    } catch (e) {
      onMessage(`[エラー] ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  const handleRun = () => {
    const urlList = urls.split('\n').map(s => s.trim()).filter(Boolean);
    if (!urlList.length) {
      onMessage('[エラー] URL を入力してください');
      return;
    }
    postJson('run', { urls: urlList });
    setUrls('');
  };

  const handleConvert = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    setLoading(true);
    onMessage(`[変換中] ${file.name} → ${file.name.replace(/\.m4a$/i, '.mp3')} (変換中はしばらくお待ちください...)`);
    const form = new FormData();
    form.append('audio', file);
    try {
      const res = await fetch('/api/convert', { method: 'POST', body: form });
      if (!res.ok) {
        const err = await res.text();
        onMessage(`[エラー] convert: ${err}`);
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const filename = file.name.replace(/\.m4a$/i, '.mp3');
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      onMessage(`[完了] ${filename} に変換しました`);
    } catch (e) {
      onMessage(`[エラー] ${e.message}`);
    } finally {
      setLoading(false);
      if (convertRef.current) convertRef.current.value = '';
    }
  };

  const handleFileUpload = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    setLoading(true);
    const form = new FormData();
    form.append('audio', file);
    try {
      const res = await fetch('/api/run/file', { method: 'POST', body: form });
      if (!res.ok) {
        const err = await res.text();
        onMessage(`[エラー] ファイルアップロード: ${err}`);
        return;
      }
      const data = await res.json();
      onTaskStart(data.task_id);
    } catch (e) {
      onMessage(`[エラー] ${e.message}`);
    } finally {
      setLoading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  return (
    <div className="command-panel">
      <div className="url-row">
        <textarea
          className="url-input"
          placeholder="URL を入力（複数行可）"
          value={urls}
          onChange={e => setUrls(e.target.value)}
          rows={2}
          onKeyDown={e => { if (e.key === 'Enter' && e.ctrlKey) handleRun(); }}
        />
        <button className="primary" onClick={handleRun} disabled={loading}>実行</button>
      </div>
      <div className="action-row">
        <label className="file-btn">
          ファイルを選択
          <input ref={fileRef} type="file" accept=".mp3,.m4a" onChange={handleFileUpload} style={{ display: 'none' }} />
        </label>
        <button onClick={() => postJson('sync', { all: false })} disabled={loading}>未同期を同期</button>
        <button onClick={() => postJson('sync', { all: true })} disabled={loading}>全件同期</button>
        <button onClick={() => postJson('summarize', { all: false })} disabled={loading}>未まとめをまとめ</button>
        <button onClick={() => postJson('summarize', { all: true })} disabled={loading}>全件まとめ</button>
        <button onClick={() => postJson('sync-notion', { all: false })} disabled={loading}>未Notion同期</button>
        <button onClick={() => postJson('sync-notion', { all: true })} disabled={loading}>全件Notion同期</button>
        <label className="file-btn">
          m4a→mp3変換
          <input ref={convertRef} type="file" accept=".m4a" onChange={handleConvert} style={{ display: 'none' }} />
        </label>
        <button onClick={() => postJson('clean', {})} disabled={loading}>一時ファイル削除</button>
      </div>
    </div>
  );
}

function App() {
  const [jobs, setJobs] = useState([]);
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [logs, setLogs] = useState([]);
  const [activeCount, setActiveCount] = useState(0);
  const [filterStatus, setFilterStatus] = useState('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [showGlossary, setShowGlossary] = useState(false);
  const [logHeight, setLogHeight] = useState(200);
  const wsRef = useRef(null);

  const onDragStart = (e) => {
  e.preventDefault();
  const startY = e.clientY;
  const startH = logHeight;
  const onMove = (ev) => {
    const delta = ev.clientY - startY;
    setLogHeight(Math.max(60, Math.min(600, startH - delta)));
  };
  const onUp = () => {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
  };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
};

  const fetchJobs = useCallback(async () => {
    try {
      const res = await fetch('/api/jobs');
      if (!res.ok) return;
      const data = await res.json();
      setJobs([...data].reverse());
    } catch (_) {}
  }, []);

  useEffect(() => {
    fetchJobs();
    const id = setInterval(fetchJobs, 3000);
    return () => clearInterval(id);
  }, [fetchJobs]);

  const connectWebSocket = useCallback((taskId) => {
    if (wsRef.current) {
      wsRef.current.close();
    }

    const ws = new WebSocket(`ws://${location.host}/ws/logs/${taskId}`);
    wsRef.current = ws;
    setActiveCount(c => c + 1);

    ws.onmessage = (e) => {
      setLogs(prev => [...prev, e.data]);
    };

    ws.onclose = () => {
      setActiveCount(c => Math.max(0, c - 1));
      fetchJobs();
    };

    ws.onerror = () => {
      setLogs(prev => [...prev, '[WebSocket エラー]']);
    };
  }, [fetchJobs]);

  const handleTaskStart = useCallback((taskId) => {
    setLogs([`[タスク開始] ${taskId}`]);
    connectWebSocket(taskId);
  }, [connectWebSocket]);

  const handleMessage = useCallback((msg) => {
    setLogs(prev => [...prev, msg]);
  }, []);

  const filteredJobs = jobs
    .filter(j => filterStatus === 'all' || j.status === filterStatus)
    .filter(j => {
      if (!searchQuery.trim()) return true;
      const q = searchQuery.toLowerCase();
      return (
        (j.title || '').toLowerCase().includes(q) ||
        (j.url || '').toLowerCase().includes(q)
      );
    });

  return (
    <div id="app" style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      <header className="app-header">
        <h1>transcribe Web UI</h1>
        {activeCount > 0 && <span className="running-badge">実行中: {activeCount}</span>}
      </header>
      <main className="app-main">
        <aside className="sidebar">
          <div className="sidebar-header">ジョブ一覧 ({filteredJobs.length}/{jobs.length})</div>
          <div style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>
            <input
              style={{
                width: '100%',
                background: 'var(--bg)',
                border: '1px solid var(--border)',
                borderRadius: '6px',
                color: 'var(--text)',
                padding: '4px 8px',
                fontSize: '12px',
              }}
              placeholder="タイトル・URLで検索"
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
            />
          </div>
          <div className="filter-row">
            {['all', 'done', 'failed', 'queued'].map(s => (
              <button
                key={s}
                className={`filter-btn${filterStatus === s ? ' active' : ''}`}
                onClick={() => setFilterStatus(s)}
              >
                {s === 'all' ? 'すべて' : s}
              </button>
            ))}
          </div>
          <JobList jobs={filteredJobs} selectedId={selectedJobId} onSelect={setSelectedJobId} />
        </aside>
        <section className="main-content">
          <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
            <button
              onClick={() => setShowGlossary(g => !g)}
              style={{ margin: '8px 8px 0', fontSize: '12px' }}
            >
              用語辞書を編集
            </button>
            {showGlossary
              ? <GlossaryEditor onClose={() => setShowGlossary(false)} />
              : <CommandPanel onTaskStart={handleTaskStart} onMessage={handleMessage} />
            }
          </div>
          <div
            onMouseDown={onDragStart}
            style={{
              height: '6px',
              cursor: 'row-resize',
              background: 'var(--border)',
              flexShrink: 0,
              margin: '4px 0',
            }}
          />
          <LogViewer logs={logs} height={logHeight} />
          {selectedJobId && (
            <JobDetail
              jobId={selectedJobId}
              onClose={() => setSelectedJobId(null)}
              onTaskStart={handleTaskStart}
              onMessage={handleMessage}
              onRefresh={fetchJobs}
            />
          )}
        </section>
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
