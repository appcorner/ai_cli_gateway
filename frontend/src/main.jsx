import React, {useState, useEffect, useCallback} from 'react';
import {createRoot} from 'react-dom/client';
import {Activity, Send, RefreshCw, Lock, LogOut, Terminal, Server, ListTodo, Trash2, Plus, MessageCircle} from 'lucide-react';
import './style.css';

function App(){
  const [token,setToken]=useState(sessionStorage.getItem('gatewayToken')||'');
  const [draft,setDraft]=useState('');
  const [overview,setOverview]=useState(null);
  const [error,setError]=useState('');
  const [systemPrompt,setSystemPrompt]=useState('');
  const [prompt,setPrompt]=useState('สวัสดีค่ะ อธิบายว่าคุณช่วยอะไรได้บ้างแบบสั้นๆ');
  const [sending,setSending]=useState(false);
  const [answer,setAnswer]=useState('');
  const [chatMessages,setChatMessages]=useState([]);
  const [historyLoading,setHistoryLoading]=useState(false);
  const [modelCatalog,setModelCatalog]=useState([]);
  const [selectedModel,setSelectedModel]=useState('codex-default');
  const [sessions,setSessions]=useState([]);
  const [sessionId,setSessionId]=useState('');
  const [saveSession,setSaveSession]=useState(false);

  const request=useCallback(async(path,options={})=>{
    const r=await fetch(path,{...options,headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json',...(options.headers||{})}});
    const body=await r.json().catch(()=>({detail:r.statusText}));
    if(!r.ok)throw new Error(typeof body.detail==='string'?body.detail:JSON.stringify(body.detail||body));
    return body;
  },[token]);

  const refresh=useCallback(async()=>{
    if(!token)return;
    try{
      setOverview(await request('/admin/overview'));
      setError('');
      try{const models=await request('/v1/models');setModelCatalog(models.data||[])}catch{}
    }catch(e){setError(e.message)}
  },[token,request]);

  const refreshSessions=useCallback(async()=>{
    if(!token)return;
    try{const result=await request('/admin/sessions');setSessions(result.sessions||[])}catch{}
  },[token,request]);

  useEffect(()=>{refresh();refreshSessions();const timer=setInterval(refresh,10000);const sessionTimer=setInterval(refreshSessions,60000);return()=>{clearInterval(timer);clearInterval(sessionTimer)}},[refresh,refreshSessions]);
  const login=()=>{sessionStorage.setItem('gatewayToken',draft.trim());setToken(draft.trim());setDraft('')};
  const logout=()=>{sessionStorage.removeItem('gatewayToken');setToken('');setOverview(null);setAnswer('');setSessions([])};
  const startSession=()=>{setSessionId('');setSaveSession(true);setAnswer('');setChatMessages([]);setError('')};
  const test=async()=>{
    setSending(true);setAnswer('');setError('');
    const userMessage=prompt.trim();
    setChatMessages(current=>[...current,{role:'user',content:userMessage}]);
    try{
      const result=await request('/v1/chat/completions',{method:'POST',body:JSON.stringify({
        model:selectedModel,messages:[{role:'user',content:userMessage}],stream:false,
        session_id:saveSession?sessionId||null:null,store_session:saveSession&&!sessionId,
        system_prompt:systemPrompt.trim()||null
      })});
      const reply=result.choices?.[0]?.message?.content||'(empty)';
      setAnswer(reply);
      setChatMessages(current=>[...current,{role:'assistant',content:reply}]);
      if(result.session_id){setSessionId(result.session_id);setSaveSession(true)}
      await Promise.all([refresh(),refreshSessions()]);
    }catch(e){setError(e.message)}finally{setSending(false)}
  };
  const deleteSession=async(id)=>{
    if(!window.confirm('ลบ session นี้พร้อมประวัติข้อความถาวรใช่ไหมคะ?'))return;
    try{
      await request('/admin/sessions/'+encodeURIComponent(id),{method:'DELETE'});
      if(sessionId===id){setSessionId('');setSaveSession(false);setAnswer('');setChatMessages([])}
      await Promise.all([refresh(),refreshSessions()]);
    }catch(e){setError(e.message)}
  };
  const continueSession=async id=>{
    setSessionId(id);setSaveSession(true);setAnswer('');setChatMessages([]);setError('');setHistoryLoading(true);
    document.getElementById('tester')?.scrollIntoView({behavior:'smooth'});
    try{const history=await request('/admin/sessions/'+encodeURIComponent(id));setChatMessages(history.messages||[])}
    catch(e){setError(e.message)}finally{setHistoryLoading(false)}
  };
  const formatDate=value=>value?new Date(typeof value==='number'&&value<1e12?value*1000:value).toLocaleString():'—';

  if(!token)return <main className="login"><div className="login-card"><div className="brand"><Terminal size={28}/> Codex Gateway</div><p>Personal Management Console</p><label>Gateway API Key</label><input type="password" value={draft} onChange={e=>setDraft(e.target.value)} onKeyDown={e=>e.key==='Enter'&&login()} placeholder="Paste GATEWAY_API_KEY"/><button disabled={!draft.trim()} onClick={login}><Lock size={16}/> Unlock Console</button><small>Key is held only in this browser tab session. Access from localhost only.</small></div></main>;

  const counts=overview?.counts||{};
  return <div className="layout">
    <aside><div className="brand"><Terminal size={24}/> Codex Gateway</div><p className="aside-sub">Personal LLM Router</p><nav><a href="#dashboard"><Activity size={17}/> Overview</a><a href="#tester"><Send size={17}/> Chat Tester</a><a href="#sessions"><Server size={17}/> Sessions</a><a href="#jobs"><ListTodo size={17}/> Jobs</a></nav><div className="aside-foot"><button className="ghost" onClick={logout}><LogOut size={16}/> Lock Console</button></div></aside>
    <main className="content">
      <header><div><h1 id="dashboard">Gateway Overview</h1><p>Codex CLI · FastAPI · Redis queue · File-based Codex sessions</p></div><button className="secondary" onClick={refresh}><RefreshCw size={15}/> Refresh</button></header>
      {error&&<div className="alert">{error}</div>}
      <section className="stats"><article><span>Gateway</span><strong>{overview?'Connected':'Checking...'}</strong><small>Bearer-authenticated API</small></article><article><span>Queued</span><strong>{overview?.queue_depth??'—'}</strong><small>Redis pending queue</small></article><article><span>Running</span><strong>{counts.running??0}</strong><small>Active CLI requests</small></article><article><span>Saved Sessions</span><strong>{sessions.length}</strong><small>Stored in Worker Codex volume</small></article></section>
      <section className="panel" id="tester"><div className="section-title"><h2>Test Codex CLI</h2><span className="pill">{modelCatalog.length} models</span></div>
        <label>Model</label><select value={selectedModel} onChange={e=>setSelectedModel(e.target.value)}>{modelCatalog.map(model=><option key={model.id} value={model.id}>{model.name||model.id}{model.is_default?' (default)':''}</option>)}</select>
        <label className="session-toggle"><input type="checkbox" checked={saveSession} onChange={e=>{setSaveSession(e.target.checked);if(!e.target.checked)setSessionId('')}}/> Save this conversation as a Codex session</label>
        {saveSession&&<div className="session-current">{sessionId?<>Continuing session <code>{sessionId}</code></>:<>A new session will be created with this request.</>} <button className="secondary" onClick={startSession}><Plus size={15}/> New session</button></div>}
        <p>A system prompt guides Codex's behavior. Saved sessions include prompts and answers in Codex history files.</p>
        <label>System prompt</label><textarea rows="3" value={systemPrompt} onChange={e=>setSystemPrompt(e.target.value)} placeholder="กำหนดบทบาท รูปแบบคำตอบ หรือข้อกำหนดให้ Codex"/>
        {sessionId&&<div className="chat-transcript" aria-live="polite">{historyLoading?<p className="muted">Loading conversation…</p>:chatMessages.map((message,index)=><div className={'chat-bubble '+message.role} key={index}><small>{message.role==='assistant'?'Codex':'You'}</small><div>{message.content}</div></div>)}</div>}
        <label>{sessionId?'Your next message':'User message'}</label><textarea rows="4" value={prompt} onChange={e=>setPrompt(e.target.value)} onKeyDown={e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter'&&!sending&&prompt.trim())test()}}/><button disabled={sending||!prompt.trim()} onClick={test}><Send size={16}/> {sending?'Running Codex...':sessionId?'Send Message':'Send Request'}</button>{sessionId&&<small className="composer-hint">Ctrl+Enter to send · This continues the selected session</small>}{!sessionId&&answer&&<pre className="answer">{answer}</pre>}
      </section>
      <section className="panel" id="sessions"><div className="section-title"><h2>Saved Sessions</h2><span className="muted">{sessions.length} found</span></div><p>Deleting a session removes its saved Codex history file.</p><div className="table-scroll"><table><thead><tr><th>Session ID</th><th>Last Updated</th><th>Model</th><th>Actions</th></tr></thead><tbody>{sessions.map(s=><tr key={s.id}><td title={s.id}>{s.id.slice(0,8)}</td><td>{formatDate(s.updated_at)}</td><td>{s.model||'—'}</td><td><button className="secondary session-action" onClick={()=>continueSession(s.id)}><MessageCircle size={15}/> Continue</button> <button className="ghost danger session-action" onClick={()=>deleteSession(s.id)} title="Delete session"><Trash2 size={16}/> Delete</button></td></tr>)}{!sessions.length&&<tr><td colSpan="4" className="empty">No saved sessions</td></tr>}</tbody></table></div></section>
      <section className="panel" id="jobs"><div className="section-title"><h2>Recent Jobs</h2><span className="muted">Expires automatically</span></div><div className="table-scroll"><table><thead><tr><th>Job ID</th><th>Status</th><th>Duration</th><th>Created</th></tr></thead><tbody>{(overview?.jobs||[]).map(j=><tr key={j.id}><td title={j.id}>{j.id.slice(0,8)}</td><td><span className={'status '+j.status}>{j.status}</span></td><td>{j.duration_ms!=null?Math.round(j.duration_ms/1000)+' s':'—'}</td><td>{formatDate(j.created_at)}</td></tr>)}{!overview?.jobs?.length&&<tr><td colSpan="4" className="empty">No jobs yet</td></tr>}</tbody></table></div></section>
      <footer><Server size={14}/> Local-first MVP · Session history is stored on disk · Do not expose publicly</footer>
    </main>
  </div>;
}

createRoot(document.getElementById('root')).render(<App/>);
