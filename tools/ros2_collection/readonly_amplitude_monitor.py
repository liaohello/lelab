#!/usr/bin/env python3
"""Serve a read-only LanderPi amplitude monitor backed by the upper API."""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LanderPi 只读幅值观察</title>
<style>
:root{color-scheme:dark;--bg:#080d14;--panel:#111a26;--line:#233246;--text:#e6eef8;
--muted:#8fa2b8;--ok:#34d399;--warn:#fbbf24;--bad:#fb7185;--accent:#38bdf8}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0%,#10253a 0,
transparent 36%),var(--bg);color:var(--text);font:14px/1.45 ui-sans-serif,system-ui,sans-serif}
main{max-width:1320px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;
align-items:flex-start;gap:20px;margin-bottom:18px}.title{font-size:25px;font-weight:750;
letter-spacing:.02em}.sub{color:var(--muted);margin-top:5px}.badge{border:1px solid var(--line);
background:var(--panel);border-radius:999px;padding:7px 12px;white-space:nowrap;font-weight:700}
.safe{color:var(--ok);border-color:#17634d}.danger{color:var(--bad);border-color:#7f2436}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.card{background:
linear-gradient(145deg,#131e2c,#0e1621);border:1px solid var(--line);border-radius:16px;
padding:17px;box-shadow:0 10px 30px #0004}.jointhead{display:flex;justify-content:space-between;
gap:12px;align-items:baseline}.jointname{font-weight:800;font-size:17px}.raw{font:750 29px/1.1
ui-monospace,SFMono-Regular,Menlo,monospace}.metrics{display:grid;grid-template-columns:repeat(3,1fr);
gap:8px;margin:14px 0}.metric{background:#0a121d;border:1px solid #1e2c3d;border-radius:10px;
padding:8px}.label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.value{font:650 14px ui-monospace,SFMono-Regular,Menlo,monospace;margin-top:3px}.track{position:relative;
height:16px;background:#08101a;border:1px solid #26384d;border-radius:99px;overflow:hidden}
.fill{position:absolute;inset:0 auto 0 0;border-radius:99px;opacity:.72;transition:width .16s linear}
.target{position:absolute;top:-4px;bottom:-4px;width:2px;background:#f8fafc;box-shadow:0 0 0 2px #0008}
.center{position:absolute;top:0;bottom:0;width:1px;background:#7c8ca1}.scale{display:flex;
justify-content:space-between;color:var(--muted);font:11px ui-monospace,monospace;margin-top:5px}
.footer{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:16px}.footer .metric{padding:11px}
.error{display:none;margin:12px 0;padding:12px 14px;border:1px solid #7f2436;background:#36121b;
border-radius:12px;color:#fecdd3}.notice{margin:12px 0 18px;padding:11px 14px;border:1px solid #1f5364;
background:#0a2530;border-radius:12px;color:#bae6fd}canvas{width:100%;height:54px;margin-top:11px;
background:#07101a;border:1px solid #1b2a3b;border-radius:9px}@media(max-width:900px){.grid{grid-template-columns:1fr 1fr}}
@media(max-width:620px){main{padding:14px}.grid{grid-template-columns:1fr}.footer{grid-template-columns:1fr 1fr}
.top{display:block}.badge{display:inline-block;margin-top:10px}}
</style>
</head>
<body><main>
<div class="top"><div><div class="title">LanderPi 只读幅值观察</div>
<div class="sub">当前真实舵机 raw、相对安全姿态偏差、近似关节角与最近 80 帧范围</div></div>
<div id="state" class="badge">读取中</div></div>
<div class="notice">只读页面：不会启动力矩、不会发送位置目标、不能清除急停。页面数据刷新周期约 250 ms。</div>
<div id="error" class="error"></div>
<div id="grid" class="grid"></div>
<div class="footer">
<div class="metric"><div class="label">控制状态</div><div id="control" class="value">—</div></div>
<div class="metric"><div class="label">执行输出</div><div id="outputs" class="value">—</div></div>
<div class="metric"><div class="label">反馈年龄</div><div id="age" class="value">—</div></div>
<div class="metric"><div class="label">读回序号</div><div id="seq" class="value">—</div></div>
</div></main>
<script>
const names=['joint1','joint2','joint3','joint4','joint5','r_joint'];
const labels={joint1:'Joint 1 · 底座',joint2:'Joint 2 · 肩',joint3:'Joint 3 · 肘',
joint4:'Joint 4 · 腕弯',joint5:'Joint 5 · 腕转',r_joint:'Gripper · 夹爪 ID10'};
const colors={joint1:'#38bdf8',joint2:'#34d399',joint3:'#a78bfa',joint4:'#fbbf24',
joint5:'#fb7185',r_joint:'#22d3ee'};
const history=Object.fromEntries(names.map(n=>[n,[]]));
const SAFE_POSE={joint1:531,joint2:492,joint3:525,joint4:502,joint5:510,r_joint:457};
const maxHistory=80;
const grid=document.getElementById('grid');
for(const n of names){
  const el=document.createElement('section'); el.className='card'; el.id='card-'+n;
  el.innerHTML=`<div class="jointhead"><div class="jointname">${labels[n]}</div>
  <div class="raw" id="raw-${n}">—</div></div>
  <div class="metrics"><div class="metric"><div class="label">安全姿态</div><div class="value" id="target-${n}">—</div></div>
  <div class="metric"><div class="label">距安全姿态</div><div class="value" id="delta-${n}">—</div></div>
  <div class="metric"><div class="label">近似角</div><div class="value" id="angle-${n}">—</div></div></div>
  <div class="track"><div class="center" style="left:50%"></div><div class="fill" id="fill-${n}"
  style="background:${colors[n]};width:0%"></div><div class="target" id="mark-${n}" style="left:0%"></div></div>
  <div class="scale"><span>0</span><span>raw 500</span><span>1000</span></div>
  <div class="metrics"><div class="metric"><div class="label">近期范围</div><div class="value"
  id="range-${n}">—</div></div><div class="metric"><div class="label">峰峰值</div><div class="value"
  id="pp-${n}">—</div></div><div class="metric"><div class="label">方向</div><div class="value"
  id="trend-${n}">—</div></div></div><canvas id="chart-${n}" width="360" height="54"></canvas>`;
  grid.appendChild(el);
}
function n(v,d=1){return Number.isFinite(v)?v.toFixed(d):'—'}
function angleText(rad,n){
  if(!Number.isFinite(rad)) return '—';
  if(n==='r_joint') return '归一化';
  return `${(rad*180/Math.PI).toFixed(1)}°`;
}
function draw(n){
  const c=document.getElementById('chart-'+n),ctx=c.getContext('2d');
  const vals=history[n]; ctx.clearRect(0,0,c.width,c.height);
  if(vals.length<2)return;
  const lo=Math.min(...vals),hi=Math.max(...vals),span=Math.max(1,hi-lo);
  ctx.strokeStyle=colors[n];ctx.lineWidth=2;ctx.beginPath();
  vals.forEach((v,i)=>{const x=i*(c.width-2)/Math.max(1,maxHistory-1)+1;
    const y=c.height-4-((v-lo)/span)*(c.height-8);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
  ctx.stroke();
}
async function update(){
  try{
    const r=await fetch('/api/status',{cache:'no-store'});
    if(!r.ok)throw new Error(`HTTP ${r.status}`);
    const d=await r.json(),f=d.feedback||{},raw=f.servo_pulses||{},rad=f.positions_rad||{};
    const targets=SAFE_POSE;
    document.getElementById('state').textContent=d.state||'unknown';
    document.getElementById('state').className='badge '+(d.outputs_enabled?'danger':'safe');
    document.getElementById('control').textContent=d.state||'—';
    document.getElementById('outputs').textContent=d.outputs_enabled?'ENABLED':'OFF';
    document.getElementById('age').textContent=Number.isFinite(f.oldest_age_s)
      ?`${(f.oldest_age_s*1000).toFixed(0)} ms`:'—';
    document.getElementById('seq').textContent=f.scan_sequence??'—';
    document.getElementById('error').style.display='none';
    for(const key of names){
      const v=Number(raw[key]),t=Number(targets[key]);
      if(Number.isFinite(v)){history[key].push(v);if(history[key].length>maxHistory)history[key].shift();}
      const vals=history[key],lo=vals.length?Math.min(...vals):NaN,hi=vals.length?Math.max(...vals):NaN;
      document.getElementById('raw-'+key).textContent=Number.isFinite(v)?String(Math.round(v)):'—';
      document.getElementById('target-'+key).textContent=Number.isFinite(t)?String(t):'—';
      document.getElementById('delta-'+key).textContent=Number.isFinite(v)&&Number.isFinite(t)
        ?`${Math.round(v-t)>0?'+':''}${Math.round(v-t)}`:'—';
      document.getElementById('angle-'+key).textContent=angleText(rad[key],key);
      document.getElementById('fill-'+key).style.width=Number.isFinite(v)
        ?`${Math.max(0,Math.min(100,v/10))}%`:'0%';
      document.getElementById('mark-'+key).style.left=Number.isFinite(t)
        ?`${Math.max(0,Math.min(100,t/10))}%`:'0%';
      document.getElementById('range-'+key).textContent=vals.length?`${lo}–${hi}`:'—';
      document.getElementById('pp-'+key).textContent=vals.length?`${hi-lo} tick`:'—';
      const trend=vals.length>=2?vals.at(-1)-vals[0]:0;
      document.getElementById('trend-'+key).textContent=trend>0?`↑ +${trend}`:trend<0?`↓ ${trend}`:'稳定';
      draw(key);
    }
  }catch(e){
    const el=document.getElementById('error');el.textContent=`读取失败：${e.message}`;el.style.display='block';
    document.getElementById('state').textContent='连接中断';
    document.getElementById('state').className='badge danger';
  }
}
update();setInterval(update,250);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    upstream = ""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, HTML.encode(), "text/html; charset=utf-8")
            return
        if self.path == "/healthz":
            self._send(200, b'{"ok":true}\n', "application/json")
            return
        if self.path == "/api/status":
            try:
                with urllib.request.urlopen(self.upstream, timeout=3.0) as response:
                    body = response.read()
                self._send(200, body, "application/json")
            except urllib.error.HTTPError as exc:
                self._send(exc.code, exc.read(), "application/json")
            except Exception as exc:  # noqa: BLE001
                self._send(503, json.dumps({"error": f"{type(exc).__name__}:{exc}"}).encode(),
                           "application/json")
            return
        self._send(404, b'{"error":"not found"}\n', "application/json")

    def do_POST(self) -> None:  # noqa: N802
        self._send(405, b'{"error":"read_only_monitor"}\n', "application/json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18300)
    parser.add_argument("--upstream",
                        default="http://127.0.0.1:18287/ros-mobile/hybrid/status")
    args = parser.parse_args()
    Handler.upstream = args.upstream
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"READ_ONLY_AMPLITUDE http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
