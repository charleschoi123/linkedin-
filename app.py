# app.py
# -*- coding: utf-8 -*-
"""
Alsos Talent · 合规AI自动化寻访（MVP）
- 单页表单（上传 ZIP/PDF/HTML/DOCX/TXT/CSV，JD 要点直接填“补充说明”）
- 后台并发解析 → 调用 DeepSeek 评估 → 实时 SSE 输出（可断点续跑）
- 导出 Excel（含评分、等级 A/A+ 等，契合点、风险点，Remark（教育/工作时间线），年龄预估）

环境变量（Render → Environment）：
- MODEL_API_KEY   : 必填（DeepSeek key）
- MODEL_BASE_URL  : 选填，默认 https://api.deepseek.com  （注意：不要再写 /v1，代码会自己补上）
- MODEL_NAME      : 选填，默认 deepseek-chat
- CONCURRENCY     : 选填，并发批大小，默认 2
- MAX_UPLOAD_MB   : 选填，默认 200（仅用于前端提示）
"""

import os, re, io, zipfile, csv, json, time, uuid, hashlib, logging
from datetime import datetime
from queue import Queue
from threading import Thread, Lock
from typing import List, Dict, Any, Optional

from flask import Flask, request, Response, send_file, redirect, url_for, render_template_string

# 解析依赖（都有容错）
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
    from openpyxl.styles import Font, Alignment
except Exception:
    openpyxl = None


# -----------------------------
# 全局配置 / 状态
# -----------------------------
app = Flask(__name__)

MODEL_API_KEY = os.getenv("MODEL_API_KEY", "").strip()
MODEL_NAME = os.getenv("MODEL_NAME", "deepseek-chat").strip() or "deepseek-chat"
# 重要：只要 host，别带 /v1，这里会自动补
_BASE = (os.getenv("MODEL_BASE_URL", "https://api.deepseek.com").strip() or "https://api.deepseek.com").rstrip("/")
CHAT_COMPLETIONS = f"{_BASE}/v1/chat/completions"

CONCURRENCY = int(os.getenv("CONCURRENCY", "2"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("alsos")

# 简单内存“任务中心”
JOBS: Dict[str, Dict[str, Any]] = {}
J_LOCK = Lock()


# -----------------------------
# 前端模板（你喜欢的“简洁专业”深色 UI）
# -----------------------------
INDEX_HTML = r"""
<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Alsos Talent · 合规AI自动化寻访（MVP）</title>
<style>
body{margin:0;background:#0b0f14;color:#e3e8f2;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial}
.wrap{max-width:1080px;margin:30px auto;padding:0 16px}
.card{background:#121824;border:1px solid #1f2b3d;border-radius:14px;padding:18px;margin:14px 0}
h1{font-size:20px;margin:0 0 14px}
label{display:block;color:#A8B4C6;font-size:14px;margin:8px 0 6px}
input[type=text],textarea{width:100%;background:#0b1018;border:1px solid #223044;border-radius:10px;color:#dbe4f0;padding:10px 12px}
textarea{min-height:100px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
small{color:#8fa0b7}
.btn{background:#2563eb;border:none;color:#fff;border-radius:10px;padding:12px 16px;cursor:pointer;font-weight:600}
a.btn{display:inline-block;text-decoration:none}
</style>
</head>
<body>
<div class="wrap">
  <h1>Alsos Talent · 合规AI自动化寻访（MVP）</h1>

  <div class="card">
    <p><small>说明：本工具<strong>不做</strong>对 LinkedIn/猎聘 的自动点开或抓取；只分析你合规导出的简历包（ZIP/PDF/HTML/DOCX/TXT/CSV）。JD 要点直接填「补充说明」。</small></p>
  </div>

  <form action="/process" method="post" enctype="multipart/form-data">
    <div class="card">
      <h3>上传候选集</h3>
      <label>选择文件（单个或多个；可直接上传 Recruiter Lite 打包 ZIP）：</label>
      <input type="file" name="files" multiple required/>
      <small>一次性总大小建议 &lt; {{max_mb}} MB；若很大，建议拆包后多次运行。上传错了？直接重新选择即可。</small>
    </div>

    <div class="card">
      <h3>岗位/筛选要求</h3>
      <div class="row">
        <div>
          <label>职位名称（必填）</label>
          <input type="text" name="role" placeholder="如：资深基础设施架构师" required/>
        </div>
        <div>
          <label>方向（选填）</label>
          <input type="text" name="track" placeholder="如：Infra / SRE / 医疗IT"/>
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
          <input type="text" name="must" placeholder="如：K8s, DevOps, 安全合规"/>
        </div>
        <div>
          <label>Nice-to-have 关键词（逗号分隔）</label>
          <input type="text" name="nice" placeholder="如：HPC, 金融, 医药"/>
        </div>
      </div>
      <label>补充说明（直接贴 JD 要点、评价规则等；这就是「JD 输入」）</label>
      <textarea name="note" placeholder="例如：有混合云架构与平台从0-1经验；能落地成本与稳定性优化；避免频繁跳槽。"></textarea>
    </div>

    <div class="card">
      <button class="btn" type="submit">开始分析（生成 Excel）</button>
      <small>提交后会跳转到「实时报告」页面，边解析边打印过程；中断可点“继续”。</small>
    </div>
  </form>

  {% if jobs %}
  <div class="card">
    <h3>历史任务</h3>
    <ul>
      {% for rid,info in jobs %}
        <li><a href="/events/{{rid}}">继续 / 查看：{{info['name']}}</a>（{{info['created']}}）</li>
      {% endfor %}
    </ul>
  </div>
  {% endif %}
</div>
</body>
</html>
"""

EVENTS_HTML = r"""
<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>任务 {{name}} · 实时报告</title>
<style>
body{margin:0;background:#0b0f14;color:#e3e8f2;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial}
.wrap{max-width:1080px;margin:24px auto;padding:0 16px}
.head{display:flex;gap:10px;align-items:center;justify-content:space-between}
h1{font-size:20px;margin:0}
.btn{background:#2563eb;color:#fff;border:none;border-radius:10px;padding:10px 14px;text-decoration:none}
.card{background:#121824;border:1px solid #1f2b3d;border-radius:14px;padding:18px;margin-top:14px;white-space:pre-wrap;font-family:ui-monospace,Menlo,Consolas,monospace}
.rowbtn{display:flex;gap:10px;margin-top:14px}
</style>
</head>
<body>
<div class="wrap">
  <div class="head">
    <h1>任务 {{name}} · 实时报告</h1>
    <div>
      <a class="btn" href="/resume/{{rid}}">继续（断点续跑）</a>
      <a class="btn" href="/">返回</a>
    </div>
  </div>

  <div class="card" id="log">连接已建立…</div>

  <div class="rowbtn">
    <a id="btnXlsx" class="btn" style="display:none" href="/xlsx/{{rid}}">下载 Excel</a>
    <a id="btnRank" class="btn" style="display:none" href="/rank/{{rid}}">查看榜单</a>
  </div>

  <div class="card"><small>连接中断，稍后自动重试或手动刷新本页。</small></div>
</div>
<script>
const rid = "{{rid}}";
const logDiv = document.getElementById('log');
function append(t){logDiv.textContent += t}
function showDone(){
  document.getElementById('btnXlsx').style.display='inline-block';
  document.getElementById('btnRank').style.display='inline-block';
}
function connect(){
  const es = new EventSource("/stream/"+rid);
  es.onmessage = e=>{
    if(e.data==="__DONE__"){showDone();return}
    append(e.data);
  };
  es.onerror = _=>{ es.close(); setTimeout(connect,2500); };
}
connect();
</script>
</body>
</html>
"""


# -----------------------------
# 工具：解析简历文本
# -----------------------------
def read_text_from_bytes(name: str, data: bytes) -> str:
    """根据扩展名解析文本；失败就做降级（原文、可读性差一点也比空好）"""
    lower = name.lower()
    if lower.endswith(".pdf") and pdf_extract_text:
        try:
            return pdf_extract_text(io.BytesIO(data)) or ""
        except Exception:
            pass
    if (lower.endswith(".docx") or lower.endswith(".doc")) and docx:
        try:
            doc = docx.Document(io.BytesIO(data))
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception:
            pass
    if (lower.endswith(".html") or lower.endswith(".htm")) and BeautifulSoup:
        try:
            soup = BeautifulSoup(data, "html.parser")
            return soup.get_text(" ")
        except Exception:
            pass
    if lower.endswith(".csv"):
        try:
            s = data.decode("utf-8", "ignore").splitlines()
            rows = list(csv.reader(s))
            return "\n".join([", ".join(r) for r in rows])
        except Exception:
            pass
    # 兜底文本
    try:
        return data.decode("utf-8", "ignore")
    except Exception:
        return ""


# -----------------------------
# 工具：与 DeepSeek 通信（已修复 /v1/v1）
# -----------------------------
import requests

def call_llm(messages: List[Dict[str, str]], temperature: float = 0.2, max_tokens: int = 1024, retries=2) -> str:
    if not MODEL_API_KEY:
        return "（LLM未配置）"
    headers = {
        "Authorization": f"Bearer {MODEL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False
    }
    last_err = None
    for _ in range(retries+1):
        try:
            r = requests.post(CHAT_COMPLETIONS, headers=headers, data=json.dumps(payload), timeout=60)
            if r.status_code == 200:
                js = r.json()
                return js.get("choices",[{}])[0].get("message",{}).get("content","").strip()
            else:
                last_err = f"{r.status_code} {r.text[:200]}"
                log.warning("LLM call failed: %s", last_err)
                time.sleep(1.2)
        except Exception as e:
            last_err = str(e)
            log.warning("LLM call exception: %s", e)
            time.sleep(1.2)
    return f"（LLM调用失败：{last_err}）"


# -----------------------------
# 评估与打分（A+、契合点、风险点、时间线Remark、年龄估算）
# -----------------------------
SCORE_PROMPT = """你是资深招聘专家。基于候选人简历文本与岗位需求进行快速评估。
请输出一个 JSON，字段：
- score: 0~100 的整数（70为合格线，85+为优秀，95+为A+）
- level: A+ / A / B / C
- fit_points: 3~6条契合要点，简洁短句
- risk_points: 3~6条风险点（如年限不符、缺核心、跳槽频繁、领域错配等）
- timeline_remark: 一段“教育+工作时间线”概述。格式示例：
  "2009-2013 本科 计算机科学 XX大学；2013-2017 XX公司 软件工程师（后端开发）；2017-2021 XX公司 资深工程师（云平台）；2021-至今 XX公司 技术经理（负责K8s平台与DevOps）"
- age_guess: 整数或 "不详"。估算方法：若能识别本科入学年份 y，则岁数 ≈ (当前年份 - y + 18)；若只看到本科毕业年份 yb，则岁数 ≈ (当前年份 - yb + 22)；否则写 "不详"。

岗位信息：
{jd}

必须严格输出 JSON，别写多余说明。
"""

def evaluate_resume(text: str, jd: str) -> Dict[str, Any]:
    messages = [
        {"role":"system","content":"你是严格、可靠的人才评估助手，只输出指令中要求的结构化信息。"},
        {"role":"user","content": SCORE_PROMPT.format(jd=jd) + "\n\n====候选人简历====\n" + text[:25000]}
    ]
    out = call_llm(messages, temperature=0.1, max_tokens=1200)
    # 容错解析 JSON
    m = re.search(r"\{.*\}", out, flags=re.S)
    js = {}
    if m:
        try:
            js = json.loads(m.group(0))
        except Exception:
            pass
    # 默认兜底
    score = int(js.get("score", 0) or 0)
    level = js.get("level") or ("A+" if score>=95 else "A" if score>=85 else "B" if score>=70 else "C")
    fit_points = js.get("fit_points") or []
    risk_points = js.get("risk_points") or []
    timeline_remark = js.get("timeline_remark") or ""
    age_guess = js.get("age_guess") or "不详"
    return {
        "score": score,
        "level": level,
        "fit": fit_points,
        "risk": risk_points,
        "timeline": timeline_remark,
        "age": age_guess
    }


# -----------------------------
# Excel 导出
# -----------------------------
def export_xlsx(rows: List[Dict[str, Any]]) -> bytes:
    if not openpyxl:
        # 极少数环境没装 openpyxl，给个 CSV 兜底
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["姓名/文件","评分","等级","年龄","契合点","风险点","Remark（教育+工作时间线）"])
        for r in rows:
            w.writerow([
                r.get("name",""),
                r.get("score",""),
                r.get("level",""),
                r.get("age",""),
                "；".join(r.get("fit",[])),
                "；".join(r.get("risk",[])),
                r.get("timeline","")
            ])
        return buf.getvalue().encode("utf-8", "ignore")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Ranking"
    ws.append(["姓名/文件","评分","等级","年龄","契合点","风险点","Remark（教育+工作时间线）"])

    for r in rows:
        ws.append([
            r.get("name",""),
            r.get("score",""),
            r.get("level",""),
            r.get("age",""),
            "；".join(r.get("fit",[])),
            "；".join(r.get("risk",[])),
            r.get("timeline","")
        ])
    # 简单美化
    ws.column_dimensions['A'].width = 36
    ws.column_dimensions['B'].width = 8
    ws.column_dimensions['C'].width = 6
    ws.column_dimensions['D'].width = 8
    ws.column_dimensions['E'].width = 40
    ws.column_dimensions['F'].width = 40
    ws.column_dimensions['G'].width = 60
    ws.freeze_panes = "A2"
    ws["A1"].font = ws["B1"].font = ws["C1"].font = ws["D1"].font = ws["E1"].font = ws["F1"].font = ws["G1"].font = Font(bold=True)
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


# -----------------------------
# 任务执行
# -----------------------------
def enqueue(rid: str, q: Queue, txts: List[Dict[str, Any]], jd: str):
    """消费者：逐个调用评估，发 SSE 日志，同时落结果"""
    results = []
    for idx, item in enumerate(txts, 1):
        name = item["name"]
        q.put(f"[{idx}/{len(txts)}] 读取 {name}\n")
        eva = evaluate_resume(item["text"], jd)
        q.put(f"  → 评分 {eva['score']} / 等级 {eva['level']}\n")
        results.append({
            "name": name,
            **eva
        })
    with J_LOCK:
        job = JOBS.get(rid, {})
        job["results"] = results
        job["done"] = True


def make_job_name(role: str) -> str:
    dt = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9_]+","_",role).strip("_")
    return f"{safe}_{dt}"


# -----------------------------
# 路由
# -----------------------------
@app.route("/", methods=["GET"])
def index():
    jobs = []
    with J_LOCK:
        for rid, j in list(JOBS.items())[-10:][::-1]:
            jobs.append((rid, {"name": j.get("name",""), "created": j.get("created","")}))
    return render_template_string(INDEX_HTML, jobs=jobs, max_mb=MAX_UPLOAD_MB)

@app.route("/process", methods=["POST"])
def process():
    role = (request.form.get("role") or "").strip()
    if not role:
        return "职位名称必填", 400
    track = (request.form.get("track") or "").strip()
    min_years = (request.form.get("min_years") or "").strip()
    location = (request.form.get("location") or "").strip()
    must = (request.form.get("must") or "").strip()
    nice = (request.form.get("nice") or "").strip()
    note = (request.form.get("note") or "").strip()

    jd = f"""岗位：{role}
方向：{track}
最低年限：{min_years}
地域/签证：{location}
Must-have：{must}
Nice-to-have：{nice}
补充说明（JD）：{note}
"""

    # 读取上传
    files = request.files.getlist("files")
    raw_items: List[Dict[str, Any]] = []

    def push_one(name: str, data: bytes):
        txt = read_text_from_bytes(name, data)
        if txt and txt.strip():
            raw_items.append({"name": name, "text": txt})

    for f in files:
        name = f.filename or "unnamed"
        data = f.read()
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for info in z.infolist():
                        if info.is_dir(): 
                            continue
                        n = info.filename.split("/")[-1]
                        if not n: 
                            continue
                        push_one(n, z.read(info))
            except Exception as e:
                log.warning("bad zip: %s", e)
        else:
            push_one(name, data)

    # 去重（按内容hash）
    uniq = {}
    for it in raw_items:
        h = hashlib.sha1(it["text"].encode("utf-8","ignore")).hexdigest()
        if h not in uniq:
            uniq[h] = it
    items = list(uniq.values())

    rid = uuid.uuid4().hex[:8]
    with J_LOCK:
        JOBS[rid] = {
            "rid": rid,
            "name": make_job_name(role),
            "created": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            "jd": jd,
            "items": items,
            "q": Queue(),
            "done": False,
            "results": []
        }

    # 启动后台线程
    t = Thread(target=enqueue, args=(rid, JOBS[rid]["q"], items, jd), daemon=True)
    t.start()
    return redirect(url_for("events", rid=rid))

@app.route("/events/<rid>")
def events(rid):
    with J_LOCK:
        job = JOBS.get(rid)
    if not job:
        return "任务不存在", 404
    return render_template_string(EVENTS_HTML, rid=rid, name=job.get("name",""))

@app.route("/stream/<rid>")
def stream(rid):
    def gen():
        with J_LOCK:
            job = JOBS.get(rid)
        if not job:
            yield "data: 任务不存在\n\n"; return
        q: Queue = job["q"]

        yield "data: 连接已建立\\n\n\n"
        while True:
            with J_LOCK:
                done = job.get("done", False)
            try:
                msg = q.get(timeout=1.0)
                safe = str(msg).replace("\r"," ").replace("\n","\n")
                yield f"data: {safe}\n\n"
            except Exception:
                if done:
                    yield "data: __DONE__\n\n"
                    return
    headers = {
        "Content-Type":"text/event-stream",
        "Cache-Control":"no-cache",
        "X-Accel-Buffering":"no",
        "Connection":"keep-alive"
    }
    return Response(gen(), headers=headers)

@app.route("/resume/<rid>")
def resume(rid):
    """断点续跑：对于未完成的任务再次拉起消费者（若前一次异常中断）"""
    with J_LOCK:
        job = JOBS.get(rid)
        if not job:
            return redirect("/")
        if not job.get("done") and job.get("items") and job.get("q"):
            # 如果队列已经空了但未 done，重新启动
            t = Thread(target=enqueue, args=(rid, job["q"], job["items"], job["jd"]), daemon=True)
            t.start()
    return redirect(url_for("events", rid=rid))

@app.route("/xlsx/<rid>")
def xlsx(rid):
    with J_LOCK:
        job = JOBS.get(rid)
    if not job:
        return "任务不存在", 404
    if not job.get("done"):
        return "任务未完成", 400
    rows = job.get("results", [])
    data = export_xlsx(rows)
    fname = f"{job.get('name','result')}.xlsx" if openpyxl else f"{job.get('name','result')}.csv"
    return send_file(io.BytesIO(data), as_attachment=True, download_name=fname)

@app.route("/rank/<rid>")
def rank(rid):
    with J_LOCK:
        job = JOBS.get(rid)
    if not job:
        return "任务不存在", 404
    results = sorted(job.get("results", []), key=lambda x:(-x.get("score",0), x.get("name","")))
    # 简单文本榜单
    buf = io.StringIO()
    for i,r in enumerate(results,1):
        buf.write(f"[{i}] {r.get('name')} → {r.get('level','')}/{r.get('score','')}; 年龄：{r.get('age','')}\n  契合：{'；'.join(r.get('fit',[]))}\n  风险：{'；'.join(r.get('risk',[]))}\n  Remark：{r.get('timeline','')}\n\n")
    return Response(buf.getvalue(), mimetype="text/plain; charset=utf-8")


# -----------------------------
# 入口
# -----------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT","10000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
