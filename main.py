import hashlib
import json
import logging
import os
import re
import sys

# FC Custom Runtime does not load the bundled /code/python dependencies by default.
FC_VENDOR_DIR = "/code/python"
if os.path.isdir(FC_VENDOR_DIR):
    sys.path.insert(0, FC_VENDOR_DIR)

import redis
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from openai import OpenAI
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from werkzeug.exceptions import RequestEntityTooLarge

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", 10 * 1024 * 1024))
MAX_RESUME_CHARS = int(os.getenv("MAX_RESUME_CHARS", 12000))
MAX_JD_CHARS = int(os.getenv("MAX_JD_CHARS", 4000))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", 3600))
DEFAULT_MODEL = os.getenv("DASHSCOPE_MODEL", "qwen-plus")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS", "https://shentuyvlv.github.io,http://127.0.0.1:9000,http://localhost:9000"
    ).split(",")
    if origin.strip()
]
CORS(app, resources={r"/analyze": {"origins": allowed_origins}})

api_key = os.getenv("DASHSCOPE_API_KEY")
base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
llm_client = OpenAI(api_key=api_key, base_url=base_url) if api_key else None


def create_redis_client():
    host = os.getenv("REDIS_HOST", "").strip()
    if not host:
        return None

    try:
        client = redis.Redis(
            host=host,
            port=int(os.getenv("REDIS_PORT", 6379)),
            password=os.getenv("REDIS_PASSWORD", ""),
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        logger.info("Redis connected successfully.")
        return client
    except Exception as error:
        logger.warning("Redis unavailable: %s", error)
        return None


redis_client = create_redis_client()


@app.route("/", methods=["GET"])
def index_page():
    return send_from_directory(app.root_path, "index.html")


@app.route("/favicon.ico", methods=["GET"])
def favicon():
    return ("", 204)


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "redis_enabled": bool(redis_client)})


@app.errorhandler(RequestEntityTooLarge)
def handle_large_file(_error):
    return jsonify({"error": f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 限制"}), 413


def clean_resume_text(raw_text):
    """保留段落语义，消除 PDF 提取后常见的空白和控制字符。"""
    lines = []
    for raw_line in raw_text.replace("\r", "\n").split("\n"):
        line = re.sub(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f]", "", raw_line)
        line = re.sub(r"[ \t\u00a0]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def extract_resume_content(file_stream):
    """读取多页、非加密的文本型 PDF，并返回清洗后的文本及元数据。"""
    try:
        reader = PdfReader(file_stream)
        if reader.is_encrypted:
            return None, "加密 PDF 暂不支持，请上传未加密文件"

        pages = [page.extract_text() or "" for page in reader.pages]
        text = clean_resume_text("\n".join(pages))
        if not text:
            return None, "无法从 PDF 提取文本，请确认不是扫描图片版简历"

        return {
            "text": text,
            "page_count": len(pages),
            "text_length": len(text),
        }, None
    except (PdfReadError, ValueError) as error:
        logger.info("Invalid PDF upload: %s", error)
        return None, "PDF 文件无效或已损坏"
    except Exception as error:
        logger.exception("PDF parsing failed")
        return None, "PDF 解析失败，请更换文件后重试"


def parse_llm_json(content):
    """兼容 Markdown 围栏和模型输出前后的少量解释文字。"""
    cleaned = content.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start < 0:
            raise
        payload, _ = json.JSONDecoder().raw_decode(cleaned[start:])
        return payload


def normalize_analysis_result(payload):
    """将模型输出收敛为稳定的 API 契约，避免前端因缺字段崩溃。"""
    payload = payload if isinstance(payload, dict) else {}
    basic = payload.get("basic_info") if isinstance(payload.get("basic_info"), dict) else {}
    other = payload.get("other_info") if isinstance(payload.get("other_info"), dict) else {}
    job = payload.get("job_analysis") if isinstance(payload.get("job_analysis"), dict) else {}
    matching = payload.get("matching_analysis") if isinstance(payload.get("matching_analysis"), dict) else {}

    def text_value(source, key):
        value = source.get(key, "未知")
        return str(value).strip()[:1000] or "未知"

    def string_list(source, key):
        value = source.get(key, [])
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:100] for item in value if str(item).strip()][:20]

    try:
        score = max(0, min(100, int(float(matching.get("score", 0)))))
    except (TypeError, ValueError):
        score = 0

    def percentage(key):
        try:
            return max(0, min(100, int(float(matching.get(key, 0)))))
        except (TypeError, ValueError):
            return 0

    return {
        "basic_info": {
            "name": text_value(basic, "name"),
            "phone": text_value(basic, "phone"),
            "email": text_value(basic, "email"),
            "address": text_value(basic, "address"),
        },
        "other_info": {
            "education": text_value(other, "education"),
            "years_of_experience": text_value(other, "years_of_experience"),
            "intent": text_value(other, "intent"),
            "expected_salary": text_value(other, "expected_salary"),
            "project_experience": string_list(other, "project_experience"),
        },
        "job_analysis": {
            "keywords": string_list(job, "keywords"),
            "required_skills": string_list(job, "required_skills"),
            "preferred_skills": string_list(job, "preferred_skills"),
        },
        "matching_analysis": {
            "score": score,
            "skills_match_rate": percentage("skills_match_rate"),
            "experience_relevance": percentage("experience_relevance"),
            "reason": text_value(matching, "reason"),
            "matching_keywords": string_list(matching, "matching_keywords"),
            "missing_keywords": string_list(matching, "missing_keywords"),
        },
    }


def analyze_with_llm(resume_text, job_description, model):
    if not llm_client:
        return None

    prompt = f"""
你是一名严谨的招聘分析助手。根据【简历】和【岗位描述】输出候选人分析。
只依据提供的文本，不得编造；缺失信息统一写"未知"，数组无内容时返回 []。

【简历】
{resume_text[:MAX_RESUME_CHARS]}

【岗位描述】
{job_description[:MAX_JD_CHARS]}

严格返回 JSON，不要 Markdown 或解释，格式：
{{
  "basic_info": {{"name": "", "phone": "", "email": "", "address": ""}},
  "other_info": {{
    "education": "最高学历和学校", "years_of_experience": "", "intent": "",
    "expected_salary": "", "project_experience": ["项目名称：与岗位相关的成果"]
  }},
  "job_analysis": {{
    "keywords": ["岗位关键词"], "required_skills": ["必需技能"], "preferred_skills": ["加分技能"]
  }},
  "matching_analysis": {{
    "score": 0,
    "skills_match_rate": 0,
    "experience_relevance": 0,
    "reason": "80字以内，说明优势与风险",
    "matching_keywords": [""],
    "missing_keywords": [""]
  }}
}}
评分规则：score 为 0-100 整数，技能匹配率和经验相关性也为 0-100；必须结合 JD 评估，不因简历信息缺失给出高分。
"""

    try:
        completion = llm_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a precise HR analytics assistant. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        content = completion.choices[0].message.content or "{}"
        return normalize_analysis_result(parse_llm_json(content))
    except json.JSONDecodeError:
        logger.warning("LLM returned invalid JSON")
    except Exception as error:
        logger.exception("LLM request failed: %s", error)
    return None


def build_cache_key(model, resume_text, job_description):
    fingerprint = hashlib.sha256(
        f"{model}\n{resume_text}\n{job_description}".encode("utf-8")
    ).hexdigest()
    return f"resume_analysis:v2:{fingerprint}"


@app.route("/analyze", methods=["POST"])
def analyze_resume():
    if "resume" not in request.files:
        return jsonify({"error": "请上传简历 PDF 文件"}), 400

    file = request.files["resume"]
    job_description = clean_resume_text(request.form.get("jd", ""))
    model = DEFAULT_MODEL.strip()

    if not file.filename:
        return jsonify({"error": "请选择简历文件"}), 400
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "仅支持 PDF 简历"}), 400
    if len(job_description) < 20:
        return jsonify({"error": "岗位描述至少需要 20 个字符"}), 400
    if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,80}", model):
        return jsonify({"error": "服务端模型配置无效"}), 500
    if not llm_client:
        return jsonify({"error": "服务端未配置 DASHSCOPE_API_KEY"}), 500

    resume, parse_error = extract_resume_content(file)
    if parse_error:
        return jsonify({"error": parse_error}), 400

    cache_key = build_cache_key(model, resume["text"], job_description)
    if redis_client:
        try:
            cached_result = redis_client.get(cache_key)
            if cached_result:
                payload = json.loads(cached_result)
                payload["meta"]["cache_hit"] = True
                return jsonify(payload)
        except Exception as error:
            logger.warning("Redis read failed: %s", error)

    result = analyze_with_llm(resume["text"], job_description, model)
    if not result:
        return jsonify({"error": "AI 分析失败，请稍后重试或查看服务端日志"}), 502

    result["meta"] = {
        "page_count": resume["page_count"],
        "text_length": resume["text_length"],
        "cache_hit": False,
    }

    if redis_client:
        try:
            redis_client.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result, ensure_ascii=False))
        except Exception as error:
            logger.warning("Redis write failed: %s", error)

    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 9000)))
