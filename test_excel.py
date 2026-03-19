"""
测试 xls/xlsx 读取 → 发给 Ollama qwen2.5vl:7b → 返回结果
"""
import io
import requests
import pandas as pd

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen2.5vl:7b"

# ── 1. 构造一个模拟报关单 xlsx（内存中生成，无需真实文件） ────────────────
print("=== Step 1: 构造测试 Excel 数据 ===")
data = {
    "项号": [1, 2],
    "品名": ["Circuit Board", "Power Supply Unit"],
    "规格型号": ["Model-A100", "PSU-500W"],
    "数量": [100, 50],
    "单位": ["PCS", "PCS"],
    "净重(KG)": [20.5, 35.0],
    "总价(USD)": [5000.00, 3500.00],
    "HS编码": ["8534009000", "8504409900"],
}
header_data = {
    "收发货人": ["深圳某贸易有限公司"],
    "监管方式": ["一般贸易"],
    "成交方式": ["FOB"],
    "合计件数": [10],
    "合计毛重(KG)": [60.0],
    "合计净重(KG)": [55.5],
    "币制": ["USD"],
}

buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as writer:
    pd.DataFrame(header_data).to_excel(writer, sheet_name="报关头信息", index=False)
    pd.DataFrame(data).to_excel(writer, sheet_name="货物明细", index=False)
buf.seek(0)
print("[OK] 内存 xlsx 构造完成（2 个 Sheet）")

# ── 2. 用与 app.py 相同逻辑读取 Excel ────────────────────────────────────
print("\n=== Step 2: pandas 读取 Excel ===")
try:
    xls = pd.read_excel(buf, sheet_name=None, engine="openpyxl")
    text_parts = []
    for sheet_name, df in xls.items():
        df = df.fillna("").astype(str)
        sheet_text = f"[Sheet: {sheet_name}]\n{df.to_string(index=False)}"
        text_parts.append(sheet_text)
        print(f"  Sheet '{sheet_name}': {df.shape[0]} 行 × {df.shape[1]} 列")
    excel_text = "\n\n".join(text_parts)
    print("[OK] Excel 读取成功")
    print("\n--- 生成的文本片段（前 500 字符）---")
    print(excel_text[:500])
except Exception as e:
    print(f"[FAIL] Excel 读取失败: {e}")
    exit(1)

# ── 3. 检查 Ollama 是否可达 ───────────────────────────────────────────────
print("\n=== Step 3: 检查 Ollama 服务 ===")
try:
    r = requests.get("http://localhost:11434/api/tags", timeout=5)
    models = [m.get("name", "") for m in r.json().get("models", [])]
    print(f"[OK] Ollama 在线，已有模型: {models}")
    if MODEL_NAME not in models:
        print(f"[WARN] {MODEL_NAME} 未在列表，继续发送请求...")
except Exception as e:
    print(f"[FAIL] 无法连接 Ollama: {e}")
    exit(1)

# ── 4. 构造提示词并发送请求 ────────────────────────────────────────────────
print("\n=== Step 4: 发送请求给模型 ===")
PROMPT = (
    'Role: 资深报关审核专家（AEO 高级认证背景），擅长数据提取。\n'
    'Task:\n'
    '1.单文档模式：提取关键报关要素。\n'
    'Data Priority:\n'
    '●若数据缺失，标注 "null"，严禁虚构。\n'
    'Output Requirement: 禁止任何开场白或中间思考过程，直接输出唯一一个符合以下格式的 JSON 字符串：\n'
    'JSON\n'
    '{\n'
    '  "processing_mode": "single_document / multi_document",\n'
    '  "header": {\n'
    '    "sender_receiver": "境内收发货人",\n'
    '    "trade_mode": "监管方式",\n'
    '    "incoterms": "成交方式",\n'
    '    "total_packages": "合计件数",\n'
    '    "total_gross_weight": "合计毛重 (KGS)",\n'
    '    "total_net_weight": "合计净重 (KGS)",\n'
    '    "currency": "币制"\n'
    '  },\n'
    '  "items": [\n'
    '    {\n'
    '      "item_no": "项号",\n'
    '      "hscode_suggested": "建议HS编码",\n'
    '      "g_name": "中文品名 (Original English Name)",\n'
    '      "g_model": "规格型号",\n'
    '      "qty": "数量",\n'
    '      "unit": "单位",\n'
    '      "net_weight": "净重",\n'
    '      "total_price": "总价"\n'
    '    }\n'
    '  ],\n'
    '  "error_check_notes": "列出数据冲突或缺失字段，若正确填 \'None\'。"\n'
    '}\n\n'
    f'以下是 Excel 表格数据：\n{excel_text}'
)

payload = {
    "model": MODEL_NAME,
    "prompt": PROMPT,
    "stream": False,
}

print(f"[INFO] 提示词总长度: {len(PROMPT)} 字符，等待模型响应（最长 120s）...")
try:
    resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
    resp.raise_for_status()
    result = resp.json()
    response_text = result.get("response", "模型未返回内容")
    print("\n=== Step 5: 模型返回结果 ===")
    print(response_text)
    print("\n[OK] 全流程测试通过！")
except requests.exceptions.ConnectionError:
    print("[FAIL] 无法连接到 Ollama（端口 11434）")
except requests.exceptions.Timeout:
    print("[FAIL] 模型响应超时（>120s）")
except Exception as e:
    print(f"[FAIL] 请求异常: {e}")
