# app.py
# -*- coding: utf-8 -*-
import os, io, re, json, uuid, zipfile, time, hashlib, logging, csv, traceback
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from threading import Lock, Event, Thread
from queue import Queue
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, request, render_template_string, send_file, redirect, url_for, Response

# -------- Optional parsers -------
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

try:
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
except Exception:
    Workbook = None

import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ------------------ CONFIG ------------------
DATA_DIR = os.path.abspath("./runs")
os.makedirs(DATA_DIR, exist_ok=True)

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "").rstrip("/")
MODEL_API_KEY  = os.getenv("MODEL_API_KEY", "")
MODEL_NAME     = os.getenv("MODEL_NAME", "deepseek-chat")

# 并发（后台配置即可，对用户隐藏）
MAX_WORKERS  = int(os.getenv("MAX_WORKERS", os.getenv("CONCURRENCY", "2")))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))

# ----------- JOB STATE -----------
JOBS: Dict[str, Dict[str, Any]] = {}
STREAMS: Dict[str, Queue] = {}
LOCK = Lock()

# ------------------ UTIL ------------------

def safe_join(*parts) -> str:
    p = os.path.join(*parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p

def rid_now(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}"

def _push(rid: str, line: str):
    q = STREAMS.get(rid)
    if q:
        q.put(line)

def _done(rid: str):
    q = STREAMS.get(rid)
    if q:
        q.put("[DONE]")

def human_size(bytes_: int) -> str:
    for unit in ["B","KB","MB","GB"]:
        if bytes_ < 1024: return f"{bytes_:.1f}{unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f}TB"

# ------------------ HTML ------------------
INDEX_HTML = r"""
<!doctype html><html lang="zh"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Alsos Talent · 合规AI自动化寻访（MVP）</title>
<style>
:root{--bg:#0b0f14;--card:#121824;--line:#1e2633;--muted:#9aa8bd;--fg:#e2e8f2;--blue:#2563eb;}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial}
.wrap{max-width:980px;margin:32px auto;padding:0 16px}
h1{font-size:22px;margin:8px 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:16px}
label{display:block;color:#A9B4C6;margin:10px 0 6px}
input[type=text],textarea{width:100%;background:#0b1018;color:#dbe4f0;border:1px solid #223044;border-radius:10px;padding:10px 12px;outline:none}
textarea{min-height:120px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.btn{background:var(--blue);color:#fff;border:0;border-radius:12px;padding:12px 16px;cursor:pointer;font-weight:600}
small{color:var(--muted)}
.badge{display:inline-block;background:#0f1c33;border:1px solid #223044;color:#bcd2ff;padding:2px 8px;border-radius:999px;margin-right:6px}
.filebox{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.filebox span{border:1px dashed #334155;border-radius:8px;padding:4px 8px}
.filebox .muted{color:#94a3b8;border-color:#3b485a}
.actions{display:flex;gap:10px;align-items:center}
a{color:#7aa0ff;text-decoration:none}
</style>
</head><body><div class="wrap">
  <h1>Alsos Talent · 合规AI自动化寻访（MVP）</h1>
  <div class="card"><p><small>说明：本工具<strong>不做任何网站抓取</strong>，只分析你合规导出的 ZIP/PDF/HTML/DOCX/TXT/CSV 文件。</small></p></div>

  <form action="/process" method="post" enctype="multipart/form-data">
    <div class="card">
      <h3>岗位/筛选要求</h3>
      <div class="row">
        <div><label>职位名称（必填）</label><input required name="role" type="text" placeholder="如：资深基础设施架构师"/></div>
        <div><label>方向（选填）</label><input name="dir" type="text" placeholder="如：Infra / SRE / 医疗IT"/></div>
      </div>
      <div class="row">
        <div><label>Must-have 关键词（逗号分隔）</label><input name="must" type="text" placeholder="如：K8s, DevOps, 安全合规"/></div>
        <div><label>Nice-to-have 关键词（逗号分隔）</label><input name="nice" type="text" placeholder="如：HPC, 金融, 医药"/></div>
      </div>
      <label>补充说明（可直接粘 JD；用于指导 AI 评估）</label>
      <textarea name="note" placeholder="例如：优先有从0→1平台建设经验；避免频繁跳槽。"></textarea>
    </div>

    <div class="card">
      <h3>上传候选集（支持多文件，单个或 ZIP 包）</h3>
      <div class="filebox">
        <input id="files" type="file" name="files" multiple required />
        <button class="btn" type="button" onclick="clearFiles()">清空已选文件</button>
        <span class="muted" id="hint">尚未选择文件</span>
      </div>
      <small>建议每次 20~30 份/包；免费实例空闲会休眠，首次请求可能较慢。</small>
    </div>

    <div class="card actions">
      <button class="btn" type="submit">开始分析（生成 Excel）</button>
      <small>提交后会跳转到“实时报告”，边分析边输出；掉线可点“继续（断点续跑）”。</small>
    </div>
  </form>
</div>

<script>
const input = document.getElementById('files');
const hint  = document.getElementById('hint');
function renderCount(){
  if(!input.files || input.files.length===0){
    hint.textContent = "尚未选择文件";
  }else{
    let total=0; for(const f of input.files){ total += f.size; }
    const size = total<1024? (total+"B") : total<1048576? (Math.round(total/102.4)/10+"KB") : (Math.round(total/104857.6)/10+"MB");
    hint.textContent = "已选择 "+input.files.length+" 个文件，共约 "+size;
  }
}
input.addEventListener('change', renderCount);
function clearFiles(){
  input.value=""; renderCount();
}
renderCount();
</script>
</body></html>
"""

EVENTS_HTML = r"""
<!doctype html><html lang="zh"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{{title}} · 实时报告</title>
<style>
:root{--bg:#0b0f14;--card:#121824;--line:#1e2633;--muted:#93a1b7;--fg:#e2e8f2;--blue:#2563eb}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial}
.wrap{max-width:980px;margin:24px auto;padding:0 16px}
h1{font-size:20px;margin:4px 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
.btn{background:var(--blue);color:#fff;border:0;border-radius:10px;padding:10px 14px;cursor:pointer;font-weight:600}
.toolbar{display:flex;gap:10px;align-items:center;justify-content:flex-end;margin-top:4px}
pre{margin:0;white-space:pre-wrap;word-break:break-word}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace}
</style></head><body>
<div class="wrap">
  <h1>任务 {{rid}} · 实时报告</h1>
  <div class="toolbar">
    <a class="btn" href="/resume/{{rid}}">继续（断点续跑）</a>
    <a class="btn" href="/">返回</a>
  </div>

  <div class="card"><pre id="log" class="mono">连接已建立…</pre></div>

  <div class="card toolbar">
    <a class="btn" href="/excel/{{rid}}">下载 Excel</a>
    <a class="btn" href="/leaderboard/{{rid}}">查看榜单</a>
    <small id="tip"></small>
  </div>
</div>

<script>
const rid = "{{rid}}";
const log = document.getElementById('log');
const tip = document.getElementById('tip');
const es = new EventSource("/stream/"+rid);
es.onmessage = (e)=>{
  if(e.data==="[DONE]"){ tip.textContent="已完成，可下载或反复下载。"; es.close(); return; }
  log.textContent += "\\n" + e.data.replaceAll("\\n","\\n");
  log.scrollTop = log.scrollHeight;
}
es.onerror = ()=>{ tip.textContent="连接中断，稍后自动重试或手动刷新本页。"; }
</script>
</body></html>
"""

# ------------------ ROUTES ------------------

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

@app.post("/process")
def process():
    role = (request.form.get("role") or "").strip()
    if not role:
        return "职位名称必填", 400
    direction = (request.form.get("dir") or "").strip()
    must = (request.form.get("must") or "").strip()
    nice = (request.form.get("nice") or "").strip()
    note = (request.form.get("note") or "").strip()

    rid = rid_now(f"{role}_{direction}" if direction else role).replace(" ", "_")
    rid = re.sub(r"[^\w\u4e00-\u9fa5_]+","_", rid)

    # 保存上传
    files = request.files.getlist("files")
    if not files:
        return "请至少选择一个文件", 400

    run_dir = os.path.join(DATA_DIR, rid)
    os.makedirs(run_dir, exist_ok=True)
    upload_dir = os.path.join(run_dir, "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    total = 0
    for fs in files:
        data = fs.read()
        total += len(data)
        if total > MAX_UPLOAD_MB*1024*1024:
            return f"超过限制：最多 {MAX_UPLOAD_MB} MB", 400
        with open(os.path.join(upload_dir, secure_name(fs.filename)), "wb") as f:
            f.write(data)

    with LOCK:
        JOBS[rid] = {
            "rid": rid,
            "name": f"{role} ({direction})" if direction else role,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "role": role, "dir": direction, "must": must, "nice": nice, "note": note,
            "dir_path": run_dir, "uploads": upload_dir,
            "result_excel": os.path.join(run_dir, f"{rid}.xlsx"),
            "state": "queued"
        }
        STREAMS[rid] = Queue()

    # 异步执行
    Thread(target=_runner, args=(rid,), daemon=True).start()
    return redirect(url_for("events", rid=rid))

@app.get("/events/<rid>")
def events(rid):
    info = JOBS.get(rid)
    if not info:
        return "任务不存在", 404
    return render_template_string(EVENTS_HTML, rid=rid, title=info["name"])

@app.get("/stream/<rid>")
def stream(rid):
    def gen():
        q = STREAMS.get(rid)
        if not q:
            yield "data: [DONE]\n\n"; return
        # 初始提示
        yield f"data: 连接已建立\\n\n\n"
        while True:
            msg = q.get()
            if msg == "[DONE]":
                yield "data: [DONE]\n\n"; break
            safe = str(msg).replace("\r"," ").replace("\n","\n")
            yield f"data: {safe}\n\n"
    return Response(gen(), headers={"Content-Type":"text/event-stream",
                                    "Cache-Control":"no-cache",
                                    "X-Accel-Buffering":"no",
                                    "Connection":"keep-alive"})

@app.get("/resume/<rid>")
def resume(rid):
    """断点续跑：如果没有结果，重新启动 worker。"""
    info = JOBS.get(rid)
    if not info:
        return redirect(url_for("index"))
    if info.get("state") not in ("running","queued"):
        # 再跑一次（例如新增文件/失败后重试）
        with LOCK:
            info["state"] = "queued"
            STREAMS[rid] = Queue()
        Thread(target=_runner, args=(rid,), daemon=True).start()
    return redirect(url_for("events", rid=rid))

@app.get("/excel/<rid>")
def excel(rid):
    info = JOBS.get(rid)
    if not info: return "任务不存在", 404
    path = info.get("result_excel")
    if not path or not os.path.exists(path):
        return "还没有结果或尚未完成。", 404
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))

@app.get("/leaderboard/<rid>")
def leaderboard(rid):
    """简单把 Excel 转为 txt 展示重点候选（A+/A）"""
    info = JOBS.get(rid)
    if not info: return "任务不存在", 404
    xlsx = info.get("result_excel")
    if not xlsx or not os.path.exists(xlsx):
        return "还没有结果或尚未完成。", 404
    try:
        import openpyxl
        wb = openpyxl.load_workbook(xlsx)
        ws = wb.active
        # 假设列顺序固定，等级在第4列
        out = ["重点候选（A+/A）：" ]
        for i,row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            lvl = (row[3] or "").strip()
            if lvl in ("A+","A"):
                out.append(f"- {row[0]}｜{row[1]}｜{row[2]}｜{lvl}")
        if len(out)==1: out.append("暂无")
        return "<pre style='white-space:pre-wrap'>"+ "\n".join(out) +"</pre>"
    except Exception as e:
        return f"读取失败：{e}", 500

# ------------------ WORKER ------------------

def _runner(rid: str):
    info = JOBS.get(rid)
    if not info: return
    _push(rid, "▶ 开始处理…")
    with LOCK: info["state"]="running"
    try:
        run_dir = info["dir_path"]; updir = info["uploads"]
        work_dir = os.path.join(run_dir, "workspace")
        os.makedirs(work_dir, exist_ok=True)

        # 1) 解压 & 收集所有可解析文件
        all_files = collect_inputs(updir, work_dir, rid)

        if not all_files:
            _push(rid, "未找到可解析的文件。"); finish_empty_excel(info); _done(rid); return

        # 2) 解析文本
        texts: List[Tuple[str,str]] = []
        for f in all_files:
            try:
                txt = read_text(f)
                if txt: texts.append((os.path.basename(f), txt))
                _push(rid, f"[读取] {os.path.basename(f)}")
            except Exception as e:
                _push(rid, f"[跳过] {os.path.basename(f)} ｜ 读取失败：{e}")

        if not texts:
            _push(rid, "没有可用文本。"); finish_empty_excel(info); _done(rid); return

        # 3) 去重（基于姓名+邮箱）
        unique: Dict[str, Tuple[str,str]] = {}
        for fn, tx in texts:
            nm = guess_name(tx)
            em = guess_email(tx)
            key = f"{(nm or '').strip().lower()}__{(em or '').strip().lower()}"
            if key not in unique:
                unique[key] = (fn, tx)
        items = list(unique.values())
        _push(rid, f"去重后候选：{len(items)} ✓")

        # 4) LLM 批量评估
        excel_rows: List[List[Any]] = []
        jd_ctx = build_jd_context(info)   # 把岗位需求合并
        _push(rid, "调用模型评估…")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = { ex.submit(eval_one, rid, jd_ctx, fn, tx): fn for (fn,tx) in items }
            for fut in as_completed(futs):
                fn = futs[fut]
                try:
                    row = fut.result()
                    if row: excel_rows.append(row)
                except Exception as e:
                    _push(rid, f"[失败] {fn} ｜ {e}")

        if not excel_rows:
            _push(rid, "模型未返回有效结构化结果。"); finish_empty_excel(info); _done(rid); return

        # 5) 生成 Excel
        build_excel(info["result_excel"], excel_rows)
        _push(rid, f"完成，共 {len(excel_rows)} 人。")
    except Exception as e:
        logging.exception(e)
        _push(rid, f"异常：{e}\n{traceback.format_exc()}")
    finally:
        with LOCK: info["state"]="finished"
        _done(rid)

# ------------------ PARSERS ------------------

ALLOWED = {".txt",".pdf",".html",".htm",".docx",".csv"}

def secure_name(name: str) -> str:
    name = os.path.basename(name).strip().replace("\\","_").replace("/","_")
    if not name: name = "file"
    return name

def collect_inputs(upload_dir: str, work_dir: str, rid: str) -> List[str]:
    paths: List[str] = []
    for root,_,files in os.walk(upload_dir):
        for f in files:
            paths.append(os.path.join(root,f))
    all_out: List[str] = []
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        if ext == ".zip":
            try:
                with zipfile.ZipFile(p) as z:
                    for nm in z.namelist():
                        if nm.endswith("/"): continue
                        ext2 = os.path.splitext(nm)[1].lower()
                        if ext2 in ALLOWED:
                            outp = os.path.join(work_dir, secure_name(nm))
                            os.makedirs(os.path.dirname(outp), exist_ok=True)
                            with z.open(nm) as src, open(outp,"wb") as dst:
                                dst.write(src.read())
                            all_out.append(outp)
                            _push(rid, f"[解压] {nm}")
            except Exception as e:
                _push(rid, f"[跳过ZIP] {os.path.basename(p)} ｜ {e}")
        elif ext in ALLOWED:
            outp = os.path.join(work_dir, secure_name(os.path.basename(p)))
            if os.path.abspath(outp) != os.path.abspath(p):
                with open(p,"rb") as src, open(outp,"wb") as dst:
                    dst.write(src.read())
            all_out.append(outp)
        else:
            _push(rid, f"[忽略] {os.path.basename(p)}")
    return all_out

def read_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".txt":
        return open(path,"r",encoding="utf-8",errors="ignore").read()
    if ext == ".csv":
        with open(path,"r",encoding="utf-8",errors="ignore") as f:
            return f.read()
    if ext == ".pdf" and pdf_extract_text:
        return pdf_extract_text(path)
    if ext == ".docx" and DocxDocument:
        doc = DocxDocument(path)
        return "\n".join(p.text for p in doc.paragraphs)
    if ext in (".html",".htm") and BeautifulSoup:
        html = open(path,"r",encoding="utf-8",errors="ignore").read()
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text(" ")
    # fallback
    return open(path,"rb").read().decode("utf-8",errors="ignore")

# ------------------ LLM ------------------

def build_jd_context(info: Dict[str,Any]) -> str:
    parts = []
    if info.get("role"): parts.append(f"职位：{info['role']}")
    if info.get("dir"):  parts.append(f"方向：{info['dir']}")
    if info.get("must"): parts.append(f"必须：{info['must']}")
    if info.get("nice"): parts.append(f"加分：{info['nice']}")
    if info.get("note"): parts.append(f"补充说明（JD）：{info['note']}")
    return "\n".join(parts)

JSON_SCHEMA = {
  "type":"object",
  "properties":{
    "name":{"type":"string"},
    "company":{"type":"string"},
    "title":{"type":"string"},
    "level":{"type":"string","description":"候选评分：A+/A/B/C"},
    "email":{"type":"string"},
    "age":{"type":"string","description":"年龄估算。缺失则填“不详”"},
    "location":{"type":"string"},
    "fit_summary":{"type":"string","description":"契合摘要：为什么符合"},
    "risks":{"type":"string","description":"风险点：为什么不匹配或注意事项"},
    "tags":{"type":"string","description":"逗号分隔"},
    "timeline_remark":{"type":"string","description":"时间线履历概述。格式：YYYY-YYYY 就读/就职于 …，专业/职位，负责…；逐条分行"}
  },
  "required":["name","level","timeline_remark"]
}

SYSTEM_PROMPT = (
"你是一名严谨的招聘分析助手。请把输入的候选人简历与岗位要求进行比对，"
"输出**严格 JSON 格式**（不可出现多余文本、注释或 Markdown），字段结构见 schema。"
"重要规则："
"1) 评分仅可为 A+ / A / B / C；"
"2) 年龄估算：尽量根据教育经历中的本科入学/毕业年份推算（18 岁入学、22 岁毕业），无法推算时写“不详”；"
"3) timeline_remark 必须是时间线（从教育到工作），每行一条，形如："
"   2010-2014 就读于 XX 大学，计算机，本科；"
"   2015-2017 就职于 XX 公司，研发工程师，负责分布式系统；"
"   2018-至今 就职于 XX 公司，平台架构师，负责 K8s 与 DevOps；"
"4) fit_summary（契合摘要）说明为什么匹配；risks（风险点）说明不足与注意点；"
"5) 如果邮箱未出现，可尝试从文本中提取，若无则留空；"
"6) 若公司/职位/地点不明显，可留空，但不要编造。"
)

def llm_chat(messages: List[Dict[str,str]], retries: int = 2, timeout: int = 120) -> str:
    """OpenAI-compatible Chat Completions"""
    if not MODEL_BASE_URL or not MODEL_API_KEY:
        raise RuntimeError("未配置模型：请设置 MODEL_BASE_URL / MODEL_API_KEY / MODEL_NAME")
    url = f"{MODEL_BASE_URL}/v1/chat/completions"  # 注意避免 /v1/v1
    headers = {
        "Authorization": f"Bearer {MODEL_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": 0.2
    }
    last_err = None
    for _ in range(retries+1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code!=200:
                raise RuntimeError(f"{resp.status_code} {resp.text}")
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            last_err = e
            time.sleep(1.5)
    raise last_err

def eval_one(rid: str, jd_ctx: str, filename: str, resume_text: str) -> Optional[List[Any]]:
    _push(rid, f"[评估] {filename}")
    # 提示：把岗位/JD 与 简历文本统一输入
    user_prompt = (
        "【岗位要求】\n" + jd_ctx + "\n\n"
        "【候选人简历（原文）】\n" + resume_text[:18000] + "\n\n"
        "请严格依据 schema 输出 JSON，不要多余说明。\n"
        f"JSON schema：{json.dumps(JSON_SCHEMA, ensure_ascii=False)}"
    )
    try:
        content = llm_chat([
            {"role":"system","content":SYSTEM_PROMPT},
            {"role":"user","content":user_prompt}
        ])
    except Exception as e:
        logging.warning("LLM call failed: %s", e)
        _push(rid, f"[失败] 模型调用：{e}")
        return None

    # 解析 JSON
    js = None
    try:
        m = re.search(r"\{[\s\S]*\}$", content.strip())
        js = json.loads(m.group(0) if m else content)
    except Exception:
        # 再试一次：提示只返回 JSON
        try:
            content2 = llm_chat([
                {"role":"system","content":SYSTEM_PROMPT},
                {"role":"user","content": user_prompt + "\n\n务必只返回 JSON。"}
            ])
            js = json.loads(re.search(r"\{[\s\S]*\}$", content2.strip()).group(0))
        except Exception as e2:
            _push(rid, f"[失败] 解析JSON：{e2}")
            return None

    # 字段清洗 + 年龄兜底
    name = (js.get("name") or "").strip() or (guess_name(resume_text) or "")
    company = (js.get("company") or "").strip()
    title = (js.get("title") or "").strip()
    level = (js.get("level") or "").strip().upper()
    if level not in ("A+","A","B","C"): level = "C"
    email = (js.get("email") or "").strip() or (guess_email(resume_text) or "")
    location = (js.get("location") or "").strip()
    fit_summary = (js.get("fit_summary") or "").strip()
    risks = (js.get("risks") or "").strip()
    tags = (js.get("tags") or "").strip()
    remark = (js.get("timeline_remark") or "").strip()

    # 年龄：若模型没给或“不详”，我们再根据文本推算
    age = (js.get("age") or "").strip()
    if not age or age == "不详":
        est = estimate_age_from_text(resume_text)
        age = est or "不详"

    return [name, company, title, level, email, age, location, fit_summary, risks, tags, remark]

# ------------------ HEURISTICS ------------------

NAME_RE = re.compile(r"(?:姓名|Name)[:：]\s*([A-Za-z\u4e00-\u9fa5·\s]+)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

def guess_name(text: str) -> Optional[str]:
    m = NAME_RE.search(text)
    if m: return m.group(1).strip()
    # 简单策略：邮件前缀或顶部行
    lines = [x.strip() for x in text.splitlines() if x.strip()][:8]
    if lines:
        l0 = lines[0]
        # 中文名很短
        if 1<=len(l0)<=12 and re.search(r"[\u4e00-\u9fa5]", l0):
            return l0
    return None

def guess_email(text: str) -> Optional[str]:
    m = EMAIL_RE.search(text)
    return m.group(0) if m else None

YEAR_RE = re.compile(r"(20\d{2}|19\d{2})")
EDU_PAT = re.compile(r"(本科|Bachelor|B\.Sc|BSc|学士)", re.I)

def estimate_age_from_text(text: str) -> Optional[str]:
    # 尝试找“本科 20xx-20xx / 入学/毕业 年份”
    # 简化：找含“本科”同段落中的年份
    best_year = None
    for para in text.split("\n"):
        if EDU_PAT.search(para):
            ys = [int(x) for x in YEAR_RE.findall(para)]
            if ys:
                y = min(ys)  # 更可能是入学年
                if 1970 < y < 2035:
                    best_year = y; break
    # 或全局最早年份
    if not best_year:
        ys = [int(x) for x in YEAR_RE.findall(text)]
        if ys: best_year = min(ys)
    if not best_year: return None
    # 默认 18 入学
    birth = best_year - 18
    age = datetime.now().year - birth
    if 15 <= age <= 80:
        return str(age)
    return None

# ------------------ EXCEL ------------------

EXCEL_HEADERS = [
    "候选人", "目前公司", "目前职位", "匹配等级",
    "E-mail", "年龄预估", "目前所在地",
    "契合摘要", "风险点", "标签",
    "Remarks（时间线概述）"
]

def build_excel(path: str, rows: List[List[Any]]):
    if not Workbook: raise RuntimeError("缺少 openpyxl 依赖")
    wb = Workbook(); ws = wb.active
    ws.title = "Candidates"
    ws.append(EXCEL_HEADERS)
    for r in rows: ws.append(r)
    # 自适应列宽
    for col in range(1, len(EXCEL_HEADERS)+1):
        maxlen = max(len(str(ws.cell(row=i, column=col).value or "")) for i in range(1, ws.max_row+1))
        ws.column_dimensions[get_column_letter(col)].width = min(max(12, maxlen+2), 60)
    wb.save(path)

def finish_empty_excel(info: Dict[str,Any]):
    build_excel(info["result_excel"], [])

# ------------------ MISC ------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
