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

function LogViewer({ logs }) {
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'auto' });
  }, [logs]);

  if (logs.length === 0) {
    return <div className="log-viewer"><span className="log-empty">コマンドを実行するとログがここに表示されます</span></div>;
  }

  return (
    <div className="log-viewer">
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

function CommandPanel({ onTaskStart, onMessage }) {
  const [urls, setUrls] = useState('');
  const [loading, setLoading] = useState(false);
  const fileRef = useRef(null);

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
      </div>
    </div>
  );
}

function App() {
  const [jobs, setJobs] = useState([]);
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [logs, setLogs] = useState([]);
  const [activeCount, setActiveCount] = useState(0);
  const wsRef = useRef(null);

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

  return (
    <div id="app" style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      <header className="app-header">
        <h1>transcribe Web UI</h1>
        {activeCount > 0 && <span className="running-badge">実行中: {activeCount}</span>}
      </header>
      <main className="app-main">
        <aside className="sidebar">
          <div className="sidebar-header">ジョブ一覧 ({jobs.length})</div>
          <JobList jobs={jobs} selectedId={selectedJobId} onSelect={setSelectedJobId} />
        </aside>
        <section className="main-content">
          <CommandPanel onTaskStart={handleTaskStart} onMessage={handleMessage} />
          <LogViewer logs={logs} />
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
