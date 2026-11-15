import React, {useState, useEffect, useCallback, useRef} from 'react';
import {createRoot} from 'react-dom/client';
import {Activity, Send, RefreshCw, Lock, LogOut, Terminal, Server, ListTodo, Trash2, Plus, MessageCircle} from 'lucide-react';
import './style.css';

function App(){
  const [token,setToken]=useState(sessionStorage.getItem('gatewayToken')||'');
  const [draft,setDraft]=useState('');
  const [overview,setOverview]=useState(null);
  const [usage,setUsage]=useState(null);
  const [error,setError]=useState('');
  const [systemPrompt,setSystemPrompt]=useState('');
  const [prompt,setPrompt]=useState('');
  const [sending,setSending]=useState(false);
  const [answer,setAnswer]=useState('');
  const [chatMessages,setChatMessages]=useState([]);
  const [historyLoading,setHistoryLoading]=useState(false);
  const [modelCatalog,setModelCatalog]=useState([]);
  const [provider,setProvider]=useState('codex');
  const [selectedModel,setSelectedModel]=useState('codex-default');
  const [executionMode,setExecutionMode]=useState('chat');
  const [agyMode,setAgyMode]=useState('chat');
  const [agyUsage,setAgyUsage]=useState(null);
  const [sessions,setSessions]=useState([]);
  const [sessionId,setSessionId]=useState('');
  const [saveSession,setSaveSession]=useState(false);
  const [savedSystemPrompts,setSavedSystemPrompts]=useState(()=>{
    try{const items=JSON.parse(localStorage.getItem('codexGateway.savedSystemPrompts')||'[]');return Array.isArray(items)?items:[]}catch{return []}
  });
  const [selectedSystemPromptId,setSelectedSystemPromptId]=useState('');
  const [systemPromptName,setSystemPromptName]=useState('');
  const chatTranscriptRef=useRef(null);

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
      if(provider==='codex')try{setUsage(await request('/admin/usage'))}catch(e){setUsage({error:e.message})}
      setError('');
      try{const models=await request('/v1/models?provider='+provider);setModelCatalog(models.data||[])}catch{}
    }catch(e){setError(e.message)}
  },[token,request,provider]);

  const refreshSessions=useCallback(async()=>{
    if(!token)return;
    try{const result=await request('/admin/sessions');setSessions(result.sessions||[])}catch{}
  },[token,request]);

  useEffect(()=>{refresh();refreshSessions();const timer=setInterval(refresh,10000);const sessionTimer=setInterval(refreshSessions,60000);return()=>{clearInterval(timer);clearInterval(sessionTimer)}},[refresh,refreshSessions]);
  useEffect(()=>{if(!sessionId)setSelectedModel(provider+'-default');setModelCatalog([])},[provider,sessionId]);
  useEffect(()=>{const panel=chatTranscriptRef.current;if(panel)panel.scrollTop=panel.scrollHeight},[chatMessages,sending,historyLoading]);
  const login=()=>{sessionStorage.setItem('gatewayToken',draft.trim());setToken(draft.trim());setDraft('')};
  const logout=()=>{sessionStorage.removeItem('gatewayToken');setToken('');setOverview(null);setAnswer('');setSessions([])};
  const startSession=()=>{setSessionId('');setSaveSession(true);setAnswer('');setChatMessages([]);setPrompt('');setAgyUsage(null);setError('')};
  const newChat=()=>{setSessionId('');setSaveSession(false);setAnswer('');setChatMessages([]);setPrompt('');setAgyUsage(null);setError('')};
  const persistSystemPrompts=items=>{
    setSavedSystemPrompts(items);
    localStorage.setItem('codexGateway.savedSystemPrompts',JSON.stringify(items));
  };
  const saveSystemPrompt=(asNew=false)=>{
    const name=systemPromptName.trim();
    const content=systemPrompt.trim();
    if(!name||!content){setError('Enter a name and system prompt before saving.');return}
    const id=!asNew&&selectedSystemPromptId?selectedSystemPromptId:String(Date.now());
    const item={id,name,content};
    const items=!asNew&&selectedSystemPromptId?savedSystemPrompts.map(saved=>saved.id===id?item:saved):[...savedSystemPrompts,item];
    persistSystemPrompts(items);setSelectedSystemPromptId(id);setError('');
  };
  const loadSystemPrompt=id=>{
    setSelectedSystemPromptId(id);
    const item=savedSystemPrompts.find(saved=>saved.id===id);
    if(item){setSystemPromptName(item.name);setSystemPrompt(item.content)}
    else{setSystemPromptName('');setSystemPrompt('')}
  };
  const deleteSystemPrompt=()=>{
    if(!selectedSystemPromptId)return;
    persistSystemPrompts(savedSystemPrompts.filter(item=>item.id!==selectedSystemPromptId));
    loadSystemPrompt('');
  };
  const test=async()=>{
    setSending(true);setAnswer('');setAgyUsage(null);setError('');
    const userMessage=prompt.trim();
    const nextMessages=[...chatMessages,{role:'user',content:userMessage}].slice(-10);
    setChatMessages(nextMessages);setPrompt('');
    try{
      const result=await request('/v1/chat/completions',{method:'POST',body:JSON.stringify({
        provider,model:selectedModel,messages:sessionId?[{role:'user',content:userMessage}]:nextMessages,stream:false,
        session_id:saveSession?sessionId||null:null,store_session:saveSession&&!sessionId,
        system_prompt:systemPrompt.trim()||null,
        gateway_options:{mode:provider==='agy'?agyMode:executionMode,max_history_messages:10}
      })});
      const reply=result.choices?.[0]?.message?.content||'(empty)';
      setAnswer(reply);
      setAgyUsage(provider==='agy'?result.usage||null:null);
      setChatMessages(current=>[...current,{role:'assistant',content:reply}].slice(-10));
      if(result.session_id){setSessionId(result.session_id);setSaveSession(true)}
      await Promise.all([refresh(),refreshSessions()]);
    }catch(e){setError(e.message);setPrompt(userMessage);setChatMessages(current=>current.slice(0,-1))}finally{setSending(false)}
  };
  const deleteSession=async(session)=>{
    if(!window.confirm('ลบ session '+(session.provider==='agy'?'Antigravity':'Codex')+' นี้พร้อมประวัติข้อความถาวรใช่ไหมคะ?'))return;
    try{
      const {id}=session;
      await request('/admin/sessions/'+encodeURIComponent(id)+'?mode='+(session.mode||'agent')+'&provider='+(session.provider||'codex'),{method:'DELETE'});
      if(sessionId===id){setSessionId('');setSaveSession(false);setAnswer('');setChatMessages([])}
      await Promise.all([refresh(),refreshSessions()]);
    }catch(e){setError(e.message)}
  };
  const continueSession=async(session)=>{
    const {id,provider:sessionProvider='codex'}=session;
    const mode=session.mode||(sessionProvider==='agy'?'chat':'agent');
    setProvider(sessionProvider);setSessionId(id);setSaveSession(true);if(sessionProvider==='codex')setExecutionMode(mode);else setAgyMode(mode);setSelectedModel(session.model||sessionProvider+'-default');setAnswer('');setAgyUsage(null);setChatMessages([]);setError('');setHistoryLoading(true);
    document.getElementById('tester')?.scrollIntoView({behavior:'smooth'});
    try{const history=await request('/admin/sessions/'+encodeURIComponent(id)+'?mode='+mode+'&provider='+sessionProvider);setChatMessages(history.messages||[])}
    catch(e){setError(e.message)}finally{setHistoryLoading(false)}
  };
  const formatDate=value=>value?new Date(typeof value==='number'&&value<1e12?value*1000:value).toLocaleString():'—';

  if(!token)return <main className="login"><div className="login-card"><div className="brand"><Terminal size={28}/> AI CLI Gateway</div><p>Personal Management Console</p><label>Gateway API Key</label><input type="password" value={draft} onChange={e=>setDraft(e.target.value)} onKeyDown={e=>e.key==='Enter'&&login()} placeholder="Paste GATEWAY_API_KEY"/><button disabled={!draft.trim()} onClick={login}><Lock size={16}/> Unlock Console</button><small>Key is held only in this browser tab session. Access from localhost only.</small></div></main>;

  const counts=overview?.counts||{};
  const usageWindow=(title,window)=>window?<article className="usage-window"><div><span>{title}</span><strong>{window.used_percent==null?'—':`${Math.max(0,100-window.used_percent)}% left`}</strong></div><div className="usage-track"><i style={{width:`${Math.min(100,Math.max(0,window.used_percent||0))}%`}}/></div><small>{window.used_percent==null?'Usage unavailable':`${window.used_percent}% used`}{window.window_duration_mins?` · ${Math.round(window.window_duration_mins/60)}h window`:''}{window.resets_at?` · resets ${formatDate(window.resets_at)}`:''}</small></article>:null;
  return <div className="layout">
    <aside><div className="brand"><Terminal size={24}/> AI CLI Gateway</div><p className="aside-sub">Personal AI CLI Router</p><nav><a href="#dashboard"><Activity size={17}/> Overview</a><a href="#tester"><Send size={17}/> Chat Tester</a><a href="#sessions"><Server size={17}/> Sessions</a><a href="#jobs"><ListTodo size={17}/> Jobs</a></nav><div className="aside-foot"><button className="ghost" onClick={logout}><LogOut size={16}/> Lock Console</button></div></aside>
    <main className="content">
      <header><div><h1 id="dashboard">Gateway Overview</h1><p>Codex CLI · Antigravity CLI · FastAPI · Redis queue</p></div><button className="secondary" onClick={refresh}><RefreshCw size={15}/> Refresh</button></header>
      {error&&<div className="alert">{error}</div>}
      <section className="stats"><article><span>Gateway</span><strong>{overview?'Connected':'Checking...'}</strong><small>Bearer-authenticated API</small></article><article><span>Queued</span><strong>{overview?.queue_depth??'—'}</strong><small>Redis pending queue</small></article><article><span>Running</span><strong>{counts.running??0}</strong><small>Active CLI requests</small></article><article><span>Saved CLI Sessions</span><strong>{sessions.length}</strong><small>Codex and Antigravity</small></article></section>
      {provider==='codex'&&<section className="panel usage-panel"><div className="section-title"><h2>Codex Usage Limits</h2><span className="muted">{usage?.fetched_at?`Updated ${formatDate(usage.fetched_at)}`:'Account status'}</span></div>{usage?.error?<p className="usage-note">Could not read usage limits. Check Codex sign-in and refresh.</p>:usage?.primary||usage?.secondary?<div className="usage-grid">{usageWindow('Current limit',usage.primary)}{usageWindow('Weekly limit',usage.secondary)}</div>:<p className="usage-note">Loading usage limits from the signed-in Codex account…</p>}</section>}
      <section className="panel" id="tester"><div className="section-title"><h2>Test AI CLI</h2><span className="pill">{modelCatalog.length} models</span></div>
        <div className="chat-toolbar"><div className="chat-selectors"><label>Provider<select value={provider} disabled={!!sessionId} onChange={e=>setProvider(e.target.value)}><option value="codex">Codex CLI</option><option value="agy">Antigravity CLI (agy)</option></select></label><label>Mode<select value={provider==='agy'?agyMode:executionMode} disabled={!!sessionId} onChange={e=>provider==='agy'?setAgyMode(e.target.value):setExecutionMode(e.target.value)}>{provider==='agy'?<><option value="chat">Chat · slash &amp; skill expansion off</option><option value="agent">Normal · full CLI behavior</option></>:<><option value="chat">Chat · lightweight</option><option value="agent">Agent · full config</option></>}</select></label><label>Model<select value={selectedModel} onChange={e=>setSelectedModel(e.target.value)}>{modelCatalog.map(model=><option key={model.id} value={model.id}>{model.name||model.id}{model.is_default?' (default)':''}</option>)}</select></label></div><div className="chat-actions"><label className="session-toggle"><input type="checkbox" checked={saveSession} onChange={e=>{setSaveSession(e.target.checked);if(!e.target.checked)setSessionId('')}}/> Save conversation</label><button className="secondary" onClick={newChat}><Plus size={15}/> New chat</button></div></div>
        {provider==='agy'&&<small className="composer-hint">{agyMode==='chat'?'Chat mode disables slash-command and skill expansion. Core instructions and global/plugin rules may still add context.':'Normal mode keeps agy slash commands and skills enabled.'}</small>}
        {sessionId&&<div className="session-current">Continuing session <code>{sessionId}</code><button className="secondary" onClick={startSession}><Plus size={15}/> New saved session</button></div>}
        <details className="system-prompt-settings" open={!!systemPrompt}><summary>System prompt <span>{systemPrompt?'Configured':'Optional'}</span></summary><p>Guide the selected CLI's role and response style. Saved prompt templates stay in this browser.</p><div className="saved-prompt-controls"><select aria-label="Saved system prompts" value={selectedSystemPromptId} onChange={e=>loadSystemPrompt(e.target.value)}><option value="">Choose a saved prompt</option>{savedSystemPrompts.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select><input aria-label="Prompt name" value={systemPromptName} onChange={e=>setSystemPromptName(e.target.value)} placeholder="Name this prompt"/><button className="secondary" onClick={()=>saveSystemPrompt(false)}>{selectedSystemPromptId?'Update prompt':'Save prompt'}</button>{selectedSystemPromptId&&<button className="secondary" onClick={()=>saveSystemPrompt(true)}>Save as new</button>}<button className="ghost danger" disabled={!selectedSystemPromptId} onClick={deleteSystemPrompt} title="Delete saved prompt"><Trash2 size={15}/></button></div><textarea rows="3" value={systemPrompt} onChange={e=>setSystemPrompt(e.target.value)} placeholder="Set the assistant's role and response style"/></details>
        <div className="chat-transcript" aria-live="polite" ref={chatTranscriptRef}>{historyLoading?<p className="empty-chat">Loading conversation…</p>:chatMessages.length?chatMessages.map((message,index)=><div className={'chat-bubble '+message.role} key={index}><small>{message.role==='assistant'?provider.toUpperCase():'You'}</small><div>{message.content}</div></div>):<div className="empty-chat"><MessageCircle size={25}/><strong>Start a conversation</strong><span>Send a message to begin. Unsaved chats stay in this tab.</span></div>}{sending&&<div className="chat-bubble assistant typing"><small>{provider.toUpperCase()}</small><div>Thinking…</div></div>}</div>
        <div className="chat-composer"><textarea rows="2" value={prompt} onChange={e=>setPrompt(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing&&!sending&&prompt.trim()){e.preventDefault();test()}}} placeholder={'Message '+(provider==='agy'?'Antigravity':'Codex')+'… (Enter to send, Shift+Enter for a new line)'}/><button disabled={sending||!prompt.trim()} onClick={test} aria-label="Send message"><Send size={17}/>{sending?'Thinking':'Send'}</button></div>{provider==='agy'&&agyUsage&&<small className="composer-hint" title="agy usage counters can be cumulative for a saved conversation">agy usage · input {agyUsage.input_tokens??'—'} · output {agyUsage.output_tokens??'—'} · thinking {agyUsage.thinking_tokens??'—'} · cache {agyUsage.cache_read_tokens??'—'}</small>}<small className="composer-hint">{sessionId?'Continuing this saved session.':provider==='agy'?'Save conversation to resume it later from Sessions.':'Chat context is kept in this tab unless you save the conversation.'}</small>
      </section>
      <section className="panel" id="sessions"><div className="section-title"><h2>Saved CLI Sessions</h2><span className="muted">{sessions.length} found</span></div><p>เลือก Continue เพื่อกลับไปคุยต่อใน CLI เดิม · Antigravity transcript ที่ Gateway เก็บไว้แสดงได้เฉพาะข้อความที่ส่งผ่าน Gateway</p><div className="table-scroll"><table><thead><tr><th>Provider</th><th>Session ID</th><th>Last Updated</th><th>Model</th><th>Actions</th></tr></thead><tbody>{sessions.map(s=><tr key={(s.provider||'codex')+':'+(s.mode||'')+':'+s.id}><td>{s.provider==='agy'?'Antigravity':'Codex'}{s.mode?' · '+s.mode:''}</td><td title={s.id}>{s.id.slice(0,8)}</td><td>{formatDate(s.updated_at)}</td><td>{s.model||'—'}</td><td><button className="secondary session-action" onClick={()=>continueSession(s)}><MessageCircle size={15}/> Continue</button> <button className="ghost danger session-action" onClick={()=>deleteSession(s)} title="Delete session"><Trash2 size={16}/> Delete</button></td></tr>)}{!sessions.length&&<tr><td colSpan="5" className="empty">No saved sessions</td></tr>}</tbody></table></div></section>
      <section className="panel" id="jobs"><div className="section-title"><h2>Recent Jobs</h2><span className="muted">Expires automatically</span></div><div className="table-scroll"><table><thead><tr><th>Job ID</th><th>Status</th><th>Duration</th><th>Created</th></tr></thead><tbody>{(overview?.jobs||[]).map(j=><tr key={j.id}><td title={j.id}>{j.id.slice(0,8)}</td><td><span className={'status '+j.status}>{j.status}</span></td><td>{j.duration_ms!=null?Math.round(j.duration_ms/1000)+' s':'—'}</td><td>{formatDate(j.created_at)}</td></tr>)}{!overview?.jobs?.length&&<tr><td colSpan="4" className="empty">No jobs yet</td></tr>}</tbody></table></div></section>
      <footer><Server size={14}/> Local-first MVP · Session history is stored on disk · Do not expose publicly</footer>
    </main>
  </div>;
}

createRoot(document.getElementById('root')).render(<App/>);
