# -*- coding: utf-8 -*-
"""
app.py —— Flask 后端入口

职责边界（很薄的一层，只做「路由 + 参数校验 + 错误翻译」）：
    GET  /              渲染前端页面
    GET  /api/health    环境自检（密钥、edge-tts 可用性、记忆概况）
    POST /api/think     前端 think() 调用：大模型回答（自动带上会话记忆）
    POST /api/tts       speak_with_edge_tts()：文字 -> MP3 音频
    GET  /api/voices    可选音色清单
    GET  /api/history   读取某个会话的对话记忆
    POST /api/reset     清空某个会话的对话记忆

真正的业务逻辑全部在 voice_core.py 里，保持可单测。

启动：
    python app.py
或双击 start.bat
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser

from flask import Flask, Response, jsonify, render_template, request

import voice_core

app = Flask(__name__)

#: 允许前端把回答里的中文正常返回（Flask 默认会把非 ASCII 转义，关掉更易读）
app.json.ensure_ascii = False

#: 监听端口，可用环境变量覆盖
PORT = int(os.getenv("VOICE_APP_PORT", "5000"))
HOST = os.getenv("VOICE_APP_HOST", "127.0.0.1")


# --------------------------------------------------------------------------- #
# 公共小工具
# --------------------------------------------------------------------------- #
def read_session_id(source: dict | None = None) -> str:
    """从请求体/查询串里取出会话 ID，取不到就用默认值。

    一个浏览器标签页 = 一个会话，服务端按这个 ID 隔离对话记忆。
    """
    raw = None
    if isinstance(source, dict):
        raw = source.get("session_id")
    if raw is None:
        raw = request.args.get("session_id")
    return str(raw or "").strip() or voice_core.DEFAULT_SESSION_ID


def read_float(source: dict, key: str, default: float, low: float, high: float) -> float:
    """从请求体里安全地读一个浮点参数，并夹到 [low, high] 区间。"""
    try:
        value = float(source.get(key, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, low), high)


# --------------------------------------------------------------------------- #
# 页面路由
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    """返回语音交互主页面。"""
    return render_template(
        "index.html",
        model=voice_core.DEFAULT_MODEL,
        default_voice=voice_core.DEFAULT_EDGE_VOICE,
        max_turns=voice_core.MAX_MEMORY_TURNS,
    )


# --------------------------------------------------------------------------- #
# 环境自检：前端启动时调用，提前告诉用户密钥/语音合成是否可用
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def api_health():
    """返回服务、密钥、语音合成与记忆的状态。

    Returns:
        {
          "ok": true,
          "api_key_configured": bool,   # DEEPSEEK_API_KEY 是否配置
          "edge_tts_available": bool,   # 服务端能否用 edge-tts 合成语音
          "default_voice": str,
          "memory": {"sessions": int, "messages": int, "max_turns": int},
          "model": str, "env_var": str
        }
    """
    return jsonify(
        {
            "ok": True,
            "api_key_configured": voice_core.has_api_key(),
            "edge_tts_available": voice_core.is_edge_tts_available(),
            "default_voice": voice_core.DEFAULT_EDGE_VOICE,
            "memory": voice_core.memory_stats(),
            "model": voice_core.DEFAULT_MODEL,
            "env_var": voice_core.ENV_API_KEY,
        }
    )



# --------------------------------------------------------------------------- #
# 核心接口：think()
# --------------------------------------------------------------------------- #
@app.post("/api/think")
def api_think():
    """接收用户问题 -> 带上会话记忆调用大模型 -> 返回文字回答。

    请求体 JSON：
        {"text": "用户说的话", "session_id": "可选",
         "history": "可选，显式指定历史则忽略服务端记忆"}

    成功响应：
        {"ok": true, "reply": "...", "elapsed_ms": 1234, "model": "deepseek-chat",
         "usage": {...}, "turns": 3, "session_id": "..."}

    失败响应（HTTP 400/502/500）：
        {"ok": false, "error": "中文错误原因"}
    """
    data = request.get_json(silent=True) or {}
    session_id = read_session_id(data)

    # --- 1. 参数校验 ---
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify({"ok": False, "error": "没有收到有效的提问内容。"}), 400
    if len(text) > 4000:
        return jsonify({"ok": False, "error": "提问内容过长，请精简后再试。"}), 400

    history = data.get("history")
    if history is not None and not isinstance(history, list):
        return jsonify({"ok": False, "error": "history 必须是数组。"}), 400

    # 允许前端临时覆盖模型 / 采样参数（做实验用），但不允许覆盖接口地址，避免 SSRF
    model = str(data.get("model") or voice_core.DEFAULT_MODEL).strip()
    temperature = read_float(data, "temperature", 1.0, 0.0, 2.0)

    # --- 2. 调用核心逻辑（think() 本身没有改动，这里只负责把记忆喂给它）---
    started = time.perf_counter()
    try:
        if history:
            # 显式传了历史就以它为准（方便其它客户端/调试），但依然会记进服务端记忆
            result = voice_core.think_with_meta(text, history, model=model, temperature=temperature)
            messages = voice_core.remember(text, result["reply"], session_id=session_id)
            result["turns"] = voice_core.count_turns(messages)
        else:
            # 常规路径：读取该会话最近的 10 轮记忆 -> 一起发给大模型 -> 记住这一轮
            result = voice_core.think_with_memory(
                text,
                session_id=session_id,
                model=model,
                temperature=temperature,
            )
    except voice_core.ThinkError as exc:
        # 可预期错误：密钥缺失、网络问题、模型报错 —— 以 502 返回中文提示
        status = 400 if not voice_core.has_api_key() else 502
        app.logger.warning("think() 失败: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), status
    except Exception as exc:  # noqa: BLE001 —— 兜底，避免把堆栈暴露给前端
        app.logger.exception("think() 未预期异常")
        return jsonify({"ok": False, "error": f"服务内部错误：{exc}"}), 500

    app.logger.info(
        "think() 成功 | %.2fs | 输入 %d 字 | 输出 %d 字 | 记忆 %d 轮 | tokens=%s",
        (time.perf_counter() - started),
        len(text),
        len(result["reply"]),
        result.get("turns", 0),
        result.get("usage", {}).get("total_tokens", "-"),
    )
    return jsonify({"ok": True, "session_id": session_id, **result})


# --------------------------------------------------------------------------- #
# 语音合成接口：speak_with_edge_tts()
# --------------------------------------------------------------------------- #
@app.post("/api/tts")
def api_tts():
    """把文字合成 MP3 音频返回给前端播放。

    请求体 JSON：
        {"text": "要朗读的文字", "voice": "zh-CN-XiaoxiaoNeural",
         "rate": 1.0, "volume": 1.0, "pitch": 0}

    成功：直接返回 audio/mpeg 音频流，并在响应头里带上实际使用的音色与耗时。

    失败（HTTP 502/503）：返回 JSON，且 fallback 字段固定为 "browser"，
        前端看到它就应该降级调用 speak_fallback()（浏览器 speechSynthesis）。
    """
    data = request.get_json(silent=True) or {}

    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify({"ok": False, "error": "没有需要朗读的内容。", "fallback": "browser"}), 400

    # 参数兜底：语速 0.5~2.0，音量 0~2.0，音调 -50~50Hz
    rate = read_float(data, "rate", 1.0, 0.5, 2.0)
    volume = read_float(data, "volume", 1.0, 0.0, 2.0)
    try:
        pitch = int(data.get("pitch", 0))
    except (TypeError, ValueError):
        pitch = 0
    pitch = min(max(pitch, -50), 50)

    try:
        meta = voice_core.synthesize_meta(
            text,
            data.get("voice"),
            rate=rate,
            volume=volume,
            pitch=pitch,
        )
    except voice_core.SpeakError as exc:
        # 没装 edge-tts -> 503（功能不可用）；装了但合成失败 -> 502（上游抖动）
        status = 503 if not voice_core.is_edge_tts_available() else 502
        app.logger.warning("speak_with_edge_tts() 失败，前端将降级到浏览器朗读: %s", exc)
        return jsonify({"ok": False, "error": str(exc), "fallback": "browser"}), status
    except Exception as exc:  # noqa: BLE001 —— 兜底，同样引导前端降级
        app.logger.exception("speak_with_edge_tts() 未预期异常")
        return jsonify({"ok": False, "error": f"语音合成异常：{exc}", "fallback": "browser"}), 500

    response = Response(meta["audio"], mimetype="audio/mpeg")
    response.headers["X-TTS-Voice"] = meta["voice"]
    response.headers["X-TTS-Elapsed-Ms"] = str(meta["elapsed_ms"])
    response.headers["Cache-Control"] = "no-store"
    app.logger.info(
        "speak_with_edge_tts() 成功 | %.2fs | 音色 %s | %d 字节 | %d 字",
        meta["elapsed_ms"] / 1000,
        meta["voice"],
        meta["bytes"],
        len(text),
    )
    return response


@app.get("/api/voices")
def api_voices():
    """返回可选的 edge-tts 音色清单，供前端下拉框使用。

    Returns:
        {"ok": true, "edge_available": bool, "default": str,
         "voices": [{"name": str, "label": str, "gender": str, "locale": str}, ...]}
    """
    return jsonify(
        {
            "ok": True,
            "edge_available": voice_core.is_edge_tts_available(),
            "default": voice_core.DEFAULT_EDGE_VOICE,
            "voices": voice_core.list_voices(),
        }
    )


# --------------------------------------------------------------------------- #
# 记忆接口：remember() 的可视化出口
# --------------------------------------------------------------------------- #
@app.get("/api/history")
def api_history():
    """读取某个会话的对话记忆（刷新页面后前端用它恢复对话区）。

    Returns:
        {"ok": true, "session_id": str, "messages": [...], "turns": int, "max_turns": int}
    """
    session_id = read_session_id()
    messages = voice_core.get_history(session_id)
    return jsonify(
        {
            "ok": True,
            "session_id": session_id,
            "messages": messages,
            "turns": voice_core.count_turns(messages),
            "max_turns": voice_core.MAX_MEMORY_TURNS,
        }
    )


@app.post("/api/reset")
def api_reset():
    """清空某个会话的对话记忆。

    Returns:
        {"ok": true, "cleared": int, "session_id": str}
    """
    data = request.get_json(silent=True) or {}
    session_id = read_session_id(data)
    cleared = voice_core.reset_history(session_id)
    app.logger.info("已清空会话 %s 的记忆：%d 条消息", session_id, cleared)
    return jsonify({"ok": True, "cleared": cleared, "session_id": session_id})



# --------------------------------------------------------------------------- #
# Agent 扩展（v3 新增）
# --------------------------------------------------------------------------- #
# 上面所有路由保持原样，一行未改。这里只是把 Agent 化的接口以 Blueprint 形式
# 追加挂载进来；即使新模块有问题，也绝不影响原有的语音对话功能。
try:
    import agent_routes

    agent_routes.register(app)
except Exception as exc:  # noqa: BLE001 —— 扩展模块异常不能拖垮主服务
    print(f"[警告] Agent 扩展加载失败，将只提供基础语音对话功能：{exc}")


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def open_browser_later(url: str, delay: float = 1.2) -> None:
    """延迟一小会儿再打开浏览器，确保 Flask 已经监听成功。"""
    threading.Timer(delay, lambda: webbrowser.open(url)).start()


def print_banner() -> None:
    """在控制台打印启动信息与各项能力状态，方便排错。"""
    line = "=" * 62
    print(line)
    print("  语音交互助手  Voice Assistant  (Flask + DeepSeek + edge-tts)")
    print(line)
    print(f"  访问地址 : http://{HOST}:{PORT}")
    print(f"  默认模型 : {voice_core.DEFAULT_MODEL}")
    if voice_core.has_api_key():
        print(f"  密钥状态 : 已从环境变量 {voice_core.ENV_API_KEY} 读取 ✔")
    else:
        print(f"  密钥状态 : ✘ 未找到环境变量 {voice_core.ENV_API_KEY}")
        print("             请设置该环境变量后重新启动，否则对话会失败。")
        print("             PowerShell 一次性设置示例：")
        print(f'             $env:{voice_core.ENV_API_KEY}="sk-你的密钥"')

    # 语音合成：优先 edge-tts（后端），失败时前端会自动降级到浏览器朗读
    if voice_core.is_edge_tts_available():
        print(f"  朗读引擎 : edge-tts 在线合成 ✔ 默认音色 {voice_core.DEFAULT_EDGE_VOICE}")
        print(f"             共 {len(voice_core.list_voices())} 个可选音色；不可用时自动降级为浏览器朗读")
    else:
        print("  朗读引擎 : ⚠ 未安装 edge-tts，将使用浏览器原生朗读")
        print("             想要更好的音质可执行：pip install edge-tts")

    print(f"  对话记忆 : 每个会话保留最近 {voice_core.MAX_MEMORY_TURNS} 轮对话")
    print(line)
    print("  提示：麦克风功能需要 Chrome / Edge 浏览器，并允许麦克风权限。")
    print("        按 Ctrl + C 可停止服务。")
    print(line)


if __name__ == "__main__":
    print_banner()
    # 只有主进程才打开浏览器（避免 debug 重载时打开两次）
    if os.getenv("WERKZEUG_RUN_MAIN") != "true":
        open_browser_later(f"http://{HOST}:{PORT}")
    try:
        app.run(host=HOST, port=PORT, debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\n已停止服务，再见。")
        sys.exit(0)
