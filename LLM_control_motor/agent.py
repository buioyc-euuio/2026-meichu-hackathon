"""
用自然語言控制機器人車（輪子 + 相機），LLM tool calling → 藍牙 → micro:bit

執行（在專案最外層）：
    .venv/bin/python LLM_control_motor/agent.py --gemini          # Gemini ＋真的 micro:bit
    .venv/bin/python LLM_control_motor/agent.py --gemini --fake   # 不連 micro:bit，只測 LLM
    .venv/bin/python LLM_control_motor/agent.py                   # 本地 llama（Ollama）
"""

import readline  # noqa: F401  讓 input() 可以用方向鍵移動游標（不會出現 ^[[D）
import sys
from pathlib import Path

from dotenv import load_dotenv

from latency import latency
from robot_bluetooth import TOOLS, robot

# .env 放在專案最外層（這支程式的上一層資料夾）
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

# LLM校正馬達轉彎/calibrate.py 產生的校正知識，存在就加到 system prompt 後面
KNOWLEDGE_PATH = Path(__file__).resolve().parent.parent / "LLM校正馬達轉彎" / "knowledge.md"

# ====== 想改的東西都在這裡 ======
OLLAMA_MODEL = "llama3.1:8b"
GEMINI_MODEL = "gemini-3.8-flash"
MAX_TOOL_CALLS = 30   # 一句話最多呼叫幾次工具（避免 LLM 無限呼叫）
LATENCY_LOG = True    # 印出每句話的延遲（LLM 思考 / 工具 / 藍牙各花多久），見 latency.py

SYSTEM_PROMPT = """你控制一台 micro:bit 機器人車（一隻機器狗），車上有一個相機和一條尾巴。

硬體：
- 兩個輪子：可以前進、後退、左轉 / 右轉。turn_left / turn_right 預設是邊前進邊轉的弧線轉彎；
  要原地轉圈時把 inner_speed 設成負的 speed（地面摩擦大時可能卡住）。用 drive 可以自訂左右輪速度。
- 相機上下：camera_up / camera_down，是馬達轉動一段時間，不是角度。
- 相機左右：camera_pan 設定絕對角度，0 = 最右、90 = 正前方、180 = 最左（角度越大越往左）。
- 尾巴：tail_emotion 用尾巴表現情緒（開心、興奮、好奇、難過、平靜）；wag_tail 自訂搖法；tail_angle 把尾巴停在某個角度。
  搖尾巴在背景進行、會馬上回傳，可以接著呼叫輪子的工具，做到「一邊搖尾巴一邊轉圈」。

規則：
- 要動就一定要呼叫工具，不要只用文字假裝做了。
- 多個動作就依照順序一個一個呼叫工具。
- 使用者有指定秒數、速度、角度就照做；沒指定就用工具預設值。「慢」約 120、「快 / 全速」約 255。
- 「車子轉」用 turn_left / turn_right；「鏡頭 / 相機轉」用 camera_pan；「搖尾巴」用 tail_emotion / wag_tail。
- 使用者說「停」就立刻呼叫 stop。
- 做不到的事（例如開燈、飛、說話）或超出硬體範圍的要求，不要亂呼叫工具，直接說明做不到或已限制範圍。
- 跟機器人無關的話就簡短聊天，不要呼叫工具。
- 完成後用一句繁體中文簡短回報做了什麼。"""
# ================================


def full_system_prompt():
    if KNOWLEDGE_PATH.exists():
        return SYSTEM_PROMPT + "\n\n" + KNOWLEDGE_PATH.read_text(encoding="utf-8")
    return SYSTEM_PROMPT


def make_ollama_chat():
    import ollama

    tools_by_name = {f.__name__: f for f in TOOLS}
    messages = [{"role": "system", "content": full_system_prompt()}]

    def chat(user_text):
        latency.start()
        messages.append({"role": "user", "content": user_text})
        for _ in range(MAX_TOOL_CALLS):
            response = ollama.chat(model=OLLAMA_MODEL, messages=messages, tools=TOOLS)
            messages.append(response.message)
            if not response.message.tool_calls:
                latency.llm_done("回覆文字")
                latency.summary()
                return response.message.content
            for call in response.message.tool_calls:
                name, args = call.function.name, call.function.arguments
                func = tools_by_name.get(name)
                if not func:
                    latency.llm_done(f"呼叫不存在的工具 {name}")
                try:
                    result = func(**args) if func else f"沒有 {name} 這個工具"
                except Exception as e:  # LLM 給錯參數時，把錯誤告訴它讓它修正
                    result = f"呼叫失敗：{e}"
                messages.append({"role": "tool", "content": str(result), "tool_name": name})
        return "（工具呼叫次數太多，已中止）"

    return chat


def make_gemini_chat():
    from google import genai
    from google.genai import types

    client = genai.Client()  # 自動讀取 GEMINI_API_KEY
    session = client.chats.create(
        model=GEMINI_MODEL,
        config=types.GenerateContentConfig(
            system_instruction=full_system_prompt(),
            tools=TOOLS,  # SDK 會自動執行工具、把結果回傳給模型
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=MAX_TOOL_CALLS),  # 預設只有 10 次，畫正方形這類任務會不夠
        ),
    )

    def chat(user_text):
        _ = client  # 讓 client 一直被引用，否則會被回收、連線被關閉
        latency.start()
        response = session.send_message(user_text)
        usage = response.usage_metadata
        thoughts = getattr(usage, "thoughts_token_count", None) if usage else None
        latency.llm_done("回覆文字" + (f"（最後一次思考用了 {thoughts} 個 thinking token）" if thoughts else ""))
        latency.summary()
        return response.text

    return chat


def make_chat(use_gemini):
    return make_gemini_chat() if use_gemini else make_ollama_chat()


def main():
    use_gemini = "--gemini" in sys.argv
    if use_gemini and not ENV_PATH.exists():
        sys.exit(f"❌ 找不到 {ENV_PATH}，請在專案最外層建立 .env 並填入 GEMINI_API_KEY")

    robot.fake = "--fake" in sys.argv
    latency.enabled = LATENCY_LOG
    robot.connect()
    chat = make_chat(use_gemini)
    print(f"🤖 使用 {'Gemini ' + GEMINI_MODEL if use_gemini else 'Ollama ' + OLLAMA_MODEL}")
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
