# app.py
import os, re, json, zipfile, time, logging
from datetime import datetime
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue, Empty

import requests
from flask import Flask, request, Response, send_file, render_template_string, redirect, url_for

# ---------- 可选解析器 ----------
try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except Exception:
    pdf_extract_text = None

try:
    import docx
except Exception:
    docx = None

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

# ---------- 基础配置 ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MODEL_API_KEY = os.getenv("MODEL_API_KEY", "")
MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "").rstrip("/")
MODEL_NAME     = os.getenv("MODEL_NAME", "deepseek-chat")
CONCURRENCY    = int(os.getenv("CONCURRENCY", "2"))
MAX_UPLOAD_MB  = int(os.getenv("MAX_UPLOAD_MB", "200"))

DATA_DIR = os.path.abspath("./data")
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
RUNS: Dict[str, Dict[str, Any]] = {}

# ---------- HTML ----------
INDEX_HTML = """
<!doctype html><html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>linkedin-批量简历分析</title>
<style>
:root{
  --bg:#0b0f14; --card:#111827; --fg:#e5e7eb; --muted:#9aa5b1;
  --primary:#2563eb; --border:#1f2937; --ok:#22c55e; --bad:#ef4444;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:ui-sans-serif,system-ui,Segoe UI,Roboto,Arial}
.wrap{max-width:980px;margin:32px auto;padding:0 16px}
h1{font-size:20px;margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px;margin-bottom:14px}
label{display:block;font-size:14px;color:var(--muted);margin:8px 0 6px}
input[type=text],textarea{width:100%;border:1px solid var(--border);border-radius:10px;background:#0b1018;color:#e5e7eb;padding:10px}
textarea{min-height:110px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.btn{background:var(--primary);color:#fff;border:0;border-radius:12px;padding:12px 16px;font-weight:600;cursor:pointer}
small{color:var(--muted)}
.filebox{border:1px dashed var(--border);border-radius:10px;padding:10px}
ul.files{list-style:none;margin:8px 0 0;padding:0}
ul.files li{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);padding:6px 0}
ul.files li button{background:#334155;color:#cbd5e1;border:0;border-radius:8px;padding:4px 8px;cursor:pointer}
.note{font-size:12px;color:var(--muted)}
</style>
</head>
<body>
<div class="wrap">
  <h1>linkedin-批量简历分析</h1>
  <div class="card"><p class="note">说明：上传你合规导出的 ZIP/PDF/HTML/TXT/DOCX，后端并发解析与AI打分，实时输出并最终产出Excel/榜单。</p></div>

  <form id="f" action="/process" method="post" enctype="multipart/form-data">
    <div class="card">
      <div class="row">
        <div>
          <label>职位名称（必填）</label>
          <input name="role" placeholder="如：资深基础设施架构师" required>
        </div>
        <div>
          <label>方向（选填）</label>
          <input name="track" placeholder="如：Infra / SRE / 医疗IT">
        </div>
      </div>

      <div class="row">
        <div>
          <label>Must-have 关键词（逗号分隔）</label>
          <input name="must" placeholder="如：K8s, DevOps, 合规"/>
        </div>
        <div>
          <label>Nice-to-have 关键词（逗号分隔）</label>
          <input name="nice" placeholder="如：HPC, 金融, 医药"/>
        </div>
      </div>

      <label>限制说明（地域/签证/语言等）</label>
      <input name="limits" placeholder="如：上海/苏州；英文流利；可出差"/>

      <label style="margin-top:10px">补充说明（可粘贴JD要点）</label>
      <textarea name="note" placeholder="可写关键点、筛选口径、其他背景要求等"></textarea>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px">上传候选集（支持多文件，ZIP/PDF/HTML/TXT/DOCX）</h3>
      <div class="filebox">
        <input id="file" type="file" name="files" multiple required>
        <ul id="flist" class="files"></ul>
        <small class="note">可把 Recruiter Lite 每页导出的 ZIP 一次选中多个；如体量很大建议分批。</small>
      </div>
    </div>

    <div class="card">
      <button class="btn" type="submit">开始分析（生成Excel清单）</button>
    </div>
  </form>
</div>

<script>
const file = document.getElementById('file');
const flist = document.getElementById('flist');
file.addEventListener('change', refreshList);
function refreshList(){
  flist.innerHTML='';
  const dt = new DataTransfer();
  Array.from(file.files).forEach((f,i)=>{
    const li = document.createElement('li');
    li.innerHTML = '<span>'+f.name+'</span>';
    const rm = document.createElement('button'); rm.textContent='删除';
    rm.onclick = (e)=>{ e.preventDefault(); removeAt(i); };
    li.appendChild(rm); flist.appendChild(li);
    dt.items.add(f);
  });
  file.files = dt.files;
}
function removeAt(idx){
  const dt = new DataTransfer();
  Array.from(file.files).forEach((f,i)=>{ if(i!==idx) dt.items.add(f); });
  file.files = dt.files; refreshList();
}
</script>
</body></html>
"""

EVENTS_HTML = """
<!doctype html><html lang="zh"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>任务 {{name}} · 实时报告</title>
<style>
:root{--bg:#0b0f14;--card:#111827;--fg:#e5e7eb;--border:#1f2937;--primary:#2563eb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:ui-sans-serif,system-ui,Segoe UI,Roboto,Arial}
.wrap{max-width:1100px;margin:22px auto;padding:0 14px}
h1{font-size:18px;margin:0 0 10px}
.row{display:flex;gap:8px;margin-bottom:10px}
.btn{background:var(--primary);color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:600;cursor:pointer}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:12px}
pre{white-space:pre-wrap;word-break:break-all;margin:0;font-size:13px;line-height:1.45}
a.btn{display:inline-block;text-decoration:none}
</style></head>
<body><div class="wrap">
  <div class="row">
    <a class="btn" href="/resume/{{rid}}">继续（断点续跑）</a>
    <a class="btn" href="/" style="background:#334155">返回</a>
  </div>
  <h1>任务 {{name}} · 实时报告</h1>
  <div class="card"><pre id="log">连接已建立…</pre></div>

  <div class="row">
    <a id="dl" class="btn" href="/download/{{rid}}" style="pointer-events:none;opacity:.5">下载 Excel</a>
    <a class="btn" id="rank" href="/report/{{rid}}" target="_blank" style="background:#16a34a">查看榜单</a>
  </div>
</div>
<script>
const log = document.getElementById('log');
const dl  = document.getElementById('dl');
const es  = new EventSource("/stream/{{rid}}");
es.onmessage = (ev)=>{
  if(ev.data==="__READY_EXCEL__"){ dl.style.pointerEvents='auto'; dl.style.opacity='1'; return; }
  log.appendChild(document.createTextNode("\\n"+ev.data));
  log.scrollTop = log.scrollHeight;
};
es.onerror = (e)=>{ es.close(); };
</script>
</body></html>
"""

RANK_HTML = """
<!doctype html><html lang="zh"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>榜单 · {{name}}</title>
<style>
:root{--bg:#0b0f14;--card:#111827;--fg:#e5e7eb;--border:#1f2937}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:ui-sans-serif,system-ui,Segoe UI,Roboto,Arial}
.wrap{max-width:1100px;margin:22px auto;padding:0 14px}
h1{font-size:18px;margin:0 0 10px}
table{width:100%;border-collapse:collapse}
th,td{border-bottom:1px solid var(--border);padding:8px 6px;text-align:left;vertical-align:top;font-size:13px}
.badge{padding:2px 8px;border-radius:999px;border:1px solid #374151;background:#0f172a}
</style></head>
<body><div class="wrap">
  <h1>榜单 · {{name}}</h1>
  <table>
    <thead><tr>
      <th>排名</th><th>候选人</th><th>公司/职位</th><th>等级</th><th>分数</th><th>Email</th><th>摘要</th>
    </tr></thead>
    <tbody>
    {% for row in rows %}
      <tr>
        <td>{{ loop.index }}</td>
        <td>{{row.get('name','')}}</td>
        <td>{{row.get('current_company','')}} / {{row.get('current_title','')}}</td>
        <td><span class="badge">{{row.get('grade','')}}</span></td>
        <td>{{row.get('score','')}}</td>
        <td>{{row.get('email','')}}</td>
        <td>{{row.get('remark','')}}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</div></body></html>
"""

# ---------- 工具 ----------
def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]+", "", text, flags=re.U).strip().lower()
    s = re.sub(r"[-\s]+", "_", s)
    return s or "job"

def ensure_run(rid:str) -> Dict[str,Any]:
    run = RUNS.get(rid)
    if not run:
        RUNS[rid] = run = {"q":Queue(), "dir":os.path.join(DATA_DIR,rid), "done":False,
                           "summary":[], "ts":time.time(), "role":"", "track":"", "name":rid}
        os.makedirs(run["dir"], exist_ok=True)
    return run

def put(rid: str, msg: str):
    RUNS.get(rid, {}).get("q", Queue()).put(msg)

def text_from_file(path:str) -> str:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf" and pdf_extract_text:
            return pdf_extract_text(path) or ""
        if ext == ".docx" and docx:
            return "\n".join(p.text for p in docx.Document(path).paragraphs)
        if ext in (".html",".htm") and BeautifulSoup:
            with open(path,"rb") as f:
                soup = BeautifulSoup(f, "html.parser")
                return soup.get_text(" ", strip=True)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception as e:
        logging.warning("parse error %s: %s", path, e)
        return ""

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
def extract_email(text:str) -> str:
    m = EMAIL_RE.search(text)
    return m.group(0) if m else ""

def estimate_age_from_edu(education: Any) -> str:
    """本科入学年+18做粗估；无信息返回“不详”"""
    try:
        year = None
        if isinstance(education, list):
            for e in education:
                deg = (e.get("degree","") or "").lower()
                st  = str(e.get("start","") or "")
                if ("bachelor" in deg) or ("本科" in deg):
                    m = re.search(r"(19|20)\\d{2}", st)
                    if m: year = int(m.group(0)); break
        if year:
            now = datetime.now().year
            return str(max(18, now - year + 18))
    except Exception:
        pass
    return "不详"

def llm_chat(messages: List[Dict[str,str]], temperature: float=0.2, max_tokens:int=1024) -> str:
    if not MODEL_API_KEY or not MODEL_BASE_URL:
        return ""
    url = f"{MODEL_BASE_URL}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {MODEL_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": MODEL_NAME, "messages": messages, "temperature": temperature,
               "max_tokens": max_tokens, "stream": False}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logging.warning("LLM call failed: %s", e)
        return ""

PROMPT_SYS = (
"你是资深猎头助理。请基于候选人简历文本，输出**严格合法的 JSON**，并做岗位匹配。\n"
"字段：\n"
"name, current_company, current_title, email, location,\n"
"age_estimate, tags(list),\n"
"education(list:{school,major,degree,start,end}),\n"
"experiences(list:{company,title,start,end,one_line}),\n"
"fit_summary(中文，概括与岗位的契合要点),\n"
"risks(中文，概括潜在风险/短板),\n"
"remark(中文时间线：如“2012-2016年 就读于XX大学/计算机 本科；2016-2020年 就职于XX公司/工程师，主要负责XXX”，≤120字),\n"
"score(0-100 数值), grade(A+/A/B/C)。\n"
"打分口径：关键词匹配度 + 最近3年经验相关性 + 平台/影响力。\n"
"若简历无邮箱，可从文本中正则抽取；不要电话。"
)

def build_messages(role:str, track:str, note:str, limits:str, must:str, nice:str, text:str)->List[Dict[str,str]]:
    user = f"""岗位：{role}
方向：{track}
限制：{limits}
Must-have：{must}
Nice-to-have：{nice}
补充说明：{note}

候选人简历文本：
{text}
"""
    return [{"role":"system","content":PROMPT_SYS},{"role":"user","content":user}]

def grade_from_score(s: float) -> str:
    try:
        s = float(s)
    except:
        return "C"
    if s >= 90: return "A+"
    if s >= 80: return "A"
    if s >= 70: return "B"
    return "C"

# ---------- 主流程 ----------
def handle_zip_or_file(upload_path: str, work_dir:str) -> List[str]:
    files = []
    base,ext = os.path.splitext(os.path.basename(upload_path))
    if ext.lower()==".zip":
        try:
            with zipfile.ZipFile(upload_path) as z:
                z.extractall(work_dir)
            for root,_,fs in os.walk(work_dir):
                for f in fs:
                    if os.path.splitext(f)[1].lower() in (".pdf",".html",".htm",".txt",".docx"):
                        files.append(os.path.join(root,f))
        except Exception as e:
            logging.warning("unzip error %s: %s", upload_path, e)
    else:
        files.append(upload_path)
    return files

def process_resume(path:str, role:str, track:str, note:str, limits:str, must:str, nice:str)->Dict[str,Any]:
    text = text_from_file(path)
    email = extract_email(text)
    msg = build_messages(role,track,note,limits,must,nice,text[:12000])
    content = llm_chat(msg, temperature=0.2, max_tokens=900)
    data = {}
    if content:
        try:
            data = json.loads(re.sub(r"```json|```","",content).strip())
        except Exception:
            content2 = llm_chat(
                [{"role":"system","content":"仅返回合法 JSON。"},
                 {"role":"user","content":content}], 0.0, 600
            )
            try:
                data = json.loads(re.sub(r"```json|```","",content2).strip())
            except Exception:
                data = {}
    # 兜底字段
    data["email"] = data.get("email") or email or ""
    data["name"]  = (data.get("name") or "").strip() or os.path.splitext(os.path.basename(path))[0]
    data["current_company"] = data.get("current_company") or ""
    data["current_title"]   = data.get("current_title") or ""
    data["remark"] = data.get("remark") or ""
    data["fit_summary"] = data.get("fit_summary") or ""
    data["risks"] = data.get("risks") or ""

    # 年龄估算兜底
    if not data.get("age_estimate"):
        data["age_estimate"] = estimate_age_from_edu(data.get("education"))

    # 分数与等级
    try:
        sc = float(data.get("score", 0))
    except:
        sc = 0.0
    data["score"] = round(sc,1)
    data["grade"] = grade_from_score(sc)

    # 去重签名
    data["_sig"] = (data["name"].strip().lower(), data["current_company"].strip().lower())
    return data

def write_excel(rows: List[Dict[str,Any]], xlsx_path:str):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Candidates"

    headers = [
        "候选人名字","目前公司","目前职位","匹配等级","分数",
        "Email","年龄预估","目前所在地","契合摘要","风险点","标签","Remarks(时间线概述)"
    ]
    ws.append(headers)

    for r in rows:
        ws.append([
            r.get("name",""),
            r.get("current_company",""),
            r.get("current_title",""),
            r.get("grade",""),
            r.get("score",""),
            r.get("email",""),
            r.get("age_estimate",""),
            r.get("location",""),
            r.get("fit_summary",""),
            r.get("risks",""),
            ", ".join(r.get("tags",[]) or []),
            r.get("remark",""),
        ])

    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = 18
    ws.column_dimensions['I'].width = 28
    ws.column_dimensions['L'].width = 48
    wb.save(xlsx_path)

# ---------- 路由 ----------
@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML)

@app.route("/process", methods=["POST"])
def process():
    role   = (request.form.get("role") or "").strip()
    track  = (request.form.get("track") or "").strip()
    note   = (request.form.get("note") or "").strip()
    limits = (request.form.get("limits") or "").strip()
    must   = (request.form.get("must") or "").strip()
    nice   = (request.form.get("nice") or "").strip()
    if not role:
        return ("职位名称必填", 400)

    rid = f"{slugify(role)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run = ensure_run(rid)
    run["role"], run["track"], run["name"] = role, track, rid

    work_dir = run["dir"]
    up_dir   = os.path.join(work_dir,"uploads")
    os.makedirs(up_dir, exist_ok=True)

    files = request.files.getlist("files")
    if not files:
        return ("请上传文件", 400)

    sz_total = 0
    for f in files:
        b = f.read()
        sz_total += len(b)
        if sz_total > MAX_UPLOAD_MB*1024*1024:
            return (f"总大小超过限制 {MAX_UPLOAD_MB}MB", 400)
        with open(os.path.join(up_dir, f.filename), "wb") as o:
            o.write(b)

    def runner():
        try:
            put(rid, "▶ 开始处理…")
            todo = []
            for fn in os.listdir(up_dir):
                todo += handle_zip_or_file(os.path.join(up_dir,fn), os.path.join(work_dir,"unz"))
            todo = [p for p in todo if os.path.splitext(p)[1].lower() in (".pdf",".html",".htm",".txt",".docx")]
            todo = sorted(set(todo))
            put(rid, f"解析 待办 {len(todo)} 个文件")

            results, seen = [], set()
            with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
                futs = [ex.submit(process_resume, p, role, track, note, limits, must, nice) for p in todo]
                for i,fut in enumerate(as_completed(futs), start=1):
                    try:
                        d = fut.result()
                    except Exception as e:
                        d = {}
                        logging.warning("worker error: %s", e)
                    if d:
                        sig = d.get("_sig")
                        if sig and sig in seen:
                            put(rid, f"[跳过重复] {d.get('name','')}")
                        else:
                            seen.add(sig)
                            results.append(d)
                            put(rid, f"[{i}/{len(todo)}] {d.get('name','?')} → {d.get('grade','')} / {d.get('score','')}")
                    else:
                        put(rid, f"[{i}/{len(todo)}] 解析失败")

            results.sort(key=lambda x: x.get("score",0), reverse=True)
            run["summary"] = results

            xlsx = os.path.join(work_dir, f"{rid}.xlsx")
            write_excel(results, xlsx)
            put(rid, "导出 Excel 完成")
            put(rid, "__READY_EXCEL__")

            run["done"] = True
            put(rid, f"✅ 完成，共 {len(results)} 人。")
        except Exception as e:
            logging.exception("runner fatal")
            put(rid, f"❌ 失败：{e}")

    from threading import Thread
    Thread(target=runner, daemon=True).start()
    return redirect(url_for("events", rid=rid))

@app.route("/events/<rid>")
def events(rid):
    run = ensure_run(rid)
    return render_template_string(EVENTS_HTML, rid=rid, name=run.get("name", rid))

@app.route("/stream/<rid>")
def stream(rid):
    run = ensure_run(rid)
    q: Queue = run["q"]
    def gen():
        yield "data: 连接已建立\n\n"
        while True:
            try:
                msg = q.get(timeout=15)
                yield f"data: {msg}\n\n"
            except Empty:
                yield "data: \n\n"
    headers = {
        "Content-Type":"text/event-stream",
        "Cache-Control":"no-cache",
        "X-Accel-Buffering":"no",
        "Connection":"keep-alive"
    }
    return Response(gen(), headers=headers)

@app.route("/download/<rid>")
def download(rid):
    work_dir = ensure_run(rid)["dir"]
    xlsx = os.path.join(work_dir, f"{rid}.xlsx")
    if not os.path.exists(xlsx):
        return ("文件尚未生成", 404)
    return send_file(xlsx, as_attachment=True, download_name=os.path.basename(xlsx))

@app.route("/report/<rid>")
def report(rid):
    run = ensure_run(rid)
    rows = run.get("summary", [])
    return render_template_string(RANK_HTML, name=run.get("name",rid), rows=rows)

@app.route("/resume/<rid>")
def resume(rid):
    run = ensure_run(rid)
    if run.get("done"):
        return redirect(url_for("events", rid=rid))
    put(rid, "▶ 继续执行…（若已完成会直接产出）")
    return redirect(url_for("events", rid=rid))

@app.route("/healthz")
def healthz():
    return "ok"

if __name__ == "__main__":
    port = int(os.getenv("PORT","10000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
