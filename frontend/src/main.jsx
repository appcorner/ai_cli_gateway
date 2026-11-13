import React, {useState, useEffect, useCallback} from 'react';
import {createRoot} from 'react-dom/client';
import {Activity, Send, RefreshCw, Lock, LogOut, Terminal, Server, ListTodo} from 'lucide-react';
import './style.css';
function App(){
  const [token,setToken]=useState(sessionStorage.getItem('gatewayToken')||'');
  const [draft,setDraft]=useState('');
  const [overview,setOverview]=useState(null);
  const [error,setError]=useState('');
  const [prompt,setPrompt]=useState('สวัสดีครับ อธิบายว่าคุณช่วยอะไรได้บ้างแบบสั้นๆ');
  const [sending,setSending]=useState(false);
  const [answer,setAnswer]=useState('');
  const [modelCatalog,setModelCatalog]=useState([]);
  const [selectedModel,setSelectedModel]=useState('codex-default');
  const request=useCallback(async(path,options={})=>{
    const r=await fetch(path,{...options,headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json',...(options.headers||{})}});
    const body=await r.json().catch(()=>({detail:r.statusText}));
    if(!r.ok)throw new Error(typeof body.detail==='string'?body.detail:JSON.stringify(body.detail||body));
    return body;
  },[token]);
  const refresh=useCallback(async()=>{
    if(!token)return;
    try{setOverview(await request('/admin/overview'));setError('');try{const models=await request('/v1/models');setModelCatalog(models.data||[])}catch{}}
    catch(e){setError(e.message);}
  },[token,request]);
  useEffect(()=>{refresh();const timer=setInterval(refresh,5000);return()=>clearInterval(timer)},[refresh]);
  const login=()=>{sessionStorage.setItem('gatewayToken',draft.trim());setToken(draft.trim());setDraft('')};
  const logout=()=>{sessionStorage.removeItem('gatewayToken');setToken('');setOverview(null);setAnswer('')};
  const test=async()=>{
    setSending(true);setAnswer('');setError('');
    try{const result=await request('/v1/chat/completions',{method:'POST',body:JSON.stringify({model:selectedModel,messages:[{role:'user',content:prompt}],stream:false})});setAnswer(result.choices?.[0]?.message?.content||'(empty)');await refresh();}
    catch(e){setError(e.message)}finally{setSending(false)}
  };
  if(!token)return <main className="login"><div className="login-card"><div className="brand"><Terminal size={28}/> Codex Gateway</div><p>Personal Management Console</p><label>Gateway API Key</label><input type="password" value={draft} onChange={e=>setDraft(e.target.value)} onKeyDown={e=>e.key==='Enter'&&login()} placeholder="Paste GATEWAY_API_KEY"/><button disabled={!draft.trim()} onClick={login}><Lock size={16}/> Unlock Console</button><small>Key is held only in this browser tab session. Access from localhost only.</small></div></main>;
  const counts=overview?.counts||{};
  return <div className="layout"><aside><div className="brand"><Terminal size={24}/> Codex Gateway</div><p className="aside-sub">Personal LLM Router</p><nav><a href="#dashboard"><Activity size={17}/> Overview</a><a href="#tester"><Send size={17}/> Chat Tester</a><a href="#jobs"><ListTodo size={17}/> Jobs</a></nav><div className="aside-foot"><button className="ghost" onClick={logout}><LogOut size={16}/> Lock Console</button></div></aside><main className="content"><header><div><h1 id="dashboard">Gateway Overview</h1><p>Codex CLI · FastAPI · Redis (memory only)</p></div><button className="secondary" onClick={refresh}><RefreshCw size={15}/> Refresh</button></header>{error&&<div className="alert">{error}</div>}<section className="stats"><article><span>Gateway</span><strong>{overview?'Connected':'Checking...'}</strong><small>Bearer-authenticated API</small></article><article><span>Queued</span><strong>{overview?.queue_depth??'—'}</strong><small>Redis pending queue</small></article><article><span>Running</span><strong>{counts.running??0}</strong><small>Active CLI requests</small></article><article><span>Completed</span><strong>{counts.completed??0}</strong><small>Jobs expire after {Math.round((overview?.job_ttl_seconds||3600)/60)} min</small></article></section><section className="panel" id="tester"><div className="section-title"><h2>Test Codex CLI</h2><span className="pill">{modelCatalog.length} models</span></div><label>Model</label><select value={selectedModel} onChange={e=>setSelectedModel(e.target.value)}>{modelCatalog.map(model=><option key={model.id} value={model.id}>{model.name||model.id}{model.is_default?' (default)':''}</option>)}</select><p>Job prompts are removed from Redis when a worker starts. Results and status expire automatically.</p><textarea rows="4" value={prompt} onChange={e=>setPrompt(e.target.value)}/><button disabled={sending||!prompt.trim()} onClick={test}><Send size={16}/> {sending?'Running Codex...':'Send Request'}</button>{answer&&<pre className="answer">{answer}</pre>}</section><section className="panel" id="jobs"><div className="section-title"><h2>Recent Jobs</h2><span className="muted">Expires automatically</span></div><div className="table-scroll"><table><thead><tr><th>Job ID</th><th>Status</th><th>Duration</th><th>Created</th></tr></thead><tbody>{(overview?.jobs||[]).map(j=><tr key={j.id}><td title={j.id}>{j.id.slice(0,8)}</td><td><span className={'status '+j.status}>{j.status}</span></td><td>{j.duration_ms!=null?Math.round(j.duration_ms/1000)+' s':'—'}</td><td>{j.created_at?new Date(j.created_at).toLocaleString():'—'}</td></tr>)}{!overview?.jobs?.length&&<tr><td colSpan="4" className="empty">No jobs yet</td></tr>}</tbody></table></div></section><footer><Server size={14}/> Local-first MVP · Subscription limits apply · Do not expose publicly</footer></main></div>;
}
createRoot(document.getElementById('root')).render(<App/>);
