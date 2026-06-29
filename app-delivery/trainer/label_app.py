"""
Labeling web app for the corner-photo training set.

Open it in a browser and just label — it AUTO-PULLS shipments for you in the
background (scrape VINs off the SD web list -> fetch Delivery photos via the official
API, dedup on shipment guid), topping up the pool as you work. No manual pulling.

The view is STABLE: it shows a page of VINs and never reshuffles under you. Click
"Next batch" at the top to wipe the page and load the next VINs that still need
labeling. Progress saves to trainer/labels.json on every click (auto-saved).

  python trainer/label_app.py [--host 0.0.0.0] [--port 8095]
  then open http://localhost:8095

Keyboard (with a photo active): f=front r=rear l=left p=right x=reject u=unset · ←/→ move
Clicking a photo's current label again (or pressing the same key) UNMARKS it. No Claude.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))                     # app-delivery root (photo_select_clip)
import puller                                                  # noqa: E402  (orchestrator)

POOL = puller.POOL
DB_PATH = puller.DB
LABELS_PATH = os.path.join(HERE, "labels.json")
SERVED_PATH = os.path.join(HERE, "served.json")               # VIN dirs already shown/done
REVIEWED_PATH = os.path.join(HERE, "reviewed.json")           # photos resolved in smart review
MODEL_PATH = os.path.join(HERE, "model.joblib")
CACHE_PATH = os.path.join(HERE, "emb_cache.joblib")
# Corner-based scheme: inspection photos are consistently corner shots, so we detect
# the four corners plus straight front/rear. `reject` (junk: VIN closeups/interiors/
# paperwork) is a utility class, excluded from the output slots.
CLASSES = ["front", "rear", "front_left", "front_right", "rear_left", "rear_right",
           "white_key", "black_key", "reject"]
IMG_EXTS = (".jpg", ".jpeg", ".png")

PAGE_VINS = int(os.getenv("TRAINER_PAGE_VINS", "20"))         # VINs shown per page
# Tesla model = 4th VIN char (3=Model 3, Y, X, S, C=Cybertruck). Pages round-robin
# across these so each batch is a model mix (not all Model 3s clustered together).
# Non-Tesla VINs that the scrape happens to pull are skipped in the labeling view.
TESLA_MODELS = ["3", "Y", "X", "S", "C"]


def _model_of(vin_dir: str) -> str:
    vin = vin_dir.split("__")[0]                              # strip the __<guid8> suffix
    return vin[3].upper() if len(vin) >= 4 else "?"
BATCH = int(os.getenv("TRAINER_BATCH", "20"))                 # shipments per background pull
# Keep at least this many FRESH (untouched, unserved) VINs buffered so "Next batch" is
# instant; pull more in the background when the buffer runs low.
MIN_FRESH = int(os.getenv("TRAINER_MIN_FRESH", "40"))

_lock = threading.Lock()
_pull = {"active": False, "log": []}


def _load_served() -> set:
    if os.path.exists(SERVED_PATH):
        try:
            return set(json.load(open(SERVED_PATH)))
        except Exception:
            return set()
    return set()


def _save_served(s: set) -> None:
    tmp = SERVED_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(sorted(s), fh)
    os.replace(tmp, SERVED_PATH)


def _load_reviewed() -> set:
    if os.path.exists(REVIEWED_PATH):
        try:
            return set(json.load(open(REVIEWED_PATH)))
        except Exception:
            return set()
    return set()


def _save_reviewed(s: set) -> None:
    tmp = REVIEWED_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(sorted(s), fh)
    os.replace(tmp, REVIEWED_PATH)


def _review_items(limit: int = 60) -> dict:
    """Smart review: run the trained head over labeled photos and surface where it
    DISAGREES with your label (or is unsure of it). Uses the cached embeddings + the
    saved model, so it's fast. Sorted worst-first (most confident disagreement)."""
    import numpy as np
    import joblib
    if not os.path.exists(MODEL_PATH):
        return {"items": [], "total": 0, "error": "no model yet — run ./train.sh train first."}
    bundle = joblib.load(MODEL_PATH)
    clf, classes = bundle["clf"], list(bundle["classes"])
    cls_idx = {c: i for i, c in enumerate(classes)}

    labels = _load_labels()
    reviewed = _load_reviewed()
    paths, meta = [], []
    for k, lab in labels.items():
        if not lab or lab not in cls_idx or k in reviewed:
            continue
        fp = os.path.join(POOL, k)
        if os.path.isfile(fp):
            paths.append(fp)
            meta.append((k, lab))
    if not paths:
        return {"items": [], "total": 0}

    cache = joblib.load(CACHE_PATH) if os.path.exists(CACHE_PATH) else {}
    missing = [p for p in paths if p not in cache]
    if missing:
        import photo_select_clip as psc
        for p, v in zip(missing, psc.embed_paths(missing)):
            cache[p] = v
        joblib.dump(cache, CACHE_PATH)
    X = np.stack([cache[p] for p in paths])
    proba = clf.predict_proba(X)

    # Only TRUE disagreements (model's top class != your label) — these are the likely
    # mislabels / genuinely-hard cases. Worst-first = where the model is most confident
    # you're wrong. (Low-confidence-but-agreeing photos aren't errors, so we skip them.)
    out = []
    for (k, lab), pr in zip(meta, proba):
        pred = classes[int(pr.argmax())]
        if pred == lab:
            continue
        out.append({"key": k, "label": lab, "pred": pred,
                    "p_pred": round(float(pr.max()), 2),
                    "p_label": round(float(pr[cls_idx[lab]]), 2), "sev": float(pr.max())})
    out.sort(key=lambda d: -d["sev"])
    return {"items": out[:limit], "total": len(out)}


# --------------------------- labels + pool ---------------------------------
def _load_labels() -> dict:
    if os.path.exists(LABELS_PATH):
        with open(LABELS_PATH) as fh:
            return json.load(fh)
    return {}


def _save_labels(d: dict) -> None:
    tmp = LABELS_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=2, sort_keys=True)
    os.replace(tmp, LABELS_PATH)


def _pool_state() -> list[dict]:
    labels = _load_labels()
    out = []
    if not os.path.isdir(POOL):
        return out
    for vin in sorted(os.listdir(POOL)):
        vdir = os.path.join(POOL, vin)
        if not os.path.isdir(vdir):
            continue
        photos = [{"key": f"{vin}/{f}", "file": f, "label": labels.get(f"{vin}/{f}")}
                  for f in sorted(os.listdir(vdir)) if f.lower().endswith(IMG_EXTS)]
        if photos:
            out.append({"vin": vin, "photos": photos})
    return out


def _fresh_groups() -> list[dict]:
    """Pool VIN groups that are FRESH: not yet served AND with no labels yet (a VIN
    with any label is considered worked-on). These are what 'Next batch' draws from."""
    served = _load_served()
    out = []
    for v in _pool_state():
        if v["vin"] in served:
            continue
        if any(p["label"] for p in v["photos"]):     # touched -> not fresh
            continue
        out.append(v)
    return out


def _fresh_balanced() -> list[dict]:
    """Fresh VIN groups for the Tesla models, ordered round-robin across models so a
    page is an even mix (3 / Y / X / S / C), not all Model 3s in a row."""
    buckets = {m: [] for m in TESLA_MODELS}
    for g in _fresh_groups():
        m = _model_of(g["vin"])
        if m in buckets:
            buckets[m].append(g)
    out = []
    while any(buckets[m] for m in TESLA_MODELS):
        for m in TESLA_MODELS:
            if buckets[m]:
                out.append(buckets[m].pop(0))
    return out


def _fresh_by_model() -> dict:
    counts = {m: 0 for m in TESLA_MODELS}
    for g in _fresh_groups():
        m = _model_of(g["vin"])
        if m in counts:
            counts[m] += 1
    return counts


def _fresh_count() -> int:
    return len(_fresh_balanced())


# --------------------------- background pulling ----------------------------
def _run_pull(n: int) -> None:
    if _pull["active"]:
        return
    _pull["active"] = True
    try:
        res = puller.pull_batch(n, log=lambda m: _pull.__setitem__("log", (_pull["log"] + [m])[-8:]))
        _pull["log"] = res["log"][-8:]
    finally:
        _pull["active"] = False


def _maybe_topup() -> None:
    if not _pull["active"] and _fresh_count() < MIN_FRESH:
        threading.Thread(target=_run_pull, args=(BATCH,), daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _maybe_topup()
    yield


app = FastAPI(lifespan=lifespan)


# --------------------------------- API -------------------------------------
class LabelOne(BaseModel):
    key: str
    label: Optional[str] = None


def _counts() -> dict:
    vins = _pool_state()
    counts = {c: 0 for c in CLASSES}
    total = labeled = 0
    for v in vins:
        for p in v["photos"]:
            total += 1
            if p["label"] in counts:
                counts[p["label"]] += 1
                labeled += 1
    return {"classes": CLASSES, "counts": counts, "total": total,
            "labeled": labeled, "unlabeled": total - labeled,
            "pulling": _pull["active"], "fresh": _fresh_count(),
            "fresh_by_model": _fresh_by_model(), "page_vins": PAGE_VINS}


@app.get("/api/state")
def api_state():
    """Counts + status for the top bar (does NOT return the grid)."""
    _maybe_topup()
    return _counts()


@app.get("/api/page")
def api_page():
    """The current page: the first PAGE_VINS FRESH (untouched, unserved) VIN groups.
    Does not mark anything — refreshing the browser shows the same page."""
    _maybe_topup()
    return {"page": _fresh_balanced()[:PAGE_VINS], **_counts()}


class Advance(BaseModel):
    vins: list[str] = []


@app.post("/api/advance")
def api_advance(body: Advance):
    """'Next batch': mark the given VINs as served (never show again), then return the
    next page of fresh VINs and kick off a background pull to refill the buffer."""
    with _lock:
        served = _load_served()
        served.update(body.vins)
        _save_served(served)
    _maybe_topup()
    return {"page": _fresh_balanced()[:PAGE_VINS], **_counts()}


@app.get("/api/review")
def api_review():
    """Smart-review items: labeled photos where the trained model disagrees / is unsure."""
    return _review_items()


class ReviewResolve(BaseModel):
    key: str


@app.post("/api/review_resolve")
def api_review_resolve(body: ReviewResolve):
    """Mark a photo as reviewed (kept or fixed) so it drops out of the review list."""
    with _lock:
        r = _load_reviewed()
        r.add(body.key)
        _save_reviewed(r)
    return {"ok": True}


@app.post("/api/label")
def api_label(body: LabelOne):
    if body.label and body.label not in CLASSES:
        raise HTTPException(400, f"bad label {body.label}")
    with _lock:
        labels = _load_labels()
        if body.label:
            labels[body.key] = body.label
        else:
            labels.pop(body.key, None)
        _save_labels(labels)
    _maybe_topup()
    return {"ok": True}


@app.get("/img")
def img(key: str):
    safe = os.path.normpath(key)
    if safe.startswith("..") or os.path.isabs(safe):
        raise HTTPException(400, "bad key")
    path = os.path.join(POOL, safe)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path)


@app.get("/", response_class=HTMLResponse)
def index():
    return _HTML


_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>corner-photo labeler</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#1e1e1e;color:#eee}
 header{position:sticky;top:0;background:#111;padding:10px 14px;border-bottom:1px solid #333;z-index:10}
 header b{font-size:15px}
 button{padding:6px 12px;margin:0 8px;background:#2d6cdf;color:#fff;border:0;border-radius:5px;cursor:pointer;font-weight:bold}
 button:hover{background:#3f7df0}
 .counts span{margin-right:12px;font-size:13px}
 .hint{font-size:12px;color:#aaa}
 .vin{padding:8px 14px;font-weight:bold;color:#9cf;border-top:1px solid #333}
 .grid{display:flex;flex-wrap:wrap;gap:8px;padding:0 14px 14px}
 .card{width:230px;border:3px solid #444;border-radius:6px;overflow:hidden;background:#262626;cursor:pointer}
 .card.active{outline:3px solid #fff}
 .card img{width:230px;height:172px;object-fit:cover;display:block;background:#333}
 .card .lab{font-size:12px;padding:3px 6px;text-transform:uppercase;font-weight:bold;color:#888}
 .chips{display:flex;gap:2px;padding:3px}
 .chips b{flex:1;text-align:center;font-size:12px;padding:4px 0;border-radius:3px;background:#333;cursor:pointer;margin:0}
 .chips b:hover{background:#555}
 #empty{padding:40px 14px;color:#aaa}
</style></head><body>
<header>
 <b>corner-photo labeler</b>
 <button onclick="loadPage(true)">Next batch ↻</button>
 <button onclick="loadReview()" style="background:#8e44ad">Review (smart) 🔍</button>
 <span class=hint>q FL·w front·e FR·a RL·s rear·d RR·k white-key·j black-key·x reject·u unset·←/→ move·same again=unmark·review: g=keep</span>
 <div class=counts id=counts></div>
</header>
<div id=app><div id=empty>loading…</div></div>
<script>
// Spatial keys: q/w/e = front-left/front/front-right, a/s/d = rear-left/rear/rear-right.
// k = white key, j = black key (key-card / fob closeups), x reject, u unset.
const CLS={q:'front_left',w:'front',e:'front_right',a:'rear_left',s:'rear',d:'rear_right',
           k:'white_key',j:'black_key',x:'reject'};
const COLORS={front:'#2e86de',rear:'#c0392b',front_left:'#27ae60',front_right:'#16a085',
              rear_left:'#e67e22',rear_right:'#8e44ad',white_key:'#95a5a6',black_key:'#2c3e50',reject:'#7f8c8d'};
const SHORT={front:'F',rear:'R',front_left:'FL',front_right:'FR',rear_left:'RL',rear_right:'RR',
             white_key:'WK',black_key:'BK',reject:'X'};
const CHIP_ORDER=['front_left','front','front_right','rear_left','rear','rear_right',
                  'white_key','black_key','reject'];
let order=[],active=0,labelByKey={},curVins=[],reviewMode=false;

function applyLabel(card,label){
 const lab=card.querySelector('.lab');
 lab.textContent=label?SHORT[label]:'·';
 lab.style.background=label?COLORS[label]:'transparent';
 lab.style.color=label?'#fff':'#888';
 card.style.borderColor=label?COLORS[label]:'#444';
}
function renderActive(){
 document.querySelectorAll('.card').forEach(e=>e.classList.remove('active'));
 const el=document.getElementById('c'+active);
 if(el){el.classList.add('active');el.scrollIntoView({block:'nearest'});}
}
async function setLabelTo(idx,label){          // set exactly (label or null to unmark)
 const key=order[idx]; if(key===undefined)return;
 labelByKey[key]=label;
 const card=document.getElementById('c'+idx); if(card)applyLabel(card,label);   // optimistic
 try{await fetch('/api/label',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({key,label})});}catch(e){}
}
function toggle(idx,cls){                       // click/press same label again -> unmark
 return setLabelTo(idx, labelByKey[order[idx]]===cls ? null : cls);
}

// Build a fresh page (wipes everything). Called on load and on "Next batch". The grid
// NEVER rebuilds on its own, so the VIN you're working on stays put at the top.
// Load a page. advance=true marks the CURRENT VINs as served (never shown again) and
// fetches the next 20 fresh VINs; advance=false just (re)loads the current page.
async function loadPage(advance){
 reviewMode=false;
 let s;
 try{
  if(advance){ s=await (await fetch('/api/advance',{method:'POST',
     headers:{'Content-Type':'application/json'},body:JSON.stringify({vins:curVins})})).json(); }
  else { s=await (await fetch('/api/page')).json(); }
 }catch(e){ return; }
 const groups=s.page||[];
 curVins=groups.map(v=>v.vin);
 labelByKey={};
 const app=document.getElementById('app'); app.innerHTML=''; order=[]; active=0;
 if(!groups.length){
  app.innerHTML='<div id=empty>no fresh VINs right now'+(s.pulling?' — pulling more in the background, click “Next batch” again in a moment…':'.')+'</div>';
  return;
 }
 for(const v of groups){
  const done=v.photos.filter(p=>p.label).length;
  const h=document.createElement('div'); h.className='vin'; h.textContent=v.vin+'  ('+done+'/'+v.photos.length+')';
  app.appendChild(h);
  const g=document.createElement('div'); g.className='grid';
  for(const p of v.photos){
   const idx=order.length; order.push(p.key); labelByKey[p.key]=p.label;
   const card=document.createElement('div'); card.className='card'; card.id='c'+idx;
   const img=document.createElement('img'); img.loading='lazy'; img.src='/img?key='+encodeURIComponent(p.key);
   const lab=document.createElement('div'); lab.className='lab';
   const chips=document.createElement('div'); chips.className='chips';
   for(const cls of CHIP_ORDER){
    const b=document.createElement('b'); b.textContent=SHORT[cls]; b.title=cls;
    b.addEventListener('click',ev=>{ev.stopPropagation();toggle(idx,cls);});
    chips.appendChild(b);
   }
   card.appendChild(img); card.appendChild(lab); card.appendChild(chips);
   card.addEventListener('click',()=>{active=idx;renderActive();});
   g.appendChild(card); applyLabel(card,p.label);
  }
  app.appendChild(g);
 }
 renderActive(); window.scrollTo(0,0);
}

// Counts bar polls so you see progress + auto-pull status, but it NEVER touches the grid.
async function poll(){
 let s; try{ s=await (await fetch('/api/state')).json(); }catch(e){ return; }
 const pull=s.pulling?'<span style="color:#fc6">⏳ pulling…</span>':'';
 const fm=s.fresh_by_model||{};
 const mix=Object.keys(fm).map(m=>m+':'+fm[m]).join(' ');
 document.getElementById('counts').innerHTML=
   '<span>labeled '+s.labeled+' · fresh by model ['+mix+']</span>'+
   s.classes.map(k=>'<span>'+(SHORT[k]||k)+': '+s.counts[k]+'</span>').join('')+pull;
}
// ---- Smart review: photos where the trained model disagrees with your label ----
async function loadReview(){
 reviewMode=true; curVins=[];
 let s; try{ s=await (await fetch('/api/review')).json(); }catch(e){ return; }
 const app=document.getElementById('app'); app.innerHTML=''; order=[]; active=0; labelByKey={};
 if(s.error){ app.innerHTML='<div id=empty>'+s.error+'</div>'; return; }
 const items=s.items||[];
 if(!items.length){ app.innerHTML='<div id=empty>no disagreements to review 🎉  Click “Next batch” to keep labeling.</div>'; return; }
 const note=document.createElement('div'); note.className='vin';
 note.textContent='Smart review — '+s.total+' flagged (worst first). For each: pick the right label, or press g / “keep” if your label is correct.';
 app.appendChild(note);
 const g=document.createElement('div'); g.className='grid';
 for(const it of items){
  const idx=order.length; order.push(it.key); labelByKey[it.key]=it.label;
  const card=document.createElement('div'); card.className='card'; card.id='c'+idx;
  const img=document.createElement('img'); img.loading='lazy'; img.src='/img?key='+encodeURIComponent(it.key);
  const lab=document.createElement('div'); lab.className='lab';
  const banner=document.createElement('div'); banner.style.cssText='font-size:11px;padding:3px 6px;background:#222';
  banner.innerHTML='you: <b style="color:'+COLORS[it.label]+'">'+SHORT[it.label]+'</b>'+
    ' · model: <b style="color:'+(COLORS[it.pred]||'#aaa')+'">'+(SHORT[it.pred]||it.pred)+'</b> '+it.p_pred;
  const chips=document.createElement('div'); chips.className='chips';
  for(const cls of CHIP_ORDER){
   const b=document.createElement('b'); b.textContent=SHORT[cls]; b.title=cls;
   b.addEventListener('click',ev=>{ev.stopPropagation();toggle(idx,cls);});   // free toggle, no lock
   chips.appendChild(b);
  }
  const keep=document.createElement('b'); keep.textContent='keep ✓'; keep.style.background='#2d6cdf'; keep.style.flex='2';
  keep.addEventListener('click',ev=>{ev.stopPropagation();reviewKeep(idx);}); chips.appendChild(keep);
  card.appendChild(img); card.appendChild(banner); card.appendChild(lab); card.appendChild(chips);
  card.addEventListener('click',()=>{active=idx;renderActive();});
  g.appendChild(card); applyLabel(card,it.label);
 }
 app.appendChild(g); renderActive(); window.scrollTo(0,0);
}
// 'keep' = I'm done with this one (label is right / I've fixed it): resolve + advance.
// Plain corner clicks just toggle the label freely (no lock), so you can click around.
async function reviewKeep(idx){
 const key=order[idx]; if(key===undefined)return;
 try{await fetch('/api/review_resolve',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({key})});}catch(e){}
 const card=document.getElementById('c'+idx); if(card){card.style.opacity=0.3;}
 if(active<order.length-1){active++; renderActive();}
}

document.addEventListener('keydown',e=>{
 if(e.target.tagName==='INPUT')return;
 if(reviewMode){
  if(e.key in CLS){ toggle(active,CLS[e.key]); e.preventDefault(); }    // change freely, no lock/advance
  else if(e.key==='u'){ setLabelTo(active,null); e.preventDefault(); }  // untag
  else if(e.key==='g'){ reviewKeep(active); e.preventDefault(); }       // g = keep -> resolve + advance
  else if(e.key==='ArrowRight'||e.key==='n'){active=Math.min(order.length-1,active+1);renderActive();}
  else if(e.key==='ArrowLeft'||e.key==='b'){active=Math.max(0,active-1);renderActive();}
  return;
 }
 if(e.key in CLS){
  const wasSame = labelByKey[order[active]]===CLS[e.key];
  toggle(active,CLS[e.key]);
  if(!wasSame && active<order.length-1) active++;   // advance on set, stay on unmark
  renderActive(); e.preventDefault();
 } else if(e.key==='u'){ setLabelTo(active,null); e.preventDefault(); }
 else if(e.key==='ArrowRight'||e.key==='n'){active=Math.min(order.length-1,active+1);renderActive();}
 else if(e.key==='ArrowLeft'||e.key==='b'){active=Math.max(0,active-1);renderActive();}
});
loadPage(false); poll(); setInterval(poll,5000);
</script></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8095)
    a = ap.parse_args()
    os.makedirs(POOL, exist_ok=True)
    import uvicorn
    print(f"labeler -> http://localhost:{a.port}   (auto-pulling into {POOL})")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
