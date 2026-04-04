from __future__ import annotations

import os
from typing import Any

import requests



def parse_model_identity(model_name: str) -> tuple[str, str]:
    if ":" in model_name:
        base_name, version = model_name.split(":", 1)
        return base_name, version
    return model_name, "unknown"


def apply_ollama_details(result: dict[str, Any], details: dict[str, Any]) -> None:
    result["quantization_level"] = details.get("quantization_level")
    result["parameter_size"] = details.get("parameter_size")
    result["format"] = details.get("format")

    quant = result["quantization_level"]
    if not isinstance(quant, str):
        return

    if quant.startswith(("Q4_", "IQ4_")):
        result["precision_mode"] = "4-bit quantized (GGUF)"
    elif quant.startswith("Q5_"):
        result["precision_mode"] = "5-bit quantized (GGUF)"
    elif quant.startswith("Q6_"):
        result["precision_mode"] = "6-bit quantized (GGUF)"
    elif quant.startswith("Q8_"):
        result["precision_mode"] = "8-bit quantized (GGUF)"
    elif quant in ("F16", "fp16"):
        result["precision_mode"] = "FP16 (half precision)"
    elif quant == "F32":
        result["precision_mode"] = "FP32 (full precision)"
    else:
        result["precision_mode"] = f"Other GGUF quant: {quant}"


def check_model_precision(model: Any) -> dict[str, Any]:

    """本地 Transformers 模型精度检查（仅在持有 model 对象时可用）。"""
    try:
        import torch
    except ImportError:
        return {
            "precision_mode": "unavailable",
            "message": "torch 未安装，无法检查本地模型参数精度",
            "sampled_param_dtypes": [],
        }

    config_torch_dtype = None
    if hasattr(model, "config") and hasattr(model.config, "torch_dtype"):
        config_torch_dtype = str(model.config.torch_dtype)

    param_dtypes: set[Any] = set()
    param_count = 0
    for _, param in model.named_parameters():
        param_dtypes.add(param.dtype)
        param_count += 1
        if len(param_dtypes) > 3 or param_count > 100:
            break

    sampled = sorted(str(dtype) for dtype in param_dtypes)
    precision_mode = "unknown"

    if torch.float16 in param_dtypes or torch.bfloat16 in param_dtypes:
        if torch.uint8 in param_dtypes or torch.int8 in param_dtypes:
            precision_mode = "4/8-bit quantized with FP16/BF16 compute"
        else:
            precision_mode = "FP16/BF16"
    elif torch.float32 in param_dtypes:
        precision_mode = "FP32"
    elif torch.uint8 in param_dtypes or torch.int8 in param_dtypes:
        precision_mode = "quantized (4/8-bit)"

    quantization_detail = None
    if hasattr(model, "quantization_method") or "bnb" in str(type(model)).lower():
        quantization_detail = "bitsandbytes quantization detected"
        for name, module in model.named_modules():
            if hasattr(module, "weight") and hasattr(module.weight, "quant_state"):
                quantization_detail = f"quantized layer: {name}, quant_state: {module.weight.quant_state}"
                break

    return {
        "config_torch_dtype": config_torch_dtype,
        "sampled_param_dtypes": sampled,
        "precision_mode": precision_mode,
        "quantization_detail": quantization_detail,
    }


def get_model_precision_via_ollama(base_url: str, model_name: str, timeout: int = 10) -> dict[str, Any]:
    """通过 Ollama /api/show 获取 GGUF 量化精度信息。"""
    show_url = f"{base_url.rstrip('/')}/api/show"
    payload = {
        "model": model_name,
        "verbose": True,
    }

    result: dict[str, Any] = {
        "model_name": model_name,
        "precision_mode": "unknown (Ollama GGUF)",
        "quantization_level": None,
        "parameter_size": None,
        "format": None,
        "error": None,
        "note": "Ollama 使用 GGUF，精度通常由 quantization_level 表示（如 Q4_K_M）",
    }

    try:
        resp = requests.post(show_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

        details = data.get("details", {})
        if isinstance(details, dict) and details:
            apply_ollama_details(result, details)


        if "model_info" in data and isinstance(data["model_info"], dict):
            result["model_info_sample"] = {
                k: v for k, v in list(data["model_info"].items())[:5]
            }

    except requests.exceptions.RequestException as e:
        result["error"] = f"请求 /api/show 失败: {str(e)}"
        result["service_status"] = "offline or unreachable"

    return result


def get_model_runtime_info(
    model_name: str,
    health_url: str,
    prompt_version: str = "unknown",
    generation_kwargs: dict[str, Any] | None = None,
    timeout: int = 5,
) -> dict[str, Any]:


    base_name, version = parse_model_identity(model_name)
    base_url = health_url.rsplit("/api/", 1)[0] if "/api/" in health_url else health_url

    info: dict[str, Any] = {
        "model_name": model_name,
        "model_base_name": base_name,
        "model_version": version,
        "service_status": "unknown",
        "available_in_ollama": False,
        "precision_info": {"precision_mode": "N/A"},
        "precision_mode": "N/A",
        "precision_check_note": "等待精度信息",
        "ollama_num_gpu": os.environ.get("OLLAMA_NUM_GPU", "未设置"),
        "ollama_flash_attention": os.environ.get("OLLAMA_FLASH_ATTENTION", "未设置"),
        "prompt_version": prompt_version,
        "generation_kwargs": dict(generation_kwargs or {}),
    }




    try:
        resp = requests.get(health_url, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        models = payload.get("models", []) if isinstance(payload, dict) else []

        matched = next((item for item in models if item.get("name") == model_name), None)
        info["service_status"] = "online"
        info["available_in_ollama"] = matched is not None

        precision_data = get_model_precision_via_ollama(base_url, model_name, timeout=timeout)
        info["precision_info"] = precision_data
        info["precision_mode"] = precision_data.get("precision_mode", "unknown")
        info["precision_check_note"] = precision_data.get("note", "-")

        if precision_data.get("error"):
            info["precision_check_note"] = precision_data["error"]

        if matched:
            info["registered_name"] = matched.get("name")
            info["model_digest"] = matched.get("digest")
            info["model_size"] = matched.get("size")
            info["modified_at"] = matched.get("modified_at")

            matched_details = matched.get("details", {})
            if isinstance(matched_details, dict) and (
                precision_data.get("error") or not precision_data.get("quantization_level")
            ):
                apply_ollama_details(precision_data, matched_details)
                info["precision_mode"] = precision_data.get("precision_mode", "unknown")
                info["precision_check_note"] = "已从 /api/tags 回退获取模型精度信息"


    except Exception as e:
        info["service_status"] = "offline"
        info["error"] = str(e)
        info["precision_info"]["error"] = str(e)
        info["precision_mode"] = "unknown"
        info["precision_check_note"] = str(e)

    return info
