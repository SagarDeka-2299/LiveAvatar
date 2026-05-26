/* ── Constants ── */
const PLACEHOLDER="/demo/avatar-placeholder.svg";
const CROP_W=1024,CROP_H=576,CROP_RATIO=16/9;

let PRESETS=[];
let VOICE_PRESETS=[];

/* ── State ── */
let studio=[],assistants=[],voices=[],selectedPersonaAvatars=[],allAvatars=[];
let selectedPersonaId=null,selectedAvatarId=null,selectedAssistantId=null,selectedVoiceId=null;

const ELEVENLABS_FALLBACK_VOICES = {
  "21m00Tcm4TlvDq8ikWAM": { name: "Rachel", gender: "female", accent: "american", description: "pleasant" },
  "AZnzlk1XydvUeBnOEeMs": { name: "Domi", gender: "female", accent: "american", description: "energetic" },
  "EXAVITQu4vr4xnSDxMaL": { name: "Bella", gender: "female", accent: "american", description: "whispery" },
  "ErXwobaYiN019PkySvjV": { name: "Antoni", gender: "male", accent: "american", description: "well-rounded" },
  "MF3mGyEYCl7XYWbV9VbO": { name: "Ellie", gender: "female", accent: "american", description: "energetic" },
  "TxGEqn7nUJQUTg2thAfZ": { name: "Josh", gender: "male", accent: "american", description: "deep" },
  "VR6A1In7x5I7udNsUpwD": { name: "Arnold", gender: "male", accent: "american", description: "crisp" },
  "pNInz6obpgq5paqqcf1X": { name: "Adam", gender: "male", accent: "american", description: "narration" },
  "yoZ06a0Zgoj9msFdQA16": { name: "Nicole", gender: "female", accent: "american", description: "whispery" },
  "GBv7mTt0atIp3Br8iCZE": { name: "Thomas", gender: "male", accent: "american", description: "calm" },
  "IKne3meq5aSn9XLyUdCD": { name: "Charlie", gender: "male", accent: "american", description: "natural" },
  "LcfcDJNbi31gfqcZImDW": { name: "Emily", gender: "female", accent: "american", description: "natural" },
  "N2lVS1w4EtoT3nt4gGfm": { name: "Sarah", gender: "female", accent: "american", description: "natural" },
  "ODq5zmih86xlqwwPnW9C": { name: "Sam", gender: "male", accent: "american", description: "narration" },
  "SOYUIqn2A4g4H6NocmaO": { name: "Gigi", gender: "female", accent: "american", description: "warm" },
  "TX329zTpHNGKVg7jgp4X": { name: "Giovanni", gender: "male", accent: "italian", description: "warm" },
  "XB0fDUnXUeTFXle5ptj5": { name: "Alice", gender: "female", accent: "british", description: "calm" },
  "XrExMAJgTE2LwoujhyS1": { name: "Bella", gender: "female", accent: "american", description: "narration" },
  "bV515pTLwSj1ib34Qthg": { name: "Mimi", gender: "female", accent: "american", description: "natural" },
  "jBpfuIE2acFn3zgnw97C": { name: "Michael", gender: "male", accent: "american", description: "natural" },
  "jsCqWAZ5z14v41Ka51DO": { name: "Lily", gender: "female", accent: "american", description: "natural" },
  "onwK4e9Gkvtpc3q34pAO": { name: "Daniel", gender: "male", accent: "british", description: "deep" },
  "pMs279TpHNGKVg7jgp4X": { name: "Serena", gender: "female", accent: "american", description: "warm" }
};
// Voice design preset multi-select state — list of currently-selected
// VOICE_PRESETS ids (at most one per partition; enforced in
// renderPresetUI + revalidated server-side).
let vdSelectedVoicePresetIds=[];
let vdVoiceCat="All";
let pvPresets=[],pvPresetCat="All";
let clonePresets=[],clonePresetCat="All";
let pvDroppedVoiceId=null;
let vdDroppedPersonaId=null;
let navDroppedPersonaId=null,navPresets=[],navPresetCat="All";
let naDroppedAvatarId=null;
let vdCloneSampleFile=null;
let libraryVoices=[];
let _libExpanded=false;
let appliedFilters = {
  voices: { gender: "", source: "", persona: "" },
  personas: { gender: "", status: "", voice: "", has_avatars: "" },
  avatars: { status: "", persona: "", assistant: "" },
  assistants: { status: "", avatar: "" }
};

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
let pollTimer=null;
// Short-burst follow-up timer used by refreshAll() to re-poll a little
// sooner than the 3s startPolling tick when a background task is mid-flight.
let studioTimer=null;
let _pendingDelete=null;

const $=id=>document.getElementById(id);

function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
function setStatus(el,msg,type=""){if(!el)return;el.textContent=msg;el.className="status-msg"+(type?" "+type:"");}
function setThumb(el,path){if(!el)return;el.style.backgroundImage=`url("${(path||PLACEHOLDER).replace(/"/g,"%22")}")`;}
function revoke(url){if(url)URL.revokeObjectURL(url);}
/* Demo runs against the local-tenant fallback. Hard-coded for simplicity —
   this file is a reference for front-end developers wiring up against the
   real API. Swap the constant out (or read it from your auth layer) to
   point at a real tenant.

   IMPORTANT: tenant_id is ALWAYS in the request body — JSON body field
   for JSON routes, multipart form field for file uploads. Never on the
   URL. Filters and pagination (limit, offset) DO go on the URL as
   query parameters. See README for the full contract. */
const TENANT_ID="local_tenant";

/* ── Presets v2 ──
 * Each preset row: { id, category, partition, subcategory, gender, prompt }
 *
 * Rendering: category tabs across the top, then for the selected category
 * (or "All") we render a partition header for every partition followed by
 * the chip row of its subcategories.
 *
 * Selection rules enforced here AND server-side:
 *   - multiple categories simultaneously: ✓
 *   - multiple partitions within a category: ✓
 *   - within a single partition: AT MOST ONE chip (mutually exclusive)
 *   - subcategories whose gender ∉ {"unisex", persona.gender} are hidden
 *
 * `pool` is `PRESETS` or `VOICE_PRESETS` — same shape, same rules. */
function renderPresetUI(catsEl,chipsEl,selArr,cat,gender,onToggle,onCat,pool){
  if(!catsEl||!chipsEl)return;
  pool=pool||PRESETS;
  const visible=pool.filter(p=>p.gender==="unisex"||gender==="unknown"||p.gender===gender);

  // ── Category tabs ──
  const cats=["All",...new Set(visible.map(p=>p.category))];
  catsEl.innerHTML="";
  cats.forEach(c=>{const b=document.createElement("button");b.type="button";b.className="preset-cat-btn"+(c===cat?" active":"");b.textContent=c;b.onclick=()=>onCat(c);catsEl.appendChild(b);});

  // ── Chips, grouped by partition ──
  chipsEl.innerHTML="";
  const inScope=visible.filter(p=>cat==="All"||p.category===cat);

  // Group: { "Category / Partition": [...presets...] } preserving insertion order
  const groups=new Map();
  inScope.forEach(p=>{
    const key=p.category+" / "+p.partition;
    if(!groups.has(key))groups.set(key,{partition:p.partition,category:p.category,items:[]});
    groups.get(key).items.push(p);
  });

  groups.forEach(({partition,category,items},key)=>{
    const header=document.createElement("div");
    header.className="preset-partition";
    header.textContent=cat==="All"?(category+" · "+partition):partition;
    chipsEl.appendChild(header);
    items.forEach(pr=>{
      const ch=document.createElement("button");
      ch.type="button";
      ch.className="preset-chip"+(selArr.includes(pr.id)?" selected":"");
      ch.textContent=pr.subcategory;
      ch.dataset.partition=key;
      ch.onclick=()=>{
        const wasSelected=selArr.includes(pr.id);
        // Mutual exclusion: deselect any other selected chip in this partition.
        for(let i=selArr.length-1;i>=0;i--){
          const other=pool.find(p=>p.id===selArr[i]);
          if(other && other.category===category && other.partition===partition && other.id!==pr.id){
            selArr.splice(i,1);
          }
        }
        // Toggle the clicked chip.
        const idx=selArr.indexOf(pr.id);
        if(idx>=0)selArr.splice(idx,1); else selArr.push(pr.id);
        // Re-render the chip row so siblings repaint as deselected.
        renderPresetUI(catsEl,chipsEl,selArr,cat,gender,onToggle,onCat,pool);
        // Notify the caller (e.g. to refresh the assembled prompt preview).
        if(onToggle)onToggle(pr.id,!wasSelected);
      };
      chipsEl.appendChild(ch);
    });
  });
}
const p2prompt=sel=>sel.map(id=>PRESETS.find(p=>p.id===id)?.subcategory).filter(Boolean).join(", ");

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
const getAvatar=id=>{
  const av = selectedPersonaAvatars.find(a=>a.id===(id??selectedAvatarId));
  if(av) return av;
  const p=getPersona();
  return p?.avatars?.find(a=>a.id===(id??selectedAvatarId))||null;
};
const getAssistant=id=>assistants.find(a=>a.id===(id??selectedAssistantId))||null;
const getVoice=id=>voices.find(v=>v.id===(id??selectedVoiceId))||null;

async function fetchDetailedPersona(id) {
  try {
    const r = await fetch(`/${TENANT_ID}/personas/${id}`);
    if (r.ok) {
      const detailed = await r.json();
      const idx = studio.findIndex(x => x.id === id);
      if (idx !== -1) {
        studio[idx] = detailed;
      }
    }
  } catch (err) {
    console.error("Failed to fetch detailed persona:", err);
  }
}

async function fetchDetailedAvatar(id) {
  try {
    const r = await fetch(`/${TENANT_ID}/avatars/${id}`);
    if (r.ok) {
      const detailed = await r.json();
      const idx = selectedPersonaAvatars.findIndex(x => x.id === id);
      if (idx !== -1) {
        selectedPersonaAvatars[idx] = detailed;
      } else {
        selectedPersonaAvatars.push(detailed);
      }
      // Also update in parent persona if available
      const p = getPersona(detailed.persona_id);
      if (p && p.avatars) {
        const aIdx = p.avatars.findIndex(x => x.id === id);
        if (aIdx !== -1) {
          p.avatars[aIdx] = detailed;
        } else {
          p.avatars.push(detailed);
        }
      }
    }
  } catch (err) {
    console.error("Failed to fetch detailed avatar:", err);
  }
}

async function loadAvatars() {
  const status = appliedFilters.avatars.status;
  const persona_id = appliedFilters.avatars.persona || selectedPersonaId || "";
  const assistant_id = appliedFilters.avatars.assistant;
  
  let url = `/${TENANT_ID}/avatars?`;
  if (persona_id) url += `persona_id=${persona_id}&`;
  if (status) url += `status=${status}&`;
  if (assistant_id) url += `assistant_id=${assistant_id}&`;
  try {
    const r = await fetch(url);
    if (r.ok) {
      selectedPersonaAvatars = await r.json();
    }
  } catch (err) {
    console.error("Failed to load avatars:", err);
  }
}

async function initVoiceView() {
  let detailed = null;
  
  if (selectedVoiceId === "default") {
    detailed = {
      id: "default",
      name: "Default Voice",
      source: "system",
      description: "Built-in ElevenLabs default text-to-speech voice",
      provider: "ElevenLabs",
      voice_id: "EXAVITQu4vr4xnSDxMaL",
      created_at: null,
      persona_id: null,
      preview_url: "https://api.elevenlabs.io/v1/voices/EXAVITQu4vr4xnSDxMaL/previews"
    };
  } else {
    // Check in custom voices
    const v = voices.find(x => String(x.id) === String(selectedVoiceId));
    if (v) {
      detailed = v;
      try {
        const r = await fetch(`/${TENANT_ID}/voices/${v.id}`);
        if (r.ok) {
          detailed = await r.json();
          const idx = voices.findIndex(x => x.id === v.id);
          if (idx !== -1) voices[idx] = detailed;
        }
      } catch (e) {
        console.error("Failed to fetch detailed voice details:", e);
      }
    } else {
      // Check in library voices
      const lv = libraryVoices.find(x => x.voice_id === selectedVoiceId);
      if (lv) {
        const gender = lv.labels?.gender || "";
        const accent = lv.labels?.accent || "";
        const tags = [gender, accent, lv.labels?.description].filter(Boolean).join(" · ");
        detailed = {
          id: lv.voice_id,
          name: lv.name,
          source: "library",
          description: `Premade ElevenLabs voice (${tags})`,
          provider: "ElevenLabs",
          voice_id: lv.voice_id,
          created_at: null,
          persona_id: null,
          preview_url: lv.preview_url || `https://api.elevenlabs.io/v1/voices/${lv.voice_id}/previews`
        };
      } else {
        // Fallback for standard ElevenLabs voices
        const fallback = ELEVENLABS_FALLBACK_VOICES[selectedVoiceId];
        if (fallback) {
          detailed = {
            id: selectedVoiceId,
            name: fallback.name,
            source: "library",
            description: `Premade ElevenLabs voice (${fallback.gender} · ${fallback.accent} · ${fallback.description})`,
            provider: "ElevenLabs",
            voice_id: selectedVoiceId,
            created_at: null,
            persona_id: null,
            preview_url: `https://api.elevenlabs.io/v1/voices/${selectedVoiceId}/previews`
          };
        }
      }
    }
  }

  if (!detailed) { showView("welcome"); return; }
  
  $("vv-name").textContent = detailed.name;
  const pill = detailed.source === "cloned" ? "cloned" : detailed.source === "system" ? "designed" : detailed.source === "library" ? "designed" : "designed";
  const label = detailed.source === "cloned" ? "Cloned" : detailed.source === "system" ? "System" : detailed.source === "library" ? "Library" : "AI Designed";
  $("vv-source-badge").textContent = label;
  $("vv-source-badge").className = `voice-badge ${pill}`;
  if (detailed.source === "system") {
    $("vv-source-badge").style.cssText = "background:rgba(100,100,255,.15);color:#b0b8ff;border-color:rgba(100,100,255,.35)";
  } else {
    $("vv-source-badge").style.cssText = "";
  }
  $("vv-description").textContent = detailed.description || "No description provided.";
  $("vv-provider").textContent = detailed.provider || "ElevenLabs";
  $("vv-elevenlabs-id").textContent = detailed.voice_id || "None";
  $("vv-created-at").textContent = detailed.created_at ? new Date(detailed.created_at).toLocaleString() : "System Preset";
  
  const pers = detailed.persona_id ? studio.find(p => p.id === detailed.persona_id) : null;
  const persEl = $("vv-persona");
  if (pers) {
    persEl.innerHTML = `<a href="#" style="color:var(--primary); font-weight:600; text-decoration:none" id="vv-persona-link">${esc(pers.name)}</a>`;
    $("vv-persona-link").onclick = e => {
      e.preventDefault();
      selectedPersonaId = pers.id;
      fetchDetailedPersona(pers.id).then(loadAvatars).then(() => showView("persona"));
    };
  } else {
    persEl.textContent = "Global preset (available to all)";
  }
  
  $("vv-play-btn").onclick = () => {
    if (detailed.id === "default") {
      playDefaultVoicePreview();
    } else {
      playVoicePreview(detailed);
    }
  };
  
  const delBtn = $("vv-delete-btn");
  if (delBtn) {
    delBtn.hidden = (detailed.id === "default" || detailed.source === "library" || detailed.source === "system");
    delBtn.onclick = () => {
      openDeleteModal("voice", detailed.id, detailed.name);
    };
  }
}

function syncPopupInputs(popupId) {
  if (popupId === "voices-filter-popup") {
    const g = $("filter-voices-gender"), s = $("filter-voices-source"), p = $("filter-voices-persona");
    if (g) g.value = appliedFilters.voices.gender;
    if (s) s.value = appliedFilters.voices.source;
    if (p) p.value = appliedFilters.voices.persona;
  } else if (popupId === "personas-filter-popup") {
    const g = $("filter-personas-gender"), s = $("filter-personas-status"), v = $("filter-personas-voice"), av = $("filter-personas-has-avatars");
    if (g) g.value = appliedFilters.personas.gender;
    if (s) s.value = appliedFilters.personas.status;
    if (v) v.value = appliedFilters.personas.voice;
    if (av) av.value = appliedFilters.personas.has_avatars;
  } else if (popupId === "avatars-filter-popup") {
    const s = $("filter-avatars-status"), p = $("filter-avatars-persona"), a = $("filter-avatars-assistant");
    if (s) s.value = appliedFilters.avatars.status;
    if (p) p.value = appliedFilters.avatars.persona;
    if (a) a.value = appliedFilters.avatars.assistant;
  } else if (popupId === "contacts-filter-popup") {
    const s = $("filter-assistants-status"), av = $("filter-assistants-avatar");
    if (s) s.value = appliedFilters.assistants.status;
    if (av) av.value = appliedFilters.assistants.avatar;
  }
}

function initFilters() {
  // Toggle popups
  document.querySelectorAll(".filter-toggle-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const popupId = btn.dataset.popup;
      const popup = $(popupId);
      if (popup) {
        // Close all other popups first
        document.querySelectorAll(".filter-popup").forEach(p => {
          if (p.id !== popupId) p.hidden = true;
        });
        popup.hidden = !popup.hidden;
        if (!popup.hidden) {
          syncPopupInputs(popupId);
        }
      }
    });
  });

  // Close popups on Close button
  document.querySelectorAll(".filter-popup-close").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const popupId = btn.dataset.popup;
      const popup = $(popupId);
      if (popup) popup.hidden = true;
    });
  });

  // Click outside popups closes them
  document.addEventListener("click", (e) => {
    const insidePopup = e.target.closest(".filter-popup");
    const insideToggle = e.target.closest(".filter-toggle-btn");
    if (!insidePopup && !insideToggle) {
      document.querySelectorAll(".filter-popup").forEach(popup => {
        popup.hidden = true;
      });
    }
  });

  // Voices Apply / Reset
  const btnApplyVoices = $("btn-apply-voices");
  if (btnApplyVoices) {
    btnApplyVoices.addEventListener("click", () => {
      appliedFilters.voices.gender = $("filter-voices-gender")?.value || "";
      appliedFilters.voices.source = $("filter-voices-source")?.value || "";
      appliedFilters.voices.persona = $("filter-voices-persona")?.value || "";
      document.querySelectorAll(".filter-popup").forEach(p => p.hidden = true);
      refreshAll().catch(()=>{});
    });
  }
  const btnResetVoices = $("btn-reset-voices");
  if (btnResetVoices) {
    btnResetVoices.addEventListener("click", () => {
      const g = $("filter-voices-gender"), s = $("filter-voices-source"), p = $("filter-voices-persona");
      if (g) g.value = "";
      if (s) s.value = "";
      if (p) p.value = "";
      appliedFilters.voices.gender = "";
      appliedFilters.voices.source = "";
      appliedFilters.voices.persona = "";
      document.querySelectorAll(".filter-popup").forEach(p => p.hidden = true);
      refreshAll().catch(()=>{});
    });
  }

  // Personas Apply / Reset
  const btnApplyPersonas = $("btn-apply-personas");
  if (btnApplyPersonas) {
    btnApplyPersonas.addEventListener("click", () => {
      appliedFilters.personas.gender = $("filter-personas-gender")?.value || "";
      appliedFilters.personas.status = $("filter-personas-status")?.value || "";
      appliedFilters.personas.voice = $("filter-personas-voice")?.value || "";
      appliedFilters.personas.has_avatars = $("filter-personas-has-avatars")?.value || "";
      document.querySelectorAll(".filter-popup").forEach(p => p.hidden = true);
      refreshAll().catch(()=>{});
    });
  }
  const btnResetPersonas = $("btn-reset-personas");
  if (btnResetPersonas) {
    btnResetPersonas.addEventListener("click", () => {
      const g = $("filter-personas-gender"), s = $("filter-personas-status"), v = $("filter-personas-voice"), av = $("filter-personas-has-avatars");
      if (g) g.value = "";
      if (s) s.value = "";
      if (v) v.value = "";
      if (av) av.value = "";
      appliedFilters.personas.gender = "";
      appliedFilters.personas.status = "";
      appliedFilters.personas.voice = "";
      appliedFilters.personas.has_avatars = "";
      document.querySelectorAll(".filter-popup").forEach(p => p.hidden = true);
      refreshAll().catch(()=>{});
    });
  }

  // Avatars Apply / Reset
  const btnApplyAvatars = $("btn-apply-avatars");
  if (btnApplyAvatars) {
    btnApplyAvatars.addEventListener("click", () => {
      appliedFilters.avatars.status = $("filter-avatars-status")?.value || "";
      appliedFilters.avatars.persona = $("filter-avatars-persona")?.value || "";
      appliedFilters.avatars.assistant = $("filter-avatars-assistant")?.value || "";
      document.querySelectorAll(".filter-popup").forEach(p => p.hidden = true);
      refreshAll().catch(()=>{});
    });
  }
  const btnResetAvatars = $("btn-reset-avatars");
  if (btnResetAvatars) {
    btnResetAvatars.addEventListener("click", () => {
      const s = $("filter-avatars-status"), p = $("filter-avatars-persona"), a = $("filter-avatars-assistant");
      if (s) s.value = "";
      if (p) p.value = "";
      if (a) a.value = "";
      appliedFilters.avatars.status = "";
      appliedFilters.avatars.persona = "";
      appliedFilters.avatars.assistant = "";
      document.querySelectorAll(".filter-popup").forEach(p => p.hidden = true);
      refreshAll().catch(()=>{});
    });
  }

  // Assistants Apply / Reset
  const btnApplyAssistants = $("btn-apply-assistants");
  if (btnApplyAssistants) {
    btnApplyAssistants.addEventListener("click", () => {
      appliedFilters.assistants.status = $("filter-assistants-status")?.value || "";
      appliedFilters.assistants.avatar = $("filter-assistants-avatar")?.value || "";
      document.querySelectorAll(".filter-popup").forEach(p => p.hidden = true);
      refreshAll().catch(()=>{});
    });
  }
  const btnResetAssistants = $("btn-reset-assistants");
  if (btnResetAssistants) {
    btnResetAssistants.addEventListener("click", () => {
      const s = $("filter-assistants-status"), av = $("filter-assistants-avatar");
      if (s) s.value = "";
      if (av) av.value = "";
      appliedFilters.assistants.status = "";
      appliedFilters.assistants.avatar = "";
      document.querySelectorAll(".filter-popup").forEach(p => p.hidden = true);
      refreshAll().catch(()=>{});
    });
  }
}

/* ── Voice playback ── */
async function playVoicePreview(voice){
  let detailedVoice = voice;
  if (!voice.preview_url) {
    try {
      const r = await fetch(`/${TENANT_ID}/voices/${voice.id}`);
      if (r.ok) {
        detailedVoice = await r.json();
        const idx = voices.findIndex(v => v.id === voice.id);
        if (idx !== -1) {
          voices[idx] = detailedVoice;
        }
      }
    } catch (err) {
      console.error("Failed to fetch detailed voice:", err);
    }
  }
  if(!detailedVoice?.preview_url){alert("No preview audio available for this voice yet.");return;}
  const el=$("voice-preview-audio");if(!el)return;
  el.src=detailedVoice.preview_url;
  el.play().catch(()=>{});
}
async function playDefaultVoicePreview(){
  const el=$("voice-preview-audio");if(!el)return;
  const defaultVoiceId="EXAVITQu4vr4xnSDxMaL";
  const lib=libraryVoices.find(v=>v.voice_id===defaultVoiceId);
  if(lib?.preview_url){
    el.src=lib.preview_url;
  }else{
    // /voices/preview-default is POST with a JSON body now — can't be used
    // directly as an <audio> src. Fetch the bytes, wrap in a blob URL.
    try{
      const _r=await fetch(`/${TENANT_ID}/voices/preview-default`);
      if(!_r.ok)throw new Error("no default preview");
      const blob=await _r.blob();
      el.src=URL.createObjectURL(blob);
    }catch{return;}
  }
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
      const thumb=document.createElement("div");thumb.className="entity-thumb";thumb.style.position="relative";setThumb(thumb,p.image_url);
      const spin=document.createElement("div");spin.className="entity-spin";thumb.appendChild(spin);
      const info=document.createElement("div");info.className="entity-info";
      info.innerHTML=`<div class="entity-name">${esc(p.name)}</div><div class="entity-stage">${esc(stageLabel(p.status,p.stage))}</div>`;
      const cancelBtn=document.createElement("button");cancelBtn.className="entity-delete-btn";cancelBtn.textContent="✕";cancelBtn.title="Cancel";
      cancelBtn.onclick=e=>{e.stopPropagation();cancelBtn.disabled=true;
        fetch(`/${TENANT_ID}/personas/${p.id}/cancel`,{method:"POST"}).then(refreshAll).catch(()=>{cancelBtn.disabled=false;});};
      card.append(thumb,info,cancelBtn);
    }else{
      const g=p.gender==="female"?"female":p.gender==="male"?"male":"unknown";
      const rc=p.avatar_count||0;
      card.className="entity-card"+(p.id===selectedPersonaId?" active":"");
      card.innerHTML=`<div class="entity-thumb" style="background-image:url('${(p.image_url||PLACEHOLDER).replace(/'/g,"\\'")}')"></div>
        <div class="entity-info"><div class="entity-name">${esc(p.name)}</div>
        <div class="entity-meta"><span class="gender-dot ${g}"></span>${p.gender||"unknown"} · ${rc} avatar${rc!==1?"s":""}</div></div>`;
      const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";
      del.onclick=e=>{e.stopPropagation();openDeleteModal("persona",p.id,p.name);};card.appendChild(del);
      card.addEventListener("click",async()=>{
        selectedPersonaId=p.id;
        selectedAvatarId=null;
        await fetchDetailedPersona(p.id);
        await loadAvatars();
        showView("persona");
      });
      card.draggable=true;
      card.addEventListener("dragstart",e=>{e.dataTransfer.setData("text/plain",JSON.stringify({type:"persona",id:p.id,name:p.name,image_url:p.image_url}));e.dataTransfer.effectAllowed="copy";});
    }
    list.appendChild(card);
  });
}

function renderLeftAvatars(){
  const list=$("left-avatar-list"),hint=$("left-avatar-hint"),badge=$("avatar-count-badge");
  list.innerHTML="";const persona=getPersona();
  if(!persona){if(hint)hint.hidden=false;if(badge)badge.textContent="0";return;}
  if(hint)hint.hidden=true;
  const avatars=selectedPersonaAvatars.filter(a=>a.status!=="cancelled");
  if(badge)badge.textContent=avatars.filter(a=>a.status==="ready").length;
  if(!avatars.length){list.innerHTML='<p class="pane-hint">No avatars yet.</p>';return;}
  avatars.forEach(av=>{
    const card=document.createElement("div");
    if(av.status==="generating"){
      card.className="entity-card entity-card--draft"+(av.id===selectedAvatarId?" active-purple":"");
      card.style.pointerEvents="auto";card.style.cursor="pointer";
      const thumb=document.createElement("div");thumb.className="entity-thumb sm";thumb.style.position="relative";setThumb(thumb,av.image_url);
      const spin=document.createElement("div");spin.className="entity-spin";thumb.appendChild(spin);
      const info=document.createElement("div");info.className="entity-info";
      info.innerHTML=`<div class="entity-name">${esc(av.name)}</div><div class="entity-stage">⚡ Generating image…</div>`;
      const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";del.style.cssText="color:var(--red);background:rgba(244,63,94,0.14)";
      del.onclick=e=>{e.stopPropagation();openDeleteModal("avatar",av.id,av.name);};
      card.append(thumb,info,del);
      card.addEventListener("click",async()=>{selectedAvatarId=av.id;await fetchDetailedAvatar(av.id);showView("avatar");});
    }else if(av.status==="not_saved"){
      card.className="entity-card entity-card--draft"+(av.id===selectedAvatarId?" active-purple":"");
      const thumb=document.createElement("div");thumb.className="entity-thumb sm";setThumb(thumb,av.image_url);
      const info=document.createElement("div");info.className="entity-info";
      info.innerHTML=`<div class="entity-name">${esc(av.name)}</div><div class="entity-stage" style="color:var(--amber)">◐ Not saved</div>`;
      const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";del.style.cssText="color:var(--red);background:rgba(244,63,94,0.14)";
      del.onclick=e=>{e.stopPropagation();openDeleteModal("avatar",av.id,av.name);};
      card.append(thumb,info,del);
      card.addEventListener("click",async()=>{selectedAvatarId=av.id;await fetchDetailedAvatar(av.id);showView("avatar");});
    }else if(av.status==="ready"){
      card.className="entity-card"+(av.id===selectedAvatarId?" active-purple":"");
      card.innerHTML=`<div class="entity-thumb sm" style="background-image:url('${(av.image_url||PLACEHOLDER).replace(/'/g,"\\'")}')"></div>
        <div class="entity-info"><div class="entity-name">${esc(av.name)}</div>
        <div class="entity-meta">${esc(av.decoration||"No style")}</div></div>`;
      const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";
      del.onclick=e=>{e.stopPropagation();openDeleteModal("avatar",av.id,av.name);};card.appendChild(del);
      card.addEventListener("click",async()=>{selectedAvatarId=av.id;await fetchDetailedAvatar(av.id);showView("avatar");});
      card.draggable=true;
      card.addEventListener("dragstart",e=>{e.dataTransfer.setData("text/plain",JSON.stringify({type:"avatar",id:av.id,name:av.name,image_url:av.image_url,persona_id:av.persona_id}));e.dataTransfer.effectAllowed="copy";});
    }else if(av.status==="processing"){
      card.className="entity-card entity-card--draft"+(av.id===selectedAvatarId?" active-purple":"");
      card.style.pointerEvents="auto";card.style.cursor="pointer";
      const thumb=document.createElement("div");thumb.className="entity-thumb sm";thumb.style.position="relative";setThumb(thumb,av.image_url);
      const spin=document.createElement("div");spin.className="entity-spin";thumb.appendChild(spin);
      const info=document.createElement("div");info.className="entity-info";
      info.innerHTML=`<div class="entity-name">${esc(av.name)}</div><div class="entity-stage">${esc(stageLabel(av.status,av.stage))}</div>`;
      const cancelBtn=document.createElement("button");cancelBtn.className="entity-delete-btn";cancelBtn.textContent="✕";cancelBtn.title="Cancel";cancelBtn.style.cssText="color:var(--red);background:rgba(244,63,94,0.14)";
      cancelBtn.onclick=e=>{e.stopPropagation();cancelBtn.disabled=true;
        fetch(`/${TENANT_ID}/avatars/${av.id}/cancel`,{method:"POST"}).then(refreshAll).catch(()=>{cancelBtn.disabled=false;});};
      card.append(thumb,info,cancelBtn);
      card.addEventListener("click",async()=>{selectedAvatarId=av.id;await fetchDetailedAvatar(av.id);showView("avatar");});
    }else if(av.status==="failed"){
      const rl=isRateLimitError(av.last_error);
      card.className="entity-card entity-card--draft"+(av.id===selectedAvatarId?" active-purple":"");
      card.style.pointerEvents="auto";card.style.cursor="pointer";
      card.addEventListener("click",async()=>{selectedAvatarId=av.id;await fetchDetailedAvatar(av.id);showView("avatar");});
      const thumb=document.createElement("div");thumb.className="entity-thumb sm";setThumb(thumb,av.image_url);
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
        try{await fetch(`/${TENANT_ID}/avatars/${av.id}/retry`,{method:"POST"}).then(r=>{if(!r.ok)throw new Error("retry failed");});await refreshAll();}
        catch(e){if(isRateLimitError(e.message)){_setRetry(rk);_ensureTick();}else{alert("Retry failed: "+e.message);}btn.disabled=false;}};
      card.appendChild(btn);if(rl)_ensureTick();
      card.appendChild(del);
    }
    list.appendChild(card);
  });
}

function renderLeftVoices(){
  const list=$("left-voice-list"),badge=$("voice-count-badge");
  list.innerHTML="";

  const sourceFilter = appliedFilters.voices.source;
  const genderFilter = appliedFilters.voices.gender;
  const personaFilter = appliedFilters.voices.persona;

  // 1. Render Default Voice card if applicable:
  const showDefault = (sourceFilter === "" || sourceFilter === "library") && 
                      (genderFilter === "" || genderFilter === "female") && 
                      (personaFilter === "");
                                     
  if (showDefault) {
    const dc=document.createElement("div");dc.className="voice-card"+("default"===selectedVoiceId?" active":"");dc.style.cursor="pointer";
    const di=document.createElement("div");di.className="voice-thumb-icon";di.textContent="🔊";
    const info=document.createElement("div");info.className="voice-card-info";
    info.innerHTML='<div class="voice-card-name">Default Voice</div><div class="voice-card-meta"><span class="voice-source-pill designed" style="background:rgba(100,100,255,.15);color:#b0b8ff;border-color:rgba(100,100,255,.35)">System</span>Built-in ElevenLabs voice</div>';
    const play=document.createElement("button");play.className="voice-play-btn";play.textContent="▶";play.title="Play preview";
    play.onclick=e=>{e.stopPropagation();playDefaultVoicePreview();};
    dc.append(di,info,play);
    dc.addEventListener("click",()=>{selectedVoiceId="default";showView("voice");});
    dc.draggable=true;
    dc.addEventListener("dragstart",e=>{e.dataTransfer.setData("text/plain",JSON.stringify({type:"voice",id:0,name:"Default Voice",isDefault:true}));e.dataTransfer.effectAllowed="copy";});
    list.appendChild(dc);
  }

  // Filter custom database voices
  const filteredVoices = voices.filter(v => {
    // source filter: prompt/clone/library
    if (sourceFilter === "prompt") {
      if (v.source !== "designed" && v.source !== "prompt") return false;
    } else if (sourceFilter === "clone") {
      if (v.source !== "cloned" && v.source !== "clone") return false;
    } else if (sourceFilter === "library") {
      return false; // hide custom database voices when library is selected
    }
    
    // gender filter
    if (genderFilter) {
      const vg = (v.gender || "").toLowerCase();
      if (vg !== genderFilter.toLowerCase() && vg !== "unisex" && vg !== "unknown") return false;
    }
    
    // persona filter
    if (personaFilter && String(v.persona_id) !== String(personaFilter)) return false;
    
    return true;
  });

  // 2. Render Custom Voices (designed/cloned from backend):
  filteredVoices.forEach(v=>{
    const card=document.createElement("div");
    if(v.status==="processing"){
      card.className="voice-card entity-card--inprog";
      card.style.pointerEvents="none";
      const icon=document.createElement("div");icon.className="voice-thumb-icon";icon.textContent="🎙";
      const info=document.createElement("div");info.className="voice-card-info";
      info.innerHTML=`<div class="voice-card-name">${esc(v.name)}</div><div class="entity-stage">⚡ Designing…</div>`;
      const spin=document.createElement("div");spin.className="entity-spin";spin.style.cssText="position:static;margin-left:auto;flex-shrink:0";
      card.append(icon,info,spin);
      list.appendChild(card);
    }else if(v.status==="failed"){
      card.className="voice-card";card.style.cursor="default";
      const icon=document.createElement("div");icon.className="voice-thumb-icon";icon.textContent="🎙";
      const info=document.createElement("div");info.className="voice-card-info";
      const errSnip=v.last_error?String(v.last_error).slice(0,60):"Unknown error";
      info.innerHTML=`<div class="voice-card-name">${esc(v.name)}</div><div class="entity-stage err" title="${esc(v.last_error||'')}">✗ ${esc(errSnip)}</div>`;
      const retry=document.createElement("button");retry.className="btn-retry-sm";retry.textContent="↻ Retry";retry.title="Retry";
      retry.onclick=async e=>{e.stopPropagation();retry.disabled=true;retry.textContent="…";
        try{await fetch(`/${TENANT_ID}/voices/${v.id}/retry`,{method:"POST"}).then(r=>{if(!r.ok)throw new Error("retry failed");});await refreshAll();}
        catch(er){alert("Retry failed: "+er.message);retry.disabled=false;retry.textContent="↻ Retry";}};
      const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";del.style.cssText="color:var(--red);background:rgba(244,63,94,0.14)";
      del.onclick=e=>{e.stopPropagation();openDeleteModal("voice",v.id,v.name);};
      card.append(icon,info,retry,del);
      list.appendChild(card);
    }else{
      card.className="voice-card"+(String(v.id)===String(selectedVoiceId)?" active":"");
      const pill=v.source==="cloned"?"cloned":"designed";
      const icon=document.createElement("div");icon.className="voice-thumb-icon";icon.textContent="🎙";
      const info=document.createElement("div");info.className="voice-card-info";
      info.innerHTML=`<div class="voice-card-name">${esc(v.name)}</div><div class="voice-card-meta"><span class="voice-source-pill ${pill}">${pill==="cloned"?"Clone":"AI"}</span>${esc(v.description?.slice(0,40)||"ElevenLabs")}</div>`;
      const play=document.createElement("button");play.className="voice-play-btn";play.textContent="▶";play.title="Play preview";
      play.onclick=e=>{e.stopPropagation();playVoicePreview(v);};
      const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";
      del.onclick=e=>{e.stopPropagation();openDeleteModal("voice",v.id,v.name);};
      card.append(icon,info,play,del);
      card.addEventListener("click",()=>{selectedVoiceId=v.id;showView("voice");});
      card.draggable=true;
      card.addEventListener("dragstart",e=>{e.dataTransfer.setData("text/plain",JSON.stringify({type:"voice",id:v.id,name:v.name}));e.dataTransfer.effectAllowed="copy";});
      list.appendChild(card);
    }
  });

  // 3. Render ElevenLabs Library subsection:
  const showLibrary = (sourceFilter === "" || sourceFilter === "library");
  let libraryCount = 0;
  if(showLibrary && libraryVoices.length){
    let filteredLib = libraryVoices;
    if (genderFilter) {
      filteredLib = libraryVoices.filter(lv => (lv.labels?.gender || "").toLowerCase() === genderFilter.toLowerCase());
    }
    if (personaFilter) {
      filteredLib = [];
    }
    libraryCount = filteredLib.length;
    
    if (filteredLib.length) {
      const hdr=document.createElement("div");
      hdr.style.cssText="display:flex;align-items:center;gap:6px;padding:8px 4px 4px;cursor:pointer;user-select:none;grid-column:1/-1";
      hdr.innerHTML=`<span style="font-size:10px;font-weight:700;letter-spacing:.08em;color:var(--muted);text-transform:uppercase">ElevenLabs Library</span><span style="font-size:10px;color:var(--muted)">(${filteredLib.length})</span><span style="margin-left:auto;font-size:11px;color:var(--muted)">${_libExpanded?"▲":"▼"}</span>`;
      hdr.addEventListener("click",()=>{_libExpanded=!_libExpanded;renderLeftVoices();});
      list.appendChild(hdr);
      if(_libExpanded){
        filteredLib.forEach(lv=>{
          const lc=document.createElement("div");lc.className="voice-card"+(String(lv.voice_id)===String(selectedVoiceId)?" active":"");lc.style.cssText="margin-left:4px;opacity:.9";
          const li=document.createElement("div");li.className="voice-thumb-icon";li.style.cssText="font-size:14px";li.textContent="🎙";
          const linfo=document.createElement("div");linfo.className="voice-card-info";
          const gender=lv.labels?.gender||"";const accent=lv.labels?.accent||"";
          const meta=[gender,accent].filter(Boolean).join(" · ")||"ElevenLabs";
          linfo.innerHTML=`<div class="voice-card-name">${esc(lv.name)}</div><div class="voice-card-meta"><span class="voice-source-pill designed" style="background:rgba(100,200,100,.1);color:#a0d8a0;border-color:rgba(100,200,100,.3)">Library</span>${esc(meta)}</div>`;
          const lplay=document.createElement("button");lplay.className="voice-play-btn";lplay.textContent="▶";lplay.title="Play preview";
          lplay.onclick=e=>{e.stopPropagation();const el=$("voice-preview-audio");if(el&&lv.preview_url){el.src=lv.preview_url;el.play().catch(()=>{});}};
          lc.append(li,linfo,lplay);
          lc.addEventListener("click",()=>{selectedVoiceId=lv.voice_id;showView("voice");});
          lc.draggable=true;
          lc.addEventListener("dragstart",e=>{e.dataTransfer.setData("text/plain",JSON.stringify({type:"library-voice",voice_id:lv.voice_id,name:lv.name,preview_url:lv.preview_url}));e.dataTransfer.effectAllowed="copy";});
          list.appendChild(lc);
        });
      }
    }
  }

  // Update badge count
  if (badge) {
    badge.textContent = filteredVoices.filter(v => v.status === "ready").length + (showDefault ? 1 : 0) + libraryCount;
  }
}

function renderLeftContacts(){
  const list=$("left-contacts-list"),badge=$("contacts-count-badge");
  const ready=assistants.filter(a=>!a.status||a.status==="ready");
  if(badge)badge.textContent=ready.length;list.innerHTML="";
  if(!ready.length){list.innerHTML='<p class="pane-hint">No assistants yet.</p>';return;}
  ready.forEach(asst=>{
    const item=document.createElement("div");
    item.className="contact-item"+(asst.id===selectedAssistantId?" active":"");
    const imgUrl = asst.avatar_image_url || PLACEHOLDER;
    const metaText = [asst.persona_name, asst.avatar_name].filter(Boolean).join(" › ") || "No avatar";
    item.innerHTML=`<div class="contact-thumb-sm" style="background-image:url('${imgUrl.replace(/'/g,"\\'")}')"></div>
      <div><div class="contact-item-name">${esc(asst.name)}</div>
      <div class="contact-item-meta">${esc(metaText)}</div></div>
      <div class="contact-call-dot"></div>`;
    const del=document.createElement("button");del.className="entity-delete-btn";del.innerHTML="🗑";del.title="Delete";
    del.onclick=e=>{e.stopPropagation();openDeleteModal("assistant",asst.id,asst.name);};item.appendChild(del);
    item.addEventListener("click",async()=>{
      selectedAssistantId=asst.id;
      // Fetch detailed assistant
      try {
        const r = await fetch(`/${TENANT_ID}/assistants/${asst.id}`);
        if (r.ok) {
          const detailed = await r.json();
          const idx = assistants.findIndex(x => x.id === asst.id);
          if (idx !== -1) {
            assistants[idx] = detailed;
          }
        }
      } catch (err) {
        console.error("Failed to fetch detailed assistant:", err);
      }
      showView("assistant");
    });
    list.appendChild(item);
  });
}

function populateFilterDropdowns(){
  const vp = $("filter-voices-persona");
  if(vp){
    const val = vp.value;
    vp.innerHTML = '<option value="">All Personas</option>';
    studio.forEach(p => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name;
      vp.appendChild(opt);
    });
    vp.value = val;
  }

  const pv = $("filter-personas-voice");
  if(pv){
    const val = pv.value;
    pv.innerHTML = '<option value="">All Voices</option>';
    voices.forEach(v => {
      const opt = document.createElement("option");
      opt.value = v.id;
      opt.textContent = v.name;
      pv.appendChild(opt);
    });
    pv.value = val;
  }

  const ap = $("filter-avatars-persona");
  if(ap){
    const val = ap.value;
    ap.innerHTML = '<option value="">All Personas</option>';
    studio.forEach(p => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name;
      ap.appendChild(opt);
    });
    ap.value = val;
  }

  const aa = $("filter-avatars-assistant");
  if(aa){
    const val = aa.value;
    aa.innerHTML = '<option value="">All Assistants</option>';
    assistants.forEach(a => {
      const opt = document.createElement("option");
      opt.value = a.id;
      opt.textContent = a.name;
      aa.appendChild(opt);
    });
    aa.value = val;
  }

  const asav = $("filter-assistants-avatar");
  if(asav){
    const val = asav.value;
    asav.innerHTML = '<option value="">All Avatars</option>';
    allAvatars.forEach(av => {
      const opt = document.createElement("option");
      opt.value = av.id;
      opt.textContent = av.name;
      asav.appendChild(opt);
    });
    asav.value = val;
  }
}

function updateActiveFilterSummaries(){
  // Voices summary
  const vg = appliedFilters.voices.gender, vs = appliedFilters.voices.source, vp = appliedFilters.voices.persona;
  const voiceFilters = [vg, vs ? (vs === "prompt" ? "designed" : vs === "clone" ? "cloned" : "library") : "", vp ? "persona" : ""].filter(Boolean);
  const voiceSummary = $("voices-active-summary");
  if (voiceSummary) {
    voiceSummary.textContent = voiceFilters.length ? `(${voiceFilters.length} active)` : "";
  }

  // Personas summary
  const pg = appliedFilters.personas.gender, ps = appliedFilters.personas.status, pv = appliedFilters.personas.voice, pa = appliedFilters.personas.has_avatars;
  const personaFilters = [pg, ps, pv ? "voice" : "", pa ? (pa === "true" ? "has avatars" : "no avatars") : ""].filter(Boolean);
  const personaSummary = $("personas-active-summary");
  if (personaSummary) {
    personaSummary.textContent = personaFilters.length ? `(${personaFilters.length} active)` : "";
  }

  // Avatars summary
  const as = appliedFilters.avatars.status, ap = appliedFilters.avatars.persona, aa = appliedFilters.avatars.assistant;
  const avatarFilters = [as, ap ? "persona" : "", aa ? "assistant" : ""].filter(Boolean);
  const avatarSummary = $("avatars-active-summary");
  if (avatarSummary) {
    avatarSummary.textContent = avatarFilters.length ? `(${avatarFilters.length} active)` : "";
  }

  // Assistants summary
  const ast = appliedFilters.assistants.status, av = appliedFilters.assistants.avatar;
  const assistantFilters = [ast, av ? "avatar" : ""].filter(Boolean);
  const assistantSummary = $("contacts-active-summary");
  if (assistantSummary) {
    assistantSummary.textContent = assistantFilters.length ? `(${assistantFilters.length} active)` : "";
  }
}

function renderAll(){
  updateActiveFilterSummaries();
  populateFilterDropdowns();
  renderLeftPersonas();
  renderLeftAvatars();
  renderLeftVoices();
  renderLeftContacts();
}

/* ── Center views ── */
const ALL_VIEWS=["cv-welcome","cv-new-persona","cv-persona","cv-avatar","cv-assistant","cv-voice-design","cv-new-avatar","cv-new-assistant","cv-voice"];
function showView(view){
  ALL_VIEWS.forEach(v=>{const e=$(v);if(e)e.hidden=true;});
  const target=$("cv-"+view);if(target)target.hidden=false;
  if(view==="new-persona")initNewPersona();
  else if(view==="persona")initPersonaView();
  else if(view==="voice")initVoiceView();
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
  setThumb($("pv-thumb"),p.image_url);$("pv-name").textContent=p.name;
  const gp=$("pv-gender");gp.textContent=p.gender==="female"?"♀ Female":p.gender==="male"?"♂ Male":"⚥ Unknown";
  const en=$("edit-persona-name");if(en)en.value=p.name;
  setStatus($("edit-persona-status"),"");
  const gender=p.gender||"unknown";
  // renderPresetUI now owns the selection state — onToggle is notify-only.
  renderPresetUI($("pv-preset-cats"),$("pv-preset-chips"),pvPresets,pvPresetCat,gender,
    ()=>renderAvPromptPreview(),
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
        const _r=await fetch(`/${TENANT_ID}/voices/from-library`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({voice_id:d.voice_id,name:d.name,preview_url:d.preview_url})});
        if(!_r.ok)throw new Error((await _r.json().catch(()=>({}))).detail||"voices/from-library failed");
        const v=await _r.json();
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
  const previewUrl=linkedVoice?.preview_url||p.voice_preview_url||"";
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
    await fetch(`/${TENANT_ID}/personas/${p.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({voice_ref_id:null})});
    pvDroppedVoiceId=null;renderPvVoiceSlot();
    await refreshAll();initPersonaView();
  }catch(e){setStatus($("edit-persona-status"),e.message,"error");}
});

/* ── Avatar view ── */
function initAvatarView(){
  const av=getAvatar(),p=getPersona();if(!av||!p){showView("welcome");return;}
  setThumb($("avv-thumb"),av.image_url);$("avv-name").textContent=av.name;$("avv-persona").textContent=p.name;
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
    $("avv-preview-img").src=av.image_url||PLACEHOLDER;
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
    ()=>{},
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
    fd.append("draft_avatar_id",av.id);
    {const _r=await fetch(`/${TENANT_ID}/avatars`,{method:"POST",body:fd});if(!_r.ok)throw new Error((await _r.json().catch(()=>({}))).detail||"avatar save failed");}
    setStatus($("avv-save-status"),"Saved!","success");await refreshAll();
  }catch(e){setStatus($("avv-save-status"),e.message,"error");}
  finally{btn.disabled=false;}
});

/* ── Assistant view ── */
function initAssistantView(){
  const asst=getAssistant();if(!asst){showView("welcome");return;}
  const p=studio.find(x=>x.id===asst.persona_id);
  const avatarUrl = asst.avatar?.image_url || PLACEHOLDER;
  const avatarName = asst.avatar?.name || "No avatar";
  setThumb($("ca-thumb"),avatarUrl);$("ca-name").textContent=asst.name;$("ca-sub").textContent=(p?.name||"")+" › "+avatarName;
  setThumb($("ca-call-thumb"),avatarUrl);$("ca-call-name").textContent=asst.name;
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
  // Voice design uses the same multi-select preset UI as the avatar
  // wizard — same renderPresetUI, just sourced from VOICE_PRESETS.
  // Selected ids accumulate in `vdSelectedVoicePresetIds`; the submit
  // handler sends them as repeated `voice_preset_ids` multipart fields.
  vdSelectedVoicePresetIds.length=0;
  const ind=$("vd-preset-indicator");if(ind)ind.hidden=true;
  const descEl2=$("vd-description");
  if(descEl2){descEl2.readOnly=false;descEl2.classList.remove("preset-locked");descEl2.placeholder="e.g. warm, confident, professional female narrator";}
  renderVdPresets();
  // Ensure design tab is active
  document.querySelector('.tab-btn[data-tab="vd-design"]')?.click();
}

// Stable closure used as both the initial render AND the onCat callback,
// so clicking category tabs always re-renders the chip grid (without it,
// the second click only updates the state variable and the DOM goes stale).
function renderVdPresets(){
  const catsEl=$("vd-preset-cats"),chipsEl=$("vd-preset-chips");
  if(!catsEl||!chipsEl)return;
  // Filter by the linked persona's gender if one was dropped, else show all.
  const linkedPersona=vdDroppedPersonaId?studio.find(p=>p.id===vdDroppedPersonaId):null;
  const gender=linkedPersona?.gender||"unknown";
  renderPresetUI(catsEl,chipsEl,vdSelectedVoicePresetIds,vdVoiceCat,gender,
    ()=>refreshVdPresetIndicator(),
    cat=>{vdVoiceCat=cat;renderVdPresets();},
    VOICE_PRESETS);
}

function refreshVdPresetIndicator(){
  const ind=$("vd-preset-indicator");if(!ind)return;
  const n=vdSelectedVoicePresetIds.length;
  if(!n){ind.hidden=true;return;}
  const labels=vdSelectedVoicePresetIds
    .map(id=>VOICE_PRESETS.find(p=>p.id===id)?.subcategory)
    .filter(Boolean)
    .join(" · ");
  ind.hidden=false;
  ind.innerHTML=`<span style="color:var(--secondary);font-weight:600">✦ ${n} preset${n!==1?"s":""}: ${esc(labels)}</span>`;
}

function renderVdPersonaSlot(){
  const slot=$("vd-persona-drop-slot");if(!slot)return;
  if(vdDroppedPersonaId){
    const p=studio.find(x=>x.id===vdDroppedPersonaId);
    const name=p?p.name:`Persona #${vdDroppedPersonaId}`;
    const imgUrl=p?.image_url||PLACEHOLDER;
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
  const hasPresets=vdSelectedVoicePresetIds.length>0;
  if(!hasPresets&&!description&&!includePersona){setStatus(statusEl,"Please describe the voice, pick at least one preset, or enable persona voice profile.","error");return;}
  btn.disabled=true;setStatus(statusEl,"Creating voice…");
  try{
    const fd=new FormData();
    fd.append("name",name);
    // Repeated multipart field — FastAPI binds to ``voice_preset_ids: list[str]``.
    vdSelectedVoicePresetIds.forEach(id=>fd.append("voice_preset_ids",id));
    if(description)fd.append("description",description);
    if(vdDroppedPersonaId){fd.append("persona_id",String(vdDroppedPersonaId));fd.append("include_persona_traits",includePersona?"true":"false");}

    {const _r=await fetch(`/${TENANT_ID}/voices/design`,{method:"POST",body:fd});if(!_r.ok)throw new Error((await _r.json().catch(()=>({}))).detail||"voice design failed");}
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
    const fd=new FormData();fd.append("voice_sample",vdCloneSampleFile,vdCloneSampleFile.name);fd.append("name",name);
    {const _r=await fetch(`/${TENANT_ID}/voices/clone`,{method:"POST",body:fd});if(!_r.ok)throw new Error((await _r.json().catch(()=>({}))).detail||"voice clone failed");}
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
    const fd=new FormData();fd.append("name",name);fd.append("persona_image",croppedBlob,"persona.png");
    const _r=await fetch(`/${TENANT_ID}/personas`,{method:"POST",body:fd});
    if(!_r.ok)throw new Error((await _r.json().catch(()=>({}))).detail||"persona create failed");
    const p=await _r.json();
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
    await fetch(`/${TENANT_ID}/personas/${selectedPersonaId}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({...body})});
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
    (grouped[p.category]=grouped[p.category]||[]).push(p.subcategory);
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
  const _r=await fetch(`/${TENANT_ID}/avatars/preview`,{method:"POST",body:fd});
  if(!_r.ok)throw new Error((await _r.json().catch(()=>({}))).detail||"avatar preview failed");
  const data=await _r.json();
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
    
    {const _r=await fetch(`/${TENANT_ID}/avatars/preview`,{method:"POST",body:fd});if(!_r.ok)throw new Error((await _r.json().catch(()=>({}))).detail||"avatar preview failed");}
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
  slot.innerHTML=`<img class="slot-thumb" src="${esc(p.image_url||PLACEHOLDER)}" alt=""/><b>${esc(p.name)}</b><span class="drop-hint" style="margin-left:auto">${esc(p.gender||"unknown")}</span><button type="button" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px;line-height:1;margin-left:12px;padding:0" title="Clear" onclick="navDroppedPersonaId=null;renderNavPersonaSlot();renderNavPresets();renderNavPromptPreview();">✕</button>`;
  if(btn)btn.disabled=false;
}
function renderNavPromptPreview(){
  const el=$("nav-prompt-preview");if(!el)return;
  const grouped={};
  navPresets.forEach(id=>{const pr=PRESETS.find(x=>x.id===id);if(!pr)return;(grouped[pr.category]=grouped[pr.category]||[]).push(pr.subcategory);});
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
    ()=>renderNavPromptPreview(),
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
    
    {const _r=await fetch(`/${TENANT_ID}/avatars/preview`,{method:"POST",body:fd});if(!_r.ok)throw new Error((await _r.json().catch(()=>({}))).detail||"avatar preview failed");}
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
  slot.innerHTML=`<img class="slot-thumb" src="${esc(av.image_url||PLACEHOLDER)}" alt=""/><b>${esc(av.name)}</b><span class="drop-hint" style="margin-left:auto">via ${esc(ownerPersona?.name||"")}</span><button type="button" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px;line-height:1;margin-left:12px;padding:0" title="Clear" onclick="naDroppedAvatarId=null;renderNaAvatarSlot()">✕</button>`;
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
    const _r=await fetch(`/${TENANT_ID}/assistants`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,prompt,first_message:fm||"Hi!",persona_id:ownerPersona.id,avatar_id:av.id})});
    if(!_r.ok)throw new Error((await _r.json().catch(()=>({}))).detail||"assistant create failed");
    const r=await _r.json();
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
  try{
    const _r=await fetch(`/${TENANT_ID}/assistants`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,prompt,first_message:fm||"Hi!",persona_id:p.id,avatar_id:av.id})});
    if(!_r.ok)throw new Error((await _r.json().catch(()=>({}))).detail||"assistant create failed");
    const r=await _r.json();
    setStatus($("asst-status"),`'${r.name}' created!`,"success");
    await refreshAll();
  }
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
    const fd=new FormData();fd.append("persona_id",p.id);fd.append("name",name);fd.append("decoration",tp);fd.append("theme_prompt",tp);fd.append("preset_ids",JSON.stringify(clonePresets));
    {const _r=await fetch(`/${TENANT_ID}/avatars`,{method:"POST",body:fd});if(!_r.ok)throw new Error((await _r.json().catch(()=>({}))).detail||"avatar save failed");}setStatus($("clone-av-status"),"Clone queued!","success");await refreshAll();
  }catch(e){setStatus($("clone-av-status"),e.message,"error");}finally{$("clone-av-save-btn").disabled=false;}
});

/* ── Clone + Call Assistant ── */
$("launch-call-btn").addEventListener("click",launchCall);
$("clone-asst-btn").addEventListener("click",async()=>{
  const asst=getAssistant();if(!asst)return;
  const name=$("clone-asst-name")?.value.trim(),prompt=$("clone-asst-prompt")?.value.trim(),fm=$("clone-asst-msg")?.value.trim();
  if(!name||!prompt)return setStatus($("clone-asst-status"),"Name and instructions required.","error");
  $("clone-asst-btn").disabled=true;setStatus($("clone-asst-status"),"Creating…");
  try{
    const _r=await fetch(`/${TENANT_ID}/assistants`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,prompt,first_message:fm,persona_id:asst.persona_id,avatar_id:asst.avatar_id})});
    if(!_r.ok)throw new Error((await _r.json().catch(()=>({}))).detail||"assistant clone failed");
    const r=await _r.json();
    setStatus($("clone-asst-status"),`'${r.name}' created!`,"success");
    await refreshAll();
  }
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
    if(type==="persona"){
      const _r=await fetch(`/${TENANT_ID}/personas/${id}/cascade-count`);
      const c=await _r.json();
      const pts=[];
      if(c.avatars)pts.push(`${c.avatars} avatar${c.avatars!==1?"s":""}`);
      if(c.assistants)pts.push(`${c.assistants} assistant${c.assistants!==1?"s":""}`);
      if(pts.length){$("delete-modal-cascade-text").textContent=`Will leave ${pts.join(" and ")} orphaned.`;$("delete-modal-cascade").hidden=false;}
    }
    else if(type==="avatar"){
      const _r=await fetch(`/${TENANT_ID}/avatars/${id}/cascade-count`);
      const c=await _r.json();
      if(c.assistants){$("delete-modal-cascade-text").textContent=`Will leave ${c.assistants} assistant${c.assistants!==1?"s":""} orphaned.`;$("delete-modal-cascade").hidden=false;}
    }
  }catch{}})();
  $("delete-modal").hidden=false;
}
$("delete-cancel-btn").addEventListener("click",()=>{$("delete-modal").hidden=true;_pendingDelete=null;});
$("delete-confirm-btn").addEventListener("click",async()=>{
  if(!_pendingDelete)return;const{type,id}=_pendingDelete;$("delete-confirm-btn").disabled=true;
  try{
    const path=type==="assistant"?"assistants":type==="avatar"?"avatars":type==="voice"?"voices":"personas";
    {
      const _r=await fetch(`/${TENANT_ID}/${path}/${id}`,{method:"DELETE"});
      if(!_r.ok)throw new Error((await _r.json().catch(()=>({}))).detail||"delete failed");
    }
    $("delete-modal").hidden=true;_pendingDelete=null;
    if(type==="persona"&&selectedPersonaId===id){selectedPersonaId=null;selectedAvatarId=null;showView("welcome");}
    else if(type==="avatar"&&selectedAvatarId===id){selectedAvatarId=null;showView("persona");}
    else if(type==="assistant"&&selectedAssistantId===id){selectedAssistantId=null;showView("welcome");}
    else if(type==="voice"){selectedVoiceId=null;}
    await refreshAll();
  }catch(e){alert("Delete failed: "+e.message);}finally{$("delete-confirm-btn").disabled=false;}
});

/* ── Call (Simli Auto via Daily) ── */
function setCallMode(m){$("call-shell").hidden=m!=="fullscreen";$("floating-call").hidden=m!=="floating";}
function setCallWaiting(show,msg){const el=$("call-waiting");if(!el)return;el.hidden=!show;if(msg)el.textContent=msg;}

let dailyCall=null;

// Attach a single Daily remote track to its corresponding DOM element.
// Called from the `track-started` event so we attach the MOMENT the
// track becomes available — waiting for `participant-updated` adds
// ~100-300ms of head-of-audio cut-off because the avatar starts
// speaking firstMessage immediately on join.
function _dailyAttachTrack(track, kind){
  if (!track) return;
  if (kind === "video") {
    const vid = $("call-video");
    if (!vid) return;
    if (vid.srcObject?.getTracks().includes(track)) return;
    vid.srcObject = new MediaStream([track]);
    vid.play().catch(e => console.warn("[Simli/Daily] video play() blocked:", e?.message));
    setCallWaiting(false);
  } else if (kind === "audio") {
    const aud = $("call-audio");
    if (!aud) return;
    if (aud.srcObject?.getTracks().includes(track)) return;
    aud.srcObject = new MediaStream([track]);
    aud.play().catch(e => console.warn("[Simli/Daily] audio play() blocked:", e?.message));
  }
}

// Late-arrival sweep: when our listeners wire up after the avatar has
// already joined, the `track-started` event has fired and is gone. Walk
// each remote participant's persistent tracks and attach what we missed.
function _dailyAttachExistingTracks(call){
  const participants = call.participants();
  Object.values(participants).forEach(p => {
    if (!p || p.local) return;
    const vt = p.tracks?.video?.persistentTrack;
    const at = p.tracks?.audio?.persistentTrack;
    if (vt) _dailyAttachTrack(vt, "video");
    if (at) _dailyAttachTrack(at, "audio");
  });
}

async function _launchSimliAutoCall(data){
  if(!window.Daily){throw new Error("Daily SDK not loaded yet — try again in a moment.");}

  // Daily's documented property names are `audioSource` / `videoSource`
  // (not `audio` / `video`). Setting them on the call object makes them
  // the policy for the entire call — startCamera + join inherit, no
  // webcam is ever requested.
  const call=window.Daily.createCallObject({
    audioSource: true,
    videoSource: false,
  });
  dailyCall=call;

  call.on("joined-meeting", ev => {
    const localAudio = ev.participants?.local?.tracks?.audio?.state;
    console.log("[Simli/Daily] joined-meeting; localAudioState=", localAudio,
      "localId=", ev.participants?.local?.session_id);
  });
  call.on("participant-joined", ev => {
    console.log("[Simli/Daily] participant-joined:",
      ev.participant?.user_name || ev.participant?.session_id);
  });
  call.on("track-started", ev => {
    if (ev.participant?.local) {
      console.log("[Simli/Daily] local track-started:", ev.track?.kind);
      return;
    }
    console.log("[Simli/Daily] remote track-started:", ev.track?.kind,
      "from", ev.participant?.user_name || ev.participant?.session_id);
    _dailyAttachTrack(ev.track, ev.track?.kind);
  });
  call.on("participant-left", ev => {
    console.log("[Simli/Daily] participant-left:",
      ev.participant?.user_name || ev.participant?.session_id);
    const v=$("call-video"); if(v) try{v.srcObject=null;}catch{}
    const a=$("call-audio"); if(a) try{a.srcObject=null;}catch{}
  });
  call.on("left-meeting", () => hangup());
  call.on("error", e => { console.error("[Simli/Daily] error:", e); alert("Call error: "+(e?.errorMsg||"unknown")); hangup(); });

  // Active-speaker fires when ANY participant becomes the dominant
  // speaker. If the local id is reported when you talk, your mic IS
  // publishing audio that Simli is receiving.
  call.on("active-speaker-change", ev => {
    const localId = dailyCall?.participants?.().local?.session_id;
    const who = ev.activeSpeaker?.peerId;
    console.log("[Simli/Daily] active-speaker:", who, who === localId ? "(you)" : "(avatar)");
  });

  // Simli broadcasts pipeline state via Daily app-messages. This is the
  // single most useful diagnostic for "welcome plays but no response":
  //   ApplicationState: 0 → 👂 Listening (waiting for user speech)
  //   ApplicationState: 1 → 💡 Thinking  (LLM in flight)
  //   ApplicationState: 2 → 💭 Talking   (TTS streaming back)
  // If the avatar stays in 0 after you speak → your mic isn't reaching
  // STT. If it goes 0→1 but never 2 → LLM call is failing server-side.
  call.on("app-message", ev => {
    const raw = ev.data;
    let label = raw;
    if (raw === "ApplicationState: 0") label = "👂 LISTENING";
    else if (raw === "ApplicationState: 1") label = "💡 THINKING";
    else if (raw === "ApplicationState: 2") label = "💭 TALKING";
    console.log("[Simli/Daily] app-message:", label);
  });

  // Inputs are already fixed by createCallObject(), so join with just the URL.
  await call.join({url:data.simli_auto.room_url});
  _dailyAttachExistingTracks(call);

  // Confirmation log a moment after join — if localAudioState is
  // "playable" the mic is publishing; anything else means STT is silent.
  setTimeout(() => {
    const local = dailyCall?.participants?.().local;
    console.log("[Simli/Daily] post-join check:",
      "localAudioState=", local?.tracks?.audio?.state,
      "localVideoState=", local?.tracks?.video?.state);
  }, 1500);
}

async function launchCall(){
  const asst=getAssistant();if(!asst)return;
  try{
    const _r=await fetch(`/${TENANT_ID}/calls`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({assistant_id:asst.id})});
    if(!_r.ok)throw new Error((await _r.json().catch(()=>({}))).detail||"call create failed");
    const data=await _r.json();
    $("call-title").textContent=asst.name;
    setCallMode("fullscreen");setCallWaiting(true,"Connecting…");

    if(data.transport==="auto" && data.simli_auto){
      await _launchSimliAutoCall(data);
    } else {
      throw new Error(
        "Frontend only supports the Simli Auto transport. "
        + "Set SIMLI_TRANSPORT=auto on the API and restart. "
        + "(Received transport: "+(data.transport||"(none)")+")"
      );
    }
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
  try{dailyCall?.leave();}catch{}
  try{dailyCall?.destroy();}catch{}
  dailyCall=null;
  const v=$("call-video");if(v){try{v.srcObject=null;}catch{}}
  const a=$("call-audio");if(a){try{a.srcObject=null;}catch{}}
  setCallWaiting(true,"Connecting…");
  setCallMode("hidden");
}
$("hangup-call-btn").addEventListener("click",hangup);$("floating-hangup-btn").addEventListener("click",hangup);

/* ── WS + Data ── */
// The pure API has no WebSocket; background-task progress is exposed via
// per-resource /status endpoints. For the demo we keep it simple and just
// re-fetch everything every 3 seconds. A production front-end would poll
// only the resources it's actively waiting on (see README for the
// per-resource polling recipe).
function startPolling(){
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(()=>{refreshAll().catch(()=>{});},3000);
}
// GET /presets — no tenant context (catalogue is global).
async function loadPresets(){
  const r=await fetch("/presets");
  const data=await r.json();
  PRESETS=data.avatar_presets||[];
  VOICE_PRESETS=data.voice_presets||[];
}

// GET /personas?tenant_id=… — filters and pagination would go on the URL too,
// e.g. ?tenant_id=…&gender=female&limit=20&offset=0.
async function loadStudio(){
  const gender = appliedFilters.personas.gender;
  const status = appliedFilters.personas.status;
  const voice_ref_id = appliedFilters.personas.voice;
  const has_avatars = appliedFilters.personas.has_avatars;
  let url = `/${TENANT_ID}/personas?`;
  if(gender) url += `gender=${gender}&`;
  if(status) url += `status=${status}&`;
  if(voice_ref_id) url += `voice_ref_id=${voice_ref_id}&`;
  if(has_avatars) url += `has_avatars=${has_avatars}&`;
  const r=await fetch(url);
  studio=await r.json();
}

async function loadAssistants(){
  const status = appliedFilters.assistants.status;
  const avatar_id = appliedFilters.assistants.avatar;
  let url = `/${TENANT_ID}/assistants?`;
  if(status) url += `status=${status}&`;
  if(avatar_id) url += `avatar_id=${avatar_id}&`;
  const r=await fetch(url);
  assistants=await r.json();
}

async function loadVoices(){
  const gender = appliedFilters.voices.gender;
  const source = appliedFilters.voices.source;
  const persona_id = appliedFilters.voices.persona;
  let url = `/${TENANT_ID}/voices?`;
  if(gender) url += `gender=${gender}&`;
  if(source === "prompt") url += `source=designed&`;
  else if(source === "clone") url += `source=cloned&`;
  else if(source === "library") url += `source=library&`;
  if(persona_id) url += `persona_id=${persona_id}&`;
  const r=await fetch(url);
  voices=await r.json();
}

async function loadLibraryVoices(){
  if(libraryVoices.length)return;
  try{
    const r=await fetch(`/${TENANT_ID}/voices/library`);
    libraryVoices=await r.json();
  }catch{}
}

async function loadAllAvatars(){
  try {
    const r = await fetch(`/${TENANT_ID}/avatars`);
    if (r.ok) {
      allAvatars = await r.json();
    }
  } catch (err) {
    console.error("Failed to load all avatars:", err);
  }
}

async function refreshAll(){
  await Promise.all([loadStudio(),loadAssistants(),loadVoices(),loadAvatars(),loadAllAvatars()]);
  renderAll();
  // Re-init persona voice slot if persona edit is visible
  if(!$("cv-persona")?.hidden){const p=getPersona();if(p)setupPvVoiceDropSlot(p);}
  const need=studio.some(p=>p.status&&p.status!=="ready")||selectedPersonaAvatars.some(a=>a.status==="processing"||a.status==="generating")||assistants.some(a=>a.status&&a.status!=="ready")||voices.some(v=>v.status==="processing");
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
  initFilters();
  ALL_VIEWS.forEach(v=>{const e=$(v);if(e)e.hidden=(v!=="cv-welcome");});
  startPolling();
  try{await Promise.all([loadPresets(),loadStudio(),loadAssistants(),loadVoices(),loadAllAvatars()]);}catch(e){console.error("Initial load error:",e);}
  renderAll();
  loadLibraryVoices().then(()=>renderLeftVoices()).catch(()=>{});
});
