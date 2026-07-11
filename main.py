import os
import json
import hashlib
import logging
import re
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import pdfplumber
import redis
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

app = Flask(__name__)
CORS(app)

api_key = os.getenv("DASHSCOPE_API_KEY")
base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
DEFAULT_MODEL = os.getenv("DASHSCOPE_MODEL", "deepseek-v4-pro")
MAX_RESUME_CHARS = int(os.getenv("MAX_RESUME_CHARS", 6000))
MAX_JD_CHARS = int(os.getenv("MAX_JD_CHARS", 3000))

client = OpenAI(
    api_key=api_key,
    base_url=base_url
)

REDIS_HOST = os.getenv("REDIS_HOST", "")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

redis_client = None
if REDIS_HOST:
    try:
        client_test = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
        client_test.ping()
        redis_client = client_test
        logger.info("Redis connected successfully.")
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}")
        redis_client = None


@app.route('/', methods=['GET'])
def index_page():
    return send_from_directory(app.root_path, 'index.html')


@app.route('/favicon.ico', methods=['GET'])
def favicon():
    return ('', 204)


def normalize_model(raw_model):
    """返回安全的模型名，默认使用环境变量配置。"""
    model = (raw_model or DEFAULT_MODEL).strip()
    if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,80}", model):
        return None
    return model


def extract_text_from_pdf(file_stream):
    """解析 PDF 提取文本"""
    text = ""
    try:
        with pdfplumber.open(file_stream) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        logger.error(f"PDF parsing error: {e}")
        return None
    return text.strip()


def parse_llm_json(content):
    """兼容模型偶尔返回 Markdown 包裹或解释性文本的情况。"""
    cleaned = content.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise
        return json.loads(match.group(0))


def analyze_with_llm(resume_text, job_description, model):
    """使用 OpenAI 兼容接口调用通义千问"""

    prompt = f"""
    你是一个专业的资深 HR。请根据以下【简历内容】和【岗位描述】，完成两个任务：
    1. 提取简历关键信息。
    2. 计算简历与岗位的匹配度并给出可解释理由。

    【简历内容开始】
    {resume_text[:MAX_RESUME_CHARS]}
    【简历内容结束】

    【岗位描述开始】
    {job_description[:MAX_JD_CHARS]}
    【岗位描述结束】

    请严格以 JSON 格式返回，不要包含 markdown 标记。JSON 结构如下：
    {{
        "basic_info": {{
            "name": "姓名",
            "phone": "电话",
            "email": "邮箱",
            "address": "地址(如果没有则填未知)"
        }},
        "other_info": {{
            "education": "最高学历及学校",
            "years_of_experience": "工作年限",
            "intent": "求职意向"
        }},
        "matching_analysis": {{
            "score": 0-100之间的整数评分,
            "reason": "简短的评分理由，说明匹配优势和主要风险，80字以内",
            "matching_keywords": ["匹配技能1", "匹配技能2"],
            "missing_keywords": ["缺失技能1"]
        }}
    }}
    """

    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': 'You are a helpful HR assistant.'},
                {'role': 'user', 'content': prompt}
            ],
            temperature=0.1,
        )

        content = completion.choices[0].message.content
        result = parse_llm_json(content)
        result["model"] = model
        return result

    except json.JSONDecodeError:
        logger.error("Failed to parse JSON from LLM response")
        return None
    except Exception as e:
        logger.error(f"OpenAI Client Error: {e}")
        return None


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "ok",
        "default_model": DEFAULT_MODEL,
        "redis_enabled": bool(redis_client)
    })


@app.route('/analyze', methods=['POST'])
def analyze_resume():
    if 'resume' not in request.files:
        return jsonify({"error": "请上传简历 PDF 文件"}), 400

    file = request.files['resume']
    jd = request.form.get('jd', '').strip()
    model = normalize_model(request.form.get('model'))

    if file.filename == '':
        return jsonify({"error": "请选择简历文件"}), 400
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({"error": "仅支持 PDF 简历"}), 400
    if not jd:
        return jsonify({"error": "请填写岗位描述 JD"}), 400
    if not model:
        return jsonify({"error": "model 仅支持字母、数字、点、下划线、短横线、冒号和斜杠，长度不超过 80"}), 400
    if not api_key:
        return jsonify({"error": "服务端未配置 DASHSCOPE_API_KEY"}), 500

    try:
        resume_text = extract_text_from_pdf(file)
        if not resume_text:
            return jsonify({"error": "无法从 PDF 提取文本，请确认不是扫描图片版简历"}), 400

        cache_key = None
        if redis_client:
            fingerprint = hashlib.md5((model + resume_text[:500] + jd).encode('utf-8')).hexdigest()
            cache_key = f"resume_analysis:{fingerprint}"
            cached_result = redis_client.get(cache_key)
            if cached_result:
                logger.info("Cache hit!")
                return jsonify(json.loads(cached_result))

        result = analyze_with_llm(resume_text, jd, model)
        if not result:
            return jsonify({"error": "AI 分析失败，请检查 model 是否可用或查看服务端日志"}), 500

        if redis_client and cache_key:
            redis_client.setex(cache_key, 3600, json.dumps(result, ensure_ascii=False))

        return jsonify(result)

    except Exception as e:
        logger.error(f"Server Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9000)
