import base64
import io
import os
import tempfile

import pandas as pd
import requests
from flask import Flask, jsonify, render_template, request
from pdf2image import convert_from_bytes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)


OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_HEALTH_URL = "http://localhost:11434/api/tags"
MODEL_NAME = "qwen2.5vl:7b"
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v6")
PROMPT_FILE_MAP = {
    "common": "common.md",
    "excel": "excel.md",
}


def get_available_prompt_versions() -> list[str]:
    if not os.path.isdir(PROMPTS_DIR):
        return []
    return sorted(
        name
        for name in os.listdir(PROMPTS_DIR)
        if os.path.isdir(os.path.join(PROMPTS_DIR, name))
    )


def read_prompt_template(prompt_type: str, version: str | None = None) -> str:
    prompt_file = PROMPT_FILE_MAP.get(prompt_type)
    if not prompt_file:
        raise ValueError(f"未知提示词类型：{prompt_type}")

    active_version = version or PROMPT_VERSION
    prompt_path = os.path.join(PROMPTS_DIR, active_version, prompt_file)
    if not os.path.exists(prompt_path):
        versions = ", ".join(get_available_prompt_versions()) or "无"
        raise FileNotFoundError(
            f"提示词文件不存在：{prompt_path}；当前可用版本：{versions}"
        )

    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read().strip()


def get_image_prompt() -> str:
    return read_prompt_template("common")


def get_word_prompt() -> str:
    return read_prompt_template("common")


def build_excel_prompt(excel_text: str) -> str:
    prompt = read_prompt_template("excel")
    placeholder = "{{EXCEL_TEXT}}"
    if placeholder in prompt:
        return prompt.replace(placeholder, excel_text)
    return prompt + "\n\n## Excel 表格数据\n\n" + excel_text



def sanitize_text(value: object) -> str:
    text = value if isinstance(value, str) else str(value)
    return ''.join(ch for ch in text if not 0xD800 <= ord(ch) <= 0xDFFF)


def check_ollama_connection() -> None:
    """启动时检测 Ollama 服务是否可达"""
    try:
        resp = requests.get(OLLAMA_HEALTH_URL, timeout=5)
        resp.raise_for_status()
        models = [m.get("name", "") for m in resp.json().get("models", [])]
        print(f"[OK] Ollama 连接成功，已加载模型：{models}")
        if MODEL_NAME not in models:
            print(f"[WARN] 模型 {MODEL_NAME} 未在列表中，请确认已拉取")
    except requests.exceptions.ConnectionError:
        print("[ERROR] 无法连接到 Ollama，请确认 Ollama 已启动（端口 11434）")
    except Exception as e:
        print(f"[ERROR] Ollama 检测异常：{sanitize_text(e)}")


@app.errorhandler(Exception)
def handle_exception(e):
    """全局异常兜底：确保始终返回 JSON，避免前端 resp.json() 解析 HTML 报错"""
    import traceback

    print(f"[ERROR] 未捕获异常：{traceback.format_exc()}")
    return jsonify({"error": f"服务器内部错误：{sanitize_text(e)}"}), 500


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    # 多文件上传：前端使用同一个 key=file 多次 append，后端统一用 getlist 接收。
    files = [file for file in request.files.getlist("file") if file and file.filename]
    if not files:
        return jsonify({"error": "未收到文件"}), 400

    def call_ollama(prompt, images_b64=None):
        payload = {"model": MODEL_NAME, "prompt": prompt, "stream": False}
        if images_b64:
            payload["images"] = images_b64
        try:
            resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
            if not resp.ok:
                try:
                    err_msg = resp.json().get("error", resp.text[:300])
                except Exception:
                    err_msg = resp.text[:300]
                return jsonify({"error": f"Ollama 返回错误（HTTP {resp.status_code}）：{sanitize_text(err_msg)}"}), 502
            result = resp.json().get("response", "模型未返回内容")
            return jsonify({"result": sanitize_text(result)})
        except requests.exceptions.ConnectionError:
            return jsonify({"error": "无法连接到 Ollama 服务，请确认 Ollama 已启动（默认端口 11434）"}), 502
        except requests.exceptions.Timeout:
            return jsonify({"error": "模型响应超时，请稍后重试"}), 504
        except Exception as e:
            return jsonify({"error": sanitize_text(e)}), 500

    # 所有图片、PDF 页、Word 页统一汇总到一个图片列表；所有 Excel 文本统一汇总后插入 prompt。
    images_b64 = []
    excel_parts = []

    for file in files:
        fname = file.filename or ""
        filename_ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

        is_excel = filename_ext in ("xls", "xlsx") or file.content_type in (
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        is_word = filename_ext in ("doc", "docx") or file.content_type in (
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        is_pdf = file.content_type == "application/pdf" or filename_ext == "pdf"
        is_image = file.content_type in ("image/jpeg", "image/png") or filename_ext in ("jpg", "jpeg", "png")

        if not any([is_excel, is_word, is_pdf, is_image]):
            return jsonify({"error": f"文件格式不支持：{sanitize_text(fname)}，仅支持 JPG、PNG、PDF、XLS、XLSX、DOC、DOCX"}), 400

        # 1) 原生图片：直接转 base64，保持现有逻辑。
        if is_image:
            try:
                images_b64.append(base64.b64encode(file.read()).decode("utf-8"))
            except Exception as e:
                return jsonify({"error": f"图片读取失败（{sanitize_text(fname)}）：{sanitize_text(e)}"}), 500
            continue

        # 2) PDF：逐页转 PNG 后追加到同一个 images_b64 列表。
        if is_pdf:
            try:
                pages = convert_from_bytes(file.read(), dpi=200, fmt="png")
            except Exception as e:
                return jsonify({"error": f"PDF 转换失败，请确认已安装 poppler（{sanitize_text(fname)}）：{sanitize_text(e)}"}), 500
            for page in pages:
                buf = io.BytesIO()
                page.save(buf, format="PNG")
                images_b64.append(base64.b64encode(buf.getvalue()).decode("utf-8"))
            continue

        # 3) Word：先转 PDF，再逐页转图片，保持现有 docx2pdf + pdf2image 逻辑。
        if is_word:
            try:
                from docx2pdf import convert as docx2pdf_convert
            except ImportError:
                return jsonify({"error": "缺少 docx2pdf 依赖，请执行: pip install docx2pdf"}), 500
            try:
                word_bytes = file.read()
                with tempfile.TemporaryDirectory() as tmpdir:
                    suffix = f".{filename_ext}" if filename_ext in ("doc", "docx") else ".docx"
                    word_path = os.path.join(tmpdir, f"input{suffix}")
                    pdf_path = os.path.join(tmpdir, "output.pdf")
                    with open(word_path, "wb") as f_tmp:
                        f_tmp.write(word_bytes)
                    try:
                        import pythoncom
                        pythoncom.CoInitialize()
                    except ImportError:
                        pass
                    try:
                        docx2pdf_convert(word_path, pdf_path)
                    finally:
                        try:
                            import pythoncom
                            pythoncom.CoUninitialize()
                        except ImportError:
                            pass
                    with open(pdf_path, "rb") as f_pdf:
                        pages = convert_from_bytes(f_pdf.read(), dpi=200, fmt="png")
            except Exception as e:
                return jsonify({"error": f"Word 转换失败（需本机已安装 Microsoft Word，文件：{sanitize_text(fname)}）：{sanitize_text(e)}"}), 500
            for page in pages:
                buf = io.BytesIO()
                page.save(buf, format="PNG")
                images_b64.append(base64.b64encode(buf.getvalue()).decode("utf-8"))
            continue

        # 4) Excel：读取所有 sheet，转成文本后按文件名收集，最后统一拼接到 prompt。
        if is_excel:
            try:
                file_bytes = file.read()
                if filename_ext == "xls":
                    try:
                        xls = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, engine="xlrd")
                    except Exception:
                        xls = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, engine="openpyxl")
                else:
                    xls = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, engine="openpyxl")
            except Exception as e:
                return jsonify({"error": f"Excel 解析失败（{sanitize_text(fname)}）：{sanitize_text(e)}"}), 500

            try:
                parts = []
                for sheet_name, df in xls.items():
                    df = df.fillna("").astype(str)
                    parts.append(f"[Sheet: {sheet_name}]\n{df.to_string(index=False)}")
                excel_parts.append((fname, "\n\n".join(parts)))
            except Exception as e:
                return jsonify({"error": f"Excel 数据转换失败（{sanitize_text(fname)}）：{sanitize_text(e)}"}), 500

    if not images_b64 and not excel_parts:
        return jsonify({"error": "未得到可分析的图片或表格内容"}), 400

    excel_text = "\n\n".join(
        f"[Excel 文件: {fname}]\n{part}"
        for fname, part in excel_parts
    )

    # 统一只调用一次模型：有 Excel 时使用 excel prompt，否则使用 common prompt。
    prompt = build_excel_prompt(excel_text) if excel_parts else get_image_prompt()
    return call_ollama(prompt, images_b64 if images_b64 else None)


if __name__ == "__main__":
    check_ollama_connection()
    app.run(host="0.0.0.0", port=5000, debug=True)
