# -*- coding: utf-8 -*-
"""
voice_core.py —— 语音交互应用的「思考层 + 语音合成层 + 记忆层」核心模块

设计原则
--------
1. 函数式架构：每个功能拆成一个独立的纯函数，输入 -> 输出，不依赖全局可变状态。
2. 每个函数都能单独测试：网络请求被抽成 `http_post_json()` 和 `_edge_synthesize()`，
   测试时传入替身就能离线跑通，不需要真实 API Key、不需要联网。
3. 不硬编码密钥：API Key 统一由 `get_api_key()` 从环境变量 DEEPSEEK_API_KEY 读取。
4. 唯一的可变状态是「会话记忆」，被收在 `remember()/get_history()/reset_history()` 三个函数里，
   并用模块级锁保护；其余函数都是无副作用的。

对应关系
--------
    listen()               -> 前端 static/app.js   （浏览器 SpeechRecognition API，录音 + 识别，架构未改动）
    think()                -> 本文件                 （调用 DeepSeek 大模型生成文字回答，架构未改动）
    speak_with_edge_tts()  -> 本文件                 （edge-tts 在线合成 MP3，支持传入音色名）
    speak_fallback()       -> 前端 static/app.js   （浏览器 speechSynthesis，edge-tts 失败时自动降级）
    remember()             -> 本文件                 （维护 messages 列表，只保留最近 10 轮对话）

命令行自测
----------
    python voice_core.py            # 直接跑内置的自测用例（不联网）
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Sequence

# --------------------------------------------------------------------------- #
# 常量配置
# --------------------------------------------------------------------------- #

#: 环境变量名，密钥从这里读取，绝不写死在代码里
ENV_API_KEY = "DEEPSEEK_API_KEY"

#: DeepSeek 官方 Chat Completions 接口
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

#: 默认模型：deepseek-chat 便宜且响应快，适合语音对话
DEFAULT_MODEL = "deepseek-chat"

#: 语音场景要求「短、口语化」，所以在系统提示里强约束输出风格
DEFAULT_SYSTEM_PROMPT = (
    "你是一个语音助手，回答会被直接朗读出来。请遵守："
    "1) 用口语化、自然的中文回答；"
    "2) 简洁直接，一般不超过 150 字，除非用户明确要求详细；"
    "3) 不要使用 Markdown 标记、代码块、星号、井号等会被读出来的符号；"
    "4) 不要输出 emoji。"
)

#: 记忆保留多少轮对话（1 轮 = 用户 + 助手各一条）；同时用于历史清洗
MAX_MEMORY_TURNS = 10

#: 兼容旧名字：think() 携带的历史轮数上限，与记忆上限保持一致
MAX_HISTORY_TURNS = MAX_MEMORY_TURNS

#: 会话记忆最多保留多少个会话，超出后淘汰最早创建的（本地单人使用足够）
MAX_MEMORY_SESSIONS = 50

#: 默认会话 ID（前端没传 session_id 时使用）
DEFAULT_SESSION_ID = "default"

# --------------------------------------------------------------------------- #
# edge-tts（在线语音合成）相关配置
# --------------------------------------------------------------------------- #

#: 默认音色：晓晓，中文女声，通用场景最自然
DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"

#: 单次合成的超时秒数（合成一句话通常 1~2 秒，留足余量）
EDGE_TTS_TIMEOUT = 20.0

#: 失败重试次数（含首次共 3 次）。
#: edge-tts 走微软的在线接口，偶发连不上是常态（实测同一台机器成功率会有波动），
#: 重试能救回大部分请求；全部失败才会抛 SpeakError 让前端降级，所以多一点更稳妥。
EDGE_TTS_RETRIES = 3

#: 可选音色清单（服务端不联网也能给出下拉框，避免网络抖动导致界面空白）
EDGE_VOICE_CATALOG: tuple[dict[str, str], ...] = (
    {"name": "zh-CN-XiaoxiaoNeural", "label": "晓晓 · 温柔女声", "gender": "女", "locale": "普通话"},
    {"name": "zh-CN-XiaoyiNeural", "label": "晓伊 · 活泼女声", "gender": "女", "locale": "普通话"},
    {"name": "zh-CN-YunxiNeural", "label": "云希 · 阳光男声", "gender": "男", "locale": "普通话"},
    {"name": "zh-CN-YunjianNeural", "label": "云健 · 浑厚男声", "gender": "男", "locale": "普通话"},
    {"name": "zh-CN-YunyangNeural", "label": "云扬 · 新闻播报", "gender": "男", "locale": "普通话"},
    {"name": "zh-CN-YunxiaNeural", "label": "云夏 · 少年音", "gender": "男", "locale": "普通话"},
    {"name": "zh-CN-liaoning-XiaobeiNeural", "label": "晓北 · 东北方言", "gender": "女", "locale": "东北官话"},
    {"name": "zh-CN-shaanxi-XiaoniNeural", "label": "晓妮 · 陕西方言", "gender": "女", "locale": "中原官话"},
    {"name": "zh-HK-HiuMaanNeural", "label": "曉曼 · 粤语女声", "gender": "女", "locale": "粤语（中国香港）"},
    {"name": "zh-HK-WanLungNeural", "label": "雲龍 · 粤语男声", "gender": "男", "locale": "粤语（中国香港）"},
    {"name": "zh-TW-HsiaoChenNeural", "label": "曉臻 · 国语女声", "gender": "女", "locale": "国语（中国台湾）"},
    {"name": "zh-TW-YunJheNeural", "label": "雲哲 · 国语男声", "gender": "男", "locale": "国语（中国台湾）"},
)

#: 允许通过环境变量强制指定代理，例如公司网络下需要走代理才能访问微软接口
ENV_EDGE_PROXY = "EDGE_TTS_PROXY"


class ThinkError(RuntimeError):
    """think() 调用过程中出现的可预期错误（无 Key、网络失败、模型报错等）。"""


class SpeakError(RuntimeError):
    """speak_with_edge_tts() 失败时抛出。

    上层（app.py / 前端）捕获它就应当自动降级到 speak_fallback()，
    所以错误信息必须是「给用户看的、可读的中文」。
    """


# --------------------------------------------------------------------------- #
# 1. 密钥读取
# --------------------------------------------------------------------------- #
def get_api_key() -> str:
    """从环境变量读取 DeepSeek API Key。

    Returns:
        str: 去除首尾空格后的密钥字符串。

    Raises:
        ThinkError: 环境变量不存在或为空时抛出，附带设置方法的提示。
    """
    key = (os.getenv(ENV_API_KEY) or "").strip()
    if not key:
        raise ThinkError(
            f"未找到环境变量 {ENV_API_KEY}。\n"
            f"请先设置：Windows 用户可在「设置 -> 系统 -> 系统信息 -> 高级系统设置 -> 环境变量」"
            f"中新增用户变量 {ENV_API_KEY}，值为你的 DeepSeek API Key，"
            f"设置后需要重新启动命令行/程序才能生效。"
        )
    return key


def has_api_key() -> bool:
    """判断环境变量里是否配置了密钥，供健康检查接口使用（不抛异常）。"""
    return bool((os.getenv(ENV_API_KEY) or "").strip())


# --------------------------------------------------------------------------- #
# 2. 请求体构造（纯函数，零副作用）
# --------------------------------------------------------------------------- #
def sanitize_history(
    history: Iterable[dict] | None,
    max_turns: int = MAX_HISTORY_TURNS,
) -> list[dict[str, str]]:
    """清洗前端传来的历史对话，只保留最近的若干轮。

    前端数据不可信，可能出现角色错误、空内容、超长文本，这里统一过滤。

    Args:
        history: [{"role": "user"/"assistant", "content": "..."}, ...]
        max_turns: 最多保留多少轮对话（一轮含两条消息）。

    Returns:
        清洗后的消息列表，元素形如 {"role": "user", "content": "你好"}。
    """
    if not history:
        return []

    cleaned: list[dict[str, str]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        content = str(item.get("content", "")).strip()
        if role not in ("user", "assistant") or not content:
            continue
        # 单条消息做个长度上限，防止把上下文撑爆
        cleaned.append({"role": role, "content": content[:2000]})

    # 只保留最近 max_turns 轮
    if max_turns > 0:
        cleaned = cleaned[-(max_turns * 2):]
    return cleaned


def build_messages(
    user_text: str,
    history: Iterable[dict] | None = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
) -> list[dict[str, str]]:
    """把「系统提示 + 历史对话 + 本次提问」拼成模型需要的 messages 数组。

    Args:
        user_text: 用户这次说的话（已经过语音识别）。
        history: 之前的对话历史，可为 None。
        system_prompt: 系统提示词，控制助手的人设与回答风格。

    Returns:
        可直接放进请求体的 messages 列表。
    """
    messages: list[dict[str, str]] = []
    if system_prompt and system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt.strip()})
    messages.extend(sanitize_history(history))
    messages.append({"role": "user", "content": user_text.strip()})
    return messages


def build_request_payload(
    messages: Sequence[dict[str, str]],
    model: str = DEFAULT_MODEL,
    temperature: float = 1.0,
    max_tokens: int = 1024,
) -> dict[str, Any]:
    """构造发给 DeepSeek 的 JSON 请求体（纯函数，方便断言测试）。

    temperature 默认 1.0：DeepSeek 官方对通用对话的推荐值。
    """
    return {
        "model": model,
        "messages": list(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }


def build_headers(api_key: str) -> dict[str, str]:
    """构造 HTTP 请求头（纯函数）。"""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


# --------------------------------------------------------------------------- #
# 3. 网络层（唯一有副作用的函数，便于注入替身做测试）
# --------------------------------------------------------------------------- #
def http_post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float = 60.0,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """发送 POST JSON 请求并把响应解析成 dict。

    这是整个模块唯一真正访问网络的地方。把它单独抽出来，
    单元测试就可以传入一个假的 `opener`，完全离线验证 think() 的逻辑。

    Args:
        url: 目标地址。
        payload: 请求体字典。
        headers: 请求头。
        timeout: 超时秒数（大模型首字响应可能较慢，默认给足 60 秒）。
        opener: 可注入的请求函数，签名兼容 urllib.request.urlopen。

    Returns:
        解析后的响应字典。

    Raises:
        ThinkError: 网络异常、超时或服务端返回非 2xx 时抛出，消息已中文化。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    open_fn = opener or urllib.request.urlopen

    try:
        with open_fn(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ThinkError(f"大模型接口返回错误（HTTP {exc.code}）：{extract_error(detail)}") from exc
    except urllib.error.URLError as exc:
        raise ThinkError(f"无法连接大模型接口，请检查网络：{exc.reason}") from exc
    except TimeoutError as exc:
        raise ThinkError("请求大模型超时，请稍后重试。") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ThinkError(f"大模型返回的内容不是合法 JSON：{raw[:200]}") from exc


def extract_error(text: str) -> str:
    """从错误响应里尽力抽出人类可读的错误信息（纯函数）。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return (text or "").strip()[:300] or "未知错误"
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            return error
        if data.get("message"):
            return str(data["message"])
    return (text or "").strip()[:300] or "未知错误"


# --------------------------------------------------------------------------- #
# 4. 响应解析（纯函数）
# --------------------------------------------------------------------------- #
def parse_response(data: dict[str, Any]) -> str:
    """从 DeepSeek 的响应里取出回答文本。

    Args:
        data: 接口返回的 JSON 字典。

    Returns:
        助手回答的纯文本（已去除首尾空白）。

    Raises:
        ThinkError: 结构不符合预期或回答为空时抛出。
    """
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        # 有些错误是以 200 + error 字段返回的
        if data.get("error"):
            raise ThinkError(f"大模型返回错误：{extract_error(json.dumps(data, ensure_ascii=False))}")
        raise ThinkError("大模型返回结构异常：缺少 choices 字段。")

    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if not content:
        raise ThinkError("大模型返回了空回答，请重试。")
    return content


def get_usage(data: dict[str, Any]) -> dict[str, int]:
    """提取 token 用量，便于在日志/界面上展示（纯函数，缺失时返回空字典）。"""
    usage = data.get("usage") or {}
    return {k: int(v) for k, v in usage.items() if isinstance(v, (int, float))}


# --------------------------------------------------------------------------- #
# 5. 对外主函数：think()
# --------------------------------------------------------------------------- #
def think(
    user_text: str,
    history: Iterable[dict] | None = None,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    temperature: float = 1.0,
    max_tokens: int = 1024,
    timeout: float = 60.0,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    url: str = DEEPSEEK_API_URL,
    opener: Callable[..., Any] | None = None,
) -> str:
    """【核心】把用户说的话交给大模型，拿回一段文字回答。

    这就是 listen() -> think() -> speak() 流程里的「思考」环节。

    Args:
        user_text: 用户说的话。
        history: 历史对话，形如 [{"role":"user","content":"..."}, ...]。
        api_key: 显式指定密钥；不传则走 get_api_key() 读环境变量。
        model: 模型名，默认 deepseek-chat。
        temperature / max_tokens: 采样参数。
        timeout: 网络超时秒数。
        system_prompt: 系统提示词。
        url: 接口地址（可换成兼容 OpenAI 协议的其它网关）。
        opener: 可注入的网络函数，测试用。

    Returns:
        大模型生成的文字回答。

    Raises:
        ThinkError: 输入为空、密钥缺失、网络异常或返回异常时抛出。

    示例：
        >>> think("你好", opener=fake_opener, api_key="sk-test")   # doctest: +SKIP
        '你好呀，有什么可以帮你的？'
    """
    text = (user_text or "").strip()
    if not text:
        raise ThinkError("没有收到有效的提问内容。")

    key = api_key or get_api_key()
    messages = build_messages(text, history, system_prompt=system_prompt)
    payload = build_request_payload(messages, model=model, temperature=temperature, max_tokens=max_tokens)

    data = http_post_json(url, payload, build_headers(key), timeout=timeout, opener=opener)
    return parse_response(data)


def think_with_meta(user_text: str, history: Iterable[dict] | None = None, **kwargs: Any) -> dict[str, Any]:
    """think() 的增强版：额外返回耗时与 token 用量，供 Web 接口直接序列化成 JSON。

    Returns:
        {"reply": str, "elapsed_ms": int, "model": str, "usage": dict}
    """
    model = kwargs.get("model", DEFAULT_MODEL)
    started = time.perf_counter()
    messages = build_messages((user_text or "").strip(), history, kwargs.get("system_prompt", DEFAULT_SYSTEM_PROMPT))
    payload = build_request_payload(
        messages,
        model=model,
        temperature=kwargs.get("temperature", 1.0),
        max_tokens=kwargs.get("max_tokens", 1024),
    )
    key = kwargs.get("api_key") or get_api_key()
    data = http_post_json(
        kwargs.get("url", DEEPSEEK_API_URL),
        payload,
        build_headers(key),
        timeout=kwargs.get("timeout", 60.0),
        opener=kwargs.get("opener"),
    )
    reply = parse_response(data)
    return {
        "reply": reply,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "model": model,
        "usage": get_usage(data),
    }


# --------------------------------------------------------------------------- #
# 6. 语音合成层：speak_with_edge_tts()
# --------------------------------------------------------------------------- #
# 说明：这一层负责把文字变成 MP3 音频。它跑在后端（Python 有 edge-tts 库），
#       前端拿到 MP3 后用 <audio> 播放。一旦这里失败（断网、微软接口抖动、
#       没装 edge-tts……），就抛 SpeakError，由前端自动降级到 speak_fallback()。
# --------------------------------------------------------------------------- #

#: 单次朗读的文本长度上限，避免把整篇长回答丢给语音接口
MAX_TTS_CHARS = 1500


def is_edge_tts_available() -> bool:
    """edge-tts 库是否已安装（只查模块是否存在，不联网）。"""
    return importlib.util.find_spec("edge_tts") is not None


def _format_percent(multiplier: Any, low: int, high: int) -> str:
    """把「倍率」换算成 edge-tts 需要的百分比字符串（纯函数）。

    edge-tts 不接受倍率，只接受相对变化量：原速是 '+0%'，快两成是 '+20%'。

    Args:
        multiplier: 1.0 表示原速。
        low: 百分比下限，防止用户传出极端值。
        high: 百分比上限。

    Returns:
        形如 '+20%' / '-30%' / '+0%' 的字符串。
    """
    try:
        value = float(multiplier)
    except (TypeError, ValueError):
        value = 1.0
    percent = int(round((value - 1.0) * 100))
    return f"{max(low, min(high, percent)):+d}%"


def format_rate(multiplier: Any = 1.0) -> str:
    """把语速倍率换算成 edge-tts 的 rate 参数（纯函数）。

    Args:
        multiplier: 0.5 ~ 2.0 的倍率，1.0 为原速。

    Returns:
        '+20%' 这样的字符串，范围夹在 -90% ~ +200%。
    """
    return _format_percent(multiplier, low=-90, high=200)


def format_volume(multiplier: Any = 1.0) -> str:
    """把音量倍率换算成 edge-tts 的 volume 参数（纯函数），范围夹在 -100% ~ +100%。"""
    return _format_percent(multiplier, low=-100, high=100)


def format_pitch(hertz: Any = 0) -> str:
    """把音调赫兹数换算成 edge-tts 的 pitch 参数（纯函数），如 '+5Hz' / '-10Hz'。"""
    try:
        value = int(round(float(hertz)))
    except (TypeError, ValueError):
        value = 0
    return f"{max(-100, min(100, value)):+d}Hz"


def resolve_voice(voice_name: Any) -> str:
    """校验并规整音色名，非法或为空时回落到默认音色（纯函数）。

    只允许字母、数字、短横线、下划线组成的名字：
    这样既挡住了乱填的内容，又不会妨碍微软后续上线音色时直接透传。

    Args:
        voice_name: 例如 'zh-CN-XiaoxiaoNeural'。

    Returns:
        合法的音色名，或默认音色 DEFAULT_EDGE_VOICE。
    """
    name = str(voice_name or "").strip()
    if not name:
        return DEFAULT_EDGE_VOICE
    if not all(ch.isalnum() or ch in "-_" for ch in name):
        return DEFAULT_EDGE_VOICE
    return name


def list_voices() -> list[dict[str, str]]:
    """返回可选音色清单（纯数据，不联网）。

    刻意用本地内置清单而不是实时拉取微软接口：网络抖动时前端下拉框依旧是满的。
    """
    return [dict(item) for item in EDGE_VOICE_CATALOG]


def resolve_proxy(proxy: str | None = None) -> str | None:
    """决定本次合成走不走代理（纯函数，只读环境变量）。

    优先级：显式参数 > EDGE_TTS_PROXY > HTTPS_PROXY。
    都没有就直连。留这个口子是为了兼容公司网络必须走代理的环境。

    Returns:
        代理地址，或 None（表示直连）。
    """
    for candidate in (proxy, os.getenv(ENV_EDGE_PROXY), os.getenv("HTTPS_PROXY"), os.getenv("https_proxy")):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return None


def _proxy_candidates(proxy: str | None = None) -> list[str | None]:
    """返回本次调用依次要尝试的代理列表（纯函数）。

    有代理时先走代理、再直连各试一次——代理本身也可能是那个不稳定因素。
    """
    found = resolve_proxy(proxy)
    return [found, None] if found else [None]


async def _edge_synthesize(
    text: str,
    voice: str,
    *,
    rate: str,
    volume: str,
    pitch: str,
    proxy: str | None,
    connect_timeout: float,
    receive_timeout: float,
) -> bytes:
    """协程：真正和微软的语音接口通信，把音频分片拼成完整的 MP3。

    这是本模块第二处（也是最后一处）真正访问网络的地方。
    edge-tts 的 stream() 会同时产出 'audio'（音频分片）和 'WordBoundary'（时间轴），
    这里只关心前者。

    Returns:
        MP3 字节流。

    Raises:
        原样抛出 edge-tts / aiohttp 的异常，由调用方包装成 SpeakError。
    """
    import edge_tts  # 延迟导入：没装 edge-tts 时也不影响其它功能

    kwargs: dict[str, Any] = {
        "rate": rate,
        "volume": volume,
        "pitch": pitch,
        "connect_timeout": connect_timeout,
        "receive_timeout": receive_timeout,
    }
    if proxy:
        kwargs["proxy"] = proxy

    communicate = edge_tts.Communicate(text, voice, **kwargs)
    audio = bytearray()
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio" and chunk.get("data"):
            audio.extend(chunk["data"])
    return bytes(audio)


def _run_coroutine(factory: Callable[[], Any], timeout: float) -> Any:
    """在同步函数里跑一个协程，并施加超时。

    Windows 上显式使用 SelectorEventLoop：aiohttp 的 WebSocket 在默认的
    Proactor 事件循环下偶发 '远程主机强迫关闭了一个现有的连接'，Selector 更稳。
    每次调用新建/销毁事件循环，避免 Flask 多线程下共用循环出问题。

    Args:
        factory: 返回协程的零参函数。用工厂而不是直接传协程，是为了避免
                 事件循环创建失败时留下 'coroutine was never awaited' 警告。
        timeout: 超时秒数。
    """
    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(asyncio.wait_for(factory(), timeout=timeout))
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(None)


def _synthesize_via_edge_tts(
    text: str,
    voice: str,
    *,
    rate: str,
    volume: str,
    pitch: str,
    proxy: str | None,
    timeout: float,
) -> bytes:
    """默认合成器：调用 edge-tts 并驱动事件循环（唯一的联网实现）。

    签名与 `speak_with_edge_tts(..., synthesizer=...)` 期望的替身一致，
    测试时换成假函数即可完全离线验证重试与降级逻辑。
    """
    return _run_coroutine(
        lambda: _edge_synthesize(
            text,
            voice,
            rate=rate,
            volume=volume,
            pitch=pitch,
            proxy=proxy,
            # 注意：edge-tts 对这两个参数做的是 int 校验，传 float 会直接抛
            # TypeError: connect_timeout must be int，所以这里必须取整。
            connect_timeout=max(1, int(min(10.0, timeout))),
            receive_timeout=max(1, int(timeout)),
        ),
        timeout=timeout,
    )


def speak_with_edge_tts(
    text: str,
    voice_name: str | None = None,
    *,
    rate: float = 1.0,
    volume: float = 1.0,
    pitch: int = 0,
    retries: int = EDGE_TTS_RETRIES,
    timeout: float = EDGE_TTS_TIMEOUT,
    proxy: str | None = None,
    synthesizer: Callable[..., bytes] | None = None,
) -> bytes:
    """【对外主函数】用 edge-tts 把文字合成一段 MP3 音频。

    这是 listen() -> think() -> speak() 流程里「朗读」环节的服务端实现。
    失败时不重试到底层崩溃，而是抛出 SpeakError，让前端降级到浏览器朗读。

    Args:
        text: 要朗读的文字。
        voice_name: 音色名，如 'zh-CN-XiaoxiaoNeural'；非法/为空则用默认音色。
        rate: 语速倍率，1.0 为原速。
        volume: 音量倍率，1.0 为原音量。
        pitch: 音调偏移，单位 Hz，0 为原调。
        retries: 总尝试次数（含首次）。edge-tts 偶发连不上，重试能救回大部分请求。
        timeout: 单次尝试的超时秒数。
        proxy: 显式指定代理；不传则读 EDGE_TTS_PROXY / HTTPS_PROXY。
        synthesizer: 可注入的合成器替身，测试用。

    Returns:
        MP3 音频字节流。

    Raises:
        SpeakError: 内容为空、未安装 edge-tts，或所有尝试都失败时抛出。
                    调用方应当捕获它并降级到 speak_fallback()。

    示例：
        >>> audio = speak_with_edge_tts("你好", "zh-CN-XiaoxiaoNeural")   # doctest: +SKIP
        >>> audio[:3]                                                     # doctest: +SKIP
        b'ID3'
    """
    content = (text or "").strip()
    if not content:
        raise SpeakError("没有需要朗读的内容。")

    if not is_edge_tts_available():
        raise SpeakError("服务端未安装 edge-tts，请执行：pip install edge-tts")

    # 长回答截断，避免合成一首「长篇评书」
    if len(content) > MAX_TTS_CHARS:
        content = content[:MAX_TTS_CHARS]

    voice = resolve_voice(voice_name)
    rate_arg, volume_arg, pitch_arg = format_rate(rate), format_volume(volume), format_pitch(pitch)

    engine = synthesizer or _synthesize_via_edge_tts
    attempts = max(1, int(retries or 1))
    proxies = _proxy_candidates(proxy)

    errors: list[BaseException] = []
    for index in range(attempts):
        current_proxy = proxies[index % len(proxies)]
        try:
            audio = engine(
                content,
                voice,
                rate=rate_arg,
                volume=volume_arg,
                pitch=pitch_arg,
                proxy=current_proxy,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 —— 网络异常种类太多，统一收口成 SpeakError
            errors.append(exc)
            # 还有下一次尝试的话，稍等一下再试（微软接口偶发抖动，退避能明显提高成功率）
            if index < attempts - 1:
                time.sleep(0.4 * (index + 1))
            continue

        if not audio:
            errors.append(SpeakError("edge-tts 返回了空音频。"))
            continue
        return audio

    detail = f"{type(errors[-1]).__name__}: {errors[-1]}" if errors else "未知错误"
    raise SpeakError(f"edge-tts 合成失败（已尝试 {attempts} 次）——{detail}")


def synthesize_meta(
    text: str,
    voice_name: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """speak_with_edge_tts() 的增强版：额外返回耗时与实际使用的音色。

    供 Web 接口记录日志 / 回传给前端展示「用了哪个引擎」。

    Returns:
        {"audio": bytes, "voice": str, "elapsed_ms": int, "bytes": int}
    """
    started = time.perf_counter()
    audio = speak_with_edge_tts(text, voice_name, **kwargs)
    return {
        "audio": audio,
        "voice": resolve_voice(voice_name),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "bytes": len(audio),
    }


# --------------------------------------------------------------------------- #
# 7. 记忆层：remember()
# --------------------------------------------------------------------------- #
# 说明：模块唯一的可变状态就在这里。前端每轮对话结束后调用一次 remember()，
#       下一轮 think() 会把这里存的历史一起发给大模型，从而实现「记得上一句」。
#       默认只保留最近 MAX_MEMORY_TURNS（10）轮，超出的自动丢弃。
# --------------------------------------------------------------------------- #

#: 会话 ID -> messages 列表。用模块级字典 + 锁，Flask 多线程下也安全。
_MEMORY: dict[str, list[dict[str, str]]] = {}
_MEMORY_LOCK = threading.Lock()


def trim_turns(messages: Iterable[dict] | None, max_turns: int = MAX_MEMORY_TURNS) -> list[dict[str, str]]:
    """纯函数：只保留最近 max_turns 轮对话。

    Args:
        messages: 形如 [{"role": "user", "content": "..."}, ...] 的消息列表。
        max_turns: 保留轮数（1 轮 = 用户 + 助手各一条）。

    Returns:
        截断后的新列表（元素是浅拷贝，不影响入参）。
    """
    if not messages or max_turns <= 0:
        return []
    # 不假设列表长度一定是偶数：直接按「最多 2×轮数 条」从尾部截取
    return [dict(item) for item in list(messages)[-(max_turns * 2):]]


def count_turns(messages: Iterable[dict] | None) -> int:
    """纯函数：把消息条数换算成「轮数」（向上取整），供界面展示。"""
    if not messages:
        return 0
    return (len(list(messages)) + 1) // 2


def remember(
    user_msg: str,
    ai_msg: str,
    *,
    session_id: str = DEFAULT_SESSION_ID,
    max_turns: int = MAX_MEMORY_TURNS,
) -> list[dict[str, str]]:
    """【对外主函数】把一轮问答记进会话记忆，返回更新后的 messages 列表。

    记忆是「滑动窗口」：超过 max_turns 轮时，最旧的一轮会被丢掉，
    所以发给大模型的上下文永远不会无限膨胀。

    Args:
        user_msg: 用户这一轮说的话。
        ai_msg: 助手这一轮的回答。
        session_id: 会话标识（一个浏览器标签页一个），用来隔离不同对话。
        max_turns: 保留多少轮。

    Returns:
        更新后的 messages 列表：形如
        [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]

    示例：
        >>> remember("今天几号", "9 月 11 日")      # doctest: +SKIP
        [{'role': 'user', 'content': '今天几号'}, {'role': 'assistant', 'content': '9 月 11 日'}]
    """
    # 两个参数都为空时不写入，避免污染上下文
    user_text = str(user_msg or "").strip()
    ai_text = str(ai_msg or "").strip()
    key = str(session_id or "").strip() or DEFAULT_SESSION_ID

    with _MEMORY_LOCK:
        messages = _MEMORY.get(key)
        if messages is None:
            # 会话数上限保护：超出后淘汰最早创建的那个会话
            if len(_MEMORY) >= MAX_MEMORY_SESSIONS:
                _MEMORY.pop(next(iter(_MEMORY)), None)
            messages = _MEMORY[key] = []

        if user_text:
            messages.append({"role": "user", "content": user_text})
        if ai_text:
            messages.append({"role": "assistant", "content": ai_text})

        # 写入后立刻裁剪，保证存储里最多只有 max_turns 轮
        _MEMORY[key] = trim_turns(messages, max_turns)
        return [dict(item) for item in _MEMORY[key]]


def get_history(session_id: str = DEFAULT_SESSION_ID) -> list[dict[str, str]]:
    """读取某个会话的历史消息（返回副本，调用方随便改都不会影响记忆）。"""
    key = str(session_id or "").strip() or DEFAULT_SESSION_ID
    with _MEMORY_LOCK:
        return [dict(item) for item in _MEMORY.get(key, [])]


def reset_history(session_id: str = DEFAULT_SESSION_ID) -> int:
    """清空某个会话的记忆。

    Returns:
        被清掉的消息条数，方便前端提示「已清空 N 条记忆」。
    """
    key = str(session_id or "").strip() or DEFAULT_SESSION_ID
    with _MEMORY_LOCK:
        return len(_MEMORY.pop(key, []))


def memory_stats() -> dict[str, int]:
    """返回记忆占用概况，供健康检查接口展示。"""
    with _MEMORY_LOCK:
        return {
            "sessions": len(_MEMORY),
            "messages": sum(len(v) for v in _MEMORY.values()),
            "max_turns": MAX_MEMORY_TURNS,
        }


def think_with_memory(
    user_text: str,
    *,
    session_id: str = DEFAULT_SESSION_ID,
    max_turns: int = MAX_MEMORY_TURNS,
    **kwargs: Any,
) -> dict[str, Any]:
    """think() 的记忆增强版：自动带上历史 + 自动记住这一轮。

    把「取历史 -> 问模型 -> 记下来」三步串成一个函数，路由层只要调它一次。
    注意 think() 本身没有改动，它依旧只关心「给一段历史、要一个回答」。

    Returns:
        think_with_meta() 的结果，并额外带上 "turns"（记完后共有多少轮）。
    """
    history = get_history(session_id)
    result = think_with_meta(user_text, history, **kwargs)
    messages = remember(user_text, result["reply"], session_id=session_id, max_turns=max_turns)
    result["turns"] = count_turns(messages)
    return result


# --------------------------------------------------------------------------- #
# 8. 自测入口：python voice_core.py
# --------------------------------------------------------------------------- #
class _FakeResponse:
    """模拟 urllib 返回对象，支持 with 语法与 read()。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _self_test() -> None:
    """离线自测：不需要 API Key、不联网，验证核心链路是否正常。"""
    print("=" * 60)
    print("voice_core 离线自测")
    print("=" * 60)

    # 1) 密钥读取
    print(f"[1] 环境变量 {ENV_API_KEY} 是否已配置: {has_api_key()}")

    # 2) messages 拼装
    msgs = build_messages("今天几号", [{"role": "user", "content": "你好"}], system_prompt="测试提示")
    assert msgs[0]["role"] == "system", msgs
    assert msgs[-1] == {"role": "user", "content": "今天几号"}, msgs
    print(f"[2] build_messages 正常，共 {len(msgs)} 条消息")

    # 3) 历史清洗：非法角色与空内容应被丢掉
    cleaned = sanitize_history([{"role": "hacker", "content": "x"}, {"role": "user", "content": ""}, {"role": "user", "content": "有效"}])
    assert cleaned == [{"role": "user", "content": "有效"}], cleaned
    print("[3] sanitize_history 正常，非法数据已过滤")

    # 4) 用假 opener 跑通 think() 全链路
    def fake_opener(request: Any, timeout: float = 60.0) -> _FakeResponse:
        body = json.loads(request.data.decode("utf-8"))
        assert body["model"] == DEFAULT_MODEL
        assert body["messages"][-1]["content"] == "你好"
        return _FakeResponse(
            json.dumps({"choices": [{"message": {"content": "你好呀"}}], "usage": {"total_tokens": 12}}).encode("utf-8")
        )

    reply = think("你好", api_key="sk-fake-for-test", opener=fake_opener)
    assert reply == "你好呀", reply
    print(f"[4] think() 全链路正常，回答: {reply}")

    # 5) 错误分支
    for bad in ("", "   "):
        try:
            think(bad, api_key="sk-x", opener=fake_opener)
        except ThinkError:
            pass
        else:
            raise AssertionError("空输入应当抛出 ThinkError")
    print("[5] 空输入校验正常")

    # 6) 语音合成：参数换算与音色解析（纯函数）
    assert format_rate(1.0) == "+0%", format_rate(1.0)
    assert format_rate(1.25) == "+25%", format_rate(1.25)
    assert format_rate(0.5) == "-50%", format_rate(0.5)
    assert format_rate(99) == "+200%", format_rate(99)        # 越界夹取
    assert format_volume(0.0) == "-100%", format_volume(0.0)
    assert format_pitch(5) == "+5Hz", format_pitch(5)
    assert resolve_voice("zh-CN-YunxiNeural") == "zh-CN-YunxiNeural"
    assert resolve_voice("") == DEFAULT_EDGE_VOICE            # 空值回落
    assert resolve_voice("../../etc/passwd") == DEFAULT_EDGE_VOICE  # 非法字符回落
    print(f"[6] 语音参数换算正常（默认音色 {DEFAULT_EDGE_VOICE}，共 {len(list_voices())} 个可选音色）")

    # 7) 语音合成：重试与失败收口（注入替身，不联网）
    calls: list[dict] = []

    def fake_synth(text: str, voice: str, *, rate: str, volume: str, pitch: str, proxy: Any, timeout: float) -> bytes:
        calls.append({"text": text, "voice": voice, "rate": rate, "proxy": proxy})
        if len(calls) == 1:
            raise ConnectionError("模拟第一次连不上")
        return b"ID3-fake-mp3"

    audio = speak_with_edge_tts("你好", "zh-CN-YunxiNeural", retries=2, synthesizer=fake_synth)
    assert audio == b"ID3-fake-mp3", audio
    assert len(calls) == 2, calls                      # 第一次失败后应当重试
    assert calls[0]["voice"] == "zh-CN-YunxiNeural"
    print(f"[7] speak_with_edge_tts 重试逻辑正常（第 2 次成功，共调用 {len(calls)} 次）")

    def always_fail(*args: Any, **kwargs: Any) -> bytes:
        raise TimeoutError("模拟一直超时")

    try:
        speak_with_edge_tts("你好", retries=2, synthesizer=always_fail)
    except SpeakError as exc:
        assert "2 次" in str(exc), str(exc)
        print(f"[8] 全部失败时抛出 SpeakError，前端可据此降级：{str(exc)[:38]}…")
    else:
        raise AssertionError("一直失败时应当抛出 SpeakError")

    # 8) 记忆：保留最近 10 轮
    session = "_selftest_"
    reset_history(session)
    for i in range(12):
        remember(f"第{i}轮问题", f"第{i}轮回答", session_id=session)
    history = get_history(session)
    assert len(history) == MAX_MEMORY_TURNS * 2, len(history)   # 12 轮只留 10 轮 = 20 条
    assert history[-1]["content"] == "第11轮回答", history[-1]
    assert history[0]["content"] == "第2轮问题", history[0]      # 最旧的 2 轮被丢掉
    assert count_turns(history) == MAX_MEMORY_TURNS
    print(f"[9] remember() 正常：写入 12 轮后只保留最近 {count_turns(history)} 轮（{len(history)} 条消息）")

    # 9) 记忆：会话隔离 / 清空 / 非法输入
    assert get_history("另一个会话") == [], "不同会话应当互相隔离"
    assert remember("", "", session_id=session) != [] and len(get_history(session)) == 20, "空内容不应写入"
    cleared = reset_history(session)
    assert cleared == 20 and get_history(session) == [], (cleared, get_history(session))
    print(f"[10] 会话隔离与清空正常（清掉 {cleared} 条）")

    print("-" * 60)
    print("全部自测通过 ✔")


if __name__ == "__main__":
    _self_test()
