"""
用自然語言控制機器人車（輪子 + 相機），本地 LLM（Lemonade Server）tool calling → 藍牙 → micro:bit

Lemonade Server 提供 OpenAI 相容 API（預設 http://localhost:13305/v1），
所以直接用 openai 套件連本機，不需要 Gemini API key。

執行（在專案最外層）：
    LLM_control_motor_lemonade/.venv/bin/python LLM_control_motor_lemonade/agent.py          # 真的 micro:bit
    LLM_control_motor_lemonade/.venv/bin/python LLM_control_motor_lemonade/agent.py --fake   # 不連 micro:bit，只測 LLM
"""

import inspect
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from robot_bluetooth import TOOLS, robot

# .env 放在專案最外層（這支程式的上一層資料夾），沒有也沒關係，全部都有預設值
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

# LLM校正馬達轉彎/calibrate.py 產生的校正知識，存在就加到 system prompt 後面
KNOWLEDGE_PATH = Path(__file__).resolve().parent.parent / "LLM校正馬達轉彎" / "knowledge.md"

# ====== 想改的東西都在這裡 ======
LEMONADE_BASE_URL = os.getenv("LEMONADE_BASE_URL", "http://localhost:13305/v1")
LEMONADE_API_KEY = os.getenv("LEMONADE_API_KEY", "lemonade")  # 本機預設不檢查，隨便填
LEMONADE_MODEL = os.getenv("LEMONADE_MODEL", "")  # 空白 = 自動挑第一個有 tool-calling 標籤的模型
MAX_TOOL_CALLS = 30   # 一句話最多呼叫幾次工具（避免 LLM 無限呼叫）

SYSTEM_PROMPT = """你控制一台 micro:bit 機器人車，車上有一個相機。

硬體：
- 兩個輪子：可以前進、後退、原地左轉 / 右轉；用 drive 分別控制左右輪可以邊走邊彎。
- 相機上下：camera_up / camera_down，是馬達轉動一段時間，不是角度。
- 相機左右：camera_pan 設定絕對角度，0 = 最右、90 = 正前方、180 = 最左（角度越大越往左）。

規則：
- 要動就一定要呼叫工具，不要只用文字假裝做了。
- 多個動作就依照順序一個一個呼叫工具。
- 使用者有指定秒數、速度、角度就照做；沒指定就用工具預設值。「慢」約 120、「快 / 全速」約 255。
- 「車子轉」用 turn_left / turn_right；「鏡頭 / 相機轉」用 camera_pan。
- 使用者說「停」就立刻呼叫 stop。
- 做不到的事（例如開燈、飛、說話）或超出硬體範圍的要求，不要亂呼叫工具，直接說明做不到或已限制範圍。
- 跟機器人無關的話就簡短聊天，不要呼叫工具。
- 完成後用一句繁體中文簡短回報做了什麼。"""
# ================================


def full_system_prompt():
    if KNOWLEDGE_PATH.exists():
        return SYSTEM_PROMPT + "\n\n" + KNOWLEDGE_PATH.read_text(encoding="utf-8")
    return SYSTEM_PROMPT


# ---------- 把 Python 函式轉成 OpenAI tools 格式 ----------
# Gemini / Ollama SDK 會自動讀函式簽名和說明，OpenAI API 要自己寫 JSON schema，
# 所以這裡從「參數型別、預設值、docstring 的 Args:」自動產生。

JSON_TYPES = {int: "integer", float: "number", str: "string", bool: "boolean"}


def function_to_tool(func):
    doc = inspect.getdoc(func) or ""
    description, _, args_part = doc.partition("Args:")
    arg_docs = dict(re.findall(r"^\s*(\w+):\s*(.+)$", args_part, flags=re.MULTILINE))

    properties, required = {}, []
    for name, param in inspect.signature(func).parameters.items():
        prop = {"type": JSON_TYPES.get(param.annotation, "string")}
        text = arg_docs.get(name, "")
        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            text = f"{text}（預設 {param.default}）".strip()
        if text:
            prop["description"] = text
        properties[name] = prop

    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": description.strip(),
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def pick_model(client):
    """沒指定 LEMONADE_MODEL 時，從 /v1/models 挑第一個支援 tool-calling 的模型。"""
    if LEMONADE_MODEL:
        return LEMONADE_MODEL
    models = client.models.list().data
    for m in models:
        if "tool-calling" in (getattr(m, "labels", None) or []):
            return m.id
    if models:
        return models[0].id
    sys.exit("❌ Lemonade Server 上沒有任何模型，請先下載一個（例如 lemonade pull ...）")


def make_chat():
    from openai import OpenAI

    client = OpenAI(base_url=LEMONADE_BASE_URL, api_key=LEMONADE_API_KEY)
    model = pick_model(client)
    tools = [function_to_tool(f) for f in TOOLS]
    tools_by_name = {f.__name__: f for f in TOOLS}
    messages = [{"role": "system", "content": full_system_prompt()}]

    def chat(user_text):
        messages.append({"role": "user", "content": user_text})
        for _ in range(MAX_TOOL_CALLS):
            response = client.chat.completions.create(model=model, messages=messages, tools=tools)
            msg = response.choices[0].message
            calls = msg.tool_calls or []
            # 只存回標準欄位（不存 reasoning_content），下一輪才不會被伺服器拒絕
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                **({"tool_calls": [c.model_dump(include={"id", "type", "function"}) for c in calls]}
                   if calls else {}),
            })
            if not calls:
                return msg.content
            for call in calls:
                name = call.function.name
                func = tools_by_name.get(name)
                try:
                    args = json.loads(call.function.arguments or "{}")
                    result = func(**args) if func else f"沒有 {name} 這個工具"
                except Exception as e:  # LLM 給錯參數時，把錯誤告訴它讓它修正
                    result = f"呼叫失敗：{e}"
                messages.append({"role": "tool", "tool_call_id": call.id, "content": str(result)})
        return "（工具呼叫次數太多，已中止）"

    chat.model = model
    return chat


def main():
    robot.fake = "--fake" in sys.argv
    chat = make_chat()   # 先確認 Lemonade 連得到，再連藍牙
    robot.connect()
    print(f"🤖 使用 Lemonade {chat.model}（{LEMONADE_BASE_URL}）")
    print("🧠 已載入校正知識" if KNOWLEDGE_PATH.exists() else "ℹ️ 尚未校正（可先執行 calibrate.py）")
    print("💬 例如「先往前走 1 秒，再把鏡頭轉向右邊」，輸入 q 離開\n")

    try:
        while True:
            text = input("你：").strip()
            if text.lower() in ("q", "quit", "exit"):
                break
            if text:
                print(f"AI：{chat(text)}\n")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        robot.disconnect()


if __name__ == "__main__":
    main()
