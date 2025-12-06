import os
import json
import hashlib
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
import pdfplumber
import redis
from openai import OpenAI  # 引入 OpenAI 客户端
from dotenv import load_dotenv

# 1. 加载环境变量
load_dotenv()

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

app = Flask(__name__)
CORS(app)

# 2. 初始化 OpenAI 客户端 (适配阿里云百炼)
# 从环境变量获取配置，如果没获取到则使用默认值
api_key = os.getenv("DASHSCOPE_API_KEY")
base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

# 初始化 client
client = OpenAI(
    api_key=api_key,
    base_url=base_url
)

# 配置 Redis (可选)
REDIS_HOST = os.getenv("REDIS_HOST", "")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")


redis_client = None
if REDIS_HOST:
    try:
        # 创建一个临时变量尝试连接
        client_test = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
        client_test.ping() # 尝试 Ping
        
        # 只有 Ping 成功了，才赋值给全局变量
        redis_client = client_test
        logger.info("Redis connected successfully.")
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}")
        # 【关键修改】确保失败时变量为 None，防止后续逻辑误判
        redis_client = None 

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

def analyze_with_llm(resume_text, job_description):
    """使用 OpenAI 兼容接口调用通义千问"""
    
    prompt = f"""
    你是一个专业的资深 HR。请根据以下【简历内容】和【岗位描述】，完成两个任务：
    1. 提取简历关键信息。
    2. 计算简历与岗位的匹配度并给出理由。

    【简历内容开始】
    {resume_text[:3000]} 
    【简历内容结束】

    【岗位描述开始】
    {job_description[:1000]}
    【岗位描述结束】

    请严格以 JSON 格式返回，不要包含 markdown 格式标记（如 ```json），直接返回 JSON 字符串。JSON 结构如下：
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
            "reason": "简短的评分理由(50字以内)",
            "matching_keywords": ["匹配技能1", "匹配技能2"],
            "missing_keywords": ["缺失技能1"]
        }}
    }}
    """

    try:
        # --- 核心修改：使用 client.chat.completions.create ---
        completion = client.chat.completions.create(
            model="qwen-plus",  # 或者 "qwen-turbo"
            messages=[
                {'role': 'system', 'content': 'You are a helpful HR assistant.'},
                {'role': 'user', 'content': prompt}
            ],
            # 可选：设置 temperature 控制随机性，0.1 比较严谨
            temperature=0.1, 
        )
        
        # 获取返回内容
        content = completion.choices[0].message.content
        
        # 清洗可能存在的 Markdown 标记 (AI 有时还是会加 ```json)
        content = content.replace("```json", "").replace("```", "").strip()
        
        return json.loads(content)

    except json.JSONDecodeError:
        logger.error("Failed to parse JSON from LLM response")
        return None
    except Exception as e:
        logger.error(f"OpenAI Client Error: {e}")
        return None

@app.route('/analyze', methods=['POST'])
def analyze_resume():
    if 'resume' not in request.files:
        return jsonify({"error": "No resume file uploaded"}), 400
    
    file = request.files['resume']
    jd = request.form.get('jd', '通用岗位')

    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    try:
        # 1. 解析 PDF
        resume_text = extract_text_from_pdf(file)
        if not resume_text:
            return jsonify({"error": "Failed to extract text from PDF"}), 400

        # 2. 缓存检查
        cache_key = None
        if redis_client:
            fingerprint = hashlib.md5((resume_text[:100] + jd).encode('utf-8')).hexdigest()
            cache_key = f"resume_analysis:{fingerprint}"
            cached_result = redis_client.get(cache_key)
            if cached_result:
                logger.info("Cache hit!")
                return jsonify(json.loads(cached_result))

        # 3. AI 分析
        result = analyze_with_llm(resume_text, jd)
        if not result:
            return jsonify({"error": "AI analysis failed"}), 500

        # 4. 写入缓存
        if redis_client and cache_key:
            redis_client.setex(cache_key, 3600, json.dumps(result))

        return jsonify(result)

    except Exception as e:
        logger.error(f"Server Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9000)