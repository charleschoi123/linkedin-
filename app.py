# app.py
# -*- coding: utf-8 -*-
"""
Alsos Talent · 合规AI寻访MVP  (完整修订版)
- 仅提取 Email，去除电话解析逻辑
- 支持 A+ / A / B / C 等级，附带数值分
- 上传/解压/排队阶段都有流式输出
- 并发默认 2（可通过 UI 或环境变量 CONCURRENCY 覆盖）
- 任务命名：<职位>_<方向>_<YYYYMMDD_HHMMSS>
- 生成 Excel 与 HTML 榜单，SSE 推送操作按钮
"""

import os, io, re, json, uuid, zipfile, time, hashlib, logging, csv
from datetime import datetime
from typing import List, Dict, Any, Optional
from queue import Queue
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import (
    Flask, request, render_template_string, send_file, redirect, url_for, Response
)
import requests

# ----------------- 可选解析器 -----------------
try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except Exception:
    pdf_extract_text = None

try:
    from docx import Document as DocxDocument
except Exception:
    DocxDocument = None

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None


# ----------------- 基本配置 -----------------
app = Flask(__name__)

# 上传大小限制（默认 200MB，可通过环境变量增大）
app.config['MAX_CONTENT_LENGTH'] = int(os.getenv('MAX_UPLOAD_MB', '200')) * 1024 * 1024
CHUNK_SIZE = 1024 * 1024  # 1MB 分块写

# 模型默认配置（可通过页面或环境变量覆盖到每个任务）
DEFAULT_MODEL_NAME = os.getenv("MODEL_NAME", "deepseek-chat")
DEFAULT_MODEL_BASE = os.getenv("MODEL_BASE_URL", "https://api.deepseek.com/v1")
DEFAULT_MODEL_KEY = os.getenv("MODEL_API_KEY", "")

# 默认并发
DEFAULT_CONCURRENCY = int(os.getenv("CONCURRENCY", "2"))

# 内存中的任务表
JOBS: Dict[str, Dict[str, Any]] = {}

# 允许解析的后缀
ALLOWED_EXTS = (".pdf", ".docx", ".doc", ".html", ".htm", ".txt")

# ----------------- HTML 模板 -----------------

INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>linkedin-批量简历分析</title>
  <style>
    :root{color-scheme:dark;}
    body{margin:0;font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Helvetica,Arial,"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif;background:#0b1220;color:#e6edf3}
    .wrap{max-width:980px;margin:40px auto;padding:0 16px}
    .card{background:#0f172a;border:1px solid #25304a;border-radius:14px;padding:20px}
    h1{font-size:26px;margin:0 0 14px}
    h2{font-size:16px;margin:22px 0 10px;color:#9fb4d5}
    label{display:block;margin:12px 0 6px;color:#bfd3f3}
    input[type=text],input[type=number],input[type=password]{width:100%;padding:10px 12px;border:1px solid #2b3a57;background:#0b1220;border-radius:10px;color:#e6edf3;outline:none}
    input[type=file]{width:100%}
    .row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
    .muted{color:#8aa2c9;font-size:13px}
    .danger{color:#ffb4b4}
    .btn{display:inline-flex;gap:8px;align-items:center;background:#2563eb;border:0;color:#fff;padding:12px 16px;border-radius:12px;cursor:pointer;font-weight:600}
    .btn:disabled{opacity:.6;cursor:not-allowed}
    .btn.secondary{background:#1f2937}
    .pill{display:inline-block;border:1px solid #2b3a57;border-radius:999px;padding:2px 8px;color:#9fb4d5;font-size:12px}
    .tip{background:#0b132e;border-left:3px solid #3b82f6;padding:10px 12px;border-radius:8px;color:#a8c1ee}
    .grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
    .switch{display:flex;align-items:center;gap:8px}
    .switch input{width:20px;height:20px}
    .hr{height:1px;background:#1d2640;margin:18px 0}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>linkedin-批量简历分析 <span class="pill">支持 PDF / DOCX / HTML / TXT（可打包 ZIP）</span></h1>
    <p class="muted">上传职位 JD（可选）与候选人简历（可多选/可ZIP），后端并发解析并<strong>实时流式</strong>输出。完成后可下载 Excel 清单。</p>

    <form id="form" class="card" action="/process" method="post" enctype="multipart/form-data">
      <!-- 基本信息 -->
      <h2>职位信息</h2>
      <div class="row">
        <div>
          <label>职位名称（必填）</label>
          <input required type="text" name="title" placeholder="如：资深基础设施架构师" />
        </div>
        <div>
          <label>方向（可选）</label>
          <input type="text" name="direction" placeholder="如：Infra / SRE / 医疗IT" />
        </div>
      </div>

      <!-- JD 上传 -->
      <h2>职位 JD（可选）</h2>
      <label>上传 JD 文件（PDF/DOCX/TXT/HTML，单个）</label>
      <input type="file" name="jd_file" accept=".pdf,.doc,.docx,.txt,.html,.htm" />

      <div class="hr"></div>

      <!-- 模型配置 -->
      <h2>模型与并发</h2>
      <div class="grid-3">
        <div>
          <label>模型名称（默认 deepseek-chat）</label>
          <input type="text" name="model_name" id="model_name" placeholder="deepseek-chat" />
        </div>
        <div>
          <label>每批次并发（默认 2）</label>
          <input type="number" name="concurrency" id="concurrency" min="1" max="8" step="1" placeholder="2" />
        </div>
        <div class="switch" style="margin-top:34px">
          <input type="checkbox" id="stream" name="stream" checked />
          <label for="stream" style="margin:0">实时流式输出（建议开启）</label>
        </div>
      </div>

      <div class="row">
        <div>
          <label>模型 Base URL（默认从环境变量）</label>
          <input type="text" name="base_url" id="base_url" placeholder="https://api.deepseek.com/v1" />
        </div>
        <div>
          <label>模型 API Key（默认从环境变量）</label>
          <input type="password" name="api_key" id="api_key" placeholder="sk-********" />
        </div>
      </div>

      <div class="tip" style="margin-top:8px">
        免费实例若长期空闲会休眠，首次请求会较慢。若上传体积较大，建议分包（如 20～30 份/包）。<br/>
        Base URL 建议以 <code>/v1</code> 结尾；模型名称如 <code>deepseek-chat</code>。表单中填写的值会覆盖环境变量，仅对本次任务生效。
      </div>

      <div class="hr"></div>

      <!-- 简历上传 -->
      <h2>候选人简历</h2>
      <label>上传文件（可多选或 ZIP 打包；支持 .pdf .docx .doc .html .htm .txt .zip）</label>
      <input required type="file" name="files" id="files" multiple
             accept=".pdf,.doc,.docx,.txt,.html,.htm,.zip" />

      <p class="muted" style="margin-top:6px">
        将自动按：<strong>职位名称_方向_时间戳</strong> 创建任务和报告文件夹；若未填写方向则省略该段。中断可在“实时报告”页点击“继续”接着跑。
      </p>

      <div style="margin-top:16px;display:flex;gap:10px">
        <button class="btn" type="submit">开始分析（生成Excel清单）</button>
        <a class="btn secondary" href="/reports" title="查看历史任务并下载报告">查看历史报告</a>
      </div>
    </form>
  </div>

  <script>
    // 将环境变量默认值（如后端注入）回填到表单
    // 如果后端没注入，这段也不会报错
    try {
      fetch('/env-defaults').then(r => r.json()).then(d => {
        if(d && typeof d === 'object'){
          if(d.MODEL_NAME && !document.getElementById('model_name').value)
            document.getElementById('model_name').value = d.MODEL_NAME;
          if(d.CONCURRENCY && !document.getElementById('concurrency').value)
            document.getElementById('concurrency').value = d.CONCURRENCY;
          if(d.MODEL_BASE_URL && !document.getElementById('base_url').value)
            document.getElementById('base_url').value = d.MODEL_BASE_URL;
          if(d.MODEL_API_KEY && !document.getElementById('api_key').value)
            document.getElementById('api_key').value = d.MODEL_API_KEY;
        }
      }).catch(()=>{});
    } catch(e){}
  </script>
</body>
</html>
"""

EVENTS_HTML = """
<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<title>任务 {{rid}} · 实时报告</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<link rel="icon" href="data:,">
<style>
  body{background:#0b0f14;color:#dfe7ef;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial}
  .wrap{max-width:1100px;margin:24px auto;padding:0 16px}
  h1{font-size:20px;margin:0 0 8px}
  .pill{display:inline-block;background:#0f1520;border:1px solid #1d2a39;border-radius:999px;padding:6px 12px;margin-left:8px}
  .card{background:#0f1520;border:1px solid #1d2a39;border-radius:14px;padding:16px;margin:16px 0}
  pre{white-space:pre-wrap;word-break:break-word;font:14px/1.6 ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas}
  .btn{display:inline-block;background:#2563eb;color:#fff;padding:8px 14px;border-radius:10px;border:none;cursor:pointer;margin-right:8px}
  a.btn{color:#fff;text-decoration:none}
</style>
</head>
<body>
<div class="wrap">
  <div style="display:flex;align-items:center;justify-content:space-between">
    <h1>任务 {{title}} <span class="pill">{{rid}}</span></h1>
    <div>
      <a class="btn" href="/events/{{rid}}">继续（断点续跑）</a>
      <a class="btn" href="/">返回</a>
    </div>
  </div>

  <div class="card">
    <pre id="log">初始化中…</pre>
    <div id="actions"></div>
  </div>
</div>

<script>
const log = document.getElementById('log');
const actions = document.getElementById('actions');
function append(msg){
  log.textContent += '\\n' + msg;
  log.scrollTop = log.scrollHeight;
}
log.textContent = "连接已建立\\n";

const es = new EventSource('/stream/{{rid}}');
es.onmessage = (e)=>{
  if (!e.data) return;
  if (e.data.startsWith('ACTION:')){
    const payload = JSON.parse(e.data.slice(7));
    actions.innerHTML = '';
    if (payload.report){
      const a = document.createElement('a');
      a.href = payload.report;
      a.textContent = '下载 Excel';
      a.className = 'btn';
      actions.appendChild(a);
    }
    if (payload.rank){
      const a2 = document.createElement('a');
      a2.href = payload.rank;
      a2.textContent = '查看榜单';
      a2.className = 'btn';
      actions.appendChild(a2);
    }
  }else{
    append(e.data);
  }
};
es.onerror = ()=>{
  append("连接中断，稍后自动重试或手动刷新本页。");
};
</script>
</body>
</html>
"""

RANK_HTML = """
<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<title>榜单 {{rid}}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<link rel="icon" href="data:,">
<style>
body{background:#0b0f14;color:#dfe7ef;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial}
.wrap{max-width:1200px;margin:24px auto;padding:0 16px}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 10px;border-bottom:1px solid #1d2a39;vertical-align:top}
.badge{padding:2px 8px;border-radius:999px;border:1px solid #1d2a39;background:#0f1520}
.Aplus{color:#fcd34d}
.A{color:#86efac}
.B{color:#93c5fd}
.C{color:#fda4af}
</style>
</head>
<body>
<div class="wrap">
  <h2>任务：{{title}} <small class="badge">{{rid}}</small></h2>
  <p><a href="/report/{{rid}}">下载Excel</a> · <a href="/">返回</a></p>
  <table>
    <thead>
      <tr>
        <th>#</th><th>候选人</th><th>当前公司</th><th>当前职位</th>
        <th>评分</th><th>等级</th><th>Email</th><th>年龄估算</th>
        <th>所在地</th><th>契合摘要</th><th>风险点</th><th>标签</th><th>Remarks</th>
      </tr>
    </thead>
    <tbody>
      {% for i,row in rows %}
      <tr>
        <td>{{i}}</td>
        <td>{{row.get('name','')}}</td>
        <td>{{row.get('company','')}}</td>
        <td>{{row.get('title','')}}</td>
        <td>{{row.get('score','')}}</td>
        <td class="{{row.get('gradeClass','')}}">{{row.get('grade','')}}</td>
        <td>{{row.get('email','')}}</td>
        <td>{{row.get('age','')}}</td>
        <td>{{row.get('location','')}}</td>
        <td>{{row.get('fit','')}}</td>
        <td>{{row.get('risks','')}}</td>
        <td>{{row.get('tags','')}}</td>
        <td>{{row.get('remarks','')}}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
</body>
</html>
"""

# ----------------- 工具函数 -----------------

def make_rid() -> str:
    return uuid.uuid4().hex[:8]

def now_ts():
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")

def safe_name(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s or "").strip()
    return s[:80] if s else "task"

def read_text_from_path(path: str) -> str:
    low = path.lower()
    try:
        if low.endswith(".pdf") and pdf_extract_text:
            return pdf_extract_text(path) or ""
        elif low.endswith(".docx") and DocxDocument:
            doc = DocxDocument(path)
            return "\n".join(p.text for p in doc.paragraphs)
        elif low.endswith((".html", ".htm")) and BeautifulSoup:
            with open(path, "rb") as f:
                soup = BeautifulSoup(f, "html.parser")
            return soup.get_text("\n")
        else:
            with open(path, "rb") as f:
                data = f.read()
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    return data.decode("gbk", errors="ignore")
                except Exception:
                    return data.decode("latin1", errors="ignore")
    except Exception as e:
        return f"[解析失败:{e}]"

def grade_from_score(s: float) -> str:
    if s >= 90:
        return "A+"
    if s >= 80:
        return "A"
    if s >= 65:
        return "B"
    return "C"

def grade_css(g: str) -> str:
    return {"A+":"Aplus","A":"A","B":"B","C":"C"}.get(g,"")

def dedup_key(row: Dict[str,Any]) -> str:
    name = (row.get("name") or "").strip().lower()
    email = (row.get("email") or "").strip().lower()
    comp = (row.get("company") or "").strip().lower()
    title = (row.get("title") or "").strip().lower()
    if email:  # email 优先
        return f"{name}|{email}"
    return f"{name}|{comp}|{title}"

# ----------------- LLM 调用 -----------------

def call_model(base: str, key: str, model: str, messages: List[Dict[str,str]]) -> Dict[str,Any]:
    """
    兼容 DeepSeek/OpenAI 格式的简易调用（非流式）。
    """
    url = base.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type":"application/json"}
    payload = {"model": model, "messages": messages, "temperature": 0.2}
    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return {"content": content}

SYSTEM_PROMPT = """你是一名资深猎头助理。请阅读一份中文或英文简历全文，并按 JSON 输出结构化要点与评分。
重要：
1) 仅保留 Email（不解析手机号/座机）。
2) 评分 score 范围 0~100；等级 grade ∈ {A+, A, B, C}。A+ 为特别契合或顶级人选（通常 score≥90）。
3) remarks 需要做**时间线**概述：教育（哪年-哪年，学校/专业/学历）；工作（哪年-哪年，公司/职位/一句话职责）。
4) 如无法确定就留空或“不详”。

输出 JSON 字段：
{
 "name": "...",
 "company": "...",           # 当前或最近公司
 "title": "...",             # 当前或最近职位
 "email": "...",             # 若无则空
 "age": "不详/xx岁(推算)",   # 可根据本科入学年约推：年龄≈(今年-入学年+18)
 "location": "...",          # 当前所在地
 "fit": "...",               # 契合摘要（2-3句）
 "risks": "...",             # 风险点（1-3条合并成一句）
 "tags": "...",              # 关键标签，用逗号
 "remarks": "...",           # 教育+工作时间线（见上）
 "score": 0-100,
 "grade": "A+/A/B/C"
}
"""

def build_messages(job: Dict[str,Any], raw_text: str) -> List[Dict[str,str]]:
    jd = job.get("jd_text","")
    return [
        {"role":"system","content": SYSTEM_PROMPT},
        {"role":"user","content": f"职位：{job.get('title','')}\n方向：{job.get('track','')}\n岗位要求/偏好（可为空）：\n{jd}\n---\n以下是候选人简历全文：\n{raw_text}\n\n请输出 JSON。"}
    ]

def safe_parse_json(s: str) -> Dict[str,Any]:
    try:
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            return json.loads(m.group(0))
        return json.loads(s)
    except Exception:
        return {}

# ----------------- 核心流程 -----------------

def run_job_async(rid: str):
    """
    后台线程：解压 -> 解析 -> LLM 评估 -> 去重/排序 -> 生成Excel/榜单
    期间不断向 q.put(...) 推送 SSE 文本。
    """
    job = JOBS[rid]
    q: Queue = job["q"]

    # 1) 解压/收集文件（已在 /process 做，这里只再确认）
    files = job.get("files", [])
    if not files:
        q.put("⚠️ 未找到可解析的文件。")
        q.put("[DONE]")
        return

    total = len(files)
    q.put(f"🗂️ 已就绪：{total} 份文件，将并发 {job['cc']} 解析…")

    results: List[Dict[str,Any]] = []
    lock = os.environ.get("DUMMY_LOCK","")

    def handle_one(path: str) -> Dict[str,Any]:
        base = os.path.basename(path)
        try:
            raw = read_text_from_path(path)
            if not raw.strip():
                return {"_file": base, "_err": "空文本"}
            msgs = build_messages(job, raw)
            r = call_model(job["model_base"], job["model_key"], job["model_name"], msgs)
            js = safe_parse_json(r.get("content",""))
            # 兜底与清洗
            score = float(js.get("score", 0) or 0)
            grd = (js.get("grade","") or "").strip().upper()
            if grd not in ("A+","A","B","C"):
                grd = grade_from_score(score)
            row = {
                "_file": base,
                "name": js.get("name",""),
                "company": js.get("company",""),
                "title": js.get("title",""),
                "email": js.get("email",""),
                "age": js.get("age","不详"),
                "location": js.get("location",""),
                "fit": js.get("fit",""),
                "risks": js.get("risks",""),
                "tags": js.get("tags",""),
                "remarks": js.get("remarks",""),
                "score": round(score,1),
                "grade": grd,
            }
            return row
        except Exception as e:
            return {"_file": base, "_err": str(e)}

    # 2) 并发解析
    with ThreadPoolExecutor(max_workers=job["cc"]) as ex:
        futs = {ex.submit(handle_one, p): p for p in files}
        done = 0
        last_ping = time.time()
        for fut in as_completed(futs):
            done += 1
            path = futs[fut]
            try:
                row = fut.result()
                if "_err" in row:
                    q.put(f"❌ 解析失败 [{done}/{total}] {os.path.basename(path)} ：{row['_err']}")
                else:
                    q.put(f"✅ 完成 [{done}/{total}] {row.get('name') or row['_file']} · 评分 {row['score']} / 等级 {row['grade']}")
                    results.append(row)
            except Exception as e:
                q.put(f"❌ 异常 [{done}/{total}] {os.path.basename(path)} ：{e}")

            if time.time() - last_ping > 2:
                q.put("…仍在工作中")
                last_ping = time.time()

    if not results:
        q.put("⚠️ 无有效结果。")
        q.put("[DONE]")
        return

    # 3) 去重（name + email | company + title）
    q.put("🧹 去重中…")
    uniq: Dict[str,Dict[str,Any]] = {}
    for r in results:
        k = dedup_key(r)
        old = uniq.get(k)
        if not old or (r["score"] > old.get("score",0)):
            uniq[k] = r
    results = list(uniq.values())

    # 4) 排序（score desc）
    results.sort(key=lambda x: x.get("score",0), reverse=True)

    # 5) 生成 Excel 与榜单
    q.put("📊 生成 Excel 与榜单…")
    out_dir = job["out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    xlsx_path = os.path.join(out_dir, f"{job['name']}.xlsx")
    html_path = os.path.join(out_dir, f"{job['name']}_rank.html")

    # Excel
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "候选清单"
    header = [
        "候选人名字","目前所在公司","目前职位",
        "匹配分数(0-100)","匹配等级(A+/A/B/C)",
        "E-mail","年龄预估","目前所在地",
        "契合摘要","风险点","标签","Remarks","来源文件"
    ]
    ws.append(header)
    for r in results:
        ws.append([
            r.get("name",""),
            r.get("company",""),
            r.get("title",""),
            r.get("score",""),
            r.get("grade",""),
            r.get("email",""),
            r.get("age",""),
            r.get("location",""),
            r.get("fit",""),
            r.get("risks",""),
            r.get("tags",""),
            r.get("remarks",""),
            r.get("_file",""),
        ])
    wb.save(xlsx_path)

    # 榜单 HTML
    rows = []
    for i,r in enumerate(results,1):
        r["gradeClass"] = grade_css(r.get("grade",""))
        rows.append((i,r))
    html = render_template_string(RANK_HTML, rid=rid, title=job["name"], rows=rows)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    job["xlsx_path"] = xlsx_path
    job["rank_path"] = html_path
    q.put("🎉 全部完成！")
    q.put(f"Excel：/report/{rid}")
    q.put(f"榜单：/rank/{rid}")
    q.put("ACTION:" + json.dumps({"report": f"/report/{rid}", "rank": f"/rank/{rid}"}))
    q.put("[DONE]")

# ----------------- 路由 -----------------

@app.route("/", methods=["GET"])
def index():
    jobs = [(rid, {"name":j["name"], "created": j["created"].strftime("%Y-%m-%d %H:%M:%S")}) for rid,j in JOBS.items()]
    # 最新在前
    jobs.sort(key=lambda x: x[1]["created"], reverse=True)
    return render_template_string(
        INDEX_HTML,
        def_model=DEFAULT_MODEL_NAME,
        def_cc=DEFAULT_CONCURRENCY,
        def_base=DEFAULT_MODEL_BASE,
        def_key=DEFAULT_MODEL_KEY,
        jobs=jobs
    )

@app.route("/process", methods=["POST"])
def process():
    # 收集基础参数
    job_title = (request.form.get("job_title") or "").strip()
    job_track = (request.form.get("job_track") or "").strip()
    if not job_title:
        return "职位名称必填", 400

    model_name = (request.form.get("model_name") or DEFAULT_MODEL_NAME).strip()
    model_base = (request.form.get("model_base") or DEFAULT_MODEL_BASE).strip()
    model_key  = (request.form.get("model_key")  or DEFAULT_MODEL_KEY).strip()
    cc = int(request.form.get("concurrency") or DEFAULT_CONCURRENCY)
    cc = max(1, min(cc, 10))

    ts = now_ts()
    name = f"{safe_name(job_title)}_{safe_name(job_track) if job_track else 'General'}_{ts}"

    rid = make_rid()
    q = Queue()

    out_dir = os.path.join("/tmp", name)
    os.makedirs(out_dir, exist_ok=True)

    job = {
        "rid": rid,
        "q": q,
        "name": name,
        "title": job_title,
        "track": job_track,
        "model_name": model_name,
        "model_base": model_base,
        "model_key": model_key,
        "cc": cc,
        "created": datetime.utcnow(),
        "out_dir": out_dir,
        "jd_text": "",     # 你之后可在表单里加 JD 输入，此处保留字段
    }
    JOBS[rid] = job

    # 1) 处理上传（单文件或多文件或 ZIP）
    upload_files = request.files.getlist("files")
    if not upload_files:
        return "未收到文件", 400

    q.put("📶 上传接收中…（大文件会较慢）")

    saved_files: List[str] = []
    # 分块保存每个上传项；如 zip 则解压
    for up in upload_files:
        if not up or not up.filename:
            continue
        fname = safe_name(up.filename)
        tmp_path = os.path.join(out_dir, fname)
        # 分块写
        with open(tmp_path, "wb") as f:
            while True:
                chunk = up.stream.read(CHUNK_SIZE)
                if not chunk: break
                f.write(chunk)

        if fname.lower().endswith(".zip"):
            q.put(f"📦 解压 {fname} …")
            try:
                with zipfile.ZipFile(tmp_path, "r") as zf:
                    names = zf.namelist()
                    cnt, shown = len(names), 0
                    for i, n in enumerate(names, 1):
                        if not n.lower().endswith(ALLOWED_EXTS):
                            continue
                        zf.extract(n, out_dir)
                        saved_files.append(os.path.join(out_dir, n))
                        if (i - shown) >= 5:
                            q.put(f"…解压进度 {i}/{cnt}")
                            shown = i
                q.put(f"✅ 完成：{fname}")
            except Exception as e:
                q.put(f"❌ 解压失败 {fname}: {e}")
        else:
            # 普通文件直接加入
            if fname.lower().endswith(ALLOWED_EXTS):
                saved_files.append(tmp_path)
            else:
                q.put(f"⚠️ 跳过不支持的文件：{fname}")

    # 仅保留存在的文件
    saved_files = [p for p in saved_files if os.path.exists(p)]
    job["files"] = saved_files

    q.put(f"🗂️ 共发现 {len(saved_files)} 份可解析文件。")

    # 启动后台线程
    import threading
    t = threading.Thread(target=run_job_async, args=(rid,), daemon=True)
    t.start()

    # 跳到 SSE 页面
    return redirect(url_for("events", rid=rid))

@app.route("/events/<rid>", methods=["GET"])
def events(rid):
    job = JOBS.get(rid)
    if not job: return "任务不存在", 404
    return render_template_string(EVENTS_HTML, rid=rid, title=job["name"])

@app.route("/stream/<rid>")
def stream(rid):
    job = JOBS.get(rid)
    if not job: return "data: 任务不存在\\n\\n", 200, {'Content-Type':'text/event-stream'}
    q: Queue = job["q"]

    def gen():
        yield "data: ▶️ 连接已建立\\n\\n"
        while True:
            msg = q.get()
            if msg == "[DONE]":
                yield "data: ☑️ 任务结束\\n\\n"
                break
            # SSE 安全编码
            safe = str(msg).replace("\\r"," ").replace("\\n","\\n")
            yield f"data: {safe}\\n\\n"

    headers = {
        "Content-Type":"text/event-stream",
        "Cache-Control":"no-cache",
        "X-Accel-Buffering":"no",
        "Connection":"keep-alive",
    }
    return Response(gen(), headers=headers)

@app.route("/report/<rid>", methods=["GET"])
def report(rid):
    job = JOBS.get(rid)
    if not job: return "任务不存在", 404
    x = job.get("xlsx_path")
    if not x or not os.path.exists(x):
        return "报告还未生成", 404
    return send_file(x, as_attachment=True, download_name=os.path.basename(x))

@app.route("/rank/<rid>", methods=["GET"])
def rank(rid):
    job = JOBS.get(rid)
    if not job: return "任务不存在", 404
    html = job.get("rank_path")
    if not html or not os.path.exists(html):
        return "榜单还未生成", 404
    with open(html, "r", encoding="utf-8") as f:
        content = f.read()
    return content

# ----------------- 入口 -----------------
if __name__ == "__main__":
    port = int(os.getenv("PORT","5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
