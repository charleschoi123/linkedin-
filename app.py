# app.py
# -*- coding: utf-8 -*-

import os, re, io, csv, sys, zipfile, json, time, uuid, hashlib, logging, shutil
from datetime import datetime
from typing import List, Dict, Any, Optional
from queue import Queue, Empty
from threading import Thread
from pathlib import Path

from flask import Flask, request, redirect, url_for, send_file, Response, render_template_string, abort

# ---------- 解析相关可选依赖 ----------
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


# ---------- 配置 & 初始化 ----------
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

LOG = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# 环境变量：模型调用
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "")
MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "")
MODEL_NAME = os.getenv("MODEL_NAME", "deepseek-chat")

# 并发与上传大小
CONCURRENCY = int(os.getenv("CONCURRENCY", os.getenv("MAX_WORKERS", "2")))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))

# 工作目录
BASE_DIR = Path("/opt/render/project/src") if os.getenv("RENDER") else Path(os.getcwd())
STORE = BASE_DIR / "runs"
STORE.mkdir(parents=True, exist_ok=True)

# 任务容器： rid -> {"q":Queue, "dir":Path, "meta":{...}, "status": "running/done/err"}
JOBS: Dict[str, Dict[str, Any]] = {}


# ---------- HTML 模板（首页 & 实时报告） ----------
INDEX_HTML = """
<!doctype html><html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Alsos Talent · 合规AI自动化寻访（MVP）</title>
<style>
  :root { --bg:#0b0f14; --card:#121824; --line:#1e2633; --muted:#93a1b7; --txt:#e3e8f2; --blue:#2563eb;}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial;background:var(--bg);color:var(--txt);}
  .wrap{max-width: 980px; margin: 28px auto; padding: 0 16px;}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin:14px 0;}
  h1{font-size:22px;margin:6px 0 14px}
  h3{margin:0 0 10px}
  label{display:block;color:#A9B4C6;margin:10px 0 6px}
  input[type=text], textarea{width:100%;background:#0b1018;color:#dbe4f0;border:1px solid #223044;border-radius:10px;padding:10px 12px;outline:none}
  textarea{min-height:100px}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .btn{background:var(--blue);color:#fff;border:none;padding:12px 16px;border-radius:12px;font-weight:600;cursor:pointer}
  .btn.ghost{background:#0c1524;border:1px solid var(--line)}
  small,.muted{color:var(--muted)}
  .files{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .pill{display:inline-block;padding:2px 8px;border:1px solid #223044;border-radius:999px;background:#0c1524;color:#B8C4D9;font-size:12px}
</style>
</head>
<body>
  <div class="wrap">
    <h1>Alsos Talent · 合规AI自动化寻访（MVP）</h1>

    <div class="card">
      <p class="muted">说明：本工具<strong>不做</strong>对 LinkedIn/猎聘 的自动点开或抓取；仅对你<strong>合规导出</strong>的 ZIP/PDF/HTML/CSV/文本做AI分析、排序并导出Excel。</p>
    </div>

    <form id="f" action="/process" method="post" enctype="multipart/form-data">

      <div class="card"><h3>上传候选集（支持多文件）</h3>
        <label>选择文件（.zip .pdf .html/.htm .docx .txt .csv）：</label>
        <div class="files">
          <input id="files" type="file" name="files" multiple required/>
          <button type="button" class="btn ghost" id="clearSel">清空已选</button>
          <span id="fileTips" class="muted"></span>
        </div>
        <small>可直接上传 Recruiter Lite 25人/包的 ZIP（一次多包）。免费实例空闲会休眠，首次请求会慢。</small>
      </div>

      <div class="card"><h3>岗位/筛选要求</h3>
        <div class="row">
          <div><label>职位名称（必填）</label><input type="text" name="role" id="role" placeholder="如：资深基础设施架构师" required/></div>
          <div><label>最低年限（选填）</label><input type="text" name="min_years" placeholder="如：8 或 10-15"/></div>
        </div>
        <div class="row">
          <div><label>Must-have 关键词（逗号分隔）</label><input type="text" name="must" placeholder="如：K8s, DevOps, 安全合规"/></div>
          <div><label>Nice-to-have 关键词（逗号分隔）</label><input type="text" name="nice" placeholder="如：HPC, 金融, 医药"/></div>
        </div>
        <div class="row">
          <div><label>地域/签证等限制（选填）</label><input type="text" name="location" placeholder="如：上海/苏州；英文流利"/></div>
          <div></div>
        </div>
        <label>补充说明（可粘贴 JD 关键要点）</label>
        <textarea name="note" placeholder="例如：优先有从0→1平台建设经验；避免频繁跳槽。"></textarea>
      </div>

      <div class="card">
        <button class="btn" type="submit">开始分析（生成Excel清单）</button>
        <small>提交后会跳到“实时报告”页面，边分析边输出。后端并发/模型等使用后端默认配置。</small>
      </div>
    </form>
  </div>

<script>
const files = document.getElementById("files");
const tips = document.getElementById("fileTips");
const clearBtn = document.getElementById("clearSel");
function showCount(){
  const n = files.files.length;
  tips.textContent = n ? ("已选择 " + n + " 个文件") : "";
}
files.addEventListener("change", showCount);
clearBtn.addEventListener("click", ()=>{ files.value=""; showCount(); });

document.getElementById("f").addEventListener("submit", (e)=>{
  const role = document.getElementById("role").value.trim();
  if(!role){ e.preventDefault(); alert("职位名称必填"); }
});
</script>

</body></html>
"""

EVENTS_HTML = """
<!doctype html><html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{{title}} · 实时报告</title>
<style>
  :root { --bg:#0b0f14; --card:#121824; --line:#1e2633; --muted:#93a1b7; --txt:#e3e8f2; --blue:#2563eb;}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial;background:var(--bg);color:var(--txt);}
  .wrap{max-width: 1100px; margin: 22px auto; padding: 0 16px;}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin:14px 0;}
  h2{font-size:20px;line-height:1.2;margin:0 12px 0 0}
  .row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:6px 0 4px}
  .btn{background:var(--blue);color:#fff;border:none;padding:10px 14px;border-radius:12px;font-weight:600;cursor:pointer}
  .btn.ghost{background:#0c1524;border:1px solid var(--line)}
  pre{white-space:pre-wrap;margin:0;font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace}
  .muted{color:var(--muted)}
</style>
</head>
<body>
  <div class="wrap">
    <div class="row">
      <h2>任务 {{title}} · 实时报告</h2>
      <a href="/resume/{{rid}}"><button class="btn">继续（断点续跑）</button></a>
      <a href="/"><button class="btn ghost">返回</button></a>
    </div>

    <div class="card"><pre id="log"></pre></div>

    <div class="card" id="doneZone" style="display:none">
      <div class="row">
        <a id="dl" href="#"><button class="btn">下载 Excel</button></a>
        <a id="csv" href="#"><button class="btn ghost">查看榜单</button></a>
        <span class="muted">完成后可反复下载。</span>
      </div>
    </div>
  </div>

<script>
const es = new EventSource("/stream/{{rid}}");
const log = document.getElementById("log");
const doneZone = document.getElementById("doneZone");
const dl = document.getElementById("dl");
const csv = document.getElementById("csv");

function appendLine(t){
  log.textContent += (log.textContent ? "\\n" : "") + t;
  log.scrollTop = log.scrollHeight;
}

es.onmessage = (e) => {
  const data = JSON.parse(e.data);
  if (data.type === "line") {
    appendLine(data.text);
  } else if (data.type === "done") {
    es.close();
    dl.href = "/download/{{rid}}?fmt=xlsx";
    csv.href = "/download/{{rid}}?fmt=csv";
    doneZone.style.display = "block";
    appendLine("\\n✅ 完成，共 " + data.total + " 人。");
  }
};
</script>
</body></html>
"""


# ---------- 小工具 ----------
def now_str():
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def safe_name(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    return re.sub(r"\s+", "_", s).strip("_")[:120] or "task"


def rid_for(role: str) -> str:
    return f"{safe_name(role)}_{now_str()}"


def to_int(s: str, default=0) -> int:
    try:
        return int(str(s).strip())
    except Exception:
        return default


def yield_sse_json(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ---------- LLM 调用 ----------
def call_llm(messages: List[Dict[str, str]], temperature: float = 0.2) -> str:
    """
    兼容 OpenAI 风格 /v1/chat/completions
    """
    if not (MODEL_BASE_URL and MODEL_API_KEY):
        # 没配置就返回空，让评分逻辑降级
        return ""

    import requests
    url = MODEL_BASE_URL.rstrip("/") + "/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {MODEL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": temperature,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    except Exception as e:
        LOG.warning("LLM call failed: %s", e)
        return ""


# ---------- 文本抽取 ----------
def extract_from_pdf(path: Path) -> str:
    if pdf_extract_text is None:
        return ""
    try:
        return pdf_extract_text(str(path))
    except Exception:
        return ""


def extract_from_docx(path: Path) -> str:
    if DocxDocument is None:
        return ""
    try:
        doc = DocxDocument(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception:
        return ""


def extract_from_html(path: Path) -> str:
    if BeautifulSoup is None:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
    try:
        html = path.read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(html, "lxml")
        return soup.get_text("\n", strip=True)
    except Exception:
        return ""


def extract_from_txt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def extract_from_csv(path: Path) -> str:
    # 简单拼列作为文本
    try:
        rows = []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f)
            for row in reader:
                rows.append(" | ".join(row))
        return "\n".join(rows)
    except Exception:
        return ""


def file_to_text(p: Path) -> str:
    suf = p.suffix.lower()
    if suf == ".pdf":
        return extract_from_pdf(p)
    if suf == ".docx":
        return extract_from_docx(p)
    if suf in [".html", ".htm"]:
        return extract_from_html(p)
    if suf == ".csv":
        return extract_from_csv(p)
    if suf in [".txt", ".log", ".md"]:
        return extract_from_txt(p)
    return ""


# ---------- 简单信息抽取 ----------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?\d[\d\- ]{8,}\d)")


def basic_candidate_from_text(text: str) -> Dict[str, Any]:
    """
    简化抽取：name 暂不强抽，取第一行较长的中文/英文词组作为名字候选
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    name = ""
    if lines:
        # 优先非包含“简历/Resume/CV”的首行
        for ln in lines[:8]:
            if len(ln) <= 40 and not re.search(r"简历|resume|curriculum|vitae|cv", ln, re.I):
                name = ln
                break
        if not name:
            name = lines[0][:40]

    email = (EMAIL_RE.search(text) or [None])[0]
    phone = (PHONE_RE.search(text) or [None])[0]

    return {
        "name": name or "",
        "email": email or "",
        "phone": phone or "",
        "raw": text[:12000],  # 控大小
    }


# ---------- 评分与概述 ----------
SCHEMA_PROMPT = """
你是资深技术招聘合伙人，请基于“岗位要求”和“候选人简历”，输出一个严格 JSON：

{
  "score": 0-100 的整数,
  "grade": "A+|A|B|C|D" 之一,
  "reason": "一句话解释为何打这个等级",
  "remark": "概要履历：教育经历（时间-学校-专业-学历）；工作经历（按时间倒序：公司-岗位-一句话职责/成果）"
}

评判要点：
- Must-have 必须满足，否则降级；
- Nice-to-have 满足可加分；
- 关注是否有从0→1或平台化、K8s/DevOps/安全合规等经验；避免频繁跳槽；
- 尽量补全 remark 中的时间、公司、岗位，如果文本里缺失则简述你能读到的关键点。
只返回 JSON，不要多余文字。
"""

def grade_by_score(s: int) -> str:
    if s >= 90: return "A+"
    if s >= 80: return "A"
    if s >= 70: return "B"
    if s >= 60: return "C"
    return "D"


def llm_score(role: str, must: str, nice: str, min_years: str, location: str, note: str, cand: Dict[str, Any]) -> Dict[str, Any]:
    # 没模型时降级启发式
    if not (MODEL_BASE_URL and MODEL_API_KEY):
        text = cand.get("raw","")
        score = 60
        for kw in [x.strip() for x in must.split(",") if x.strip()]:
            if re.search(re.escape(kw), text, re.I): score += 8
            else: score -= 10
        for kw in [x.strip() for x in nice.split(",") if x.strip()]:
            if re.search(re.escape(kw), text, re.I): score += 4
        score = max(0, min(100, score))
        return {
            "score": score,
            "grade": grade_by_score(score),
            "reason": "无模型降级打分（关键词启发式）",
            "remark": "",
        }

    job_req = f"职位：{role}\n最低年限：{min_years}\n地域/签证：{location}\nMust-have：{must}\nNice-to-have：{nice}\n补充：{note}"
    resume = cand.get("raw","")
    content = call_llm([
        {"role":"system","content":SCHEMA_PROMPT.strip()},
        {"role":"user","content": f"岗位要求：\n{job_req}\n\n候选人简历：\n{resume}"}
    ])
    # 解析 JSON
    try:
        data = json.loads(re.findall(r"\{.*\}", content, re.S)[0])
    except Exception:
        # 兜底
        s = 70
        data = {"score": s, "grade": grade_by_score(s), "reason": "模型未返回结构化JSON，兜底", "remark": ""}

    # 保障字段类型
    data["score"] = to_int(data.get("score", 0), 0)
    data["grade"] = str(data.get("grade") or grade_by_score(data["score"]))
    data["reason"] = str(data.get("reason",""))
    data["remark"] = str(data.get("remark",""))
    return data


# ---------- 导出 ----------
def write_csv_xlsx(result_rows: List[Dict[str, Any]], out_dir: Path, rid: str):
    import openpyxl
    import openpyxl.utils

    # CSV
    csv_path = out_dir / f"{rid}.csv"
    headers = ["name","email","phone","score","grade","reason","remark"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for r in result_rows:
            writer.writerow({k: r.get(k,"") for k in headers})

    # XLSX
    xlsx_path = out_dir / f"{rid}.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Ranking"
    ws.append(headers)
    for r in result_rows:
        ws.append([r.get(k,"") for k in headers])

    # 简单样式
    from openpyxl.styles import Font, Alignment
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = 20
    wb.save(str(xlsx_path))


# ---------- 核心：处理线程 ----------
def worker_process(rid: str):
    job = JOBS.get(rid)
    if not job:
        return
    q: Queue = job["q"]
    meta = job["meta"]
    wdir: Path = job["dir"]
    try:
        # 1) 列出所有文本文件
        files: List[Path] = []
        for p in wdir.glob("**/*"):
            if p.is_file() and p.suffix.lower() in [".pdf",".docx",".html",".htm",".txt",".csv"]:
                files.append(p)
        q.put({"type":"line","text": f"开始处理… 解析 {len(files)} 个文件"})

        # 2) 转文本 & 简单解析
        candidates: List[Dict[str,Any]] = []
        for idx, fp in enumerate(files, start=1):
            q.put({"type":"line","text": f"[提取 {idx}/{len(files)}] {fp.name}"})
            text = file_to_text(fp)
            if not text.strip():
                continue
            cand = basic_candidate_from_text(text)
            if not (cand.get("name") or cand.get("email") or cand.get("phone")):
                # 过于空的跳过
                continue
            candidates.append(cand)

        # 3) 去重（优先 email；否则 name+phone）
        seen = set()
        uniq: List[Dict[str,Any]] = []
        for c in candidates:
            key = c.get("email") or (c.get("name","") + "|" + (c.get("phone","")))
            if key and key not in seen:
                seen.add(key)
                uniq.append(c)
        q.put({"type":"line","text": f"去重后候选：{len(uniq)} 人"})

        # 4) 打分（分批并发）
        from concurrent.futures import ThreadPoolExecutor, as_completed
        role = meta["role"]; must = meta["must"]; nice = meta["nice"]
        min_years = meta["min_years"]; location = meta["location"]; note = meta["note"]

        results: List[Dict[str,Any]] = []
        total = len(uniq)
        if total == 0:
            job["status"] = "done"
            write_csv_xlsx([], wdir, rid)
            q.put({"type":"done","total":0})
            return

        def one_score(i_cand):
            i, cand = i_cand
            data = llm_score(role, must, nice, min_years, location, note, cand)
            r = {
                "name": cand.get("name",""),
                "email": cand.get("email",""),
                "phone": cand.get("phone",""),
                "score": data.get("score",0),
                "grade": data.get("grade",""),
                "reason": data.get("reason",""),
                "remark": data.get("remark",""),
            }
            return i, r

        batch = list(enumerate(uniq, start=1))
        with ThreadPoolExecutor(max_workers=max(1, CONCURRENCY)) as ex:
            futs = [ex.submit(one_score, x) for x in batch]
            for fut in as_completed(futs):
                i, r = fut.result()
                results.append(r)
                q.put({"type":"line","text": f"✅ [{i}/{total}] {r['name'] or '匿名'} ➜ {r['grade']} / {r['score']}；{r['email'] or ''}"})

        # 5) 排序导出
        results.sort(key=lambda x:(x.get("score",0), x.get("grade","")), reverse=True)
        write_csv_xlsx(results, wdir, rid)
        job["status"] = "done"
        q.put({"type":"done","total": total})

    except Exception as e:
        LOG.exception("worker error")
        job["status"] = "err"
        q.put({"type":"line","text": f"❌ 发生异常：{e}"})


# ---------- 路由 ----------
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/process", methods=["POST"])
def process():
    role = (request.form.get("role") or "").strip()
    if not role:
        return "职位名称必填", 400

    files = request.files.getlist("files")
    if not files:
        return "请至少选择一个文件", 400

    rid = rid_for(role)
    wdir = STORE / rid
    wdir.mkdir(parents=True, exist_ok=True)

    # 保存 & 解压 zip
    total_mb = 0.0
    for f in files:
        filename = safe_name(f.filename or "file")
        buf = f.read()
        total_mb += len(buf)/1024/1024
        if total_mb > MAX_UPLOAD_MB:
            return f"上传总大小超过限制（{MAX_UPLOAD_MB}MB）", 400
        p = wdir / filename
        with open(p, "wb") as fp:
            fp.write(buf)
        if p.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(p, "r") as zf:
                    zf.extractall(wdir / p.stem)
            except Exception:
                pass

    q = Queue()
    JOBS[rid] = {
        "q": q,
        "dir": wdir,
        "meta": {
            "role": role,
            "min_years": (request.form.get("min_years") or "").strip(),
            "must": (request.form.get("must") or "").strip(),
            "nice": (request.form.get("nice") or "").strip(),
            "location": (request.form.get("location") or "").strip(),
            "note": (request.form.get("note") or "").strip(),
        },
        "status": "running",
    }

    # 启线程
    Thread(target=worker_process, args=(rid,), daemon=True).start()

    return redirect(url_for("events", rid=rid))


@app.route("/events/<rid>")
def events(rid: str):
    job = JOBS.get(rid)
    if not job:
        return "任务不存在", 404
    title = rid
    return render_template_string(EVENTS_HTML, rid=rid, title=title)


@app.route("/stream/<rid>")
def stream(rid: str):
    job = JOBS.get(rid)
    if not job:
        return "Not Found", 404
    q: Queue = job["q"]

    def gen():
        # 严格 JSON：首条提示
        yield yield_sse_json({"type":"line","text":"连接已建立 ▶ 自动开始…"})
        while True:
            try:
                msg = q.get(timeout=60*30)
            except Empty:
                break
            # 后台可能 put 字符串，这里统一打包成 JSON
            if isinstance(msg, str):
                msg = {"type":"line","text": msg}
            yield yield_sse_json(msg)
            if isinstance(msg, dict) and msg.get("type") == "done":
                break

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(gen(), headers=headers)


@app.route("/resume/<rid>")
def resume(rid: str):
    """断点续跑：如果任务结束了就重新开一个同目录任务；否则直接跳转 events"""
    job = JOBS.get(rid)
    if not job:
        return redirect(url_for("index"))
    if job["status"] == "running":
        return redirect(url_for("events", rid=rid))

    # 复制 meta，重新跑
    role = job["meta"]["role"]
    new_rid = rid_for(role)
    new_dir = job["dir"]  # 直接复用老目录
    q = Queue()
    JOBS[new_rid] = {
        "q": q,
        "dir": new_dir,
        "meta": job["meta"],
        "status": "running",
    }
    Thread(target=worker_process, args=(new_rid,), daemon=True).start()
    return redirect(url_for("events", rid=new_rid))


@app.route("/download/<rid>")
def download(rid: str):
    fmt = (request.args.get("fmt") or "xlsx").lower()
    p = (STORE / rid / f"{rid}.{fmt}")
    if not p.exists():
        # 兼容复用目录的情况（resume 后 rid 变化）
        # 尝试找目录下的最新 xlsx/csv
        cand = sorted((STORE / rid).glob(f"*.{fmt}"), key=lambda x: x.stat().st_mtime, reverse=True)
        if cand:
            p = cand[0]
    if not p.exists():
        return "文件尚未生成", 404
    return send_file(str(p), as_attachment=True, download_name=p.name)


# ---------- 健康检查 ----------
@app.route("/healthz")
def healthz():
    return "ok", 200


# ---------- 本地启动 ----------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
