model_config = {
    "ollama_url": "http://localhost:11434/api/generate",
    "ollama_health_url": "http://localhost:11434/api/tags",
    "model_name": "qwen-vl-bigctx:latest",
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
    "max_new_tokens": 4096,
    "repetition_penalty": 1.05,
    "use_cache": True,
}

