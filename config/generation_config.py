model_config = {
    "ollama_url": "http://localhost:11434/api/generate",
    "ollama_health_url": "http://localhost:11434/api/tags",
    "model_name": "qwen2.5vl:7b",
}

api_config = {
    "task_next_url": "http://ai.guanwu.imohe.com/parse/ai/task/next",
    "task_file_url_template": "http://ai.guanwu.imohe.com/parse/ai/task/{task_id}/files/{file_ref}",
    "task_result_url_template": "http://ai.guanwu.imohe.com/parse/ai/task/{task_id}/result",
    "temp_dir": "temp",
}



prompt_version = "v8"


prompt_file_map = {
    "common": "common.md",
    "excel": "excel.md",
}

generation_kwargs = {
    "do_sample": False,
    "temperature": 0.0,
    "top_p": 1.0,
    "num_beams": 1,
    "max_new_tokens": 8192,
    "repetition_penalty": 1.05,
    "use_cache": True,
    "num_ctx": 8192,
}

# 输入剪裁与页数限制，避免超过模型上下文
input_limits = {
    "max_prompt_chars": 20000,
    "max_pdf_pages": 5,
    "max_images": 8,
    "excel_max_rows": 50,
    "excel_max_cols": 12,
}
