# AI 赋能的智能简历分析系统

面向招聘场景的单页简历筛选工具：上传单个 PDF 简历并输入岗位 JD，服务端提取文本、调用百炼 OpenAI 兼容模型生成结构化候选人信息和可解释的岗位匹配评分。

线上页面：<https://shentuyvlv.github.io/>

## 架构

```text
GitHub Pages 单页前端
        |
        | multipart/form-data (PDF + JD)
        v
阿里云函数计算 FC /analyze
        |-- PyPDF：多页 PDF 文本提取与清洗
        |-- 百炼 OpenAI 兼容接口：信息提取、JD 关键词、匹配评分
        `-- Redis（可选）：相同简历 + JD 结果缓存
```

## 功能与接口

- `POST /analyze`：接收 `resume`（单个 PDF）和 `jd`（岗位描述），返回 JSON。
- PDF 支持多页文本提取、冗余空白清洗、加密/损坏/扫描版文件提示，以及 10MB 上传限制。
- 返回姓名、电话、邮箱、地址、求职意向、期望薪资、学历、工作年限、相关项目经历。
- 对 JD 提取岗位关键词、必需技能、加分技能；返回总匹配分、技能匹配率、经验相关性、已匹配和缺失关键词、评分依据。
- Redis 可选缓存。缓存键由完整简历文本、JD 和服务端模型配置组成，默认有效期一小时。

请求示例：

```bash
curl -X POST http://127.0.0.1:9000/analyze \
  -F "resume=@./resume.pdf" \
  -F "jd=3年以上 Python 开发经验，熟悉 Flask、Redis 和大模型应用。"
```

响应包含 `basic_info`、`other_info`、`job_analysis`、`matching_analysis` 和 `meta`。前端不暴露模型或密钥；模型通过服务端环境变量 `DASHSCOPE_MODEL` 配置。

## 本地运行

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

在 `.env` 中填写 `DASHSCOPE_API_KEY`，然后访问 <http://127.0.0.1:9000>。可选配置 Redis 后，服务启动日志会显示缓存是否已启用。

运行测试：

```bash
python -m unittest discover -s tests -v
```

## 部署

### 1. 部署后端到阿里云 FC

`s.yaml` 已配置为 Flask Custom Runtime。部署前将 `vars.functionName` 改为你已有的函数名；如果保留默认值，会新建 `ai-resume-analyzer`。不要把 `.env` 提交到 Git。

```bash
npm install -g @serverless-devs/s
s config add
set -a
source .env
set +a
s deploy
```

部署前请启动 Docker Desktop。部署完成后，FC 会输出 HTTP 域名。将该域名加上 `/analyze`，替换 [index.html](/Users/zed/all code/A我的/shentuyvlv.github.io/index.html:227) 中的 `PRODUCTION_API_URL`，然后提交并推送前端。函数计算在 Linux 运行，`s.yaml` 会将依赖安装到随代码上传的 `python/` 目录。

若使用 Redis，在 FC 控制台的环境变量中设置 `REDIS_HOST`、`REDIS_PORT`、`REDIS_PASSWORD`；同时把 `CORS_ORIGINS` 设置为你的 GitHub Pages 域名。FC 的 `DASHSCOPE_API_KEY` 必须只保存在环境变量中。

### 2. 部署前端到 GitHub Pages

仓库名是 `shentuyvlv.github.io`，公开仓库的根目录 `index.html` 即是 Pages 首页。在 GitHub 仓库进入 **Settings → Pages**，选择 **Deploy from a branch**，并设置 `main` / `root`。

之后每次 `git push origin main`，GitHub Pages 会自动重新构建和发布；在仓库的 **Actions** 页面可查看状态。GitHub Pages 只能托管静态前端，不能运行 Flask，因此 API 必须保留在 FC。

## 安全说明

- `.env` 已被 `.gitignore` 忽略，公开仓库可以正常使用；私有仓库不是保护 API Key 的替代品。
- 该仓库历史中曾提交过 `.env`，请立即在百炼控制台轮换 API Key。若确定要彻底公开历史，再使用 `git filter-repo` 或 BFG 清理旧提交中的密钥。
- 简历包含个人信息。生产场景建议给 FC 增加访问频率限制、日志脱敏、缓存过期策略和数据删除机制。

## 提交给面试官

发送以下三项：GitHub 仓库地址、线上演示地址、你的真实姓名与联系方式。
