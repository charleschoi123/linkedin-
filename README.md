# Alsos Talent · 合规AI自动化寻访（MVP）

> 合规前提：**仅处理你手动/系统导出的 ZIP/PDF/HTML/CSV/文本**。不做任何自动登录、自动点开或爬虫行为。

## 一、功能
- 直接上传 **Recruiter Lite 导出的 ZIP**（每包 25 人，可一次性 8 包=200人）。
- 自动解压并解析 **HTML / PDF / DOCX / TXT / CSV**。
- 批量调用 DeepSeek / OpenAI（OpenAI 兼容接口），**评分与分桶**（A+/A/B/C）。
- 输出 **Excel《候选清单》**（固定13列，含下拉校验）。
- **年龄预估**：仅当识别到“本科入学年份”时 → 出生≈入学年-18 → “约YY年生”；否则“不详”。
- **去重**：姓名 + 文本指纹（MinHash 近似）。

## 二、部署（Render）
1. 推送本仓库到 GitHub（`app.py` + `requirements.txt`）。
2. 在 Render 新建 Web Service：  
   - Build Command：`pip install -r requirements.txt`  
   - Start Command：`gunicorn app:app`  
   - 环境变量：
     - `MODEL_API_KEY`（DeepSeek 或 OpenAI Key）
     - `MODEL_BASE_URL`（DeepSeek 的 OpenAI 兼容 Base URL，如 `https://api.deepseek.com`）
     - `MODEL_NAME`（如 `deepseek-chat`）
     - `MAX_WORKERS`（建议 2~4，默认 3）
3. 打开服务地址，上传 ZIP 测试（先 1 包，再 8 包）。

## 三、本地运行
```bash
pip install -r requirements.txt
gunicorn app:app
# 或：python app.py
