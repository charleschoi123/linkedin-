# app.py
# Alsos Talent · 合规AI自动化寻访（MVP）
# - 仅分析你合规导出的 ZIP/PDF/DOCX/HTML/TXT/CSV
# - 模型与并发走环境变量，UI 不暴露，便于商业化
# - 实时 SSE 流式报告、断点续跑、去重、A+/A/B/C 评分、Excel 导出
# - 任务目录命名：职位_方向_YYYYMMDD_HHMMSS

import os, io, re, json, zipfile, uuid, time, hashlib, logging, csv
from datetime import datetime
from typing import List, Dict, Any, Optional
from queue import Queue
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, request, Response, send_file, redirect, url_for, render_template_string

import requests
from bs4 import BeautifulSoup

# 可选解析器
try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except Exception:
    pdf_extract_text = None

try:
    import docx
except Exception:
    docx = None

from openpyxl import Workbook

# =========================
# 环境变量（隐藏给用户）
# =========================
MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "").rstrip("/")
MODEL_API_KEY  = os.getenv("MODEL_API_KEY", "")
MODEL_NAME     = os.getenv("MODEL_NAME", "deepseek-chat")
MAX_WORKERS    = int(os.getenv("CONCURRENCY", os.getenv("MAX_WORKERS", "2")))
MAX_UPLOAD_MB  = int(os.getenv("MAX_UPLOAD_MB", "200"))

assert MODEL_API_KEY and MODEL_BASE_URL, "请配置环境变量 MODEL_API_KEY / MODEL_BASE_URL"

# =========================
# Flask 基础
# =========================
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

DATA_DIR = os.path.abspath("data")
os.makedirs(DATA_DIR, exist_ok=True)

JOBS: Dict[str, Dict[str, Any]] = {}   # rid -> {q, created, name, folder, files, results, done, params}

# =========================
# 前端：极简表单（支持文件 x 删除）
# =========================
INDEX_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Alsos Talent · 合规AI自动化寻访（MVP）</title>
  <style>
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial;margin:0;background:#0b0f14;color:#e3e8f2}
    .wrap{max-width:980px;margin:28px auto;padding:0 16px}
    .card{background:#121824;border:1px solid #1e2633;border-radius:16px;padding:20px;margin-bottom:18px}
    label{display:block;font-size:14px;color:#A9B4C6;margin:8px 0 6px}
    input[type="text"],textarea{width:100%;background:#0b1018;color:#dbe4f0;border:1px solid #223044;border-radius:10px;padding:10px 12px;outline:none}
    textarea{min-height:120px}
    .row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
    .btn{background:#2563eb;color:#fff;border:none;padding:12px 16px;border-radius:12px;cursor:pointer;font-weight:600}
    small{color:#93a1b7}
    .files{margin-top:8px}
    .file-pill{display:inline-flex;align-items:center;gap:6px;margin:4px 6px 0 0;padding:4px 8px;border-radius:999px;background:#0c1320;border:1px solid #223044;font-size:12px}
    .file-pill button{background:transparent;color:#9db3ff;border:none;cursor:pointer}
    .error{color:#ff8181;margin-top:6px}
    a{color:#89aaff;text-decoration:none}
  </style>
</head>
<body>
<div class="wrap">
  <h2>linkedin-批量简历分析（合规版）</h2>

  <form action="/process" method="post" enctype="multipart/form-data" onsubmit="return guardSubmit();">
    <div class="card">
      <h3>上传候选集（支持多文件）</h3>
      <label>选择文件（.zip .pdf .html/.htm .docx .txt .csv）：</label>
      <input id="fileInput" type="file" name="files" multiple />
      <div id="fileList" class="files"></div>
      <small>可直接上传 Recruiter Lite 的 25人/包 ZIP（可多包）。若传错，可在下方‘×’删除重选。</small>
      <div id="fileErr" class="error" style="display:none;">请至少选择 1 个文件</div>
    </div>

    <div class="card">
      <h3>岗位/筛选要求</h3>
      <div class="row">
        <div>
          <label>职位名称（必填）</label>
          <input type="text" name="role" id="role" placeholder="如：资深基础设施架构师" required />
        </div>
        <div>
          <label>方向（选填）</label>
          <input type="text" name="direction" placeholder="如：Infra / SRE / 医疗IT" />
        </div>
      </div>
      <div class="row">
        <div>
          <label>最低年限（选填）</label>
          <input type="text" name="min_years" placeholder="如：8 或 10-15" />
        </div>
        <div>
          <label>地域/签证限制（选填）</label>
          <input type="text" name="location" placeholder="如：上海/苏州；英文流利" />
        </div>
      </div>
      <div class="row">
        <div>
          <label>Must-have 关键词（逗号分隔）</label>
          <input type="text" name="must" placeholder="如：K8s, DevOps, 合规" />
        </div>
        <div>
          <label>Nice-to-have 关键词（逗号分隔）</label>
          <input type="text" name="nice" placeholder="如：HPC, 金融, 医药" />
        </div>
      </div>
      <label>补充说明（可直接粘贴 JD 文本）</label>
      <textarea name="note" placeholder="例如：JD 整体要求/一句话职责，或粘贴完整 JD。"></textarea>
    </div>

    <div class="card">
      <button class="btn" type="submit">开始分析（生成 Excel 清单）</button>
      <small>提交后会跳到“实时报告”，边解析边输出；Render 免费实例若空闲会有冷启动延迟。</small>
    </div>
  </form>

  {% if jobs %}
  <div class="card">
    <h3>历史报告（可继续）</h3>
    <ul>
      {% for rid, info in jobs %}
        <li><a href="/events/{{rid}}">继续 / 查看：{{info['name']}}</a>（{{info['created']}}）</li>
      {% endfor %}
    </ul>
  </div>
  {% endif %}
</div>

<script>
  const input = document.getElementById('fileInput');
  const list  = document.getElementById('fileList');
  const err   = document.getElementById('fileErr');

  function renderFiles(files){
    list.innerHTML = '';
    [...files].forEach((f, idx) => {
      const pill = document.createElement('span');
      pill.className = 'file-pill';
      pill.innerHTML = `${f.name} <button type="button" aria-label="移除" data-i="${idx}">×</button>`;
      list.appendChild(pill);
    });
  }
  function rebuildFiles(skipIndex){
    const dt = new DataTransfer();
    [...input.files].forEach((f, i) => { if(i !== skipIndex) dt.items.add(f); });
    input.files = dt.files; renderFiles(input.files);
  }
  input.addEventListener('change', () => { err.style.display='none'; renderFiles(input.files); });
  list.addEventListener('click', (e) => { if(e.target.tagName==='BUTTON'){ rebuildFiles(Number(e.target.dataset.i)); } });

  function guardSubmit(){
    if(!document.getElementById('role').value.trim()){ alert('请填写职位名称'); return false; }
    if(input.files.length===0){ err.style.display='block'; return false; }
    return true;
  }
</script>
</body>
</html>
"""

EVENTS_HTML = """<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>实时报告</title>
  <style>
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial;margin:0;background:#0b0f14;color:#e3e8f2}
    .wrap{max-width:980px;margin:20px auto;padding:0 16px}
    .card{background:#121824;border:1px solid #1e2633;border-radius:16px;padding:20px;margin-bottom:18px}
    pre{white-space:pre-wrap;word-break:break-word}
    .btn{background:#2563eb;color:#fff;border:none;padding:10px 14px;border-radius:10px;cursor:pointer}
    a{color:#89aaff;text-decoration:none}
  </style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h3>任务 {{rid}} · 实时报告</h3>
    <div id="box" style="min-height:260px"><pre id="log"></pre></div>
    <div id="actions" style="display:none;">
      <a class="btn" href="/report/{{rid}}">下载 Excel</a>
      <a class="btn" href="/">返回首页</a>
    </div>
  </div>
</div>
<script>
  const log = document.getElementById('log');
  const evt = new EventSource("/stream/{{rid}}");
  log.textContent += "连接已建立\\n\\n";
  evt.onmessage = (e) => {
    if(e.data === "[DONE]"){ evt.close(); document.getElementById('actions').style.display='block'; return; }
    log.textContent += e.data + "\\n";
    log.parentElement.scrollTop = log.parentElement.scrollHeight;
  };
  evt.onerror = () => { log.textContent += "\\n[!] 连接中断，稍后自动重试或刷新本页。\\n"; };
</script>
</body>
</html>
"""

# =========================
# 工具函数：解析、LLM、打分
# =========================

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}")

def safe_mkdir(p: str):
    os.makedirs(p, exist_ok=True)

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def extract_text_from_file(fullpath: str, data: bytes = None) -> str:
    """从多种格式抽文本"""
    name = fullpath.lower()
    try:
        if name.endswith(".pdf") and pdf_extract_text:
            with open(fullpath, "rb") as f:
                return pdf_extract_text(f)
        elif name.endswith(".docx") and docx:
            d = docx.Document(fullpath)
            return "\n".join(p.text for p in d.paragraphs)
        elif name.endswith((".html", ".htm")):
            with open(fullpath, "rb") as f:
                soup = BeautifulSoup(f, "lxml")
                return soup.get_text(" ", strip=True)
        elif name.endswith(".csv"):
            with open(fullpath, "r", encoding="utf-8", errors="ignore") as f:
                return "\n".join([",".join(row) for row in csv.reader(f)])
        else:  # .txt 等
            with open(fullpath, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception as e:
        return f"[PARSE_ERROR] {e}"

def llm_json(prompt: str, temperature: float = 0.2, max_tokens: int = 800) -> Dict[str, Any]:
    """
    调用自定义大模型接口（兼容 OpenAI 格式）
    返回 JSON（若模型返回文本，尽力解析 JSON）
    """
    url = f"{MODEL_BASE_URL}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {MODEL_API_KEY}", "Content-Type":"application/json"}
    body = {
        "model": MODEL_NAME,
        "messages": [{"role":"system","content":"你是资深猎头助手。回答请使用简洁中文。"},
                     {"role":"user","content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type":"json_object"}
    }
    try:
        r = requests.post(url, headers=headers, data=json.dumps(body), timeout=120)
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        return {"error": str(e)}

def score_prompt(role: str, direction: str, must: str, nice: str, years: str, location: str, note: str, resume_text: str) -> str:
    return f"""
请阅读下方候选人简历内容，并基于【岗位要求】给出结构化判断，JSON 返回（严格键名）：
- name（若无写“不详”）
- current_company
- current_title
- email（若无写空字符串）
- location
- age_estimate（推断年龄，若不确定写“不详”，可按“本科入学 18 岁”推）
- summary（100字内概述过往经历：年份-学校-专业-学历；年份-公司-职位-一句话职责）
- risks（不超过3条，频繁跳槽/行业不匹配/关键经验缺失等）
- tags（3~6个，逗号分隔）
- grade（A+ / A / B / C，A+为非常匹配，A匹配，B一般，C不匹配）
- remarks（200字内，用中文完整概述简历，便于电话沟通）

【岗位名称】{role}
【方向】{direction}
【Must-have】{must}
【Nice-to-have】{nice}
【最低年限】{years}
【地点/签证】{location}
【补充说明/JD】{note}

【候选人简历】
{resume_text}
"""

def normalize_grade(g: str) -> str:
    g = (g or "").strip().upper()
    if g in ["A+", "A", "B", "C"]:
        return g
    # 容错：可能返回 "A plus"/"A++"
    if "A+" in g or "PLUS" in g:
        return "A+"
    if g.startswith("A"): return "A"
    if g.startswith("B"): return "B"
    return "C"

def excel_export(rows: List[Dict[str, Any]], outpath: str):
    """
    导出 Excel，字段顺序：
    名字、目前所在公司、目前职位、匹配等级、电话（工作/手机留空）、E-mail、年龄预估、目前所在地、
    契合摘要、风险点、标签、Remarks
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "候选清单"

    headers = [
        "候选人名字","目前所在公司","目前职位","匹配等级（A+/A/B/C）",
        "工作电话","手机","E-mail","年龄预估","目前所在地",
        "契合摘要","风险点","标签","Remarks"
    ]
    ws.append(headers)

    for r in rows:
        ws.append([
            r.get("name","不详"),
            r.get("current_company",""),
            r.get("current_title",""),
            r.get("grade",""),
            "", "",  # 电话占位
            r.get("email",""),
            r.get("age_estimate","不详"),
            r.get("location",""),
            r.get("summary",""),
            "；".join(r.get("risks", [])) if isinstance(r.get("risks"), list) else r.get("risks",""),
            r.get("tags",""),
            r.get("remarks",""),
        ])
    wb.save(outpath)

# =========================
# 后端核心：处理任务
# =========================

def iter_all_files_from_upload(job_folder: str, files) -> List[str]:
    """
    保存上传，解压 zip，返回所有待解析文件绝对路径
    """
    save_dir = os.path.join(job_folder, "uploads")
    safe_mkdir(save_dir)
    total = 0
    saved_paths: List[str] = []

    # 限制总大小
    for f in files:
        total += len(f.read())
        f.seek(0)
    if total > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"上传总大小超过限制：{MAX_UPLOAD_MB}MB")
    # 保存 & 展开
    for f in files:
        filename = os.path.basename(f.filename)
        if not filename:
            continue
        dst = os.path.join(save_dir, filename)
        f.save(dst)
        saved_paths.append(dst)

        if filename.lower().endswith(".zip"):
            with zipfile.ZipFile(dst, "r") as z:
                for name in z.namelist():
                    if name.endswith("/"):  # 跳过文件夹
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in (".pdf", ".docx", ".txt", ".html", ".htm", ".csv"):
                        continue
                    data = z.read(name)
                    out = os.path.join(save_dir, f"{uuid.uuid4().hex}{ext}")
                    with open(out, "wb") as wf:
                        wf.write(data)
                    saved_paths.append(out)
    # 过滤掉 zip 源文件
    final_files = [p for p in saved_paths if not p.lower().endswith(".zip")]
    return final_files

def sse_put(q: Queue, msg: str):
    try:
        q.put_nowait(msg)
    except:
        pass

def process_one_file(fullpath: str,
                     params: Dict[str,str]) -> Optional[Dict[str, Any]]:
    """
    解析->LLM评分->返回结果字典
    """
    text = extract_text_from_file(fullpath)
    if not text.strip():
        return None
    prompt = score_prompt(
        role=params["role"],
        direction=params.get("direction",""),
        must=params.get("must",""),
        nice=params.get("nice",""),
        years=params.get("min_years",""),
        location=params.get("location",""),
        note=params.get("note",""),
        resume_text=text[:120000]  # 防止超长
    )
    data = llm_json(prompt)
    if "error" in data:
        # 尝试再容错一次：有些模型会把 JSON 放在文本里
        try:
            data = json.loads(re.findall(r"\{[\\s\\S]*\}", str(data))[0])
        except:
            return {"name":"解析失败","current_company":"","current_title":"",
                    "grade":"C","email":"","age_estimate":"不详","location":"",
                    "summary":f"LLM 调用失败：{data['error']}","risks":[],"tags":"","remarks":""}

    # 归一化
    data["grade"] = normalize_grade(data.get("grade",""))
    # 邮箱兜底从文本匹配
    if not data.get("email"):
        m = EMAIL_RE.search(text)
        data["email"] = m.group(0) if m else ""
    return data

def dedupe_rows(rows: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    """
    去重优先级：email > name+company
    """
    seen_email, seen_pair = set(), set()
    out = []
    for r in rows:
        key_email = (r.get("email","") or "").lower().strip()
        key_pair  = ( (r.get("name","") or "").strip(), (r.get("current_company","") or "").strip() )
        if key_email:
            if key_email in seen_email: 
                continue
            seen_email.add(key_email)
        else:
            if key_pair in seen_pair:
                continue
            seen_pair.add(key_pair)
        out.append(r)
    return out

def run_job(rid: str):
    """
    后台线程：跑并发，SSE 实时写入
    """
    job = JOBS[rid]
    q: Queue = job["q"]
    params = job["params"]
    files  = job["files"]
    sse_put(q, f"任务开始，共 {len(files)} 份文件")

    rows: List[Dict[str,Any]] = []
    ok, fail = 0, 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        fut2file = {ex.submit(process_one_file, f, params): f for f in files}
        for fut in as_completed(fut2file):
            f = fut2file[fut]
            name = os.path.basename(f)
            try:
                res = fut.result()
                if res:
                    rows.append(res)
                    ok += 1
                    sse_put(q, f"[{ok}/{len(files)}] 完成：{name} · 评分 {res.get('grade','')}")
                else:
                    fail += 1
                    sse_put(q, f"[!] 解析失败：{name}")
            except Exception as e:
                fail += 1
                sse_put(q, f"[!] 处理异常：{name} · {e}")

    sse_put(q, "去重与排序中…")
    rows = dedupe_rows(rows)
    # 排序：A+ > A > B > C
    rank = {"A+":0, "A":1, "B":2, "C":3}
    rows.sort(key=lambda r: rank.get(r.get("grade","C"), 9))

    out_xlsx = os.path.join(job["folder"], "候选清单.xlsx")
    excel_export(rows, out_xlsx)

    job["results"] = {"total":len(files), "ok":ok, "fail":fail, "xlsx": out_xlsx}
    job["done"] = True

    sse_put(q, f"完成：共 {len(files)} 份，成功 {ok}，失败 {fail}。")
    sse_put(q, "[DONE]")

# =========================
# 路由
# =========================

@app.get("/")
def index():
    jobs_sorted = sorted(
        [(rid, {"name": info["name"], "created": info["created"]})
         for rid, info in JOBS.items()],
        key=lambda x: x[1]["created"],
        reverse=True,
    )
    return render_template_string(INDEX_HTML, jobs=jobs_sorted)

@app.post("/process")
def process():
    # —— 表单取值（键名与前端一致）——
    role       = request.form.get("role","").strip()
    if not role:
        return Response("职位名称必填", status=400)
    direction  = request.form.get("direction","").strip()
    min_years  = request.form.get("min_years","").strip()
    location   = request.form.get("location","").strip()
    must       = request.form.get("must","").strip()
    nice       = request.form.get("nice","").strip()
    note       = request.form.get("note","").strip()
    files      = request.files.getlist("files")

    if not files or all(not f.filename for f in files):
        return Response("请至少上传 1 个文件", status=400)

    # —— 任务命名：职位_方向_时间戳 —— 
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jobname = f"{role}_{direction}_{ts}".replace("/", "_").replace("\\", "_").replace(" ", "_")
    folder  = os.path.join(DATA_DIR, jobname)
    safe_mkdir(folder)

    try:
        all_files = iter_all_files_from_upload(folder, files)
    except Exception as e:
        return Response(f"上传/解压失败：{e}", status=400)

    rid = uuid.uuid4().hex[:8]
    q = Queue()
    JOBS[rid] = {
        "q": q,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "name": jobname,
        "folder": folder,
        "files": all_files,
        "results": None,
        "done": False,
        "params": {
            "role": role, "direction": direction, "min_years": min_years,
            "location": location, "must": must, "nice": nice, "note": note
        }
    }

    # 后台跑
    from threading import Thread
    t = Thread(target=run_job, args=(rid,), daemon=True)
    t.start()
    return redirect(url_for("events", rid=rid))

@app.get("/events/<rid>")
def events(rid: str):
    if rid not in JOBS:
        return Response("任务不存在", status=404)
    return render_template_string(EVENTS_HTML, rid=rid)

@app.get("/stream/<rid>")
def stream(rid: str):
    if rid not in JOBS:
        return Response("任务不存在", status=404)
    job = JOBS[rid]
    q: Queue = job["q"]

    def gen():
        yield "data: ▶ 连接已建立\\n\\n"
        while True:
            msg = q.get()
            if msg == "[DONE]":
                yield "data: [DONE]\\n\\n"
                break
            safe = str(msg).replace("\\r"," ").replace("\\n","\\n")
            yield f"data: {safe}\\n\\n"

    headers = {
        "Content-Type":"text/event-stream",
        "Cache-Control":"no-cache",
        "X-Accel-Buffering":"no",
        "Connection":"keep-alive"
    }
    return Response(gen(), headers=headers)

@app.get("/report/<rid>")
def report(rid: str):
    if rid not in JOBS:
        return Response("任务不存在", status=404)
    job = JOBS[rid]
    if not job.get("done"):
        return Response("任务尚未完成", status=400)
    xlsx = job["results"]["xlsx"]
    return send_file(xlsx, as_attachment=True, download_name=os.path.basename(xlsx))

# 健康检查
@app.get("/healthz")
def healthz():
    return {"ok": True, "workers": MAX_WORKERS, "model": MODEL_NAME}

# =========================
# 本地启动
# =========================
if __name__ == "__main__":
    # 本地调试：python app.py
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
