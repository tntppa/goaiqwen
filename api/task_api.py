from __future__ import annotations

import base64
import io
import json
import re
import tempfile
import threading

from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import pandas as pd
import requests
from flask import Blueprint, jsonify, request
from pdf2image import convert_from_bytes


from config.generation_config import api_config, generation_kwargs, model_config, prompt_file_map, prompt_version, input_limits

api_bp = Blueprint("api", __name__, url_prefix="/api")
ROOT_DIR = Path(__file__).resolve().parents[1]
TEMP_DIR = ROOT_DIR / api_config.get("temp_dir", "temp")
PROMPTS_DIR = ROOT_DIR / "prompts"
OLLAMA_URL = model_config["ollama_url"]
MODEL_NAME = model_config["model_name"]
POLL_INTERVAL_SECONDS = 2
TASK_HTTP_SESSION = requests.Session()
TASK_HTTP_SESSION.trust_env = False
_polling_thread: threading.Thread | None = None

_polling_stop_event = threading.Event()
_polling_lock = threading.Lock()
DOWNLOAD_URL_KEYS = {
    "download_url",
    "downloadUrl",
    "oss_url",
    "ossUrl",
    "file_url",
    "fileUrl",
    "url",
}


def _extract_task_id(data: Any) -> Any:
    if isinstance(data, dict):
        if "task_id" in data:
            return data["task_id"]
        if "task" in data:
            return _extract_task_id(data["task"])
        if "data" in data:
            return _extract_task_id(data["data"])
        if "result" in data:
            return _extract_task_id(data["result"])
    return None


def _extract_task_payload(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None

    task = data.get("task")
    if isinstance(task, dict):
        return task

    for key in ("data", "result"):
        nested = _extract_task_payload(data.get(key))
        if nested:
            return nested

    return data if "task_id" in data else None


def _extract_task_urls(data: Any) -> list[str]:
    urls: list[str] = []

    if isinstance(data, dict):
        value = data.get("urls")
        if isinstance(value, list):
            urls.extend(item for item in value if isinstance(item, str) and item.startswith(("http://", "https://")))
        elif isinstance(value, str) and value.startswith(("http://", "https://")):
            urls.append(value)

        for key in ("task", "data", "result"):
            urls.extend(_extract_task_urls(data.get(key)))

    return list(dict.fromkeys(urls))


def _extract_download_url(data: Any) -> str | None:
    if isinstance(data, dict):
        for key in DOWNLOAD_URL_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value

        for value in data.values():
            found = _extract_download_url(value)
            if found:
                return found

    if isinstance(data, list):
        for item in data:
            found = _extract_download_url(item)
            if found:
                return found

    if isinstance(data, str) and data.startswith(("http://", "https://")):
        return data

    return None


def _build_task_file_url(task_id: str, file_ref: str) -> str:
    return api_config["task_file_url_template"].format(
        task_id=quote(task_id, safe=""),
        file_ref=quote(file_ref, safe=""),
    )


def _build_task_result_url(task_id: str) -> str:
    return api_config["task_result_url_template"].format(
        task_id=quote(task_id, safe=""),
    )


def _parse_response_data(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text.strip()


def _filename_from_content_disposition(value: str | None) -> str | None:

    if not value:
        return None

    utf8_match = re.search(r"filename\*=UTF-8''([^;]+)", value, flags=re.IGNORECASE)
    if utf8_match:
        return unquote(utf8_match.group(1).strip().strip('"'))

    match = re.search(r'filename="?([^";]+)"?', value, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()

    return None


def _safe_filename(filename: str) -> str:
    filename = unquote(filename).strip().replace("\\", "_").replace("/", "_")
    filename = re.sub(r'[<>:"|?*\x00-\x1f]', "_", filename)
    filename = filename.strip(" ._")
    return filename or "downloaded_file"


def _filename_from_download(download_url: str, file_ref: str, content_disposition: str | None) -> str:
    filename = _filename_from_content_disposition(content_disposition)
    if not filename:
        parsed_name = Path(unquote(urlparse(download_url).path)).name
        filename = parsed_name or Path(file_ref).name or f"task_file_{file_ref}"
    return _safe_filename(filename)


def _unique_path(directory: Path, filename: str) -> Path:
    target = directory / filename
    if not target.exists():
        return target

    stem = target.stem or "downloaded_file"
    suffix = target.suffix
    index = 1
    while True:
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _download_to_temp(download_url: str, task_id: str, file_ref: str, depth: int = 0) -> Path:
    if depth > 3:
        raise ValueError("下载地址跳转层级过深")

    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    response = TASK_HTTP_SESSION.get(download_url, stream=True, timeout=120)

    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "application/json" in content_type:
        data = response.json()
        real_download_url = _extract_download_url(data)
        if not real_download_url or real_download_url == download_url:
            raise ValueError("文件接口返回值中未找到有效下载地址")
        return _download_to_temp(real_download_url, task_id, file_ref, depth + 1)

    filename = _filename_from_download(
        download_url,
        file_ref,
        response.headers.get("Content-Disposition"),
    )
    task_dir = TEMP_DIR / _safe_filename(str(task_id))
    task_dir.mkdir(parents=True, exist_ok=True)
    save_path = _unique_path(task_dir, filename)

    with open(save_path, "wb") as file_obj:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                file_obj.write(chunk)

    return save_path


def _sanitize_text(value: object) -> str:
    text = value if isinstance(value, str) else str(value)
    return "".join(ch for ch in text if not 0xD800 <= ord(ch) <= 0xDFFF)


def _validate_generation_kwargs() -> None:
    do_sample = generation_kwargs.get("do_sample", False)
    num_beams = generation_kwargs.get("num_beams", 1)
    use_cache = generation_kwargs.get("use_cache", True)

    if not isinstance(do_sample, bool):
        raise ValueError("config/generation_config.py 中的 do_sample 必须为布尔值")
    if not isinstance(num_beams, int) or num_beams < 1:
        raise ValueError("config/generation_config.py 中的 num_beams 必须为大于等于 1 的整数")
    if num_beams != 1:
        raise ValueError("当前 Ollama 接口不支持 beam search，num_beams 必须保持为 1")
    if not isinstance(use_cache, bool):
        raise ValueError("config/generation_config.py 中的 use_cache 必须为布尔值")


def _build_ollama_options() -> dict[str, float | int]:
    _validate_generation_kwargs()
    options = {
        "temperature": generation_kwargs.get("temperature", 0.0),
        "top_p": generation_kwargs.get("top_p", 1.0),
        "repeat_penalty": generation_kwargs.get("repetition_penalty", 1.0),
        "num_predict": generation_kwargs.get("max_new_tokens", 4096),
        "num_ctx": generation_kwargs.get("num_ctx", 4096),
    }
    if generation_kwargs.get("do_sample") is False:
        options["top_k"] = 1
    return options


def _build_keep_alive() -> str | int:
    _validate_generation_kwargs()
    return "30m" if generation_kwargs.get("use_cache", True) else 0


def _read_prompt_template(prompt_type: str) -> str:
    prompt_file = prompt_file_map.get(prompt_type)
    if not prompt_file:
        raise ValueError(f"未知提示词类型：{prompt_type}")

    prompt_path = PROMPTS_DIR / prompt_version / prompt_file
    if not prompt_path.exists():
        raise FileNotFoundError(f"提示词文件不存在：{prompt_path}")

    return prompt_path.read_text(encoding="utf-8").strip()


def _build_excel_prompt(excel_text: str) -> str:
    prompt = _read_prompt_template("excel")
    placeholder = "{{EXCEL_TEXT}}"
    if placeholder in prompt:
        return prompt.replace(placeholder, excel_text)
    return prompt + "\n\n## Excel 表格数据\n\n" + excel_text


def _file_to_ollama_inputs(file_path: Path, images_b64: list[str], excel_parts: list[tuple[str, str]]) -> None:
    max_images = int(input_limits.get("max_images", 8))
    max_pdf_pages = int(input_limits.get("max_pdf_pages", 5))
    excel_max_rows = int(input_limits.get("excel_max_rows", 50))
    excel_max_cols = int(input_limits.get("excel_max_cols", 12))
    suffix = file_path.suffix.lower().lstrip(".")
    filename = file_path.name

    if suffix in ("jpg", "jpeg", "png"):
        images_b64.append(base64.b64encode(file_path.read_bytes()).decode("utf-8"))
        return

    if suffix == "pdf":
        pages = convert_from_bytes(file_path.read_bytes(), dpi=200, fmt="png")
        for page in pages:
            buf = io.BytesIO()
            page.save(buf, format="PNG")
            images_b64.append(base64.b64encode(buf.getvalue()).decode("utf-8"))
        return

    if suffix in ("xls", "xlsx"):
        file_bytes = file_path.read_bytes()
        if suffix == "xls":
            try:
                xls = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, engine="xlrd")
            except Exception:
                xls = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, engine="openpyxl")
        else:
            xls = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, engine="openpyxl")

        parts = []
        for sheet_name, df in xls.items():
            df = df.fillna("").astype(str)
            df = df.iloc[:excel_max_rows, :excel_max_cols]
            parts.append(f"[Sheet: {sheet_name}]\n{df.to_string(index=False)}")
        excel_parts.append((filename, "\n\n".join(parts)))
        return

    if suffix in ("doc", "docx"):
        from docx2pdf import convert as docx2pdf_convert  # pyright: ignore[reportMissingImports]


        with tempfile.TemporaryDirectory() as tmpdir:
            word_path = Path(tmpdir) / f"input.{suffix}"
            pdf_path = Path(tmpdir) / "output.pdf"
            word_path.write_bytes(file_path.read_bytes())
            try:
                import pythoncom  # pyright: ignore[reportMissingModuleSource]
                pythoncom.CoInitialize()
            except ImportError:
                pass
            try:
                docx2pdf_convert(str(word_path), str(pdf_path))
            finally:
                try:
                    import pythoncom  # pyright: ignore[reportMissingModuleSource]
                    pythoncom.CoUninitialize()
                except ImportError:
                    pass
            pages = convert_from_bytes(pdf_path.read_bytes(), dpi=200, fmt="png")

        for page in pages:
            buf = io.BytesIO()
            page.save(buf, format="PNG")
            images_b64.append(base64.b64encode(buf.getvalue()).decode("utf-8"))
        return

    raise ValueError(f"文件格式不支持：{filename}")


def _extract_json_from_model_text(text: str) -> dict[str, Any]:
    cleaned = _sanitize_text(text).strip()
    cleaned = re.sub(r"^```(?:json|JSON)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start < 0:
            raise ValueError("模型未返回 JSON 对象")

        depth = 0
        in_string = False
        escape = False
        end = -1
        for index, char in enumerate(cleaned[start:], start=start):
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break

        if end < 0:
            raise ValueError("模型返回的 JSON 对象不完整")
        parsed = json.loads(cleaned[start:end])

    if not isinstance(parsed, dict):
        raise ValueError("模型返回 JSON 不是对象格式")
    return parsed


def _remove_ignored_result_fields(result: dict[str, Any]) -> dict[str, Any]:
    for key in ("data_consistency_check", "error_check_notes"):
        result.pop(key, None)
    return result


def _call_ollama_for_files(file_paths: list[Path]) -> dict[str, Any]:
    images_b64: list[str] = []
    excel_parts: list[tuple[str, str]] = []

    for file_path in file_paths:
        _file_to_ollama_inputs(file_path, images_b64, excel_parts)

    if not images_b64 and not excel_parts:
        raise ValueError("未得到可分析的图片或表格内容")

    excel_text = "\n\n".join(f"[Excel 文件: {fname}]\n{part}" for fname, part in excel_parts)
    max_prompt_chars = int(input_limits.get("max_prompt_chars", 20000))
    if len(excel_text) > max_prompt_chars:
        excel_text = excel_text[:max_prompt_chars] + "\n\n[后续内容已截断，以防超过模型上下文限制]"
    prompt = _build_excel_prompt(excel_text) if excel_parts else _read_prompt_template("common")

    payload: dict[str, object] = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False,
        "options": _build_ollama_options(),
        "keep_alive": _build_keep_alive(),
    }
    if images_b64:
        payload["images"] = images_b64

    response = requests.post(OLLAMA_URL, json=payload, timeout=300)
    if not response.ok:
        try:
            error_text = response.json().get("error", response.text[:500])
        except Exception:
            error_text = response.text[:500]
        raise RuntimeError(f"Ollama 返回错误（HTTP {response.status_code}）：{_sanitize_text(error_text)}")

    response_json = response.json()
    model_text = _sanitize_text(response_json.get("response", ""))
    if not model_text:
        raise ValueError("模型未返回内容")
    return _remove_ignored_result_fields(_extract_json_from_model_text(model_text))


def _fetch_and_download_next_task() -> tuple[dict[str, Any] | None, int]:
    task_next_url = api_config["task_next_url"]

    try:
        response = TASK_HTTP_SESSION.get(task_next_url, timeout=30)

        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"error": "无法连接任务接口", "url": task_next_url}, 502
    except requests.exceptions.Timeout:
        return {"error": "任务接口请求超时", "url": task_next_url}, 504
    except requests.exceptions.HTTPError as exc:
        return {"error": f"任务接口返回错误：{exc}", "url": task_next_url}, 502
    except Exception as exc:
        return {"error": f"任务接口请求失败：{exc}", "url": task_next_url}, 500

    try:
        data = response.json()
    except ValueError:
        data = response.text.strip()

    if data is None or data == "" or (isinstance(data, str) and data.strip().lower() == "null"):
        return None, 204
    if isinstance(data, dict) and "task" in data and data.get("task") is None:
        return None, 204

    task_payload = _extract_task_payload(data)
    task_id = _extract_task_id(data)
    if task_id is None and isinstance(data, str) and data:
        task_id = data

    if task_id is None:
        return {"error": "任务接口返回值中未找到 task_id", "raw": data}, 502

    urls = _extract_task_urls(task_payload or data)
    if not urls:
        return {"error": "任务接口返回值中未找到 urls", "task_id": task_id, "raw": data}, 502

    downloaded_files: list[dict[str, Any]] = []
    download_errors: list[dict[str, str]] = []

    for file_url in urls:
        file_ref = Path(unquote(urlparse(file_url).path)).name or file_url
        try:
            saved_path = _download_to_temp(file_url, str(task_id), file_ref)
            downloaded_files.append(
                {
                    "url": file_url,
                    "saved_path": str(saved_path),
                    "filename": saved_path.name,
                    "size": saved_path.stat().st_size,
                }
            )
        except Exception as exc:
            download_errors.append({"url": file_url, "error": str(exc)})

    result: dict[str, Any] = {
        "task_id": task_id,
        "input_kind": task_payload.get("input_kind") if task_payload else None,
        "urls": urls,
        "downloaded_files": downloaded_files,
        "download_count": len(downloaded_files),
        "temp_dir": str(TEMP_DIR / _safe_filename(str(task_id))),
    }

    if download_errors:
        result["download_errors"] = download_errors
        return result, 502

    return result, 200


def process_task_once() -> dict[str, Any] | None:
    task_info, status_code = _fetch_and_download_next_task()
    if task_info is None:
        return None
    if status_code != 200:
        print(f"[TASK] 获取或下载任务失败：{task_info}")
        return task_info

    task_id = str(task_info["task_id"])
    file_paths = [Path(item["saved_path"]) for item in task_info.get("downloaded_files", [])]

    try:
        ollama_json = _call_ollama_for_files(file_paths)
        upstream_result, upstream_status = _post_task_result(task_id, ollama_json)
        task_info["ollama_result"] = ollama_json
        task_info["submit_status"] = upstream_status
        task_info["submit_response"] = upstream_result
        print(f"[TASK] 任务 {task_id} 已处理并回传，状态：{upstream_status}")
    except Exception as exc:
        task_info["process_error"] = _sanitize_text(exc)
        print(f"[TASK] 任务 {task_id} 处理失败：{task_info['process_error']}")

    return task_info


def _task_polling_loop(interval_seconds: int = POLL_INTERVAL_SECONDS) -> None:
    print(f"[TASK] 任务轮询已启动，每 {interval_seconds} 秒请求一次")
    while not _polling_stop_event.is_set():
        try:
            process_task_once()
        except Exception as exc:
            print(f"[TASK] 轮询异常：{_sanitize_text(exc)}")
        _polling_stop_event.wait(interval_seconds)


def start_task_polling(interval_seconds: int = POLL_INTERVAL_SECONDS) -> None:
    global _polling_thread
    with _polling_lock:
        if _polling_thread and _polling_thread.is_alive():
            return
        _polling_stop_event.clear()
        _polling_thread = threading.Thread(
            target=_task_polling_loop,
            args=(interval_seconds,),
            daemon=True,
            name="task-polling-worker",
        )
        _polling_thread.start()


@api_bp.route("/task/next", methods=["GET"])
def get_next_task():
    result, status_code = _fetch_and_download_next_task()
    if result is None:
        return jsonify({"task": None, "message": "暂无任务"})
    return jsonify(result), status_code


@api_bp.route("/task/<task_id>/files/<path:file_ref>", methods=["GET"])
def get_task_file(task_id: str, file_ref: str):
    task_file_url = _build_task_file_url(task_id, file_ref)

    try:
        response = TASK_HTTP_SESSION.get(task_file_url, timeout=30)

        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "无法连接任务文件接口", "url": task_file_url}), 502
    except requests.exceptions.Timeout:
        return jsonify({"error": "任务文件接口请求超时", "url": task_file_url}), 504
    except requests.exceptions.HTTPError as exc:
        return jsonify({"error": f"任务文件接口返回错误：{exc}", "url": task_file_url}), 502
    except Exception as exc:
        return jsonify({"error": f"任务文件接口请求失败：{exc}", "url": task_file_url}), 500

    try:
        file_data = response.json()
    except ValueError:
        file_data = response.text.strip()

    download_url = _extract_download_url(file_data)
    if not download_url:
        return jsonify({"error": "任务文件接口返回值中未找到 OSS 下载地址", "raw": file_data}), 502

    try:
        saved_path = _download_to_temp(download_url, task_id, file_ref)
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "无法连接 OSS 下载地址", "download_url": download_url}), 502
    except requests.exceptions.Timeout:
        return jsonify({"error": "OSS 文件下载超时", "download_url": download_url}), 504
    except requests.exceptions.HTTPError as exc:
        return jsonify({"error": f"OSS 文件下载失败：{exc}", "download_url": download_url}), 502
    except Exception as exc:
        return jsonify({"error": f"OSS 文件保存失败：{exc}", "download_url": download_url}), 500

    return jsonify(
        {
            "task_id": task_id,
            "file_ref": file_ref,
            "download_url": download_url,
            "saved_path": str(saved_path),
            "filename": saved_path.name,
            "size": saved_path.stat().st_size,
        }
    )


def _post_task_result(task_id: str, payload: dict[str, Any]) -> tuple[Any, int]:
    task_result_url = _build_task_result_url(task_id)

    try:
        response = TASK_HTTP_SESSION.post(task_result_url, json=payload, timeout=60)

        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"error": "无法连接任务结果回传接口", "url": task_result_url}, 502
    except requests.exceptions.Timeout:
        return {"error": "任务结果回传接口请求超时", "url": task_result_url}, 504
    except requests.exceptions.HTTPError as exc:
        response_text = exc.response.text[:500] if exc.response is not None else ""
        return {
            "error": f"任务结果回传接口返回错误：{exc}",
            "url": task_result_url,
            "response": response_text,
        }, 502

    except Exception as exc:
        return {"error": f"任务结果回传失败：{exc}", "url": task_result_url}, 500

    return {
        "task_id": task_id,
        "result_url": task_result_url,
        "upstream_response": _parse_response_data(response),
    }, 200


@api_bp.route("/task/<task_id>/result", methods=["POST"])
def submit_task_result(task_id: str):
    payload = request.get_json(silent=True)

    if payload is None and "file" in request.files:
        result_file = request.files["file"]
        try:
            payload = json.loads(result_file.read().decode("utf-8"))
        except Exception as exc:
            return jsonify({"error": f"JSON 文件解析失败：{exc}"}), 400

    if not isinstance(payload, dict):
        return jsonify({"error": "请提交 JSON 对象或上传字段名为 file 的 JSON 文件作为 AI 解析结果"}), 400

    result, status_code = _post_task_result(task_id, payload)
    return jsonify(result), status_code



@api_bp.route("/task/<task_id>/result/requeue", methods=["POST"])
@api_bp.route("/task/<task_id>/result/retry", methods=["POST"])
def requeue_task_result(task_id: str):
    result, status_code = _post_task_result(task_id, {"requeue": True})
    return jsonify(result), status_code


def create_api_blueprint() -> Blueprint:
    return api_bp

