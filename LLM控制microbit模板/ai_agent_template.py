"""
用自然語言控制 micro:bit 車子（AI tool calling）

你打字：「往前走兩秒再右轉」
AI 決定呼叫：move_forward(seconds=2) → turn_right(seconds=0.5)
函式透過藍牙送指令給 micro:bit

執行：
    .venv/bin/python ai_agent_template.py            # 本地 llama（Ollama）＋真的 micro:bit
    .venv/bin/python ai_agent_template.py --fake     # 不連 micro:bit，只測 AI
    .venv/bin/python ai_agent_template.py --gemini   # 改用 Gemini（需要 .env 裡的 GEMINI_API_KEY）
"""

import functools
import sys
from pathlib import Path

from dotenv import load_dotenv

# robot_bluetooth.py 跟這支程式放在同一個資料夾，Python 會自動找到
import robot_bluetooth
from robot_bluetooth import TOOLS, robot

# .env 放在專案最外層（這支程式的上一層資料夾），所有子資料夾共用
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# ====== 想改的東西都在這裡 ======
OLLAMA_MODEL = "llama3.1:8b"
GEMINI_MODEL = "gemini-3.8-flash"

SYSTEM_PROMPT = """你是一台遙控車的駕駛。使用者會用中文告訴你要怎麼移動。
規則：
- 要移動就一定要呼叫工具，不要只用文字說你做了。
- 使用者一次說多個動作，就依照順序一個一個呼叫工具。
- 使用者說的秒數要照做；沒說就用工具的預設值。
- 不是移動相關的話，就簡短聊天，不要呼叫工具。
- 動作完成後，用一句繁體中文簡短回報。"""
# ================================


# ---------- 本地 llama（Ollama）----------
def make_ollama_chat():
    import ollama

    tools_by_name = {f.__name__: f for f in TOOLS}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    def chat(user_text):
        messages.append({"role": "user", "content": user_text})
        while True:
            # 把對話和工具清單交給模型，模型決定：直接回答，或要呼叫哪些工具
            response = ollama.chat(model=OLLAMA_MODEL, messages=messages, tools=TOOLS)
            messages.append(response.message)

            if not response.message.tool_calls:        # 沒有要呼叫工具 → 這就是最後回答
                return response.message.content

            for call in response.message.tool_calls:   # 有要呼叫工具 → 我們幫它執行
                name, args = call.function.name, call.function.arguments
                print(f"🔧 AI 呼叫 {name}({args})")
                func = tools_by_name.get(name)
                result = func(**args) if func else f"沒有 {name} 這個工具"
                # 把執行結果告訴模型，讓它決定下一步
                messages.append({"role": "tool", "content": str(result), "tool_name": name})

    return chat


# ---------- Gemini ----------
def make_gemini_chat():
    from google import genai
    from google.genai import types

    client = genai.Client()  # 自動讀取環境變數 GEMINI_API_KEY

    def log_tool(func):
        # functools.wraps 會保留原函式的名稱、說明和參數，Gemini 才看得到 seconds 這個參數
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            print(f"🔧 AI 呼叫 {func.__name__}({args or ''}{kwargs or ''})")
            return func(*args, **kwargs)
        return wrapper

    session = client.chats.create(
        model=GEMINI_MODEL,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[log_tool(f) for f in TOOLS],  # Gemini SDK 會自動執行工具、把結果回傳給模型
        ),
    )
    def chat(user_text):
        _ = client  # 讓 client 一直被引用，否則函式結束後會被回收、連線被關閉
        return session.send_message(user_text).text

    return chat


# ---------- 主程式 ----------
def main():
    load_dotenv(ENV_PATH)  # 讀取最外層的 .env 檔（放 GEMINI_API_KEY）
    if "--gemini" in sys.argv and not ENV_PATH.exists():
        sys.exit(f"❌ 找不到 {ENV_PATH}，請在專案最外層建立 .env 並填入 GEMINI_API_KEY")

    robot_bluetooth.robot.fake = "--fake" in sys.argv
    use_gemini = "--gemini" in sys.argv

    robot.connect()
    chat = make_gemini_chat() if use_gemini else make_ollama_chat()
    print(f"🤖 使用 {'Gemini ' + GEMINI_MODEL if use_gemini else 'Ollama ' + OLLAMA_MODEL}")
    print("💬 輸入指令，例如「往前走兩秒再左轉」，輸入 q 離開\n")

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
