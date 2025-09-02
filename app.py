import os, io, re, json, zipfile, time, uuid, hashlib, logging, csv
from datetime import datetime
from typing import List, Dict, Any, Optional
from queue import Queue, Empty as QEmpty
from threading import Thread
from flask import Flask, request, Response, send_file, redirect

# ------------- 环境开关（对用户隐藏）-------------
MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "").strip()
MODEL_API_KEY  = os.getenv("MODEL_API_KEY", "").strip()
MODEL_NAME     = os.getenv("MODEL_NAME", "deepseek-chat").strip()

CONCURRENCY    = int(os.getenv("CONCURRENCY", "2"))           # 并发(worker)数
MAX_UPLOAD_MB  = int(os.getenv("MAX_UPLOAD_MB", "200"))       # Render 免费版 512MB 上限，保守用 200
WORK_DIR       = "/tmp/alsos_jobs"                            # 任务根目录
os.makedirs(WORK_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("alsos")

# ------------- 内存任务表 -------------
JOBS: Dict[str, Dict[str, Any]] = {}  # rid -> {q, created, name, folder, status, last}

# ------------- HTML 模板（极简 UI+可删文件+历史任务）-------------
INDEX_HTML = r"""
<!doctype html><html lang="zh">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Alsos Talent · 合规AI自动化寻访（MVP）</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial;margin:0;background:#0b0f14;color:#e3e8f2}
 .wrap{max-width:980px;margin:26px auto;padding:0 16px}
 h1{font-size:22px;margin:6px 0 18px}
 .card{background:#121824;border:1px solid #1e2633;border-radius:16px;padding:20px;margin-bottom:18px}
 label{display:block;font-size:14px;color:#A9B4C6;margin:8px 0 6px}
 input[type="text"],textarea{width:100%;background:#0b1018;color:#dbe4f0;border:1px solid #223044;border-radius:10px;padding:10px 12px;outline:none}
 textarea{min-height:110px}
 .row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
 .btn{background:#2563eb;color:#fff;border:none;padding:12px 16px;border-radius:12px;cursor:pointer;font-weight:600}
 .muted{color:#92a1b7;font-size:12px}
 .pill{display:inline-block;padding:2px 8px;background:#102033;border:1px solid #223044;border-radius:999px;margin-right:6px;font-size:12px;color:#B8C4D9}
 ul.files{list-style:none;padding:0;margin:8px 0 0}
 ul.files li{display:flex;justify-content:space-between;align-items:center;padding:6px 10px;background:#0b1018;border:1px solid #223044;border-radius:8px;margin-top:6px}
 a{color:#7aa0ff;text-decoration:none}
</style>
</head>
<body>
<div class="wrap">
  <h1>Alsos Talent · 合规AI自动化寻访（MVP）</h1>

  <div class="card">
    <p class="muted">说明：本工具<strong>不做</strong>对 LinkedIn/猎聘 的自动点开或抓取；仅对你<strong>合规导出</strong>的 ZIP/PDF/HTML/DOCX/CSV/TXT 进行 AI 解析、去重、排序并导出 Excel。</p>
  </div>

  <form id="f" action="/process" method="post" enctype="multipart/form-data" onsubmit="return submitForm()">
    <div class="card">
      <h3>上传候选集（支持多文件）</h3>
      <input id="file" type="file" name="files" multiple required accept=".zip,.pdf,.docx,.html,.htm,.txt,.csv"/>
      <ul id="fileList" class="files"></ul>
      <small class="muted">可直接上传 Recruiter Lite 25人/包的 ZIP（可一次多包）。支持 PDF/HTML/DOCX/TXT/CSV 混合上传。</small>
    </div>

    <div class="card">
      <h3>岗位/筛选要求</h3>
      <div class="row">
        <div>
          <label>职位名称（必填）</label>
          <input type="text" name="role" id="role" placeholder="如：资深基础设施架构师" required/>
        </div>
        <div>
          <label>方向（选填）</label>
          <input type="text" name="direction" placeholder="如：Infra / SRE / 医疗IT"/>
        </div>
      </div>
      <div class="row">
        <div>
          <label>最低年限（选填）</label>
          <input type="text" name="min_years" placeholder="如：8 或 10-15"/>
        </div>
        <div>
          <label>地域/签证限制（选填）</label>
          <input type="text" name="location" placeholder="如：上海/苏州；英文流利"/>
        </div>
      </div>
      <div class="row">
        <div>
          <label>Must-have 关键词（逗号分隔）</label>
          <input type="text" name="must" placeholder="如：K8s, DevOps, 合规"/>
        </div>
        <div>
          <label>Nice-to-have 关键词（逗号分隔）</label>
          <input type="text" name="nice" placeholder="如：HPC, 金融, 医药"/>
        </div>
      </div>
      <label>补充说明（可直接粘贴 JD 全文）</label>
      <textarea name="note" placeholder="如：优先有从0→1平台建设经验；避免频繁跳槽。"></textarea>
    </div>

    <div class="card">
      <button class="btn" type="submit">开始分析（生成Excel清单）</button>
      <small class="muted">提交后会跳到“实时报告”页面；如断开可从首页历史任务继续。</small>
    </div>
  </form>

  {% if jobs %}
  <div class="card">
    <h3>历史任务</h3>
    <ul>
      {% for rid,info in jobs %}
        <li><a href="/events/{{rid}}">继续 / 查看：{{info['name']}}</a> <span class="muted">（{{info['created']}}）</span></li>
      {% endfor %}
    </ul>
  </div>
  {% endif %}
</div>

<script>
const fileInput = document.getElementById('file');
const fileList = document.getElementById('fileList');
let dt = new DataTransfer();

fileInput.addEventListener('change', () => {
  // 重新以 DataTransfer 管理 files，才能删除单个
  for (const f of fileInput.files) dt.items.add(f);
  render();
});

function render(){
  fileList.innerHTML = '';
  for (let i=0;i<dt.files.length;i++){
    const li = document.createElement('li');
    li.innerHTML = '<span>'+ dt.files[i].name +'</span><button type="button" class="btn" style="padding:6px 10px;border-radius:8px;background:#1f2a44" onclick="del('+i+')">删除</button>';
    fileList.appendChild(li);
  }
  fileInput.files = dt.files;
}

function del(idx){
  const ndt = new DataTransfer();
  for (let i=0;i<dt.files.length;i++){
    if (i!==idx) ndt.items.add(dt.files[i]);
  }
  dt = ndt; render();
}

function submitForm(){
  if (!document.getElementById('role').value.trim()){
    alert('职位名称必填'); return false;
  }
  if (dt.files.length===0){ alert('请至少选择一个文件'); return false; }
  // 将 DataTransfer 内容重新回填给 input
  fileInput.files = dt.files;
  return true;
}
</script>
</body></html>
"""

EVENTS_HTML = r"""
<!doctype html><html lang="zh">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>任务 {{rid}} · 实时报告</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial;margin:0;background:#0b0f14;color:#e3e8f2}
 .wrap{max-width:980px;margin:26px auto;padding:0 16px}
 h1{font-size:22px;margin:6px 0 18px}
 .card{background:#121824;border:1px solid #1e2633;border-radius:16px;padding:20px}
 pre{white-space:pre-wrap;word-break:break-word}
 .btn{background:#2563eb;color:#fff;border:none;padding:10px 14px;border-radius:10px;cursor:pointer}
</style>
</head>
<body>
<div class="wrap">
  <h1>任务 {{rid}} · 实时报告  <a class="btn" href="/resume/{{rid}}">继续（断点续跑）</a>  <a class="btn" href="/">返回</a></h1>
  <div class="card"><pre id="log">连接已建立…</pre></div>
  <div class="card" id="actions" style="margin-top:12px;display:none">
    <a class="btn" id="dl" href="#">下载 Excel</a>
    <a class="btn" id="rank" href="#">查看榜单</a>
  </div>
</div>
<script>
  const log = document.getElementById('log');
  const es = new EventSource('/stream/{{rid}}');
  let done = false;

  es.onmessage = (e)=>{
    const t = e.data || '';
    if (!t) return;
    if (t.startsWith('[DONE]')) {
      done = true; es.close();
      document.getElementById('actions').style.display='block';
      const rid = '{{rid}}';
      document.getElementById('dl').href = '/report/'+rid;
      document.getElementById('rank').href = '/top/'+rid;
      return;
    }
    log.textContent += '\\n' + t;
    log.scrollTop = log.scrollHeight;
  };
  es.onerror = ()=>{ if(!done) log.textContent += '\\n[!] 连接中断，可点击“继续”重试。'; };
</script>
</body></html>
"""

from flask import render_template_string

app = Flask(__name__)

# ----------------- 工具函数 -----------------
def rid_now(role: str, direction: str) -> str:
    base = role.strip()
    if direction.strip():
        base += "_" + direction.strip()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9_\-]", "_", base)[:60]
    return f"{safe}_{ts}"

def push(q: Queue, msg: str):
    try:
        q.put_nowait(msg)
    except Exception:
        pass

def norm_email(text: str) -> Optional[str]:
    m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    return m.group(0).lower() if m else None

def parse_html(html: str) -> Dict[str, Any]:
    # 很轻量的抽取(避免依赖太重)，若你已有更稳的解析器可以替换
    name = re.search(r"<title[^>]*>([^<]{2,40})</title>", html, re.I)
    name = (name.group(1) if name else "").strip()
    email = norm_email(html) or ""
    company = ""
    title = ""
    # 尝试找“Company”/“公司”
    mc = re.search(r"(?:Company|公司)[^:：]{0,8}[:：]\s*([^\n<]{2,50})", html, re.I)
    if mc: company = mc.group(1).strip()
    mt = re.search(r"(?:Title|职位)[^:：]{0,8}[:：]\s*([^\n<]{2,50})", html, re.I)
    if mt: title = mt.group(1).strip()
    return dict(name=name or "未命名", email=email, company=company, title=title)

def parse_pdf(bytes_data: bytes) -> Dict[str, Any]:
    # 尽量轻，避免 pdfminer 对 Render 的 CPU 压力；这里只提取 email + 简短姓名启发
    text = ""
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(io.BytesIO(bytes_data)) or ""
    except Exception:
        pass
    email = norm_email(text) or ""
    name = ""
    mn = re.search(r"([A-Za-z\u4e00-\u9fa5]{2,20})", text)
    if mn: name = mn.group(1)
    return dict(name=name or "未命名", email=email, company="", title="")

def parse_docx(bytes_data: bytes) -> Dict[str, Any]:
    text = ""
    try:
        import docx
        f = io.BytesIO(bytes_data)
        d = docx.Document(f)
        text = "\n".join(p.text for p in d.paragraphs)
    except Exception:
        pass
    email = norm_email(text) or ""
    name = ""
    mn = re.search(r"([A-Za-z\u4e00-\u9fa5]{2,20})", text)
    if mn: name = mn.group(1)
    return dict(name=name or "未命名", email=email, company="", title="")

def parse_txt_csv(bytes_data: bytes) -> Dict[str, Any]:
    text = bytes_data.decode("utf-8", "ignore")
    email = norm_email(text) or ""
    name = ""
    mn = re.search(r"([A-Za-z\u4e00-\u9fa5]{2,20})", text)
    if mn: name = mn.group(1)
    return dict(name=name or "未命名", email=email, company="", title="")

def excel_save(rows: List[Dict[str, Any]], path: str):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "候选清单"
    header = ["候选人名字","目前所在公司","目前职位","匹配等级","分数(0-100)","E-mail","年龄预估","目前所在地","契合摘要","风险点","标签","Remarks"]
    ws.append(header)
    for r in rows:
        ws.append([
            r.get("name",""), r.get("company",""), r.get("title",""),
            r.get("grade",""), r.get("score",0), r.get("email",""),
            r.get("age",""), r.get("location",""), r.get("fit",""),
            r.get("risk",""), r.get("tags",""), r.get("remarks",""),
        ])
    wb.save(path)

def score_and_grade(candidate: Dict[str, Any], spec: Dict[str, Any]) -> (int, str, str):
    # 非模型版的保底打分逻辑（你可以换为调用 DeepSeek 的函数；保持同样返回即可）
    base = 60
    textbag = " ".join([
        candidate.get("name",""), candidate.get("title",""), candidate.get("company",""),
        candidate.get("raw","")
    ]).lower()
    # must / nice
    for k in spec.get("must", []):
        if k and k.lower() in textbag: base += 12
    for k in spec.get("nice", []):
        if k and k.lower() in textbag: base += 5
    # 年限 / 地域只是轻微调整（真实调用模型时可更精细）
    if spec.get("min_years"):
        try:
            yrs = int(re.findall(r"\d+", spec["min_years"])[0])
            base += min(10, yrs // 2)
        except: pass
    # clamp
    score = max(0, min(100, base))
    if score >= 90: grade = "A+"
    elif score >= 80: grade = "A"
    elif score >= 65: grade = "B"
    else: grade = "C"
    summary = f"{candidate.get('title','')} @ {candidate.get('company','')}".strip(" @")
    risk = ""
    tags = ",".join(spec.get("must", [])[:2])
    # 备注示例
    remarks = candidate.get("remarks","") or candidate.get("brief","") or ""
    return score, grade, remarks or summary, risk, tags

# ----------------- 模型（可替换成 DeepSeek 调用） -----------------
def call_model_summarize(text: str, jd: str) -> Dict[str, Any]:
    """
    如果你要接入 DeepSeek/OpenAI，就在这里实现调用并返回：
    {"education":"…","timeline":[{"from":"2018","to":"2021","company":"…","role":"…","one":"…"}]}
    这里给一个安全兜底：用规则生成“Remarks”即可。
    """
    # 简易规则生成
    lines = []
    # 教育
    edu = re.findall(r"(20\d{2}|19\d{2}).{0,20}(大学|学院|硕士|博士|本科)", text)
    if edu:
        years = sorted(set(y for y,_ in edu))
        lines.append("教育经历：" + "；".join(sorted(years)) + "（推断）")
    # 时间线
    rolls = re.findall(r"(20\d{2}|19\d{2}).{0,5}[-–~到至].{0,5}(20\d{2}|至今|现在).{0,20}([^\n，。,;]{2,40})", text)
    for a,b,c in rolls[:4]:
        lines.append(f"{a}–{b}：{c}")
    return {"remarks":"；".join(lines[:6])}

# ----------------- 任务执行 -----------------
def worker_run(rid: str, spec: Dict[str, Any]):
    job = JOBS[rid]; q: Queue = job["q"]
    folder = job["folder"]
    push(q, "▶ 开始处理…")
    uploads_dir = os.path.join(folder, "uploads")
    out_xlsx = os.path.join(folder, "result.xlsx")
    os.makedirs(uploads_dir, exist_ok=True)

    # 聚合候选
    candidates: List[Dict[str, Any]] = []

    # 解压 & 逐文件解析
    for fn in os.listdir(uploads_dir):
        fpath = os.path.join(uploads_dir, fn)
        push(q, f"• 解析 {fn} …")
        try:
            with open(fpath, "rb") as f:
                data = f.read()
            if fn.lower().endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for n in z.namelist():
                        if n.endswith("/"): continue
                        raw = z.read(n)
                        cand = parse_any_by_name(n, raw)
                        cand["raw"] = (raw[:5000]).decode("utf-8","ignore") if isinstance(raw, bytes) else str(raw)[:5000]
                        candidates.append(cand)
            else:
                cand = parse_any_by_name(fn, data)
                cand["raw"] = data.decode("utf-8","ignore")[:5000] if isinstance(data, bytes) else str(data)[:5000]
                candidates.append(cand)
        except Exception as e:
            push(q, f"[!] 解析 {fn} 失败：{e}")

    if not candidates:
        push(q, "[!] 未解析到任何候选，请确认上传包是否包含 PDF/HTML/DOCX/TXT/CSV。")
        push(q, "[DONE]"); job["status"]="done"; return

    # 去重（优先 email）
    dedup: Dict[str, Dict[str, Any]] = {}
    for c in candidates:
        key = c.get("email") or (c.get("name","") + "|" + c.get("company",""))
        if key in dedup:
            old = dedup[key]
            # 合并信息
            for k in ("title","company"):
                if len((c.get(k) or "")) > len(old.get(k) or ""):
                    old[k] = c.get(k)
        else:
            dedup[key] = c
    candidates = list(dedup.values())
    push(q, f"• 去重后候选：{len(candidates)}")

    # 模型/规则 生成 remarks + 打分分级
    rows = []
    for i,c in enumerate(candidates,1):
        if i % 8 == 0: push(q, f"  进度：{i}/{len(candidates)}")
        # 生成备注
        try:
            info = call_model_summarize(c.get("raw",""), spec.get("note",""))
            if info and "remarks" in info: c["remarks"] = info["remarks"]
        except Exception:
            pass
        score, grade, summary, risk, tags = score_and_grade(c, spec)
        rows.append({
            "name": c.get("name",""),
            "company": c.get("company",""),
            "title": c.get("title",""),
            "grade": grade,
            "score": score,
            "email": c.get("email",""),
            "age": "",  # 你有需要再加推断
            "location": spec.get("location",""),
            "fit": summary,
            "risk": risk,
            "tags": tags,
            "remarks": c.get("remarks",""),
        })

    # 排序：分数 → A+ 优先
    rows.sort(key=lambda r: (r["grade"]!="A+", -r["score"]), reverse=False)
    excel_save(rows, out_xlsx)

    job["status"]="done"
    job["result"] = rows
    push(q, f"✅ 完成，共 {len(rows)} 人。")
    push(q, "[DONE]")

def parse_any_by_name(name: str, data: bytes) -> Dict[str, Any]:
    low = name.lower()
    if low.endswith(".pdf"): return parse_pdf(data)
    if low.endswith(".docx"): return parse_docx(data)
    if low.endswith(".html") or low.endswith(".htm"):
        return parse_html(data.decode("utf-8","ignore"))
    if low.endswith(".txt") or low.endswith(".csv"):
        return parse_txt_csv(data)
    # 兜底尝试 html/txt
    try:
        txt = data.decode("utf-8","ignore")
        if "<html" in txt.lower(): return parse_html(txt)
        else: return parse_txt_csv(data)
    except Exception:
        return {"name":"未命名","email":"","company":"","title":""}

# ----------------- 路由 -----------------
@app.get("/")
def index():
    jobs = sorted([(rid,info) for rid,info in JOBS.items()], key=lambda x: x[1]["created"], reverse=True)[:20]
    return render_template_string(INDEX_HTML, jobs=jobs)

@app.post("/process")
def process():
    # 校验职位名称
    role = (request.form.get("role") or "").strip()
    if not role:
        return "职位名称必填", 400

    direction = (request.form.get("direction") or "").strip()
    rid = rid_now(role, direction)
    folder = os.path.join(WORK_DIR, rid)
    os.makedirs(folder, exist_ok=True)
    uploads = os.path.join(folder, "uploads")
    os.makedirs(uploads, exist_ok=True)

    # 文件落地
    total = 0
    fs = request.files.getlist("files")
    for f in fs:
        b = f.read()
        total += len(b)
        if total > MAX_UPLOAD_MB*1024*1024:
            return f"上传超出 {MAX_UPLOAD_MB}MB 限制", 400
        with open(os.path.join(uploads, f.filename), "wb") as out:
            out.write(b)

    # 建任务
    q = Queue()
    JOBS[rid] = {
        "q": q,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "name": rid,
        "folder": folder,
        "status": "running",
        "last": time.time(),
    }
    spec = dict(
        role=role,
        direction=direction,
        min_years=(request.form.get("min_years") or "").strip(),
        location=(request.form.get("location") or "").strip(),
        must=[x.strip() for x in (request.form.get("must") or "").split(",") if x.strip()],
        nice=[x.strip() for x in (request.form.get("nice") or "").split(",") if x.strip()],
        note=(request.form.get("note") or "").strip(),
    )
    Thread(target=worker_run, args=(rid, spec), daemon=True).start()
    return redirect(f"/events/{rid}")

@app.get("/events/<rid>")
def events_page(rid):
    if rid not in JOBS: return "任务不存在", 404
    return render_template_string(EVENTS_HTML, rid=rid)

@app.get("/stream/<rid>")
def stream(rid):
    if rid not in JOBS: return "data: 任务不存在\n\n", 200, {"Content-Type":"text/event-stream"}
    q: Queue = JOBS[rid]["q"]

    def gen():
        yield "data: 连接已建立\n\n"
        hb = 0
        while True:
            try:
                msg = q.get(timeout=2)
                if msg == "[DONE]":
                    yield "data: [DONE]\n\n"; break
                safe = str(msg).replace("\r"," ").replace("\n","\\n")
                yield f"data: {safe}\n\n"
            except QEmpty:
                # 心跳：Render 免费版防休眠
                hb += 1
                if hb >= 6:   # 每 ~12 秒
                    hb = 0
                    yield "data: …\n\n"
    return Response(gen(), headers={
        "Content-Type":"text/event-stream",
        "Cache-Control":"no-cache",
        "X-Accel-Buffering":"no",
        "Connection":"keep-alive",
    })

@app.get("/report/<rid>")
def report(rid):
    if rid not in JOBS: return "任务不存在", 404
    path = os.path.join(JOBS[rid]["folder"], "result.xlsx")
    if not os.path.exists(path): return "报告未生成", 404
    return send_file(path, as_attachment=True, download_name=f"{rid}.xlsx")

@app.get("/top/<rid>")
def top(rid):
    if rid not in JOBS: return "任务不存在", 404
    rows = JOBS[rid].get("result") or []
    html = ["<html><meta charset='utf-8'><body style='background:#0b0f14;color:#e3e8f2;font-family:ui-sans-serif'>",
            f"<h2>榜单 · {rid}</h2><ol>"]
    for r in rows[:50]:
        html.append(f"<li>{r.get('grade')} · {r.get('score')} · {r.get('name')} — {r.get('title','')} @ {r.get('company','')} · {r.get('email','')}</li>")
    html.append("</ol><a href='/'>返回</a></body></html>")
    return "\n".join(html)

@app.get("/resume/<rid>")
def resume(rid):
    if rid not in JOBS: return redirect("/")
    job = JOBS[rid]
    if job.get("status") == "done":
        return redirect(f"/events/{rid}")
    # 简单的“继续”：往队列塞一个提示，线程仍在跑；若线程已挂可在此重启（此处保守不自动拉起，避免重复）
    push(job["q"], "↻ 若上次中断，请稍等，任务会继续输出…")
    return redirect(f"/events/{rid}")

# ----------------- 健康检查 -----------------
@app.get("/healthz")
def health():
    return {"ok": True, "ts": time.time()}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
