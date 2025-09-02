# -*- coding: utf-8 -*-
import os, io, re, json, zipfile, time, uuid, logging, csv, html
from datetime import datetime
from typing import List, Dict, Any, Optional
from queue import Queue
from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, request, Response, render_template_string, send_file, redirect, url_for

# ===== 可选解析依赖（存在则用，不在也能跑） =====
try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except Exception:
    pdf_extract_text = None

try:
    import docx  # python-docx
except Exception:
    docx = None

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

try:
    import openpyxl
    from openpyxl import Workbook
except Exception:
    openpyxl = None
    Workbook = None

import requests

# ======================== 基本配置 ========================
app = Flask(__name__)

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "https://api.deepseek.com/v1")
MODEL_API_KEY  = os.getenv("MODEL_API_KEY", "")
MODEL_NAME     = os.getenv("MODEL_NAME", "deepseek-chat")
CONCURRENCY    = int(os.getenv("CONCURRENCY", "2"))

# Render 免费实例常见超时，适当延长
REQUEST_TIMEOUT = 60

# 任务仓库（内存）
JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = Lock()

# ======================== 简单页面（和你原来的风格保持一致） ========================
INDEX_HTML = """
<!doctype html><html lang="zh"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>linkedin-批量简历分析</title>
<style>
body{background:#0b0f14;color:#e3e8f2;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial;margin:0}
.wrap{max-width:1100px;margin:32px auto;padding:0 16px}
h1{font-size:20px;margin:0 0 16px}
.card{background:#111827;border:1px solid #1f2937;border-radius:14px;padding:18px;margin:16px 0}
label{display:block;margin:8px 0 6px;color:#a5b4c3}
input[type=text],textarea{width:100%;background:#0b1018;border:1px solid #223044;color:#dbe4f0;border-radius:10px;padding:10px}
.btn{background:#2563eb;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:600;cursor:pointer}
small{color:#93a1b7}
a{color:#7aa0ff;text-decoration:none}
</style></head><body><div class="wrap">
  <h1>linkedin-批量简历分析</h1>
  <div class="card">
    <form action="/process" method="post" enctype="multipart/form-data">
      <label>职位名称（必填）</label>
      <input type="text" name="role" placeholder="如：资深基础设施架构师" required />
      <label>方向（可选）</label>
      <input type="text" name="direction" placeholder="如：Infra / SRE / 医疗IT" />
      <label>上传文件（单个或ZIP，可多选）</label>
      <input type="file" name="files" multiple required />
      <div style="margin-top:8px"><button class="btn" type="submit">开始分析（生成Excel清单）</button></div>
      <small>说明：仅对你合规导出的 ZIP/PDF/HTML/DOCX/TXT 做本地解析 + 模型评估，并实时输出日志。</small>
    </form>
  </div>
  {% if jobs %}
  <div class="card">
    <h3>历史任务</h3>
    <ul>
      {% for rid, info in jobs %}
        <li><a href="/events/{{rid}}">继续 / 查看：{{info['name']}}</a>（{{info['created']}}）</li>
      {% endfor %}
    </ul>
  </div>
  {% endif %}
</div></body></html>
"""

EVENTS_HTML = """
<!doctype html><html lang="zh"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>任务 {{name}} · 实时报告</title>
<style>
body{background:#0b0f14;color:#e3e8f2;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial;margin:0}
.wrap{max-width:1180px;margin:18px auto;padding:0 16px}
h1{font-size:18px;margin:0 0 12px}
.area{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:14px;white-space:pre-wrap}
.btn{background:#2563eb;color:#fff;border:0;border-radius:10px;padding:8px 12px;font-weight:600;cursor:pointer;margin-right:10px}
.box{display:flex;gap:12px;margin-top:14px;align-items:center}
</style></head><body><div class="wrap">
  <h1>任务 {{name}} · 实时报告
    <a class="btn" href="/resume/{{rid}}">继续（断点续跑）</a>
    <a class="btn" href="/">返回</a>
  </h1>
  <div class="area" id="log">连接已建立…</div>
  <div class="box">
    <a class="btn" href="/download/{{rid}}">下载 Excel</a>
    <a class="btn" href="/ranking/{{rid}}">查看榜单</a>
    <small>连接中断，稍后自动重试或手动刷新本页。</small>
  </div>
<script>
function connect(){
  var es = new EventSource("/stream/{{rid}}");
  es.onmessage = function(e){
    const el = document.getElementById('log');
    el.textContent += (e.data || "") + "\\n";
    el.scrollTop = el.scrollHeight;
  }
  es.onerror = function(){ setTimeout(()=>{location.reload()}, 4000) }
}
connect()
</script>
</div></body></html>
"""

# ======================== 工具函数 ========================

def safe_text(x: Optional[str]) -> str:
    return x if isinstance(x, str) else ""

def read_pdf(fp: io.BytesIO) -> str:
    if pdf_extract_text:
        try:
            return pdf_extract_text(fp)
        except Exception:
            fp.seek(0)
    # 兜底：尽量返回空字符串，而不是抛异常
    return ""

def read_docx(fp: io.BytesIO) -> str:
    if not docx:
        return ""
    try:
        f = io.BytesIO(fp.read())
        document = docx.Document(f)
        return "\n".join([p.text for p in document.paragraphs])
    except Exception:
        return ""

def read_html(fp: io.BytesIO) -> str:
    if not BeautifulSoup:
        try:
            return fp.read().decode("utf-8", "ignore")
        except Exception:
            return ""
    try:
        soup = BeautifulSoup(fp.read(), "html.parser")
        return soup.get_text(separator="\n")
    except Exception:
        return ""

def read_txt(fp: io.BytesIO) -> str:
    try:
        return fp.read().decode("utf-8", "ignore")
    except Exception:
        return ""

def extract_from_file(filename: str, data: bytes) -> str:
    name = filename.lower()
    fp = io.BytesIO(data)
    if name.endswith(".pdf"):
        return read_pdf(fp)
    if name.endswith(".docx"):
        return read_docx(fp)
    if name.endswith(".htm") or name.endswith(".html"):
        return read_html(fp)
    if name.endswith(".txt"):
        return read_txt(fp)
    # 其他未知类型：按文本尝试
    return read_txt(fp)

# 仅提取**候选人姓名**（不要文件后缀/文件夹等）
# 你的文件常见形式：`Zhang_San_XXXXXXXX.pdf`，我们优先取前两个 token 作为姓名
def prettify_name(file_path: str) -> str:
    base = os.path.basename(file_path)
    base = re.sub(r"\.[A-Za-z0-9]+$", "", base)     # 去后缀
    parts = re.split(r"[_\-\s]+", base)
    # 经验：很多导出的文件前两段是姓名（英文）
    if len(parts) >= 2 and all(p and p[0].isalpha() for p in parts[:2]):
        return f"{parts[0]} {parts[1]}"
    # 如果包含中文姓名，取中文连续2-4字
    m = re.search(r"[\u4e00-\u9fa5]{2,4}", base)
    if m:
        return m.group(0)
    # 否则退化到第一段
    return parts[0] if parts else base

# ======================== 模型调用 ========================

PROMPT_SYS = """你是一名资深技术招聘顾问。请基于输入的“候选人原始简历文本”和“岗位信息”进行结构化评估。
**必须**返回 JSON，字段如下（不要多余字段）：
{
  "name": "候选人姓名（尽量从文本或文件名推断）",
  "grade": "A+ / A / B / C 中之一",
  "score": 0-100 的整数,
  "email": "如解析失败则空字符串",
  "phone": "如解析失败则空字符串",
  "fit_summary": "3-5条要点，说明契合点，聚焦岗位核心要求",
  "risks": "2-4条要点，说明风险/不足/不匹配之处",
  "education_timeline": [
    {"from":"YYYY","to":"YYYY/或今","school":"", "major":"", "degree":""}
  ],
  "work_timeline": [
    {"from":"YYYY","to":"YYYY/或今","company":"", "title":"", "one_line_responsibility":""}
  ],
  "remark": "严格按此格式拼接：例如：2005-2009 南京大学，化学，学士；2010-2015 强生，研究员，负责小分子合成。",
  "age_estimate": "若教育经历中含“本科入学/毕业年份”，以约 18 岁入学、22 岁毕业推算年龄；否则写“不详”。"
}
评分口径：以“资深基础设施/架构/云平台/安全/合规/实验室支持”等为主线；A+ 罕见，仅在核心要求高度匹配且经历突出时给出。
务必输出**合法 JSON**（不要 markdown、不要注释），不允许出现反引号。
"""

def llm_call(prompt: str) -> Dict[str, Any]:
    """
    统一的 LLM 调用，兼容 DeepSeek（OpenAI 格式）。
    """
    url = f"{MODEL_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {MODEL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": PROMPT_SYS},
            {"role": "user",   "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
        "stream": False
        # DeepSeek 的 base_url 若自带 /v1，这里不要再重复 /v1
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    content = data["choices"][0]["message"]["content"]
    # 清理潜在的包裹
    content = content.strip()
    # 只取 JSON
    # 防御：若模型误带了```json```包装，剥离
    content = re.sub(r"^```json|```$", "", content, flags=re.I|re.M).strip()
    try:
        return json.loads(content)
    except Exception:
        # 尝试从内容中提取 {} 块
        m = re.search(r"\{[\s\S]*\}$", content)
        if m:
            return json.loads(m.group(0))
        raise

def build_prompt(role: str, direction: str, person_name: str, raw_text: str) -> str:
    base = f"岗位名称：{role}\n方向：{direction}\n候选人（文件名推断）：{person_name}\n——\n候选人原始简历如下：\n{raw_text}\n"
    return base[:24000]   # 限制上下文，避免超长

# ======================== 任务执行 ========================

def push_log(q: Queue, msg: str):
    q.put(msg)

def analyze_one(
    rid: str, q: Queue, role: str, direction: str, filename: str, data: bytes
) -> Dict[str, Any]:
    # 解析文本
    name_shown = prettify_name(filename)
    push_log(q, f"[读取] {name_shown}")
    text = extract_from_file(filename, data)

    if not text.strip():
        push_log(q, f"[解析] {name_shown} ⛔ 文本为空，跳过")
        return {
            "name": name_shown, "grade": "C", "score": 0, "email": "", "phone": "",
            "fit_summary": "", "risks": "", "remark": "文本为空，无法评估", "age_estimate": "不详"
        }

    prompt = build_prompt(role, direction, name_shown, text)
    try:
        js = llm_call(prompt)
    except Exception as e:
        logging.warning("LLM 调用失败：%s", e)
        return {
            "name": name_shown, "grade": "B", "score": 70, "email": "", "phone": "",
            "fit_summary": "模型未返回结构化JSON", "risks": "", "remark": "→ B / 70; 模型未返回JSON，兜底", "age_estimate": "不详"
        }

    # 取字段（防御：缺失用兜底）
    name = safe_text(js.get("name")) or name_shown
    grade = safe_text(js.get("grade")) or "B"
    score = js.get("score") if isinstance(js.get("score"), int) else 70
    email = safe_text(js.get("email"))
    phone = safe_text(js.get("phone"))
    fit_summary = safe_text(js.get("fit_summary"))
    risks = safe_text(js.get("risks"))
    remark = safe_text(js.get("remark"))
    age_estimate = safe_text(js.get("age_estimate")) or "不详"

    # 统一 remark 末尾不带句号重复
    remark = remark.strip().rstrip("；;，,")

    return {
        "name": name, "grade": grade, "score": score, "email": email, "phone": phone,
        "fit_summary": fit_summary, "risks": risks, "remark": remark, "age_estimate": age_estimate
    }

def scan_uploads(files: List) -> List[Dict[str, Any]]:
    """
    将上传的多个文件统一拆解成 [{filename, data}, ...]
    支持 zip：会解压一层取其中 pdf/docx/html/txt
    """
    out = []
    for f in files:
        fn = f.filename
        if not fn:
            continue
        b = f.read()
        if fn.lower().endswith(".zip"):
            try:
                zf = zipfile.ZipFile(io.BytesIO(b))
                for name in zf.namelist():
                    if name.endswith("/") or "__MACOSX" in name:
                        continue
                    low = name.lower()
                    if low.endswith((".pdf", ".docx", ".htm", ".html", ".txt")):
                        out.append({"filename": os.path.basename(name), "data": zf.read(name)})
            except Exception:
                # 非法 zip 时，按普通文件处理
                out.append({"filename": fn, "data": b})
        else:
            out.append({"filename": fn, "data": b})
    return out

def write_excel(rows: List[Dict[str, Any]]) -> bytes:
    """
    导出 Excel：
    姓名 | 等级 | 分数 | E-mail | 手机 | 契合点 | 风险点 | 年龄估算 | remark
    """
    if not Workbook:
        # 无 openpyxl 时，导出 csv 也能用
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["姓名", "等级", "分数", "E-mail", "手机", "契合点", "风险点", "年龄估算", "remark"])
        for r in rows:
            w.writerow([r["name"], r["grade"], r["score"], r["email"], r["phone"], r["fit_summary"], r["risks"], r["age_estimate"], r["remark"]])
        return buf.getvalue().encode("utf-8-sig")

    wb = Workbook()
    ws = wb.active
    ws.title = "结果"
    ws.append(["姓名", "等级", "分数", "E-mail", "手机", "契合点", "风险点", "年龄估算", "remark"])
    for r in rows:
        ws.append([
            r["name"], r["grade"], r["score"], r["email"], r["phone"],
            r["fit_summary"], r["risks"], r["age_estimate"], r["remark"]
        ])
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out.read()

def run_job(rid: str):
    with JOBS_LOCK:
        job = JOBS[rid]
    q: Queue = job["q"]
    push_log(q, "连接已建立\\n▶ 开始处理…")

    files = job["files"]
    role = job["role"]
    direction = job["direction"]

    # 去重（按 base 名）
    seen = set()
    uniq = []
    for obj in files:
        base = os.path.basename(obj["filename"])
        if base in seen:
            continue
        seen.add(base)
        uniq.append(obj)
    files = uniq
    push_log(q, f"解析 {len(files)} 个文件")

    results = []
    errors = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = []
        for obj in files:
            futs.append(ex.submit(analyze_one, rid, q, role, direction, obj["filename"], obj["data"]))
        for fu in as_completed(futs):
            try:
                res = fu.result()
                results.append(res)
                push_log(q, f"✅ {res['name']} ➜ {res['grade']} / {res['score']};")
            except Exception as e:
                errors += 1
                push_log(q, f"⛔ 评估失败：{e}")

    # 排序：按分数 desc
    results.sort(key=lambda x: (x.get("score", 0), x.get("grade", "")), reverse=True)
    job["results"] = results
    job["done"] = True

    push_log(q, f"完成，共 {len(results)} 人。")
    q.put("[DONE]")

# ======================== 路由 ========================

@app.route("/", methods=["GET"])
def index():
    with JOBS_LOCK:
        jobs = [(rid, {"name": info["name"], "created": info["created"]}) for rid, info in JOBS.items()]
        jobs = sorted(jobs, key=lambda x: x[1]["created"], reverse=True)[:15]
    return render_template_string(INDEX_HTML, jobs=jobs)

@app.route("/process", methods=["POST"])
def process():
    role = request.form.get("role", "").strip()
    direction = request.form.get("direction", "").strip()
    if not role:
        return "职位名称必填", 400

    files = request.files.getlist("files")
    if not files:
        return "请上传文件", 400

    file_objs = scan_uploads(files)
    if not file_objs:
        return "未找到可解析的文件", 400

    rid = uuid.uuid4().hex[:8]
    job_name = f"{role}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    q = Queue()

    with JOBS_LOCK:
        JOBS[rid] = {
            "name": job_name,
            "created": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "role": role,
            "direction": direction,
            "files": file_objs,
            "q": q,
            "done": False,
            "results": []
        }

    # 异步线程跑
    t = Thread(target=run_job, args=(rid,), daemon=True)
    t.start()

    return redirect(url_for("events", rid=rid))

@app.route("/events/<rid>", methods=["GET"])
def events(rid: str):
    with JOBS_LOCK:
        if rid not in JOBS:
            return "任务不存在", 404
        name = JOBS[rid]["name"]
    return render_template_string(EVENTS_HTML, rid=rid, name=name)

@app.route("/stream/<rid>")
def stream(rid: str):
    with JOBS_LOCK:
        if rid not in JOBS:
            return "任务不存在", 404
        q: Queue = JOBS[rid]["q"]

    def gen():
        # 首行友好提示
        yield f"data: 连接已建立\\n\\n"
        while True:
            msg = q.get()
            if msg == "[DONE]":
                yield f"data: 任务结束\\n\\n"
                break
            # 清理换行以适配 SSE
            safe = str(msg).replace("\\r", " ").replace("\\n", "\\n")
            yield f"data: {safe}\\n\\n"

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(gen(), headers=headers)

@app.route("/download/<rid>")
def download(rid: str):
    with JOBS_LOCK:
        if rid not in JOBS:
            return "任务不存在", 404
        results = JOBS[rid]["results"]
        done = JOBS[rid]["done"]
    if not results and not done:
        return "任务尚未完成", 400
    blob = write_excel(results)
    fname = f"{JOBS[rid]['name']}.xlsx" if Workbook else f"{JOBS[rid]['name']}.csv"
    return send_file(io.BytesIO(blob), as_attachment=True, download_name=fname)

@app.route("/ranking/<rid>")
def ranking(rid: str):
    with JOBS_LOCK:
        if rid not in JOBS:
            return "任务不存在", 404
        rows = JOBS[rid]["results"][:]
    # 简单文本榜单
    lines = []
    for i, r in enumerate(rows, 1):
        lines.append(f"{i:02d}. {r['name']}  → {r['grade']} / {r['score']}")
    return "<pre style='color:#e3e8f2;background:#0b0f14'>" + html.escape("\n".join(lines)) + "</pre>"

@app.route("/resume/<rid>")
def resume(rid: str):
    # “断点续跑”：如果还没 done，不做处理；若 done，则重新触发仅对“失败或空文本”的条目重跑（这里简单处理为整包重跑）
    with JOBS_LOCK:
        if rid not in JOBS:
            return "任务不存在", 404
        job = JOBS[rid]
    # 重新跑
    if job["done"]:
        job["done"] = False
        job["results"] = []
        t = Thread(target=run_job, args=(rid,), daemon=True)
        t.start()
    return redirect(url_for("events", rid=rid))

# ======================== 启动 ========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    # gthread + keep-alive 在 Render 表现更稳
    app.run(host="0.0.0.0", port=port)
