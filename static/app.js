/* ── Constants ── */
const PLACEHOLDER="/static/avatar-placeholder.svg";
const CROP_W=1024,CROP_H=576,CROP_RATIO=16/9;

let PRESETS=[];
let VOICE_PRESETS=[];

/* ── State ── */
let studio=[],assistants=[],voices=[];
let selectedPersonaId=null,selectedAvatarId=null,selectedAssistantId=null,selectedVoiceId=null;
let vdSelectedPresetId=null;
let pvPresets=[],pvPresetCat="All";
let clonePresets=[],clonePresetCat="All";
let pvDroppedVoiceId=null;
let vdDroppedPersonaId=null;
let navDroppedPersonaId=null,navPresets=[],navPresetCat="All";
let naDroppedAvatarId=null;
let vdCloneSampleFile=null;
let libraryVoices=[];
let _libExpanded=false;

/* ── Rate-limit + retry helpers ── */
function isRateLimitError(e){const s=String(e||"").toLowerCase();return s.includes("rate limit")||s.includes("429")||s.includes("quota")||s.includes("too many requests");}
const _retryAt={};let _retryTick=null;
function _setRetry(k){_retryAt[k]=Date.now()+60000;}
function _retryLeft(k){const t=_retryAt[k];return t?Math.max(0,Math.ceil((t-Date.now())/1000)):0;}
function _ensureTick(){
  if(_retryTick)return;
  (function run(){
    const btns=[...document.querySelectorAll("[data-rk]")];
    if(!btns.length){_retryTick=null;return;}
    btns.forEach(b=>{const s=_retryLeft(b.dataset.rk);b.textContent=s>0?`Retry in ${s}s`:"↻ Retry";b.disabled=s>0;});
    _retryTick=setTimeout(run,1000);
  })();
}
let cropper=null,sourceFile=null,sourceUrl="",croppedBlob=null,croppedUrl="";
let ws=null,wsTimer=null,studioTimer=null;
let lkRoom=null,_pendingDelete=null;

const clientId=(()=>{let id=localStorage.getItem("lili-cid");if(!id){id=crypto.randomUUID();localStorage.setItem("lili-cid",id);}return id;})();
const $=id=>document.getElementById(id);

function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
function setStatus(el,msg,type=""){if(!el)return;el.textContent=msg;el.className="status-msg"+(type?" "+type:"");}
function setThumb(el,path){if(!el)return;el.style.backgroundImage=`url("${(path||PLACEHOLDER).replace(/"/g,"%22")}")`;}
function revoke(url){if(url)URL.revokeObjectURL(url);}
async function api(path,init={}){
  const isForm=init.body instanceof FormData;
  const h={...(init.headers||{})};
  if(!isForm&&!h["Content-Type"])h["Content-Type"]="application/json";
  const res=await fetch(path,{...init,headers:h});
  let data={};try{data=JSON.parse(await res.text());}catch{}
  if(!res.ok)throw new Error(data.detail||data.error||"Request failed");
  return data;
}

/* ── Presets ── */
function renderPresetUI(catsEl,chipsEl,selArr,cat,gender,onToggle,onCat){
  if(!catsEl||!chipsEl)return;
  const cats=["All",...new Set(PRESETS.filter(p=>p.gender==="all"||gender==="unknown"||p.gender===gender).map(p=>p.cat))];
  catsEl.innerHTML="";
  cats.forEach(c=>{const b=document.createElement("button");b.type="button";b.className="preset-cat-btn"+(c===cat?" active":"");b.textContent=c;b.onclick=()=>onCat(c);catsEl.appendChild(b);});
  chipsEl.innerHTML="";
  PRESETS.filter(p=>(cat==="All"||p.cat===cat)&&(p.gender==="all"||gender==="unknown"||p.gender===gender)).forEach(pr=>{
    const ch=document.createElement("button");ch.type="button";ch.className="preset-chip"+(selArr.includes(pr.id)?" selected":"");
    ch.textContent=pr.label;ch.onclick=()=>{onToggle(pr.id);ch.classList.toggle("selected");};chipsEl.appendChild(ch);
  });
}
const p2prompt=sel=>sel.map(id=>PRESETS.find(p=>p.id===id)?.label).filter(Boolean).join(", ");

/* ── Cropper ── */
function destroyCropper(){if(cropper){cropper.destroy();cropper=null;}}
function openCropper(file){sourceFile=file;destroyCropper();revoke(sourceUrl);sourceUrl=URL.createObjectURL(file);
  $("cropper-image").src=sourceUrl;$("cropper-modal").hidden=false;
  $("cropper-image").onload=()=>{cropper=new window.Cropper($("cropper-image"),{aspectRatio:CROP_RATIO,viewMode:1,dragMode:"move",autoCropArea:.9,cropBoxResizable:false,background:false,responsive:true,zoomable:true,wheelZoomRatio:0.1});};
}
function closeCropper(){$("cropper-modal").hidden=true;destroyCropper();}
async function getCropped(){return new Promise((res,rej)=>{const c=cropper?.getCroppedCanvas({width:CROP_W,height:CROP_H,fillColor:"#fff"});if(!c)return rej(new Error("Cropper not ready"));c.toBlob(b=>b?res(b):rej(new Error("Crop failed")),"image/png",.95);});}
function showPersonaPreview(blob){croppedBlob=blob;revoke(croppedUrl);croppedUrl=URL.createObjectURL(blob);
  setThumb($("persona-preview-thumb"),croppedUrl);
  $("persona-upload-zone").hidden=true;$("persona-name-prearea").hidden=true;$("persona-preview-area").hidden=false;
  const cpb=$("create-persona-btn");if(cpb)cpb.disabled=false;
  const pre=$("persona-name-pre-input")?.value.trim();if(pre&&!$("persona-name-input").value.trim())$("persona-name-input").value=pre;
}

/* ── Stage label ── */
function stageLabel(status,stage){
  const M={detecting_gender:"🔍 Looking into the person…",queued:"⏳ Queued",processing:"⚡ Processing…",uploading:"⬆ Uploading…",ready:"✓ Ready",failed:"✗ Failed",cancelled:"⊘ Cancelled"};
  return M[stage]||M[status]||stage||status||"";
}

/* ── Data helpers ── */
const getPersona=id=>studio.find(p=>p.id===(id??selectedPersonaId))||null;
const getAvatar=id=>{const p=getPersona();return p?.avatars?.find(a=>a.id===(id??selectedAvatarId))||null;};
const getAssistant=id=>assistants.find(a=>a.id===(id??selectedAssistantId))||null;
const getVoice=id=>voices.find(v=>v.id===(id??selectedVoiceId))||null;

/* ── Voice playback ── */
function playVoicePreview(voice){
  if(!voice?.preview_path){alert("No preview audio available for this voice yet.");return;}
  const el=$("voice-preview-audio");if(!el)return;
  el.src=voice.preview_path;
  el.play().catch(()=>{});
}
function playDefaultVoicePreview(){
  const el=$("voice-preview-audio");if(!el)return;
  const defaultVoiceId="EXAVITQu4vr4xnSDxMaL";
  const lib=libraryVoices.find(v=>v.voice_id===defaultVoiceId);
  if(lib?.preview_url){el.src=lib.preview_url;}
  else{el.src="/api/studio/voices/preview-default";}
  el.play().catch(()=>{});
}

/* ── Left pane renders ── */
function renderLeftPersonas(){
  const list=$("left-persona-list"),badge=$("persona-count-badge");
  if(badge)badge.textContent=studio.length;
  list.innerHTML="";
  if(!studio.length){list.innerHTML='<p class="pane-hint">No personas yet.</p>';return;}
  studio.forEach(p=>{
    const card=document.createElement("div");
    if(p.status==="processing"){
      card.className="entity-card entity-card--inprog";
      const thumb=document.createElement("div");thumb.className="entity-thumb";thumb.style.position="relative";setThumb(thumb,p.image_path);
      const spin=document.createElement("div");spin.className="entity-spin";thumb.appendChild(spin);
      const info=document.createElement("div");info.className="entity-info";
      info.innerHTML=`<div class="entity-name">${esc(p.name)}</div><div class="entity-stage">${esc(stageLabel(p.status,p.stage))}</div>`;
      const cancelBtn=document.createElement("button");cancelBtn.className="entity-delete-btn";cancelBtn.textContent="✕";cancelBtn.title="Cancel";
      cancelBtn.onclick=e=>{e.stopPropagation();cancelBtn.disabled=true;
        api(`/api/studio/personas/${p.id}/cancel`,{method:"POST"}).then(refreshAll).catch(()=>{cancelBtn.disabled=false;});};
      card.append(thumb,info,cancelBtn);
    }else{
      const g=p.gender==="female"?"female":p.gender==="male"?"male":"unknown";
      const rc=(p.avatars||[]).filter(a=>a.status==="ready").length;
      card.className="entity-card"+(p.id===selectedPersonaId?" active":"");
      card.innerHTML=`<div class="entity-thumb" style="background-image:url('${(p.image_path||PLACEHOLDER).replace(/'/g,"\\'")}')"></div>
        <div class="entity-info"><div class="entity-name">${esc(p.name)}</div>
        <div class="entity-meta"><span class="gender-dot ${g}"></span>${p.gender||"unknown"} · ${rc} avatar${rc!==1?"s":""}</div></div>`;
      const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";
      del.onclick=e=>{e.stopPropagation();openDeleteModal("persona",p.id,p.name);};card.appendChild(del);
      card.addEventListener("click",()=>{selectedPersonaId=p.id;selectedAvatarId=null;showView("persona");});
      card.draggable=true;
      card.addEventListener("dragstart",e=>{e.dataTransfer.setData("text/plain",JSON.stringify({type:"persona",id:p.id,name:p.name,image_path:p.image_path}));e.dataTransfer.effectAllowed="copy";});
    }
    list.appendChild(card);
  });
}

function renderLeftAvatars(){
  const list=$("left-avatar-list"),hint=$("left-avatar-hint"),badge=$("avatar-count-badge");
  list.innerHTML="";const persona=getPersona();
  if(!persona){if(hint)hint.hidden=false;if(badge)badge.textContent="0";return;}
  if(hint)hint.hidden=true;
  const avatars=(persona.avatars||[]).filter(a=>a.status!=="cancelled");
  if(badge)badge.textContent=avatars.filter(a=>a.status==="ready").length;
  if(!avatars.length){list.innerHTML='<p class="pane-hint">No avatars yet.</p>';return;}
  avatars.forEach(av=>{
    const card=document.createElement("div");
    if(av.status==="generating"){
      card.className="entity-card entity-card--draft"+(av.id===selectedAvatarId?" active-purple":"");
      card.style.pointerEvents="auto";card.style.cursor="pointer";
      const thumb=document.createElement("div");thumb.className="entity-thumb sm";thumb.style.position="relative";setThumb(thumb,av.image_path);
      const spin=document.createElement("div");spin.className="entity-spin";thumb.appendChild(spin);
      const info=document.createElement("div");info.className="entity-info";
      info.innerHTML=`<div class="entity-name">${esc(av.name)}</div><div class="entity-stage">⚡ Generating image…</div>`;
      const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";del.style.cssText="color:var(--red);background:rgba(244,63,94,0.14)";
      del.onclick=e=>{e.stopPropagation();openDeleteModal("avatar",av.id,av.name);};
      card.append(thumb,info,del);
      card.addEventListener("click",()=>{selectedAvatarId=av.id;showView("avatar");});
    }else if(av.status==="not_saved"){
      card.className="entity-card entity-card--draft"+(av.id===selectedAvatarId?" active-purple":"");
      const thumb=document.createElement("div");thumb.className="entity-thumb sm";setThumb(thumb,av.image_path);
      const info=document.createElement("div");info.className="entity-info";
      info.innerHTML=`<div class="entity-name">${esc(av.name)}</div><div class="entity-stage" style="color:var(--amber)">◐ Not saved</div>`;
      const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";del.style.cssText="color:var(--red);background:rgba(244,63,94,0.14)";
      del.onclick=e=>{e.stopPropagation();openDeleteModal("avatar",av.id,av.name);};
      card.append(thumb,info,del);
      card.addEventListener("click",()=>{selectedAvatarId=av.id;showView("avatar");});
    }else if(av.status==="ready"){
      card.className="entity-card"+(av.id===selectedAvatarId?" active-purple":"");
      card.innerHTML=`<div class="entity-thumb sm" style="background-image:url('${(av.image_path||PLACEHOLDER).replace(/'/g,"\\'")}')"></div>
        <div class="entity-info"><div class="entity-name">${esc(av.name)}</div>
        <div class="entity-meta">${esc(av.decoration||"No style")}</div></div>`;
      const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";
      del.onclick=e=>{e.stopPropagation();openDeleteModal("avatar",av.id,av.name);};card.appendChild(del);
      card.addEventListener("click",()=>{selectedAvatarId=av.id;showView("avatar");});
      card.draggable=true;
      card.addEventListener("dragstart",e=>{e.dataTransfer.setData("text/plain",JSON.stringify({type:"avatar",id:av.id,name:av.name,image_path:av.image_path,persona_id:av.persona_id}));e.dataTransfer.effectAllowed="copy";});
    }else if(av.status==="processing"){
      card.className="entity-card entity-card--draft"+(av.id===selectedAvatarId?" active-purple":"");
      card.style.pointerEvents="auto";card.style.cursor="pointer";
      const thumb=document.createElement("div");thumb.className="entity-thumb sm";thumb.style.position="relative";setThumb(thumb,av.image_path);
      const spin=document.createElement("div");spin.className="entity-spin";thumb.appendChild(spin);
      const info=document.createElement("div");info.className="entity-info";
      info.innerHTML=`<div class="entity-name">${esc(av.name)}</div><div class="entity-stage">${esc(stageLabel(av.status,av.stage))}</div>`;
      const cancelBtn=document.createElement("button");cancelBtn.className="entity-delete-btn";cancelBtn.textContent="✕";cancelBtn.title="Cancel";cancelBtn.style.cssText="color:var(--red);background:rgba(244,63,94,0.14)";
      cancelBtn.onclick=e=>{e.stopPropagation();cancelBtn.disabled=true;
        api(`/api/studio/avatars/${av.id}/cancel`,{method:"POST"}).then(refreshAll).catch(()=>{cancelBtn.disabled=false;});};
      card.append(thumb,info,cancelBtn);
      card.addEventListener("click",()=>{selectedAvatarId=av.id;showView("avatar");});
    }else if(av.status==="failed"){
      const rl=isRateLimitError(av.last_error);
      card.className="entity-card entity-card--draft"+(av.id===selectedAvatarId?" active-purple":"");
      card.style.pointerEvents="auto";card.style.cursor="pointer";
      card.addEventListener("click",()=>{selectedAvatarId=av.id;showView("avatar");});
      const thumb=document.createElement("div");thumb.className="entity-thumb sm";setThumb(thumb,av.image_path);
      const info=document.createElement("div");info.className="entity-info";
      info.innerHTML=`<div class="entity-name">${esc(av.name)}</div><div class="entity-stage err">✗ ${rl?"Rate limited":"Failed"}</div>`;
      const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";del.style.cssText="color:var(--red);background:rgba(244,63,94,0.14)";
      del.onclick=e=>{e.stopPropagation();openDeleteModal("avatar",av.id,av.name);};
      card.append(thumb,info);
      const rk=`avatar-${av.id}`;if(rl&&!_retryAt[rk])_setRetry(rk);
      const secs=_retryLeft(rk);
      const btn=document.createElement("button");btn.className="btn-retry-sm";btn.dataset.rk=rk;
      btn.textContent=secs>0?`Retry in ${secs}s`:"↻ Retry";btn.disabled=secs>0;
      btn.onclick=async e=>{e.stopPropagation();btn.disabled=true;
        try{await api(`/api/studio/avatars/${av.id}/retry?client_id=${clientId}`,{method:"POST"});await refreshAll();}
        catch(e){if(isRateLimitError(e.message)){_setRetry(rk);_ensureTick();}else{alert("Retry failed: "+e.message);}btn.disabled=false;}};
      card.appendChild(btn);if(rl)_ensureTick();
      card.appendChild(del);
    }
    list.appendChild(card);
  });
}

function renderLeftVoices(){
  const list=$("left-voice-list"),badge=$("voice-count-badge");
  const ready=voices.filter(v=>v.status==="ready");
  if(badge)badge.textContent=ready.length;
  list.innerHTML="";
  // Permanent "Default Voice" card — always first, cannot be deleted
  {
    const dc=document.createElement("div");dc.className="voice-card";dc.style.cursor="pointer";
    const di=document.createElement("div");di.className="voice-thumb-icon";di.textContent="🔊";
    const info=document.createElement("div");info.className="voice-card-info";
    info.innerHTML='<div class="voice-card-name">Default Voice</div><div class="voice-card-meta"><span class="voice-source-pill designed" style="background:rgba(100,100,255,.15);color:#b0b8ff;border-color:rgba(100,100,255,.35)">System</span>Built-in ElevenLabs voice</div>';
    const play=document.createElement("button");play.className="voice-play-btn";play.textContent="▶";play.title="Play preview";
    play.onclick=e=>{e.stopPropagation();playDefaultVoicePreview();};
    dc.append(di,info,play);
    dc.addEventListener("click",playDefaultVoicePreview);
    dc.draggable=true;
    dc.addEventListener("dragstart",e=>{e.dataTransfer.setData("text/plain",JSON.stringify({type:"voice",id:0,name:"Default Voice",isDefault:true}));e.dataTransfer.effectAllowed="copy";});
    list.appendChild(dc);
  }
  if(!voices.length)return;
  voices.forEach(v=>{
    const card=document.createElement("div");
    if(v.status==="processing"){
      card.className="voice-card entity-card--inprog";
      card.style.pointerEvents="none";
      const icon=document.createElement("div");icon.className="voice-thumb-icon";icon.textContent="🎙";
      const info=document.createElement("div");info.className="voice-card-info";
      info.innerHTML=`<div class="voice-card-name">${esc(v.name)}</div><div class="entity-stage">⚡ Designing…</div>`;
      const spin=document.createElement("div");spin.className="entity-spin";spin.style.cssText="position:static;margin-left:auto;flex-shrink:0";
      card.append(icon,info,spin);
    }else if(v.status==="failed"){
      card.className="voice-card";card.style.cursor="default";
      const icon=document.createElement("div");icon.className="voice-thumb-icon";icon.textContent="🎙";
      const info=document.createElement("div");info.className="voice-card-info";
      const errSnip=v.last_error?String(v.last_error).slice(0,60):"Unknown error";
      info.innerHTML=`<div class="voice-card-name">${esc(v.name)}</div><div class="entity-stage err" title="${esc(v.last_error||'')}">✗ ${esc(errSnip)}</div>`;
      const retry=document.createElement("button");retry.className="btn-retry-sm";retry.textContent="↻ Retry";retry.title="Retry";
      retry.onclick=async e=>{e.stopPropagation();retry.disabled=true;retry.textContent="…";
        try{await api(`/api/studio/voices/${v.id}/retry?client_id=${clientId}`,{method:"POST"});await refreshAll();}
        catch(er){alert("Retry failed: "+er.message);retry.disabled=false;retry.textContent="↻ Retry";}};
      const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";del.style.cssText="color:var(--red);background:rgba(244,63,94,0.14)";
      del.onclick=e=>{e.stopPropagation();openDeleteModal("voice",v.id,v.name);};
      card.append(icon,info,retry,del);
    }else{
      card.className="voice-card"+(v.id===selectedVoiceId?" active":"");
      const pill=v.source==="cloned"?"cloned":"designed";
      const icon=document.createElement("div");icon.className="voice-thumb-icon";icon.textContent="🎙";
      const info=document.createElement("div");info.className="voice-card-info";
      info.innerHTML=`<div class="voice-card-name">${esc(v.name)}</div><div class="voice-card-meta"><span class="voice-source-pill ${pill}">${pill==="cloned"?"Clone":"AI"}</span>${esc(v.description?.slice(0,40)||"ElevenLabs")}</div>`;
      const play=document.createElement("button");play.className="voice-play-btn";play.textContent="▶";play.title="Play preview";
      play.onclick=e=>{e.stopPropagation();playVoicePreview(v);};
      const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";
      del.onclick=e=>{e.stopPropagation();openDeleteModal("voice",v.id,v.name);};
      card.append(icon,info,play,del);
      card.addEventListener("click",()=>playVoicePreview(v));
      card.draggable=true;
      card.addEventListener("dragstart",e=>{e.dataTransfer.setData("text/plain",JSON.stringify({type:"voice",id:v.id,name:v.name}));e.dataTransfer.effectAllowed="copy";});
    }
    list.appendChild(card);
  });

  // ── ElevenLabs Library subsection ──
  if(libraryVoices.length){
    const hdr=document.createElement("div");
    hdr.style.cssText="display:flex;align-items:center;gap:6px;padding:8px 4px 4px;cursor:pointer;user-select:none;grid-column:1/-1";
    hdr.innerHTML=`<span style="font-size:10px;font-weight:700;letter-spacing:.08em;color:var(--muted);text-transform:uppercase">ElevenLabs Library</span><span style="font-size:10px;color:var(--muted)">(${libraryVoices.length})</span><span style="margin-left:auto;font-size:11px;color:var(--muted)">${_libExpanded?"▲":"▼"}</span>`;
    hdr.addEventListener("click",()=>{_libExpanded=!_libExpanded;renderLeftVoices();});
    list.appendChild(hdr);
    if(_libExpanded){
      libraryVoices.forEach(lv=>{
        const lc=document.createElement("div");lc.className="voice-card";lc.style.cssText="margin-left:4px;opacity:.9";
        const li=document.createElement("div");li.className="voice-thumb-icon";li.style.cssText="font-size:14px";li.textContent="🎙";
        const linfo=document.createElement("div");linfo.className="voice-card-info";
        const gender=lv.labels?.gender||"";const accent=lv.labels?.accent||"";
        const meta=[gender,accent].filter(Boolean).join(" · ")||"ElevenLabs";
        linfo.innerHTML=`<div class="voice-card-name">${esc(lv.name)}</div><div class="voice-card-meta"><span class="voice-source-pill designed" style="background:rgba(100,200,100,.1);color:#a0d8a0;border-color:rgba(100,200,100,.3)">Library</span>${esc(meta)}</div>`;
        const lplay=document.createElement("button");lplay.className="voice-play-btn";lplay.textContent="▶";lplay.title="Play preview";
        lplay.onclick=e=>{e.stopPropagation();const el=$("voice-preview-audio");if(el&&lv.preview_url){el.src=lv.preview_url;el.play().catch(()=>{});}};
        lc.append(li,linfo,lplay);
        lc.addEventListener("click",()=>{const el=$("voice-preview-audio");if(el&&lv.preview_url){el.src=lv.preview_url;el.play().catch(()=>{});}});
        lc.draggable=true;
        lc.addEventListener("dragstart",e=>{e.dataTransfer.setData("text/plain",JSON.stringify({type:"library-voice",voice_id:lv.voice_id,name:lv.name,preview_url:lv.preview_url}));e.dataTransfer.effectAllowed="copy";});
        list.appendChild(lc);
      });
    }
  }
}

function renderLeftContacts(){
  const list=$("left-contacts-list"),badge=$("contacts-count-badge");
  const ready=assistants.filter(a=>!a.status||a.status==="ready");
  if(badge)badge.textContent=ready.length;list.innerHTML="";
  if(!ready.length){list.innerHTML='<p class="pane-hint">No assistants yet.</p>';return;}
  ready.forEach(asst=>{
    const p=studio.find(x=>x.id===asst.persona_id),av=p?.avatars?.find(x=>x.id===asst.avatar_id);
    const item=document.createElement("div");
    item.className="contact-item"+(asst.id===selectedAssistantId?" active":"");
    item.innerHTML=`<div class="contact-thumb-sm" style="background-image:url('${(av?.image_path||PLACEHOLDER).replace(/'/g,"\\'")}')"></div>
      <div><div class="contact-item-name">${esc(asst.name)}</div>
      <div class="contact-item-meta">${esc(p?.name||"")} › ${esc(av?.name||"")}</div></div>
      <div class="contact-call-dot"></div>`;
    const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";
    del.onclick=e=>{e.stopPropagation();openDeleteModal("assistant",asst.id,asst.name);};item.appendChild(del);
    item.addEventListener("click",()=>{selectedAssistantId=asst.id;showView("assistant");});
    list.appendChild(item);
  });
}

function renderAll(){renderLeftPersonas();renderLeftAvatars();renderLeftVoices();renderLeftContacts();}

/* ── Center views ── */
const ALL_VIEWS=["cv-welcome","cv-new-persona","cv-persona","cv-avatar","cv-assistant","cv-voice-design","cv-new-avatar","cv-new-assistant"];
function showView(view){
  ALL_VIEWS.forEach(v=>{const e=$(v);if(e)e.hidden=true;});
  const target=$("cv-"+view);if(target)target.hidden=false;
  if(view==="new-persona")initNewPersona();
  else if(view==="persona")initPersonaView();
  else if(view==="avatar")initAvatarView();
  else if(view==="assistant")initAssistantView();
  else if(view==="voice-design")initVoiceDesignView();
  else if(view==="new-avatar")initNewAvatarView();
  else if(view==="new-assistant")initNewAssistantView();
  renderAll();
}

/* ── Tab switching ── */
document.addEventListener("click",e=>{
  const btn=e.target.closest(".tab-btn");if(!btn||!btn.closest(".tab-bar"))return;
  const bar=btn.closest(".tab-bar");
  bar.querySelectorAll(".tab-btn").forEach(b=>b.classList.remove("active"));btn.classList.add("active");
  const view=btn.closest(".center-view");
  view?.querySelectorAll(".tab-pane").forEach(p=>{p.hidden=p.id!==btn.dataset.tab;});
});

/* ── New Persona ── */
function initNewPersona(){
  $("persona-upload-zone").hidden=false;$("persona-name-prearea").hidden=false;$("persona-preview-area").hidden=true;
  const cpb=$("create-persona-btn");if(cpb)cpb.disabled=true;
  setStatus($("persona-status"),"");
}

/* ── Persona view ── */
function initPersonaView(){
  const p=getPersona();if(!p){showView("welcome");return;}
  setThumb($("pv-thumb"),p.image_path);$("pv-name").textContent=p.name;
  const gp=$("pv-gender");gp.textContent=p.gender==="female"?"♀ Female":p.gender==="male"?"♂ Male":"⚥ Unknown";
  const en=$("edit-persona-name");if(en)en.value=p.name;
  setStatus($("edit-persona-status"),"");
  const gender=p.gender||"unknown";
  renderPresetUI($("pv-preset-cats"),$("pv-preset-chips"),pvPresets,pvPresetCat,gender,
    id=>{const i=pvPresets.indexOf(id);i>=0?pvPresets.splice(i,1):pvPresets.push(id);renderAvPromptPreview();},
    cat=>{pvPresetCat=cat;initPersonaView();});
  const avName=$("av-name-input");if(avName)avName.value=p.name;
  setStatus($("av-status"),"");
  // Set up voice drop slot
  setupPvVoiceDropSlot(p);
}

function setupPvVoiceDropSlot(p){
  pvDroppedVoiceId=p.voice_ref_id?p.voice_ref_id:null;
  renderPvVoiceSlot();
  const slot=$("pv-voice-drop-slot");if(!slot)return;
  slot.ondragover=e=>{e.preventDefault();slot.classList.add("drag-over");};
  slot.ondragleave=()=>slot.classList.remove("drag-over");
  slot.ondrop=async e=>{
    e.preventDefault();slot.classList.remove("drag-over");
    try{
      const d=JSON.parse(e.dataTransfer.getData("text/plain")||"{}");
      if(d.type==="voice"){pvDroppedVoiceId=d.id;renderPvVoiceSlot();}
      else if(d.type==="library-voice"){
        const v=await api("/api/studio/voices/from-library",{method:"POST",body:JSON.stringify({voice_id:d.voice_id,name:d.name,preview_url:d.preview_url})});
        if(!voices.find(x=>x.id===v.id))voices=[v,...voices];
        pvDroppedVoiceId=v.id;renderPvVoiceSlot();renderLeftVoices();
      }
    }catch(err){setStatus($("edit-persona-status"),err.message,"error");}
  };
  // Update badge info row
  const badge=$("pv-voice-badge-inline"),meta=$("pv-voice-meta-inline"),playBtn=$("pv-voice-play-btn"),unlinkBtn=$("pv-voice-unlink-btn");
  // Match by voice_ref_id first; fall back to matching by ElevenLabs voice_id
  // (older personas created via AI-design path may not have voice_ref_id set).
  const linkedVoice=p.voice_ref_id
    ?voices.find(x=>x.id===p.voice_ref_id)
    :(p.voice_id?voices.find(x=>x.voice_id===p.voice_id):null);
  if(badge){
    const src=p.voice_source||"",st=p.voice_status||"";
    if(st==="processing"){badge.textContent="Processing…";badge.className="voice-badge processing";}
    else if(src==="designed"&&p.voice_id){badge.textContent="AI-designed";badge.className="voice-badge designed";}
    else if(src==="cloned"&&p.voice_id){badge.textContent="Cloned";badge.className="voice-badge cloned";}
    else{badge.textContent="Default";badge.className="voice-badge";}
  }
  if(meta)meta.textContent=linkedVoice?`ElevenLabs · ${linkedVoice.name}`:p.voice_id?`ElevenLabs · ${p.voice_id.slice(0,10)}…`:"No custom voice";
  const previewUrl=linkedVoice?.preview_url||p.voice_preview_path||"";
  if(playBtn){playBtn.hidden=!previewUrl;playBtn.onclick=()=>{const audio=$("voice-preview-audio");if(audio&&previewUrl){audio.src=previewUrl;audio.play().catch(()=>{});}};}
  if(unlinkBtn)unlinkBtn.hidden=!p.voice_id;
}

function renderPvVoiceSlot(){
  const slot=$("pv-voice-drop-slot");if(!slot)return;
  if(pvDroppedVoiceId!==null){
    const isDefault=pvDroppedVoiceId===0;
    const v=isDefault?null:voices.find(x=>x.id===pvDroppedVoiceId);
    const name=isDefault?"Default Voice":v?v.name:`Voice #${pvDroppedVoiceId}`;
    const pill=isDefault?"system":v?.source==="cloned"?"cloned":"designed";
    const label=isDefault?"System":pill==="cloned"?"Clone":"AI";
    const pillStyle=isDefault?"background:rgba(100,100,255,.15);color:#b0b8ff;border-color:rgba(100,100,255,.35)":"";
    slot.innerHTML=`<span class="voice-source-pill ${pill}" style="${pillStyle}">${label}</span><span style="font-size:13px;font-weight:600;flex:1">${esc(name)}</span><button type="button" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px;line-height:1;padding:0" title="Clear" onclick="pvDroppedVoiceId=null;renderPvVoiceSlot()">✕</button>`;
  }else{
    slot.innerHTML='<span class="drop-hint">Drag a voice here from the Voices panel</span>';
  }
}

$("pv-voice-unlink-btn")?.addEventListener("click",async()=>{
  const p=getPersona();if(!p)return;
  if(!confirm("Unlink voice from this persona?"))return;
  try{
    await api(`/api/studio/personas/${p.id}`,{method:"PATCH",body:JSON.stringify({voice_ref_id:null})});
    pvDroppedVoiceId=null;renderPvVoiceSlot();
    await refreshAll();initPersonaView();
  }catch(e){setStatus($("edit-persona-status"),e.message,"error");}
});

/* ── Avatar view ── */
function initAvatarView(){
  const av=getAvatar(),p=getPersona();if(!av||!p){showView("welcome");return;}
  setThumb($("avv-thumb"),av.image_path);$("avv-name").textContent=av.name;$("avv-persona").textContent=p.name;
  const inProgress=av.status==="generating"||av.status==="processing"||av.status==="failed"||av.status==="not_saved";
  const notSaved=av.status==="not_saved";
  $("avv-not-saved-block").hidden=!inProgress;
  $("avv-not-saved-actions").hidden=!notSaved;
  document.querySelector('.tab-bar[data-group="avv"]').hidden=inProgress;
  $("avv-assistant").hidden=inProgress;
  $("avv-clone").hidden=true;
  $("avv-assistant-actions").hidden=inProgress;
  $("avv-clone-actions").hidden=true;
  if(inProgress){
    $("avv-preview-img").src=av.image_path||PLACEHOLDER;
    const lbl=$("avv-preview-label");
    const hint=$("avv-not-saved-block").querySelector("p[style]");
    const spin=$("avv-preview-spin");
    let labelText="Not saved",labelBg="rgba(245,158,11,.85)",hintText="This avatar exists only as a preview. Click Save Avatar to register it with Simli.";
    if(av.status==="generating"){labelText="Generating…";labelBg="rgba(168,85,247,.85)";hintText="The image is being generated by AI. Please wait — this can take 10–30 seconds.";}
    else if(av.status==="processing"){labelText="Uploading to Simli…";labelBg="rgba(14,143,161,.85)";hintText="Image generated. Now registering with Simli — this can take a minute.";}
    else if(av.status==="failed"){labelText="Failed";labelBg="rgba(244,63,94,.85)";hintText="Generation failed: "+(av.last_error||"unknown error");}
    if(lbl){lbl.textContent=labelText;lbl.style.background=labelBg;}
    if(hint)hint.textContent=hintText;
    if(spin)spin.hidden=!(av.status==="generating"||av.status==="processing");
    setStatus($("avv-save-status"),"");
    return;
  }
  setStatus($("asst-status"),"");
  clonePresets=[...(av.preset_ids||[])];clonePresetCat="All";
  const cn=$("clone-av-name");if(cn)cn.value=av.name+" Clone";
  const ct=$("clone-av-theme");if(ct)ct.value=av.theme_prompt||"";
  renderPresetUI($("clone-preset-cats"),$("clone-preset-chips"),clonePresets,clonePresetCat,p.gender||"unknown",
    id=>{const i=clonePresets.indexOf(id);i>=0?clonePresets.splice(i,1):clonePresets.push(id);},
    cat=>{clonePresetCat=cat;initAvatarView();});
  const cap=$("clone-av-preview-area");if(cap)cap.hidden=true;setStatus($("clone-av-status"),"");
}

$("avv-save-avatar-btn").addEventListener("click",async()=>{
  const av=getAvatar(),p=getPersona();if(!av||!p)return;
  const btn=$("avv-save-avatar-btn");btn.disabled=true;
  setStatus($("avv-save-status"),"Uploading to Simli…");
  try{
    const fd=new FormData();
    fd.append("persona_id",p.id);fd.append("name",av.name);
    fd.append("decoration",av.decoration||"");fd.append("theme_prompt",av.theme_prompt||"");
    fd.append("preset_ids",JSON.stringify(av.preset_ids||[]));
    fd.append("draft_avatar_id",av.id);fd.append("client_id",clientId);
    await api("/api/studio/avatars",{method:"POST",body:fd});
    setStatus($("avv-save-status"),"Saved!","success");await refreshAll();
  }catch(e){setStatus($("avv-save-status"),e.message,"error");}
  finally{btn.disabled=false;}
});

/* ── Assistant view ── */
function initAssistantView(){
  const asst=getAssistant();if(!asst){showView("welcome");return;}
  const p=studio.find(x=>x.id===asst.persona_id),av=p?.avatars?.find(x=>x.id===asst.avatar_id);
  setThumb($("ca-thumb"),av?.image_path);$("ca-name").textContent=asst.name;$("ca-sub").textContent=(p?.name||"")+" › "+(av?.name||"");
  setThumb($("ca-call-thumb"),av?.image_path);$("ca-call-name").textContent=asst.name;
  setStatus($("ca-call-status"),"");
  const cn=$("clone-asst-name");if(cn)cn.value=asst.name+" Clone";
  const cp=$("clone-asst-prompt");if(cp)cp.value=asst.prompt||"";
  const cm=$("clone-asst-msg");if(cm)cm.value=asst.first_message||"";
  setStatus($("clone-asst-status"),"");
}

/* ── Voice Design view ── */
function initVoiceDesignView(){
  // Reset design tab
  const nameEl=$("vd-name");if(nameEl)nameEl.value="";
  const descEl=$("vd-description");if(descEl)descEl.value="";
  setStatus($("vd-design-status"),"");
  // Reset clone tab
  const cnEl=$("vd-clone-name");if(cnEl)cnEl.value="";
  const sampleEl=$("vd-clone-sample");if(sampleEl)sampleEl.value="";
  const snEl=$("vd-clone-sample-name");if(snEl)snEl.textContent="";
  const origArea=$("vd-clone-original-area");if(origArea)origArea.hidden=true;
  setStatus($("vd-clone-status"),"");
  vdCloneSampleFile=null;
  // Reset persona toggle + slot
  vdDroppedPersonaId=null;
  const toggle=$("vd-include-persona");
  const slotWrap=$("vd-persona-slot-wrap");
  if(toggle){toggle.checked=false;toggle.onchange=()=>{if(slotWrap)slotWrap.hidden=!toggle.checked;if(!toggle.checked){vdDroppedPersonaId=null;renderVdPersonaSlot();}};}
  if(slotWrap)slotWrap.hidden=true;
  renderVdPersonaSlot();
  const slot=$("vd-persona-drop-slot");
  if(slot){
    slot.ondragover=e=>{e.preventDefault();slot.classList.add("drag-over");};
    slot.ondragleave=()=>slot.classList.remove("drag-over");
    slot.ondrop=e=>{
      e.preventDefault();slot.classList.remove("drag-over");
      try{const d=JSON.parse(e.dataTransfer.getData("text/plain")||"{}");if(d.type!=="persona")return;vdDroppedPersonaId=d.id;renderVdPersonaSlot();}catch{}
    };
  }
  // Reset preset indicator
  vdSelectedPresetId=null;
  const ind=$("vd-preset-indicator");if(ind)ind.hidden=true;
  const descEl2=$("vd-description");if(descEl2){descEl2.readOnly=false;descEl2.classList.remove("preset-locked");descEl2.placeholder="e.g. warm, confident, professional female narrator";}
  // Render preset chips
  const chipsEl=$("vd-preset-chips");
  if(chipsEl){
    chipsEl.innerHTML="";
    VOICE_PRESETS.forEach(vp=>{
      const btn=document.createElement("button");btn.type="button";btn.className="voice-preset-chip";btn.textContent=vp.label;btn.dataset.presetId=vp.id;
      btn.onclick=()=>applyVoicePreset(vp.id,vp.label);
      chipsEl.appendChild(btn);
    });
  }
  // Ensure design tab is active
  document.querySelector('.tab-btn[data-tab="vd-design"]')?.click();
}

function applyVoicePreset(id,label){
  vdSelectedPresetId=id;
  const d=$("vd-description");
  if(d){d.value="";d.readOnly=true;d.classList.add("preset-locked");d.placeholder=label;}
  const ind=$("vd-preset-indicator");
  if(ind){
    ind.hidden=false;
    ind.innerHTML=`<span style="color:var(--secondary);font-weight:600">✦ Preset: ${esc(label)}</span><button type="button" onclick="clearVoicePreset()" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:13px;padding:0 0 0 6px">✕</button>`;
  }
  document.querySelectorAll(".voice-preset-chip").forEach(b=>{b.classList.toggle("selected",b.dataset.presetId===id);});
}
function clearVoicePreset(){
  vdSelectedPresetId=null;
  const d=$("vd-description");
  if(d){d.value="";d.readOnly=false;d.classList.remove("preset-locked");d.placeholder="e.g. warm, confident, professional female narrator";}
  const ind=$("vd-preset-indicator");if(ind)ind.hidden=true;
  document.querySelectorAll(".voice-preset-chip").forEach(b=>b.classList.remove("selected"));
}

function renderVdPersonaSlot(){
  const slot=$("vd-persona-drop-slot");if(!slot)return;
  if(vdDroppedPersonaId){
    const p=studio.find(x=>x.id===vdDroppedPersonaId);
    const name=p?p.name:`Persona #${vdDroppedPersonaId}`;
    const imgUrl=p?.image_path||PLACEHOLDER;
    slot.innerHTML=`<img class="slot-thumb" src="${imgUrl.replace(/"/g,'%22')}" alt=""/><span style="font-size:13px;font-weight:600;flex:1">${esc(name)}</span><button type="button" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px;line-height:1;padding:0" title="Clear" onclick="vdDroppedPersonaId=null;renderVdPersonaSlot()">✕</button>`;
  }else{
    slot.innerHTML='<span class="drop-hint">Drag a persona from the Personas panel</span>';
  }
}


$("vd-design-submit-btn")?.addEventListener("click",async()=>{
  const name=($("vd-name")?.value||"").trim()||"New Voice";
  const description=($("vd-description")?.value||"").trim();
  const btn=$("vd-design-submit-btn"),statusEl=$("vd-design-status");
  const includePersona=$("vd-include-persona")?.checked&&!!vdDroppedPersonaId;
  if(!vdSelectedPresetId&&!description&&!includePersona){setStatus(statusEl,"Please describe the voice, select a preset, or enable persona voice profile.","error");return;}
  btn.disabled=true;setStatus(statusEl,"Creating voice…");
  try{
    const fd=new FormData();
    fd.append("name",name);
    if(vdSelectedPresetId){fd.append("voice_preset_id",vdSelectedPresetId);}
    else{fd.append("description",description);}
    if(vdDroppedPersonaId){fd.append("persona_id",String(vdDroppedPersonaId));fd.append("include_persona_traits",includePersona?"true":"false");}
    fd.append("client_id",clientId);
    await api("/api/studio/voices/design",{method:"POST",body:fd});
    setStatus(statusEl,"Voice design started — watch the Voices panel.","success");
    await refreshAll();
  }catch(e){setStatus(statusEl,e.message,"error");}
  finally{btn.disabled=false;}
});

$("vd-clone-sample")?.addEventListener("change",e=>{
  const f=e.target.files?.[0];vdCloneSampleFile=f||null;
  const sn=$("vd-clone-sample-name");if(sn)sn.textContent=f?`${f.name} · ${Math.round(f.size/1024)} KB`:"";
  const orig=$("vd-clone-original-area"),audio=$("vd-clone-original-audio");
  if(f&&orig&&audio){orig.hidden=false;audio.src=URL.createObjectURL(f);}
  else if(orig)orig.hidden=true;
});

$("vd-clone-submit-btn")?.addEventListener("click",async()=>{
  if(!vdCloneSampleFile){setStatus($("vd-clone-status"),"Pick an audio file first.","error");return;}
  const name=($("vd-clone-name")?.value||"").trim()||"Cloned Voice";
  const btn=$("vd-clone-submit-btn"),statusEl=$("vd-clone-status");
  btn.disabled=true;setStatus(statusEl,"Cloning voice…");
  try{
    const fd=new FormData();fd.append("voice_sample",vdCloneSampleFile,vdCloneSampleFile.name);fd.append("name",name);fd.append("client_id",clientId);
    await api("/api/studio/voices/clone",{method:"POST",body:fd});
    setStatus(statusEl,"Voice cloning started — watch the Voices panel.","success");
    await refreshAll();
  }catch(e){setStatus(statusEl,e.message,"error");}
  finally{btn.disabled=false;}
});

/* ── Persona upload ── */
$("persona-upload-zone").addEventListener("click",()=>$("persona-image-input").click());
$("persona-upload-zone").addEventListener("dragover",e=>{e.preventDefault();$("persona-upload-zone").classList.add("drag-over");});
$("persona-upload-zone").addEventListener("dragleave",()=>$("persona-upload-zone").classList.remove("drag-over"));
$("persona-upload-zone").addEventListener("drop",e=>{e.preventDefault();$("persona-upload-zone").classList.remove("drag-over");const f=e.dataTransfer.files[0];if(f)openCropper(f);});
$("pick-persona-image-btn").addEventListener("click",e=>{e.stopPropagation();$("persona-image-input").click();});
$("persona-image-input").addEventListener("change",()=>{const[f]=$("persona-image-input").files||[];if(f)openCropper(f);});
$("edit-crop-btn").addEventListener("click",()=>{if(sourceFile)openCropper(sourceFile);});
$("cancel-crop-btn").addEventListener("click",closeCropper);
$("confirm-crop-btn").addEventListener("click",async()=>{
  $("confirm-crop-btn").disabled=true;
  try{const b=await getCropped();showPersonaPreview(b);closeCropper();}catch(e){alert(e.message);}finally{$("confirm-crop-btn").disabled=false;}
});
$("crop-zoom-in").addEventListener("click",()=>cropper?.zoom(0.1));
$("crop-zoom-out").addEventListener("click",()=>cropper?.zoom(-0.1));
$("create-persona-btn").addEventListener("click",async()=>{
  const name=($("persona-name-input")?.value||$("persona-name-pre-input")?.value||"").trim();
  if(!name)return setStatus($("persona-status"),"Enter a name.","error");
  if(!croppedBlob)return setStatus($("persona-status"),"Crop an image first.","error");
  $("create-persona-btn").disabled=true;setStatus($("persona-status"),"Uploading…");
  try{
    const fd=new FormData();fd.append("name",name);fd.append("persona_image",croppedBlob,"persona.png");fd.append("client_id",clientId);
    const p=await api("/api/studio/personas",{method:"POST",body:fd});
    croppedBlob=null;revoke(croppedUrl);croppedUrl="";revoke(sourceUrl);sourceUrl="";sourceFile=null;$("persona-image-input").value="";
    selectedPersonaId=p.id;selectedAvatarId=null;
    setStatus($("persona-status"),`'${p.name}' created!`,"success");await refreshAll();showView("persona");
  }catch(e){setStatus($("persona-status"),e.message,"error");}finally{$("create-persona-btn").disabled=false;}
});

/* ── Save / Delete Persona ── */
$("save-persona-btn").addEventListener("click",async()=>{
  const name=$("edit-persona-name")?.value.trim();if(!name||!selectedPersonaId)return;
  $("save-persona-btn").disabled=true;
  try{
    const body={name,voice_ref_id:pvDroppedVoiceId||null};
    await api(`/api/studio/personas/${selectedPersonaId}`,{method:"PATCH",body:JSON.stringify(body)});
    setStatus($("edit-persona-status"),"Saved!","success");await refreshAll();initPersonaView();
  }
  catch(e){setStatus($("edit-persona-status"),e.message,"error");}finally{$("save-persona-btn").disabled=false;}
});
$("delete-persona-from-edit-btn").addEventListener("click",()=>{const p=getPersona();if(p)openDeleteModal("persona",p.id,p.name);});

/* ── Avatar preview/generate ── */
function renderAvPromptPreview(){
  const el=$("av-prompt-preview");if(!el)return;
  const grouped={};
  pvPresets.forEach(id=>{
    const p=PRESETS.find(x=>x.id===id);if(!p)return;
    (grouped[p.cat]=grouped[p.cat]||[]).push(p.label);
  });
  const order=["Outfit","Hair","Facial Hair","Makeup","Accessories","Jewelry","Background"];
  const labelMap={Outfit:"Clothing"};
  const lines=[];
  order.forEach(cat=>{
    if(grouped[cat]){
      const lbl=labelMap[cat]||cat;
      lines.push(`<b style="color:var(--muted)">${lbl}:</b> ${esc(grouped[cat].join(", "))}`);
    }
  });
  if(lines.length){el.innerHTML=lines.join("<br>");el.hidden=false;}
  else{el.hidden=true;}
}

async function genAvatarPreview(personaId,name,sel,themeEl,previewAreaEl,imgEl,statusEl){
  const tp=[p2prompt(sel),themeEl?.value.trim()||""].filter(Boolean).join(", ");
  setStatus(statusEl,"Generating preview…");if(previewAreaEl)previewAreaEl.hidden=true;
  const fd=new FormData();fd.append("persona_id",personaId);fd.append("name",name);fd.append("theme_prompt",tp);fd.append("preset_ids",JSON.stringify(sel));
  const data=await api("/api/studio/avatars/preview",{method:"POST",body:fd});
  if(imgEl&&data.preview_url)imgEl.src=data.preview_url;if(previewAreaEl)previewAreaEl.hidden=false;
  setStatus(statusEl,"Preview ready.");return tp;
}

function applyAvSkipStyle(){
  const cb=$("av-skip-style"),block=$("av-style-block");
  if(!cb||!block)return;
  block.classList.toggle("style-block-disabled",cb.checked);
}
$("av-skip-style")?.addEventListener("change",applyAvSkipStyle);

$("av-generate-btn").addEventListener("click",async()=>{
  const p=getPersona();if(!p)return;
  const nameEl=$("av-name-input");
  const name=(nameEl?.value.trim())||p.name;
  const customPrompt=$("av-theme-input")?.value.trim()||"";
  const skipStyle=!!$("av-skip-style")?.checked;
  const btn=$("av-generate-btn");btn.disabled=true;
  setStatus($("av-status"),"Queued — generating in background…","success");
  try{
    const fd=new FormData();
    fd.append("persona_id",p.id);fd.append("name",name);
    fd.append("preset_ids",JSON.stringify(skipStyle?[]:pvPresets));
    fd.append("custom_prompt",skipStyle?"":customPrompt);
    fd.append("skip_style",skipStyle?"true":"false");
    fd.append("client_id",clientId);
    await api("/api/studio/avatars/preview",{method:"POST",body:fd});
    pvPresets=[];pvPresetCat="All";if(nameEl)nameEl.value=p.name;
    const ti=$("av-theme-input");if(ti)ti.value="";
    const sk=$("av-skip-style");if(sk)sk.checked=false;applyAvSkipStyle();
    renderAvPromptPreview();initPersonaView();await refreshAll();
  }catch(e){setStatus($("av-status"),e.message,"error");}
  finally{btn.disabled=false;}
});

/* ── New Avatar view (standalone, with persona drop slot) ── */
function renderNavPersonaSlot(){
  const slot=$("nav-persona-drop-slot");if(!slot)return;
  const btn=$("nav-generate-btn");
  if(!navDroppedPersonaId){
    slot.innerHTML='<span class="drop-hint">Drag a persona here from the Personas panel</span>';
    if(btn)btn.disabled=true;return;
  }
  const p=studio.find(x=>x.id===navDroppedPersonaId);
  if(!p){navDroppedPersonaId=null;return renderNavPersonaSlot();}
  slot.innerHTML=`<img class="slot-thumb" src="${esc(p.image_path||PLACEHOLDER)}" alt=""/><b>${esc(p.name)}</b><span class="drop-hint" style="margin-left:auto">${esc(p.gender||"unknown")}</span>`;
  if(btn)btn.disabled=false;
}
function renderNavPromptPreview(){
  const el=$("nav-prompt-preview");if(!el)return;
  const grouped={};
  navPresets.forEach(id=>{const pr=PRESETS.find(x=>x.id===id);if(!pr)return;(grouped[pr.cat]=grouped[pr.cat]||[]).push(pr.label);});
  const order=["Outfit","Hair","Facial Hair","Makeup","Accessories","Jewelry","Background"];
  const labelMap={Outfit:"Clothing"};
  const lines=[];
  order.forEach(cat=>{if(grouped[cat]){const lbl=labelMap[cat]||cat;lines.push(`<b style="color:var(--muted)">${lbl}:</b> ${esc(grouped[cat].join(", "))}`);}});
  if(lines.length){el.innerHTML=lines.join("<br>");el.hidden=false;}else{el.hidden=true;}
}
function renderNavPresets(){
  const p=navDroppedPersonaId?studio.find(x=>x.id===navDroppedPersonaId):null;
  const gender=p?.gender||"unknown";
  renderPresetUI($("nav-preset-cats"),$("nav-preset-chips"),navPresets,navPresetCat,gender,
    id=>{const i=navPresets.indexOf(id);i>=0?navPresets.splice(i,1):navPresets.push(id);renderNavPromptPreview();},
    cat=>{navPresetCat=cat;renderNavPresets();});
}
function initNewAvatarView(){
  if(selectedPersonaId&&!navDroppedPersonaId)navDroppedPersonaId=selectedPersonaId;
  if(navDroppedPersonaId){const p=studio.find(x=>x.id===navDroppedPersonaId);if(!p)navDroppedPersonaId=null;}
  renderNavPersonaSlot();
  renderNavPresets();
  renderNavPromptPreview();
  setStatus($("nav-status"),"");
  const slot=$("nav-persona-drop-slot");
  slot.ondragover=e=>{e.preventDefault();slot.classList.add("drag-over");};
  slot.ondragleave=()=>slot.classList.remove("drag-over");
  slot.ondrop=e=>{
    e.preventDefault();slot.classList.remove("drag-over");
    try{const d=JSON.parse(e.dataTransfer.getData("text/plain")||"{}");
      if(d.type!=="persona"||!d.id)return;
      navDroppedPersonaId=d.id;navPresets=[];navPresetCat="All";
      renderNavPersonaSlot();renderNavPresets();renderNavPromptPreview();
    }catch{}
  };
}
function applyNavSkipStyle(){
  const cb=$("nav-skip-style"),block=$("nav-style-block");
  if(!cb||!block)return;
  block.classList.toggle("style-block-disabled",cb.checked);
}
$("nav-skip-style")?.addEventListener("change",applyNavSkipStyle);

$("nav-generate-btn").addEventListener("click",async()=>{
  if(!navDroppedPersonaId){setStatus($("nav-status"),"Drop a persona onto the slot first.","error");return;}
  const p=studio.find(x=>x.id===navDroppedPersonaId);if(!p)return;
  const nameEl=$("nav-name-input");
  const name=(nameEl?.value.trim())||p.name;
  const customPrompt=$("nav-theme-input")?.value.trim()||"";
  const skipStyle=!!$("nav-skip-style")?.checked;
  const btn=$("nav-generate-btn");btn.disabled=true;
  setStatus($("nav-status"),"Queued — generating in background…","success");
  try{
    const fd=new FormData();
    fd.append("persona_id",p.id);fd.append("name",name);
    fd.append("preset_ids",JSON.stringify(skipStyle?[]:navPresets));
    fd.append("custom_prompt",skipStyle?"":customPrompt);
    fd.append("skip_style",skipStyle?"true":"false");
    fd.append("client_id",clientId);
    await api("/api/studio/avatars/preview",{method:"POST",body:fd});
    navPresets=[];navPresetCat="All";if(nameEl)nameEl.value="";
    const ti=$("nav-theme-input");if(ti)ti.value="";
    const sk=$("nav-skip-style");if(sk)sk.checked=false;applyNavSkipStyle();
    renderNavPromptPreview();renderNavPresets();await refreshAll();
  }catch(e){setStatus($("nav-status"),e.message,"error");}
  finally{btn.disabled=false;}
});

/* ── New Assistant view (standalone, with avatar drop slot) ── */
function renderNaAvatarSlot(){
  const slot=$("na-avatar-drop-slot");if(!slot)return;
  const btn=$("na-create-asst-btn");
  if(!naDroppedAvatarId){
    slot.innerHTML='<span class="drop-hint">Drag a ready avatar here from the Avatars panel</span>';
    if(btn)btn.disabled=true;return;
  }
  let av=null,ownerPersona=null;
  for(const p of studio){const found=(p.avatars||[]).find(a=>a.id===naDroppedAvatarId);if(found){av=found;ownerPersona=p;break;}}
  if(!av){naDroppedAvatarId=null;return renderNaAvatarSlot();}
  slot.innerHTML=`<img class="slot-thumb" src="${esc(av.image_path||PLACEHOLDER)}" alt=""/><b>${esc(av.name)}</b><span class="drop-hint" style="margin-left:auto">via ${esc(ownerPersona?.name||"")}</span>`;
  if(btn)btn.disabled=false;
}
function initNewAssistantView(){
  naDroppedAvatarId=null;
  renderNaAvatarSlot();
  setStatus($("na-asst-status"),"");
  const slot=$("na-avatar-drop-slot");
  slot.ondragover=e=>{e.preventDefault();slot.classList.add("drag-over");};
  slot.ondragleave=()=>slot.classList.remove("drag-over");
  slot.ondrop=e=>{
    e.preventDefault();slot.classList.remove("drag-over");
    try{const d=JSON.parse(e.dataTransfer.getData("text/plain")||"{}");
      if(d.type!=="avatar"||!d.id)return;
      naDroppedAvatarId=d.id;renderNaAvatarSlot();
    }catch{}
  };
}
$("na-create-asst-btn").addEventListener("click",async()=>{
  if(!naDroppedAvatarId){setStatus($("na-asst-status"),"Drop a ready avatar onto the slot first.","error");return;}
  let av=null,ownerPersona=null;
  for(const p of studio){const found=(p.avatars||[]).find(a=>a.id===naDroppedAvatarId);if(found){av=found;ownerPersona=p;break;}}
  if(!av||!ownerPersona){setStatus($("na-asst-status"),"Avatar no longer available.","error");return;}
  const name=$("na-asst-name")?.value.trim(),prompt=$("na-asst-prompt")?.value.trim(),fm=$("na-asst-msg")?.value.trim();
  if(!name||!prompt){setStatus($("na-asst-status"),"Name and instructions required.","error");return;}
  const btn=$("na-create-asst-btn");btn.disabled=true;setStatus($("na-asst-status"),"Creating…");
  try{
    const r=await api("/api/studio/assistants",{method:"POST",body:JSON.stringify({name,prompt,first_message:fm||"Hi!",persona_id:ownerPersona.id,avatar_id:av.id,client_id:clientId})});
    setStatus($("na-asst-status"),`'${r.name}' created!`,"success");await refreshAll();
  }catch(e){setStatus($("na-asst-status"),e.message,"error");}
  finally{btn.disabled=false;}
});

/* ── Image lightbox ── */
function openLightbox(src){if(!src)return;const m=$("image-lightbox"),img=$("lightbox-img");if(!m||!img)return;img.src=src;m.hidden=false;}
function closeLightbox(){const m=$("image-lightbox");if(m)m.hidden=true;const img=$("lightbox-img");if(img)img.src="";}
function _extractThumbSrc(el){
  if(!el)return"";
  if(el.tagName==="IMG")return el.src||"";
  const bg=(el.style&&el.style.backgroundImage)||"";
  const m=bg.match(/url\(["']?([^"')]+)["']?\)/);
  return m?m[1]:"";
}
document.addEventListener("click",e=>{
  const close=e.target.closest("#lightbox-close");
  if(close){e.stopPropagation();closeLightbox();return;}
  const onLightbox=e.target.closest("#image-lightbox");
  if(onLightbox&&!e.target.closest("#lightbox-img")){closeLightbox();return;}
  const thumb=e.target.closest(".entity-thumb,.cv-thumb,.avatar-preview-img,.slot-thumb");
  if(!thumb)return;
  const src=_extractThumbSrc(thumb);
  if(!src||src.endsWith("/avatar-placeholder.svg"))return;
  e.stopPropagation();
  openLightbox(src);
});
document.addEventListener("keydown",e=>{if(e.key==="Escape"){const m=$("image-lightbox");if(m&&!m.hidden)closeLightbox();}});

/* ── Create Assistant ── */
$("create-asst-btn").addEventListener("click",async()=>{
  const p=getPersona(),av=getAvatar();if(!p||!av)return;
  const name=$("asst-name-input")?.value.trim(),prompt=$("asst-prompt-input")?.value.trim(),fm=$("asst-msg-input")?.value.trim();
  if(!name||!prompt)return setStatus($("asst-status"),"Name and instructions required.","error");
  $("create-asst-btn").disabled=true;setStatus($("asst-status"),"Creating…");
  try{const r=await api("/api/studio/assistants",{method:"POST",body:JSON.stringify({name,prompt,first_message:fm||"Hi!",persona_id:p.id,avatar_id:av.id})});setStatus($("asst-status"),`'${r.name}' created!`,"success");await refreshAll();}
  catch(e){setStatus($("asst-status"),e.message,"error");}finally{$("create-asst-btn").disabled=false;}
});

/* ── Clone Avatar ── */
$("clone-av-gen-btn").addEventListener("click",async()=>{
  const p=getPersona();if(!p)return;
  const name=$("clone-av-name")?.value.trim();if(!name)return;
  $("clone-av-gen-btn").disabled=true;
  try{await genAvatarPreview(p.id,name,clonePresets,$("clone-av-theme"),$("clone-av-preview-area"),$("clone-av-preview-img"),$("clone-av-status"));$("clone-av-save-btn").hidden=false;}
  catch(e){setStatus($("clone-av-status"),e.message,"error");}finally{$("clone-av-gen-btn").disabled=false;}
});
$("clone-av-regen-btn").addEventListener("click",()=>$("clone-av-gen-btn").click());
$("clone-av-save-btn").addEventListener("click",async()=>{
  const p=getPersona();if(!p)return;const name=$("clone-av-name")?.value.trim();if(!name)return;
  const tp=[p2prompt(clonePresets),$("clone-av-theme")?.value.trim()||""].filter(Boolean).join(", ");
  $("clone-av-save-btn").disabled=true;setStatus($("clone-av-status"),"Saving…");
  try{
    const fd=new FormData();fd.append("persona_id",p.id);fd.append("name",name);fd.append("decoration",tp);fd.append("theme_prompt",tp);fd.append("preset_ids",JSON.stringify(clonePresets));fd.append("client_id",clientId);
    await api("/api/studio/avatars",{method:"POST",body:fd});setStatus($("clone-av-status"),"Clone queued!","success");await refreshAll();
  }catch(e){setStatus($("clone-av-status"),e.message,"error");}finally{$("clone-av-save-btn").disabled=false;}
});

/* ── Clone + Call Assistant ── */
$("launch-call-btn").addEventListener("click",launchCall);
$("clone-asst-btn").addEventListener("click",async()=>{
  const asst=getAssistant();if(!asst)return;
  const name=$("clone-asst-name")?.value.trim(),prompt=$("clone-asst-prompt")?.value.trim(),fm=$("clone-asst-msg")?.value.trim();
  if(!name||!prompt)return setStatus($("clone-asst-status"),"Name and instructions required.","error");
  $("clone-asst-btn").disabled=true;setStatus($("clone-asst-status"),"Creating…");
  try{const r=await api("/api/studio/assistants",{method:"POST",body:JSON.stringify({name,prompt,first_message:fm,persona_id:asst.persona_id,avatar_id:asst.avatar_id})});setStatus($("clone-asst-status"),`'${r.name}' created!`,"success");await refreshAll();}
  catch(e){setStatus($("clone-asst-status"),e.message,"error");}finally{$("clone-asst-btn").disabled=false;}
});

/* ── Section header clicks ── */
$("personas-section-header").addEventListener("click",e=>{if(e.target.closest(".section-collapse-btn"))return;selectedPersonaId=null;selectedAvatarId=null;showView("new-persona");});
$("avatars-section-header").addEventListener("click",e=>{if(e.target.closest(".section-collapse-btn"))return;showView("new-avatar");});
$("voices-section-header").addEventListener("click",e=>{if(e.target.closest(".section-collapse-btn"))return;showView("voice-design");});
$("contacts-section-header").addEventListener("click",e=>{if(e.target.closest(".section-collapse-btn"))return;showView("new-assistant");});

/* ── Collapse sections ── */
document.querySelectorAll(".section-collapse-btn").forEach(btn=>{
  btn.addEventListener("click",e=>{e.stopPropagation();const s=$(btn.dataset.section);if(s)s.classList.toggle("collapsed");});
});

/* ── Pane maximize ── */
const _lpm=$("left-pane-max");if(_lpm)_lpm.addEventListener("click",()=>{const p=$("left-pane");p.classList.toggle("pane-maximized");_lpm.textContent=p.classList.contains("pane-maximized")?"⤡":"⤢";});

/* ── Pane resize ── */
function initPaneResize(handleId,paneId,varName){
  const h=$(handleId),pane=$(paneId);if(!h||!pane)return;
  h.addEventListener("mousedown",e=>{
    e.preventDefault();h.classList.add("dragging");
    const sx=e.clientX,sw=pane.getBoundingClientRect().width;
    const dir=varName==="--left-w"?1:-1;
    const move=e=>{const w=Math.max(160,Math.min(580,sw+dir*(e.clientX-sx)));pane.style.width=w+"px";document.documentElement.style.setProperty(varName,w+"px");};
    const up=()=>{h.classList.remove("dragging");document.removeEventListener("mousemove",move);document.removeEventListener("mouseup",up);};
    document.addEventListener("mousemove",move);document.addEventListener("mouseup",up);
  });
}
initPaneResize("left-resize-handle","left-pane","--left-w");

/* ── Section vertical resize ── */
function initSectionResize(hId,prevId,nextId){
  const h=$(hId),prev=$(prevId),next=$(nextId);if(!h||!prev||!next)return;
  h.addEventListener("mousedown",e=>{
    e.preventDefault();h.classList.add("dragging");
    const sy=e.clientY,sph=prev.getBoundingClientRect().height,snh=next.getBoundingClientRect().height;
    const move=e=>{const dy=e.clientY-sy;prev.style.cssText+=`;flex:none;height:${Math.max(60,sph+dy)}px`;next.style.cssText+=`;flex:none;height:${Math.max(60,snh-dy)}px`;};
    const up=()=>{h.classList.remove("dragging");document.removeEventListener("mousemove",move);document.removeEventListener("mouseup",up);};
    document.addEventListener("mousemove",move);document.addEventListener("mouseup",up);
  });
}
initSectionResize("sect-h-1","voices-pane-section","personas-pane-section");
initSectionResize("sect-h-2","personas-pane-section","avatars-pane-section");
initSectionResize("sect-h-3","avatars-pane-section","contacts-pane-section");

/* ── Mobile ── */
const backdrop=$("mobile-backdrop");
function closeMobilePanes(){["left-pane","right-pane"].forEach(id=>{$(id)?.classList.remove("mobile-open");});backdrop?.classList.remove("visible");}
$("mob-left-btn")?.addEventListener("click",()=>{$("left-pane").classList.add("mobile-open");backdrop.classList.add("visible");});
$("mob-sync-btn")?.addEventListener("click",async()=>{try{await refreshAll();}catch{}});
$("left-pane-close")?.addEventListener("click",closeMobilePanes);
backdrop?.addEventListener("click",closeMobilePanes);

/* ── Delete modal ── */
function openDeleteModal(type,id,name){
  _pendingDelete={type,id,name};$("delete-modal-title").textContent=`Delete ${type[0].toUpperCase()+type.slice(1)}?`;
  $("delete-modal-body").textContent=`"${name}" will be permanently removed.`;$("delete-modal-cascade").hidden=true;
  (async()=>{try{
    if(type==="persona"){const c=await api(`/api/studio/personas/${id}/cascade-count`);const pts=[];if(c.avatars)pts.push(`${c.avatars} avatar${c.avatars!==1?"s":""}`);if(c.assistants)pts.push(`${c.assistants} assistant${c.assistants!==1?"s":""}`);if(pts.length){$("delete-modal-cascade-text").textContent=`Also deletes: ${pts.join(" and ")}.`;$("delete-modal-cascade").hidden=false;}}
    else if(type==="avatar"){const c=await api(`/api/studio/avatars/${id}/cascade-count`);if(c.assistants){$("delete-modal-cascade-text").textContent=`Also deletes ${c.assistants} assistant${c.assistants!==1?"s":""}.`;$("delete-modal-cascade").hidden=false;}}
  }catch{}})();
  $("delete-modal").hidden=false;
}
$("delete-cancel-btn").addEventListener("click",()=>{$("delete-modal").hidden=true;_pendingDelete=null;});
$("delete-confirm-btn").addEventListener("click",async()=>{
  if(!_pendingDelete)return;const{type,id}=_pendingDelete;$("delete-confirm-btn").disabled=true;
  try{
    const path=type==="assistant"?"assistants":type==="avatar"?"avatars":type==="voice"?"voices":"personas";
    await api(`/api/studio/${path}/${id}`,{method:"DELETE"});
    $("delete-modal").hidden=true;_pendingDelete=null;
    if(type==="persona"&&selectedPersonaId===id){selectedPersonaId=null;selectedAvatarId=null;showView("welcome");}
    else if(type==="avatar"&&selectedAvatarId===id){selectedAvatarId=null;showView("persona");}
    else if(type==="assistant"&&selectedAssistantId===id){selectedAssistantId=null;showView("welcome");}
    else if(type==="voice"){selectedVoiceId=null;}
    await refreshAll();
  }catch(e){alert("Delete failed: "+e.message);}finally{$("delete-confirm-btn").disabled=false;}
});

/* ── Call (LiveKit) ── */
function setCallMode(m){$("call-shell").hidden=m!=="fullscreen";$("floating-call").hidden=m!=="floating";}
function setCallWaiting(show,msg){const el=$("call-waiting");if(!el)return;el.hidden=!show;if(msg)el.textContent=msg;}

function attachTrack(pub){
  const track=pub.track;if(!track)return;
  const vid=$("call-video");
  // Route both audio and video through the same <video> element so the browser
  // keeps them on the same media clock — the only way to guarantee AV sync.
  if(track.kind==="video"){track.attach(vid);setCallWaiting(false);}
  else if(track.kind==="audio"){track.attach(vid);}
}
function detachTrack(pub){const t=pub.track;if(t)t.detach().forEach(el=>el.remove?.());}

async function launchCall(){
  const asst=getAssistant();if(!asst)return;
  if(!window.LivekitClient){alert("LiveKit client not loaded yet — try again in a moment.");return;}
  const {Room,RoomEvent,Track}=window.LivekitClient;
  try{
    const data=await api("/api/studio/calls",{method:"POST",body:JSON.stringify({assistant_id:asst.id})});
    $("call-title").textContent=asst.name;
    setCallMode("fullscreen");setCallWaiting(true,"Connecting…");

    const room=new Room({adaptiveStream:true,dynacast:true});
    lkRoom=room;

    room.on(RoomEvent.TrackSubscribed,(track,pub,participant)=>{
      if(participant?.isLocal)return;
      attachTrack(pub);
    });
    room.on(RoomEvent.TrackUnsubscribed,(track,pub)=>detachTrack(pub));
    room.on(RoomEvent.ParticipantConnected,p=>{
      if(p.isAgent||p.identity?.startsWith("agent-")||p.kind==="agent"){setCallWaiting(true,"Agent joining…");}
    });
    room.on(RoomEvent.Disconnected,()=>hangup());

    try{
      const probe=await navigator.mediaDevices.getUserMedia({audio:true});
      probe.getTracks().forEach(t=>t.stop());
    }catch(micErr){
      const isDenied=micErr.name==="NotAllowedError"||/permission|denied/i.test(micErr.message||"");
      throw new Error(isDenied
        ? "Microphone access is blocked. Click the lock/info icon in the address bar → Site settings → allow Microphone, then reload."
        : "Microphone unavailable: "+(micErr.message||micErr.name||"unknown error"));
    }

    await room.connect(data.livekit_url,data.participant_token);
    await room.localParticipant.setMicrophoneEnabled(true);

    room.remoteParticipants.forEach(p=>{
      p.trackPublications.forEach(pub=>{if(pub.isSubscribed&&pub.track)attachTrack(pub);});
    });
  }catch(e){console.error(e);alert("Call failed: "+e.message);hangup();}
}

$("minimize-call-btn").addEventListener("click",()=>{
  $("floating-call-title").textContent=$("call-title").textContent;
  $("floating-widget-host").innerHTML="";
  $("floating-widget-host").appendChild($("call-widget-host"));
  setCallMode("floating");
});
$("restore-call-btn").addEventListener("click",()=>{
  $("call-shell").querySelector(".call-topbar")?.after($("call-widget-host"));
  setCallMode("fullscreen");
});

function hangup(){
  try{lkRoom?.disconnect();}catch{}
  lkRoom=null;
  const v=$("call-video");if(v){try{v.srcObject=null;}catch{}}
  setCallWaiting(true,"Connecting…");
  setCallMode("hidden");
}
$("hangup-call-btn").addEventListener("click",hangup);$("floating-hangup-btn").addEventListener("click",hangup);

/* ── WS + Data ── */
function connectWS(){if(ws)ws.close();const proto=location.protocol==="https:"?"wss":"ws";ws=new WebSocket(`${proto}://${location.host}/ws/updates/${clientId}`);ws.addEventListener("message",e=>{try{const m=JSON.parse(e.data);if(m.event?.match(/\./))refreshAll().catch(()=>{});}catch{}});ws.addEventListener("close",()=>{clearTimeout(wsTimer);wsTimer=setTimeout(connectWS,1500);});}
async function loadPresets(){const data=await api("/api/studio/presets");PRESETS=data.avatar_presets||[];VOICE_PRESETS=data.voice_presets||[];}
async function loadStudio(){studio=await api("/api/studio/personas");}
async function loadAssistants(){assistants=await api("/api/studio/assistants");}
async function loadVoices(){voices=await api("/api/studio/voices");}
async function loadLibraryVoices(){if(libraryVoices.length)return;try{libraryVoices=await api("/api/studio/voices/library");}catch{}}
async function refreshAll(){
  await Promise.all([loadStudio(),loadAssistants(),loadVoices()]);
  renderAll();
  // Re-init persona voice slot if persona edit is visible
  if(!$("cv-persona")?.hidden){const p=getPersona();if(p)setupPvVoiceDropSlot(p);}
  const need=studio.some(p=>p.status&&p.status!=="ready")||studio.some(p=>(p.avatars||[]).some(a=>a.status==="processing"))||assistants.some(a=>a.status&&a.status!=="ready")||voices.some(v=>v.status==="processing");
  clearTimeout(studioTimer);if(need)studioTimer=setTimeout(()=>refreshAll().catch(()=>{}),6000);
}

/* ── Tab-action visibility ── */
document.addEventListener("click", e => {
  const btn = e.target.closest(".tab-btn");
  if (!btn) return;
  const tabId = btn.dataset.tab;

  // persona view tabs
  if (tabId === "pv-edit" || tabId === "pv-avatar") {
    $("pv-edit-actions").hidden    = (tabId !== "pv-edit");
    $("pv-avatar-actions").hidden  = (tabId !== "pv-avatar");
  }
  // avatar view tabs
  if (tabId === "avv-assistant" || tabId === "avv-clone") {
    $("avv-assistant-actions").hidden = (tabId !== "avv-assistant");
    $("avv-clone-actions").hidden     = (tabId !== "avv-clone");
  }
  // assistant view tabs
  if (tabId === "ca-call" || tabId === "ca-clone") {
    $("ca-call-actions").hidden  = (tabId !== "ca-call");
    $("ca-clone-actions").hidden = (tabId !== "ca-call");
  }
  // voice design tabs
  if (tabId === "vd-design" || tabId === "vd-clone") {
    $("vd-design-actions").hidden = (tabId !== "vd-design");
    $("vd-clone-actions").hidden  = (tabId !== "vd-clone");
  }
});

/* ── Boot ── */
window.addEventListener("DOMContentLoaded",async()=>{
  setCallMode("hidden");
  ALL_VIEWS.forEach(v=>{const e=$(v);if(e)e.hidden=(v!=="cv-welcome");});
  connectWS();
  try{await Promise.all([loadPresets(),loadStudio(),loadAssistants(),loadVoices()]);}catch(e){console.error("Initial load error:",e);}
  renderAll();
  loadLibraryVoices().then(()=>renderLeftVoices()).catch(()=>{});
});
