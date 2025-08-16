# bridge.py — GH → Moonraker bridge with tiny dashboard (patched)
import asyncio, json, time, os, re
from aiohttp import web, ClientSession
from aiohttp import web

HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("BRIDGE_PORT", "8090"))
MOONRAKER_HTTP = os.environ.get("MOONRAKER_HTTP", "http://your local printer") #replace this line with your printers actual local host!!! 
API_KEY = os.environ.get("MOONRAKER_API_KEY")  # optional
LINE_DELAY_S = float(os.environ.get("LINE_DELAY_S", "0.0"))

# -------- G-code parsing helpers --------
coord_re = re.compile(r'(?:[Gg]0?1)|(?:[Xx]-?\d)|(?:[Yy]-?\d)|(?:[Zz]-?\d)')
num = r'(-?\d+(?:\.\d+)?)'

def extract_xyz(line: str):
    mX = re.search(r'[Xx]'+num, line); mY = re.search(r'[Yy]'+num, line); mZ = re.search(r'[Zz]'+num, line)
    out = {}
    if mX: out['x'] = float(mX.group(1))
    if mY: out['y'] = float(mY.group(1))
    if mZ: out['z'] = float(mZ.group(1))
    return out

def build_points_from_lines(lines, seed_xyz):
    """Build mini-batch points from the submitted G-code lines.
    Only lines that mention any of X/Y/Z are counted as motion points.
    Accumulates absolute axes based on what's present per line.
    """
    pts = []
    cur = dict(seed_xyz)  # {'x':..,'y':..,'z':..}
    for ln in lines:
        if not isinstance(ln, str):
            continue
        ln = ln.strip()
        if not ln or ln.startswith(';'):
            continue
        if not coord_re.search(ln):
            continue
        upd = extract_xyz(ln)
        if upd:
            cur.update(upd)
            pts.append({'x': float(cur.get('x', 0.0)),
                        'y': float(cur.get('y', 0.0)),
                        'z': float(cur.get('z', 0.0))})
    return pts

# -------- Bridge state --------
class State:
    def __init__(self):
        self.q = asyncio.Queue()
        self.lines_sent = 0
        self.last_line = ""
        self.last_err = ""
        self.line_delay_s = LINE_DELAY_S
        # viewer state (active mini-batch only)
        self.active_points = []      # list[{x,y,z}] for current batch (coord-bearing lines only)
        self.active_total  = 0       # total coord-bearing lines in current batch
        self.done_count    = 0       # coord-bearing lines sent in current batch
        self.points_version = 0      # increment per batch for viewer refresh
        # NEW: full-line accounting (includes non-motion lines like G90, M114, comments filtered out upstream)
        self.active_lines_total = 0  # total lines enqueued for current batch
        self.active_lines_done  = 0  # total lines actually posted to Moonraker in current batch
        # optional TCP
        self.tcp = {"x":0.0,"y":0.0,"z":0.0}


STATE = State()

# -------- Dashboard (Three.js mini viewer) --------
DASH = """
<!doctype html>
<meta charset="utf-8">
<title>GH → Klipper Bridge — Minimal</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
:root{--bg:#0b0f17;--card:#101625;--ink:#e8eaf1;--muted:#9aa3b2;--accent:#7c9cff;--ok:#22c55e;--err:#ef4444;--line:#1b2436}
@media (prefers-color-scheme: light){:root{--bg:#ffffff;--card:#f8fafc;--ink:#10131a;--muted:#6b7280;--accent:#3b82f6;--ok:#16a34a;--err:#ef4444;--line:#e5e7eb}}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,var(--bg),#0d111a 60%);color:var(--ink);font:14px ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto}
.wrap{max-width:1120px;margin:24px auto;padding:0 18px}
.grid{display:grid;gap:16px;grid-template-columns:repeat(12,1fr)}
.card{grid-column:span 12;background:radial-gradient(1200px 300px at 0 -50px,rgba(124,156,255,.08),transparent),var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;box-shadow:0 12px 24px rgba(0,0,0,.12)}
@media(min-width:880px){.span8{grid-column:span 8}.span4{grid-column:span 4}}
.badge{display:inline-flex;align-items:center;gap:8px;padding:6px 10px;border-radius:999px;font-size:12px;font-weight:700;border:1px solid var(--line);background:rgba(255,255,255,.02)}
.badge.ok{color:var(--ok)}.badge.err{color:var(--err)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input,button,select{border:1px solid var(--line);border-radius:12px;background:#0000;color:var(--ink);padding:10px 12px;font-weight:600}
button{cursor:pointer;transition:transform .06s ease, background .2s ease}
button:hover{transform:translateY(-1px)}button:active{transform:translateY(0)}
.small{font-size:12px;color:var(--muted)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:700;letter-spacing:.2px}
.kpi{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:10px}
.kpi>div{background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:12px;padding:10px}
.progress{height:8px;background:#111827;border:1px solid var(--line);border-radius:999px;overflow:hidden}
.progress>i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#22d3ee)}
.stat{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center}
.stat b{font-weight:800}
#log{white-space:pre-wrap;font:12px ui-monospace;max-height:280px;overflow:auto;background:rgba(0,0,0,.15);padding:8px 10px;border-radius:10px;border:1px solid var(--line)}
.footer{display:flex;justify-content:space-between;align-items:center;margin-top:10px;color:var(--muted);font-size:12px}
hr{border:none;border-top:1px solid var(--line);opacity:.5;margin:10px 0}
</style>

<div class="wrap">
  <div class="row" style="justify-content:space-between;margin-bottom:8px">
    <h1 style="margin:0;font-size:20px;font-weight:800;letter-spacing:.2px">GH → Klipper Bridge <span id="badge" class="badge">—</span></h1>
    <div class="row small">
      <label for="base">Base URL</label>
      <input id="base" placeholder="http://localhost:8090" style="min-width:220px">
      <label for="dt">Interval (ms)</label>
      <input id="dt" type="number" value="500" min="100" style="width:110px">
      <label><input id="auto" type="checkbox" checked style="transform:translateY(2px);margin-right:6px">Auto</label>
      <button id="refresh">Refresh</button>
    </div>
  </div>

  <div class="grid">
    <div class="card span8">
      <div class="row" style="justify-content:space-between;margin-bottom:6px">
        <div><b>Status</b> <span class="small">updates from <b>/status</b></span></div>
        <div class="small">Last update: <b id="lastTs">—</b></div>
      </div>
      <div class="kpi">
        <div>
          <div class="small">Points progress</div>
          <div class="stat"><span class="small">Done</span><div class="progress" title="Points"><i id="pbar" style="width:0%"></i></div><b><span id="pDone">0</span>/<span id="pTotal">0</span></b></div>
        </div>
        <div>
          <div class="small">Lines progress</div>
          <div class="stat"><span class="small">Done</span><div class="progress" title="Lines"><i id="lbar" style="width:0%"></i></div><b><span id="lDone">0</span>/<span id="lTotal">0</span></b></div>
        </div>
        <div>
          <div class="small">Queue</div>
          <div class="stat"><span class="small">Size</span><div></div><b id="qsize">0</b></div>
        </div>
      </div>
      <hr>
      <div class="row small">
        <div>TCP: <span class="mono" id="tcp">—</span></div>
        <div style="opacity:.6">•</div>
        <div>Last line: <span id="lline">—</span></div>
      </div>
    </div>

    <div class="card span4">
      <div style="display:grid;gap:10px">
        <div class="small">Controls</div>
        <div class="row">
          <input id="gcode" placeholder="G1 X10 Y10 F600" style="flex:1;min-width:220px">
          <button id="send">Send</button>
        </div>
        <div class="row">
          <input id="delayIn" type="number" min="0" value="0" style="width:120px" title="Delay in ms">
          <button id="setDelay">Set delay</button>
          <button id="clear">Clear</button>
          <button id="estop" style="background:#ff000015;color:#ff6161">E‑STOP</button>
        </div>
        <div class="small">Event Log</div>
        <div id="log"></div>
      </div>
    </div>
  </div>

  <div class="footer">
    <div>Badge: <span class="small">READY/ERR based on <b>/status.ok</b></span></div>
    <div class="small">Keyboard: <b>Enter</b> send, <b>Ctrl/⌘+Enter</b> repeat last</div>
  </div>
</div>

<script>
const $=s=>document.querySelector(s), logEl=$('#log');
let lastSent = '';
function log(m,c=''){const d=document.createElement('div');d.textContent='['+new Date().toLocaleTimeString()+'] '+m; if(c) d.style.color=c; logEl.appendChild(d); logEl.scrollTop=logEl.scrollHeight}
function base(){let b=$('#base').value.trim(); if(!b) b=location.origin; if(!/^https?:\/\//i.test(b)) b='http://'+b; return b.replace(/\/$/,'');}
async function get(path){const url=base()+path; const r=await fetch(url,{cache:'no-store'}); if(!r.ok) throw new Error(path+' → '+r.status); return r.json();}
async function post(path,body={}){const url=base()+path; const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let j=null; try{j=await r.json()}catch{j={raw:await r.text()}}; log(path+' → '+JSON.stringify(j), r.ok?'#22c55e':'#ef4444'); if(!r.ok) throw new Error(j && j.error || (path+' failed')); return j;}

let pollTimer=null, inflight=false;
function setBadge(ok){const b=$('#badge'); b.textContent=ok? 'READY':'ERR'; b.className='badge '+(ok?'ok':'err');}
function setProgress(done,total,barId,doneId,totalId){done=+done||0; total=+total||0; const pct= total? Math.min(100, Math.round(done*100/total)) : 0; $(barId).style.width=pct+'%'; $(doneId).textContent=done; $(totalId).textContent=total;}
function setTCP(t){ if(!t){$('#tcp').textContent='—'; return;} const x=(t.x??0).toFixed(2), y=(t.y??0).toFixed(2), z=(t.z??0).toFixed(2); $('#tcp').textContent=`${x}, ${y}, ${z}`; }

async function poll(){ if(inflight) return; inflight=true; try{
  const s = await get('/status');
  setBadge(!!s.ok);
  $('#qsize').textContent = s.queue_size ?? '0';
  $('#lline').textContent = s.last_line || '—';
  $('#lastTs').textContent = new Date().toLocaleTimeString();
  $('#delayIn').value = Math.round((s.line_delay_s||0)*1000);
  setProgress(s.points_done, s.points_total, '#pbar', '#pDone', '#pTotal');
  setProgress(s.lines_done, s.lines_total, '#lbar', '#lDone', '#lTotal');
  setTCP(s.tcp);
} catch(e){ setBadge(false); log('poll failed: '+e.message,'#ef4444'); }
 finally{ inflight=false; }
}

function start(){ const ms=Math.max(100, parseInt($('#dt').value||'500',10)); if(pollTimer) clearInterval(pollTimer); pollTimer=setInterval(poll, ms); }
function stop(){ if(pollTimer) { clearInterval(pollTimer); pollTimer=null; } }

// Controls
$('#refresh').addEventListener('click', poll);
$('#auto').addEventListener('change', e=> e.target.checked ? start() : stop());
$('#dt').addEventListener('change', ()=>{ if($('#auto').checked) start(); });

$('#send').addEventListener('click', async ()=>{ const t=$('#gcode').value.trim(); if(!t) return; lastSent=t; await post('/script',{gcode:t}); $('#gcode').value=''; });
$('#gcode').addEventListener('keydown', async e=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); $('#send').click(); } if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){ e.preventDefault(); if(lastSent) await post('/script',{gcode:lastSent}); }});
$('#setDelay').addEventListener('click', async ()=>{ const ms=parseInt($('#delayIn').value||'0',10); await post('/delay',{ms}); });
$('#clear').addEventListener('click', async ()=>{ await post('/clear'); });
$('#estop').addEventListener('click', async ()=>{ if(confirm('Confirm EMERGENCY STOP?')) await post('/estop'); });

// Boot
$('#base').value = location.origin;
$('#auto').checked = true; start(); poll();
</script>
"""

# -------- Moonraker POST helper --------
async def moonraker_post(session, endpoint, payload):
    url = f"{MOONRAKER_HTTP}{endpoint}"
    headers = {"X-Api-Key": API_KEY} if API_KEY else None
    async with session.post(url, json=payload, headers=headers) as resp:
        txt = await resp.text()
        if resp.status >= 400:
            raise web.HTTPBadRequest(text=f"Moonraker {resp.status}: {txt}")
        try:
            return json.loads(txt)
        except:
            return {"raw": txt}

# -------- Background worker --------
async def worker(app):
    async with ClientSession() as session:
        while True:
            line = await STATE.q.get()
            STATE.last_line = line
            try:
                # Moonraker script endpoint
                await moonraker_post(session, "/printer/gcode/script", {"script": line})
                STATE.lines_sent += 1

                # count full lines
                if STATE.active_lines_total > 0:
                    STATE.active_lines_done = min(STATE.active_lines_total, STATE.active_lines_done + 1)

                # count coord-bearing “points”
                if coord_re.search(line) and STATE.active_total > 0:
                    STATE.done_count = min(STATE.active_total, STATE.done_count + 1)

                await asyncio.sleep(STATE.line_delay_s)
            except Exception as e:
                STATE.last_err = str(e)
            finally:
                STATE.q.task_done()


# -------- HTTP handlers --------
async def dashboard(request):
    return web.Response(text=DASH, content_type="text/html")

async def ping(request):
    return web.json_response({"ok": True, "ts": time.time()})

async def status(request):
    body = {
        "ok": True,
        "queue_size": STATE.q.qsize(),
        "lines_sent": STATE.lines_sent,
        "last_line": STATE.last_line,
        "last_error": STATE.last_err,
        "line_delay_s": STATE.line_delay_s,

        # per-batch: points (coord-bearing)
        "points_done": STATE.done_count,
        "points_total": STATE.active_total,
        "points_remaining": max(0, STATE.active_total - STATE.done_count),

        # per-batch: lines (all)
        "lines_done": STATE.active_lines_done,
        "lines_total": STATE.active_lines_total,
        "lines_remaining": max(0, STATE.active_lines_total - STATE.active_lines_done),

        # viewer
        "points_version": STATE.points_version,
        "tcp": STATE.tcp,
    }
    return web.json_response(body, headers={"Cache-Control": "no-store"})


async def get_points(request):
    return web.json_response({"ok": True, "points": STATE.active_points})

async def script(request):
    g = (await request.json()).get("gcode", "").strip()
    if not g:
        return web.json_response({"ok": False, "error": "missing gcode"}, status=400)
    await STATE.q.put(g)
    # optional: single-line mini-batch if line has coords
    try:
        seed = STATE.tcp if isinstance(STATE.tcp, dict) else {"x":0.0,"y":0.0,"z":0.0}
        pts = build_points_from_lines([g], seed)
        if pts:
            STATE.active_points = pts
            STATE.active_total  = len(pts)
            STATE.done_count    = 0
            STATE.points_version += 1
    except Exception:
        pass
    return web.json_response({"ok": True, "queued": 1, "queue_size": STATE.q.qsize()})

async def send_lines(request):
    p = await request.json()
    lines = p.get("lines") or []
    delay = p.get("delay_s")

    if not isinstance(lines, list):
        return web.json_response({"ok": False, "error": "'lines' must be list"}, status=400)
    if isinstance(delay, (int, float)) and delay >= 0:
        STATE.line_delay_s = float(delay)

    # enqueue all lines
    cleaned = []
    for ln in lines:
        ln = ("" if ln is None else str(ln)).strip()
        if ln:
            cleaned.append(ln)
            await STATE.q.put(ln)

    # define the new mini-batch for accounting + viewer
    try:
        seed = STATE.tcp if isinstance(STATE.tcp, dict) else {"x":0.0,"y":0.0,"z":0.0}
        pts = build_points_from_lines(cleaned, seed)

        # lines (all) accounting
        STATE.active_lines_total = len(cleaned)
        STATE.active_lines_done  = 0

        # points (coord-bearing lines) accounting
        STATE.active_points = pts
        STATE.active_total  = len(pts)
        STATE.done_count    = 0

        # force viewer refresh
        STATE.points_version += 1
    except Exception:
        pass

    return web.json_response({
        "ok": True,
        "queued": len(cleaned),
        "queue_size": STATE.q.qsize(),
        "delay_s": STATE.line_delay_s
    })


async def clear_queue(request):
    cleared = 0
    try:
        while True:
            STATE.q.get_nowait(); STATE.q.task_done(); cleared += 1
    except asyncio.QueueEmpty:
        pass
    return web.json_response({"ok": True, "cleared": cleared, "queue_size": STATE.q.qsize()})

async def set_delay(request):
    ms = (await request.json()).get("ms")
    try:
        STATE.line_delay_s = max(0.0, float(ms)/1000.0)
    except Exception:
        return web.json_response({"ok": False, "error": "ms must be number"}, status=400)
    return web.json_response({"ok": True, "line_delay_s": STATE.line_delay_s})

async def estop(request):
    async with ClientSession() as s:
        try:
            await moonraker_post(s, "/printer/gcode/script", {"script": "M112"})
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        
# -------- App boot --------
app = web.Application()
app.router.add_get("/", dashboard)
app.router.add_get("/status", status)
app.router.add_get("/ping", ping)
app.router.add_get("/points", get_points)
app.router.add_post("/script", script)
app.router.add_post("/send", send_lines)
app.router.add_post("/clear", clear_queue)
app.router.add_post("/delay", set_delay)
app.router.add_post("/estop", estop)


async def on_startup(app):
    app["worker"] = asyncio.create_task(worker(app))

async def on_cleanup(app):
    t = app.get("worker")
    if t:
        t.cancel()
        try:
            await t
        except Exception:
            pass

app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host=HOST, port=PORT)
