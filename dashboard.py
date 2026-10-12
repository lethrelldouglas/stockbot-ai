"""ForexBot dashboard + intraday auto-run scheduler.

    python dashboard.py        -> http://localhost:5250

Shows account, open positions, P&L, Claude's latest analysis, and the trade
log. A background thread runs one bot cycle every config.POLL_MINUTES while the
FX market is open and this dashboard stays running. Big kill switch included.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, request

import config
import risk

app = Flask(__name__)

_run_lock = threading.Lock()
_last_manual_result: dict | None = None


def _run_bot(force: bool = False) -> dict:
    global _last_manual_result
    if not _run_lock.acquire(blocking=False):
        return {"skipped": "a cycle is already running"}
    try:
        import bot
        result = bot.run_cycle(force=force)
        _last_manual_result = result
        return result
    finally:
        _run_lock.release()


def _minutes_since_last_run() -> float:
    ts = risk.last_run()
    if not ts:
        return 1e9
    try:
        then = datetime.fromisoformat(ts)
        return (datetime.now(timezone.utc) - then).total_seconds() / 60.0
    except ValueError:
        return 1e9


def _scheduler() -> None:
    import broker
    while True:
        try:
            due = (
                not risk.kill_switch_on()
                and config.ready()["oanda"]
                and config.ready()["anthropic"]
                and _minutes_since_last_run() >= config.POLL_MINUTES
                and broker.market_open()
            )
            if due:
                threading.Thread(target=_run_bot, daemon=True).start()
        except Exception:
            pass
        time.sleep(60)


@app.get("/api/state")
def api_state():
    creds = config.ready()
    state = {
        "creds": creds,
        "live": config.LIVE,
        "kill_switch": risk.kill_switch_on(),
        "schedule": f"every {config.POLL_MINUTES} min while FX open",
        "last_run": risk.last_run(),
        "account": None,
        "positions": [],
        "orders": [],
        "analysis": None,
        "trades": [],
        "market_open": None,
        "error": None,
    }
    if config.ANALYSIS_FILE.exists():
        state["analysis"] = json.loads(config.ANALYSIS_FILE.read_text(encoding="utf-8"))
    if config.TRADE_LOG.exists():
        import csv
        with open(config.TRADE_LOG, newline="", encoding="utf-8") as f:
            state["trades"] = list(csv.DictReader(f))[-20:][::-1]
    if creds["oanda"]:
        try:
            import broker
            state["account"] = broker.get_account()
            positions = broker.get_positions()
            insts = [p["instrument"] for p in positions] or config.PAIRS[:1]
            prices = broker.pricing(insts)
            for p in positions:
                p["current_price"] = prices.get(p["instrument"], {}).get("mid")
            state["positions"] = positions
            state["orders"] = broker.get_recent_orders(limit=15)
            state["market_open"] = broker.market_open()
            day_start = risk.record_day_start(state["account"]["equity"])
            state["day_start_equity"] = day_start
            state["day_pl"] = state["account"]["equity"] - day_start
        except Exception as e:
            state["error"] = str(e)
    return jsonify(state)


@app.post("/api/run")
def api_run():
    force = bool(request.json and request.json.get("force"))
    result = _run_bot(force=force)
    return jsonify(result)


@app.post("/api/kill")
def api_kill():
    on = bool(request.json and request.json.get("on"))
    risk.set_kill_switch(on)
    return jsonify({"kill_switch": risk.kill_switch_on()})


@app.post("/api/close_all")
def api_close_all():
    import broker
    closed = broker.close_all()
    return jsonify({"ok": True, "closed": closed})


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>ResolvOps ForexBot</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e;
--green:#3fb950;--red:#f85149;--amber:#d29922;--accent:#58a6ff}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--text);font:14px/1.5 'Segoe UI',system-ui,sans-serif;padding:24px;max-width:1100px;margin:0 auto}
h1{font-size:20px;margin-bottom:4px}
.sub{color:var(--dim);margin-bottom:20px;font-size:13px}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600;margin-left:8px}
.badge.practice{background:#1f3d2b;color:var(--green)}
.badge.live{background:#4d1a1a;color:var(--red)}
.badge.killed{background:#4d1a1a;color:var(--red)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px}
.card .label{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.5px}
.card .value{font-size:22px;font-weight:700;margin-top:2px}
.pos{color:var(--green)}.neg{color:var(--red)}
section{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:16px}
section h2{font-size:14px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--border)}
td{padding:6px 8px;border-bottom:1px solid #21262d}
.btn{background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer;margin-right:8px}
.btn:hover{border-color:var(--accent)}
.btn.danger{background:#3d1518;border-color:#6e2b30;color:var(--red)}
.btn.go{background:#1f3d2b;border-color:#2e5e40;color:var(--green)}
.outlook{background:#0d1a2d;border:1px solid #1f4068;border-radius:6px;padding:12px;font-size:13px;margin-bottom:10px}
.setup{background:#2d2008;border:1px solid #5e4a1a;border-radius:8px;padding:16px;margin-bottom:16px}
.setup code{background:#161b22;padding:2px 6px;border-radius:4px;font-size:12px}
.muted{color:var(--dim)}
#toast{position:fixed;bottom:20px;right:20px;background:#1f3d2b;border:1px solid #2e5e40;padding:10px 18px;border-radius:8px;display:none}
</style></head><body>
<h1>ResolvOps ForexBot <span id="mode" class="badge practice">PRACTICE</span><span id="killbadge"></span></h1>
<div class="sub">AI intraday FX day-trading &middot; auto-run <span id="sched"></span> &middot; every entry carries a server-side stop-loss &amp; take-profit</div>

<div id="setup" class="setup" style="display:none"></div>

<div class="grid">
  <div class="card"><div class="label">Equity (NAV)</div><div class="value" id="equity">&mdash;</div></div>
  <div class="card"><div class="label">Day P&amp;L</div><div class="value" id="daypl">&mdash;</div></div>
  <div class="card"><div class="label">Margin avail.</div><div class="value" id="cash">&mdash;</div></div>
  <div class="card"><div class="label">Positions</div><div class="value" id="npos">&mdash;</div></div>
  <div class="card"><div class="label">FX Market</div><div class="value" id="mkt">&mdash;</div></div>
</div>

<section>
  <h2>Controls</h2>
  <button class="btn go" onclick="runNow(false)">Run analysis now</button>
  <button class="btn" onclick="runNow(true)" title="Runs even if the market shows closed">Force run (testing)</button>
  <button class="btn danger" id="killbtn" onclick="toggleKill()">Engage kill switch</button>
  <button class="btn danger" onclick="closeAll()">Close ALL positions</button>
  <span id="runstatus" class="muted"></span>
</section>

<section>
  <h2>Claude's latest analysis</h2>
  <div id="outlook" class="outlook muted">No analysis yet &mdash; hit "Run analysis now" while FX is open.</div>
  <table id="decisions" style="display:none"><thead><tr><th>Pair</th><th>Action</th><th>Conf.</th><th>Stop</th><th>Target</th><th>Rationale</th></tr></thead><tbody></tbody></table>
</section>

<section>
  <h2>Open positions</h2>
  <table><thead><tr><th>Pair</th><th>Side</th><th>Units</th><th>Entry</th><th>Now</th><th>Unrealized P&amp;L</th></tr></thead><tbody id="positions"><tr><td colspan="6" class="muted">&mdash;</td></tr></tbody></table>
</section>

<section>
  <h2>Recent trades (broker)</h2>
  <table><thead><tr><th>Time</th><th>Pair</th><th>Side</th><th>Units</th><th>Fill</th><th>State</th><th>Realized</th></tr></thead><tbody id="orders"><tr><td colspan="7" class="muted">&mdash;</td></tr></tbody></table>
</section>

<section>
  <h2>Bot trade log</h2>
  <table><thead><tr><th>Time</th><th>Pair</th><th>Action</th><th>Units</th><th>Price</th><th>Stop(p)</th><th>Target(p)</th><th>Why</th></tr></thead><tbody id="trades"><tr><td colspan="8" class="muted">&mdash;</td></tr></tbody></table>
</section>

<div id="toast"></div>
<script>
const $=id=>document.getElementById(id);
const money=v=>v==null?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:2});
const plspan=v=>{const s=(v>=0?'+':'')+money(v);return `<span class="${v>=0?'pos':'neg'}">${s}</span>`};
function toast(m){const t=$('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',4000)}

async function refresh(){
  const s=await (await fetch('/api/state')).json();
  $('mode').textContent=s.live?'LIVE':'PRACTICE';
  $('mode').className='badge '+(s.live?'live':'practice');
  $('killbadge').innerHTML=s.kill_switch?'<span class="badge killed">KILL SWITCH ON</span>':'';
  $('killbtn').textContent=s.kill_switch?'Release kill switch':'Engage kill switch';
  $('sched').textContent=s.schedule+(s.last_run?' (last run '+s.last_run.slice(11,16)+' UTC)':'');
  const setup=$('setup');
  if(!s.creds.oanda||!s.creds.anthropic){
    setup.style.display='block';
    setup.innerHTML='<b>Setup needed:</b> '+(!s.creds.oanda?'Add your OANDA API token and account id to <code>stockbot/.env</code> (OANDA_API_TOKEN / OANDA_ACCOUNT_ID) — from <a href="https://www.oanda.com" target="_blank" style="color:var(--accent)">OANDA</a> &rarr; Manage API Access. ':'')+(!s.creds.anthropic?'Add ANTHROPIC_API_KEY to <code>stockbot/.env</code>. ':'')+'Restart the dashboard after.';
  } else setup.style.display='none';
  if(s.account){
    $('equity').textContent=money(s.account.equity);
    $('cash').textContent=money(s.account.cash);
    $('daypl').innerHTML=s.day_pl!=null?plspan(s.day_pl):'—';
    $('npos').textContent=s.positions.length;
    $('mkt').textContent=s.market_open?'OPEN':'closed';
    $('mkt').className='value '+(s.market_open?'pos':'muted');
  }
  if(s.error) toast('Broker error: '+s.error);
  $('positions').innerHTML=s.positions.length?s.positions.map(p=>
    `<tr><td><b>${p.instrument}</b></td><td class="${p.side=='long'?'pos':'neg'}">${p.side}</td><td>${Math.abs(p.units)}</td><td>${p.avg_entry.toFixed(5)}</td><td>${p.current_price!=null?p.current_price.toFixed(5):'—'}</td><td>${plspan(p.unrealized_pl)}</td></tr>`
  ).join(''):'<tr><td colspan="6" class="muted">No open positions</td></tr>';
  $('orders').innerHTML=s.orders.length?s.orders.map(o=>
    `<tr><td>${(o.time||'').slice(0,16).replace('T',' ')}</td><td><b>${o.instrument}</b></td><td>${o.side}</td><td>${o.units}</td><td>${o.fill?o.fill.toFixed(5):''}</td><td>${o.state}</td><td>${plspan(o.realized_pl)}</td></tr>`
  ).join(''):'<tr><td colspan="7" class="muted">—</td></tr>';
  $('trades').innerHTML=s.trades.length?s.trades.map(t=>
    `<tr><td>${(t.timestamp||'').slice(0,16).replace('T',' ')}</td><td><b>${t.instrument}</b></td><td>${t.action}</td><td>${t.units}</td><td>${t.price||''}</td><td>${t.stop_pips||''}</td><td>${t.target_pips||''}</td><td class="muted">${t.rationale||''}</td></tr>`
  ).join(''):'<tr><td colspan="8" class="muted">No bot trades yet</td></tr>';
  if(s.analysis){
    $('outlook').className='outlook';
    $('outlook').innerHTML='<b>'+ (s.analysis.timestamp||'').slice(0,16).replace('T',' ') +' UTC</b> — '+s.analysis.market_outlook;
    const d=s.analysis.decisions||[];
    $('decisions').style.display=d.length?'table':'none';
    $('decisions').querySelector('tbody').innerHTML=d.map(x=>
      `<tr><td><b>${x.pair}</b></td><td>${x.action}</td><td>${(x.confidence*100).toFixed(0)}%</td><td>${x.stop_pips||'—'}</td><td>${x.target_pips||'—'}</td><td class="muted">${x.rationale}</td></tr>`).join('');
  }
}
async function runNow(force){
  $('runstatus').textContent='Running… (Claude is analyzing, ~30-60s)';
  try{
    const r=await (await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({force})})).json();
    $('runstatus').textContent='';
    toast(r.skipped?('Skipped: '+r.skipped):`Done: ${r.orders.length} entries, ${r.closes.length} closes`);
  }catch(e){$('runstatus').textContent='';toast('Run failed: '+e)}
  refresh();
}
async function toggleKill(){
  const s=await (await fetch('/api/state')).json();
  await fetch('/api/kill',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:!s.kill_switch})});
  refresh();
}
async function closeAll(){
  if(!confirm('Close ALL open positions?'))return;
  await fetch('/api/close_all',{method:'POST'});
  toast('Closing all positions…');refresh();
}
refresh();setInterval(refresh,30000);
</script></body></html>"""


@app.get("/")
def index():
    return PAGE


if __name__ == "__main__":
    config.assert_safe()
    threading.Thread(target=_scheduler, daemon=True).start()
    print(f"ForexBot dashboard -> http://localhost:{config.DASHBOARD_PORT}"
          f"  ({'LIVE' if config.LIVE else 'PRACTICE'} mode)")
    app.run(port=config.DASHBOARD_PORT, debug=False)
