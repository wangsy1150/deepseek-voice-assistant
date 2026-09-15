# -*- coding: utf-8 -*-
"""
agent_core.py —— Agent 决策层：工具箱 + Function Calling + decide()

这一层回答了那个问题：「AI 怎么自己决定要不要查资料？」

工作流程（ReAct 风格，最多 AGENT_MAX_STEPS 轮）
----------------------------------------------
        ┌──────────────────────────────────────────────┐
        │  decide()  带上工具箱，请模型做一次决策          │
        │     ├─ action="tool"  → 模型要调工具            │
        │     │      run_tool() 执行 → 结果塞回对话       │
        │     │      ↑ 回到 decide() 继续下一轮            │
        │     └─ action="final" → 模型给出最终回答，结束    │
        └──────────────────────────────────────────────┘

工具箱（TOOLBOX）
-----------------
目前只挂了一个工具 search_knowledge_base。每个工具在 TOOLBOX 里都有完整的
「描述说明」，这份说明会原样交给模型，模型就是靠它来判断该不该调用的。

诚实性是怎么保证的
------------------
1. 系统提示里把「资料里没有提到」写成了最高优先级规则；
2. 检索为空时，塞回模型的观察结果里再次强调不许编造；
3. 兜底：达到最大步数还没收口，就强制一次不带工具的收口回答。

命令行自测
----------
    python agent_core.py      # 离线自测，用假的 opener，不联网、不需要密钥
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterable, Sequence

import knowledge_base
from voice_core import (
    DEEPSEEK_API_URL,
    DEFAULT_MODEL,
    DEFAULT_SESSION_ID,
    MAX_MEMORY_TURNS,
    ThinkError,
    build_headers,
    build_request_payload,
    count_turns,
    get_api_key,
    get_history,
    get_usage,
    http_post_json,
    remember,
    reset_history,
    sanitize_history,
)

# --------------------------------------------------------------------------- #
# 1. 工具箱定义
# --------------------------------------------------------------------------- #

#: 工具名（同时也是函数名，两边保持一致）
SEARCH_TOOL_NAME = "search_knowledge_base"

#: ★ 这是交给模型的工具描述说明 ★
#: 模型在 auto 模式下就是靠这段话判断「我现在该不该查资料」，
#: 所以里面必须写清：能做什么、什么时候必须用、查不到意味着什么。
SEARCH_TOOL_DESCRIPTION = (
    "在用户上传的本地知识库（.md / .txt 文档）中做相关性检索，"
    "返回最相关的若干资料片段及其来源文件名。"
    "【什么时候必须调用】当用户的问题涉及事实、定义、数字、流程、政策、规则、"
    "产品/项目/公司细节，或任何有可能写在文档里的内容时，必须先调用本工具，"
    "不要凭自己的记忆回答。"
    "【什么时候不用】纯闲聊（打招呼、道谢、让你把上一句再说一遍等）不必调用。"
    "【返回什么】每个片段包含来源文件名和正文；如果返回的片段列表为空，"
    "说明知识库里没有这部分资料，此时必须如实回答「资料里没有提到」，"
    "绝对不要编造、不要推测、也不要用你自己的知识补充。"
    "【可以多次调用】第一次没查到或查得不够时，可以换更具体的关键词再查一次。"
)

#: 工具箱：一个工具一项，前端会直接把它渲染成「AI 能用哪些工具」的说明卡片
TOOLBOX: list[dict[str, Any]] = [
    {
        "name": SEARCH_TOOL_NAME,
        "label": "知识库检索",
        "icon": "📚",
        "description": SEARCH_TOOL_DESCRIPTION,
        "when": "问题涉及事实、定义、数字、流程、政策或文档内容时",
        "returns": "最相关的资料片段 + 来源文件名；查不到时返回空列表",
        "readonly": True,
        "parameters": [
            {
                "name": "query",
                "type": "string",
                "required": True,
                "description": "检索用的关键词或问题。建议使用用户原话里的核心名词，"
                               "去掉「请问」「是什么」这类口语词；第一次没查到时换同义词再试。",
            },
            {
                "name": "top_k",
                "type": "integer",
                "required": False,
                "description": "最多返回几条片段，1~8，默认 4。问题比较宽泛时可以调大。",
            },
        ],
    }
]

#: 工具名 -> 实现函数（在文件末尾 register 后填充）
_TOOL_IMPLS: dict[str, Callable[..., dict[str, Any]]] = {}

#: 一次 Agent 对话最多允许几轮「决策 + 调用工具」
AGENT_MAX_STEPS = 3

#: Agent 决策用的采样参数：温度低一点，判断更稳定
AGENT_TEMPERATURE = 0.2
AGENT_MAX_TOKENS = 900

#: 写进每个观察结果里的诚实约束（放在这里便于统一修改/测试）
HONESTY_REMINDER = "请严格依据以上资料作答；上面没有的内容，直接回答「资料里没有提到」，不要编造。"

#: 模型「宣称查不到」时的典型措辞。用于一致性校验 —— 见 claims_not_found()。
NOT_FOUND_PATTERNS: tuple[str, ...] = (
    "资料里没有", "资料中没有", "资料未提及", "资料里未提及", "资料未说明",
    "没有提到", "未提及", "没有找到", "找不到", "没有相关", "没有这方面",
    "知识库中没有", "知识库里没有", "没有记录", "没有说明", "没有规定", "没有收录",
    "不知道", "无法回答", "无法确定", "不清楚",
)


def claims_not_found(reply: str) -> bool:
    """判断一段回答是不是在「宣称查不到」（纯函数）。"""
    text = str(reply or "")
    if not text:
        return False
    return any(pattern in text for pattern in NOT_FOUND_PATTERNS)


def describe_toolbox() -> list[dict[str, Any]]:
    """返回工具箱的可序列化说明（深拷贝，调用方随便改）。"""
    return json.loads(json.dumps(TOOLBOX, ensure_ascii=False))


def build_tool_schemas() -> list[dict[str, Any]]:
    """把 TOOLBOX 转成 OpenAI / DeepSeek 兼容的 tools 参数（纯函数）。"""
    schemas: list[dict[str, Any]] = []
    for tool in TOOLBOX:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in tool.get("parameters", []):
            properties[param["name"]] = {"type": param["type"], "description": param["description"]}
            if param.get("required"):
                required.append(param["name"])
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
        )
    return schemas


def build_agent_system_prompt(knowledge_hint: str = "") -> str:
    """构造 Agent 版系统提示（纯函数）。

    Args:
        knowledge_hint: 关于知识库现状的一句话提示，例如「当前知识库里有 3 篇资料」
            或「当前知识库是空的」。让模型提前知道该不该抱期望。
    """
    lines = [
        "你是一个可以查阅资料的语音助手。你手上有工具箱，工具箱里挂着工具（函数）。",
        "",
        "【核心工作原则】",
        "1. 先判断：这个问题需不需要查资料？凡是涉及事实、定义、数字、流程、政策、"
        "产品/项目/公司细节，或任何可能写在文档里的内容，都必须先调用工具检索，"
        "绝不允许凭记忆或凭常识直接回答。",
        "2. 【查过才有资格下结论】如果你不确定知识库里有没有相关内容，就先调用工具查一遍。"
        "绝对不允许在没有调用过任何工具的情况下回答「资料里没有提到」——"
        "「查不到」这个结论必须建立在真的检索过的基础上。",
        "3. 只有纯闲聊（打招呼、道谢、让你重复上一句）才可以直接回答，不调用工具。",
        "4. 答案必须严格来自工具返回的资料片段，可以综合多个片段，但不能添加资料之外的内容。",
        "5. 【红线】如果检索结果为空、或与问题无关，必须原样回答「资料里没有提到」，"
        "一个字都不要编造、不要猜测、不要用你自己的知识补充。"
        "宁可回答「资料里没有提到」，也绝不允许编一个看起来合理的答案。",
        "6. 资料里能找到答案时：简洁准确地作答，并在句末用括号注明来源文件名，"
        "例如（来源：员工手册.md）。",
        "",
        "【输出要求】",
        "7. 回答会被语音朗读出来，所以要口语化、简短，一般不超过 200 字。",
        "8. 不要使用 Markdown 标记、代码块、星号、井号、列表符号，也不要输出 emoji。",
    ]
    if knowledge_hint:
        lines.extend(["", f"【知识库现状】{knowledge_hint}"])
    return "\n".join(lines)


def knowledge_hint() -> str:
    """根据当前知识库状态生成提示语（纯函数 + 一次轻量磁盘统计）。"""
    stats = knowledge_base.knowledge_stats()
    if stats["empty"]:
        return "当前知识库是空的，用户还没有上传任何资料。任何需要查资料的问题都应回答「资料里没有提到」。"
    names = "、".join(stats["sources"][:6])
    more = "等" if len(stats["sources"]) > 6 else ""
    return f"当前知识库里有 {stats['documents']} 篇资料（{names}{more}），共切分成 {stats['chunks']} 个可检索片段。"


# --------------------------------------------------------------------------- #
# 2. 构造发给模型的对话（纯函数）
# --------------------------------------------------------------------------- #

def build_agent_messages(
    question: str,
    history: Iterable[dict] | None = None,
    observations: Sequence[dict[str, Any]] | None = None,
    system_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """拼出这一轮要发给模型的 messages（纯函数，方便断言测试）。

    结构：system 提示 -> 历史对话 -> 用户提问 -> [assistant 调工具 + tool 结果]×N

    Args:
        question: 用户这次说的话。
        history: 之前的对话历史。
        observations: 已经执行过的工具调用及其返回，格式见 new_observation()。
        system_prompt: 覆盖默认系统提示。

    Returns:
        可直接放进请求体的 messages 列表。
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": (system_prompt if system_prompt is not None else build_agent_system_prompt()).strip()}
    ]
    messages.extend(sanitize_history(history))
    messages.append({"role": "user", "content": str(question or "").strip()})

    for obs in observations or []:
        api_call = obs.get("tool_call") or {}
        if not api_call:
            continue
        # 1) 回放模型当时那次「我要调用工具」的发言（DeepSeek 要求原样带回）
        messages.append(
            {
                "role": "assistant",
                "content": obs.get("assistant_content") or "",
                "tool_calls": [api_call],
            }
        )
        # 2) 紧跟着放工具的执行结果
        messages.append(
            {
                "role": "tool",
                "tool_call_id": api_call.get("id") or "call_0",
                "content": str(obs.get("text") or ""),
            }
        )
    return messages


def build_decide_payload(
    messages: Sequence[dict[str, Any]],
    model: str = DEFAULT_MODEL,
    temperature: float = AGENT_TEMPERATURE,
    max_tokens: int = AGENT_MAX_TOKENS,
    tools: Sequence[dict[str, Any]] | None = None,
    tool_choice: Any = "auto",
) -> dict[str, Any]:
    """构造带 tools 的请求体（纯函数）。

    tool_choice="auto" 是「让 AI 自己决定调不调用工具」的关键 ——
    我们给出工具箱，但绝不替它做决定。
    """
    payload = build_request_payload(messages, model=model, temperature=temperature, max_tokens=max_tokens)
    if tools is not None:
        payload["tools"] = list(tools)
        payload["tool_choice"] = tool_choice
    return payload


# --------------------------------------------------------------------------- #
# 3. 解析模型的决策（纯函数）
# --------------------------------------------------------------------------- #

def parse_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """从 assistant 消息里解析出 tool_calls（纯函数）。

    返回的每一项既包含好用的扁平字段（name / arguments），
    也保留 api_call 原始结构，回填给模型时必须用它。

    参数不是合法 JSON 时不抛异常，而是标记 parse_error，交给执行层兜底报错 ——
    模型偶尔会输出坏 JSON，这不该让整轮对话崩掉。
    """
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []

    calls: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            continue
        fn = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue

        raw_args = fn.get("arguments")
        parse_error = ""
        if isinstance(raw_args, dict):
            arguments = raw_args
            raw_args = json.dumps(raw_args, ensure_ascii=False)
        else:
            raw_args = raw_args if isinstance(raw_args, str) else ""
            try:
                arguments = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError as exc:
                arguments = {}
                parse_error = f"工具参数不是合法 JSON：{exc}"
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}

        call_id = str(raw.get("id") or f"call_{index}")
        calls.append(
            {
                "id": call_id,
                "name": name,
                "arguments": arguments,
                "parse_error": parse_error,
                "api_call": {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": raw_args or "{}"},
                },
            }
        )
    return calls


def parse_decision(data: dict[str, Any]) -> dict[str, Any]:
    """把一次模型响应解析成「决策」（纯函数）。

    Returns:
        {
          "action": "tool" | "final",
          "thought": str,              # 模型的自述（决定调工具时它是「为什么要查」）
          "tool_calls": [...],         # action="tool" 时非空
          "reply": str,                # action="final" 时的最终回答
          "finish_reason": str,
          "usage": {...},
        }

    Raises:
        ThinkError: 响应结构异常，或模型既没回答也没调用工具。
    """
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        if data.get("error"):
            raise ThinkError(f"大模型返回错误：{json.dumps(data.get('error'), ensure_ascii=False)[:300]}")
        raise ThinkError("大模型返回结构异常：缺少 choices 字段。")

    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = str(message.get("content") or "").strip()
    tool_calls = parse_tool_calls(message)

    if tool_calls:
        action = "tool"
    elif content:
        action = "final"
    else:
        raise ThinkError("大模型既没有给出回答，也没有调用工具，请重试。")

    return {
        "action": action,
        "thought": content if action == "tool" else "",
        "tool_calls": tool_calls,
        "reply": content if action == "final" else "",
        "finish_reason": str(choice.get("finish_reason") or ""),
        "usage": get_usage(data),
    }


# --------------------------------------------------------------------------- #
# 4. 工具实现与分发
# --------------------------------------------------------------------------- #

def search_knowledge_base(query: str, top_k: Any = None) -> dict[str, Any]:
    """【工具实现】查阅本地知识库。

    这是工具箱里 search_knowledge_base 这个工具的落地函数：做一层薄薄的
    参数容错（模型可能把 top_k 传成字符串），真正的检索交给 knowledge_base。

    Args:
        query: 检索词。
        top_k: 返回片段数，None 表示用默认值。

    Returns:
        knowledge_base.search_knowledge_base() 的结果；参数非法时返回 ok=False。
    """
    text = str(query or "").strip()
    if not text:
        return {
            "ok": False,
            "hit": False,
            "query": "",
            "count": 0,
            "results": [],
            "message": "工具调用缺少 query 参数，无法检索。",
            "elapsed_ms": 0,
        }
    limit = knowledge_base.KB_DEFAULT_TOP_K if top_k in (None, "") else top_k
    return knowledge_base.search_knowledge_base(text, top_k=limit)


def run_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """按名字执行工具箱里的工具，返回结构化结果（永不抛异常）。

    任何意外都被收敛成一个 ok=False 的结果对象，因为工具执行失败
    本身也是可以继续对话的一种「观察结果」，不该中断整轮问答。
    """
    tool_name = str(name or "").strip()
    args = arguments if isinstance(arguments, dict) else {}
    impl = _TOOL_IMPLS.get(tool_name)
    if impl is None:
        return {
            "ok": False,
            "hit": False,
            "count": 0,
            "results": [],
            "message": f"工具箱里没有名为「{tool_name}」的工具。",
            "elapsed_ms": 0,
        }
    try:
        return impl(**args)
    except TypeError as exc:
        return {
            "ok": False,
            "hit": False,
            "count": 0,
            "results": [],
            "message": f"调用工具 {tool_name} 时参数不匹配：{exc}",
            "elapsed_ms": 0,
        }
    except Exception as exc:  # noqa: BLE001 —— 工具失败也要能继续对话
        return {
            "ok": False,
            "hit": False,
            "count": 0,
            "results": [],
            "message": f"执行工具 {tool_name} 时出错：{exc}",
            "elapsed_ms": 0,
        }


_TOOL_IMPLS[SEARCH_TOOL_NAME] = search_knowledge_base


def build_observation_text(tool_name: str, result: dict[str, Any]) -> str:
    """把工具结果整理成给模型看的文本（纯函数）。

    这里是「禁止编造」的第二道闸门：查不到时，观察结果里会直接写明
    应当回答什么，模型顺着说就不会编。
    """
    if not result.get("ok"):
        return (
            f"【工具执行失败】{result.get('message', '未知错误')}\n"
            "因为没能取到资料，请如实告诉用户「资料里没有提到」，或说明这次查询没有成功。"
            "绝对不能凭记忆或自己的知识编造答案。"
        )

    query = str(result.get("query") or "")
    if not result.get("hit"):
        return (
            f"【检索结果】在知识库中没有找到与「{query}」相关的任何资料"
            f"（已扫描 {result.get('scanned_chunks', 0)} 个片段）。\n"
            "接下来按这个顺序处理：\n"
            "1) 如果问句里用了口语化的说法，换一个更贴近文档用语的关键词再检索一次"
            "（例如把「叫什么名字」换成「名称」，把「几天」换成「天数」，把「贵不贵」换成「价格」）。\n"
            "2) 如果换了关键词仍然查不到，就如实回答：资料里没有提到。"
            "不要编造、不要推测、不要用你自己的知识补充，也不要假装查到了。"
        )

    lines = [
        f"【检索结果】查询「{query}」，在知识库中找到以下 {result.get('count', 0)} 条资料片段：",
        "",
    ]
    for i, item in enumerate(result.get("results") or [], 1):
        lines.append(f"[片段 {i}] 来源文件：{item.get('source', '未知')}")
        lines.append(str(item.get("text") or ""))
        lines.append("")
    lines.append(HONESTY_REMINDER)
    return "\n".join(lines)


def new_observation(decision: dict[str, Any], tool_call: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """把「一次工具调用」打包成观察结果，供下一轮 decide() 使用（纯函数）。"""
    return {
        "tool_name": tool_call["name"],
        "tool_call": tool_call["api_call"],
        "assistant_content": decision.get("thought") or "",
        "text": build_observation_text(tool_call["name"], result),
    }


def make_guard_call(query: str, top_k: Any = None) -> dict[str, Any]:
    """构造一次「系统自动补查」的工具调用记录（纯函数）。

    用来兜住这种情况：模型一次工具都没调，就直接回答「资料里没有提到」。
    「查不到」这个结论必须建立在真的检索过的基础上，所以由系统替它补一次，
    再把检索结果塞回对话让它重新作答。

    返回结构与 parse_tool_calls() 的输出保持一致，可以直接交给 new_observation()。
    """
    args: dict[str, Any] = {"query": str(query or "")[:200]}
    if top_k is not None:
        args["top_k"] = top_k
    call_id = f"call_guard_{int(time.time() * 1000)}"
    return {
        "id": call_id,
        "name": SEARCH_TOOL_NAME,
        "arguments": args,
        "parse_error": "",
        "api_call": {
            "id": call_id,
            "type": "function",
            "function": {"name": SEARCH_TOOL_NAME, "arguments": json.dumps(args, ensure_ascii=False)},
        },
    }


# --------------------------------------------------------------------------- #
# 5. decide() —— 让模型自己决定「调工具」还是「直接回答」
# --------------------------------------------------------------------------- #

def decide(
    question: str,
    history: Iterable[dict] | None = None,
    *,
    observations: Sequence[dict[str, Any]] | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    temperature: float = AGENT_TEMPERATURE,
    max_tokens: int = AGENT_MAX_TOKENS,
    timeout: float = 60.0,
    system_prompt: str | None = None,
    url: str = DEEPSEEK_API_URL,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """【核心】做一次决策：要不要调用工具？还是直接给出回答？

    关键点：请求里带上 tools 和 tool_choice="auto"，把「调不调工具」这个
    决定权完全交给模型，我们不做任何预设。

    Args:
        question: 用户这一轮说的话。
        history: 历史对话。
        observations: 本轮之前已经执行过的工具调用（用于多轮 decide）。
        api_key: 显式指定密钥；不传走环境变量。
        opener: 可注入的网络函数，测试用。

    Returns:
        见 parse_decision()，并额外带上 model / elapsed_ms。

    Raises:
        ThinkError: 密钥缺失、网络异常或响应异常时抛出。
    """
    text = str(question or "").strip()
    if not text:
        raise ThinkError("没有收到有效的提问内容。")

    prompt = system_prompt if system_prompt is not None else build_agent_system_prompt(knowledge_hint())
    messages = build_agent_messages(text, history, observations, system_prompt=prompt)
    payload = build_decide_payload(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        tools=build_tool_schemas(),
        tool_choice="auto",
    )

    started = time.perf_counter()
    key = api_key or get_api_key()
    data = http_post_json(url, payload, build_headers(key), timeout=timeout, opener=opener)
    decision = parse_decision(data)
    decision["model"] = model
    decision["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return decision


# --------------------------------------------------------------------------- #
# 6. agent_answer() —— 完整一轮：decide → 调工具 → decide → … → 回答
# --------------------------------------------------------------------------- #

def summarize_tool_result(tool_call: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """把工具结果压成给前端「决策过程面板」用的摘要（纯函数，不含长正文）。"""
    previews = [
        {
            "source": item.get("source", ""),
            "score": item.get("score", 0),
            "coverage": item.get("coverage", 0),
            "text": str(item.get("text") or "")[:120],
        }
        for item in (result.get("results") or [])
    ]
    return {
        "name": tool_call.get("name", ""),
        "arguments": tool_call.get("arguments") or {},
        "ok": bool(result.get("ok")),
        "hit": bool(result.get("hit")),
        "count": int(result.get("count") or 0),
        "sources": sorted({p["source"] for p in previews if p["source"]}),
        "message": str(result.get("message") or ""),
        "preview": previews,
        "scanned_chunks": int(result.get("scanned_chunks") or 0),
        "elapsed_ms": int(result.get("elapsed_ms") or 0),
        "parse_error": str(tool_call.get("parse_error") or ""),
    }


def force_final_answer(
    question: str,
    history: Iterable[dict] | None,
    observations: Sequence[dict[str, Any]],
    **kwargs: Any,
) -> str:
    """已达最大步数仍未给出回答时，去掉工具、强制模型基于已有资料收口（纯函数调用）。

    这一步很重要：如果模型的决策循环迟迟不收口，我们不能让它无限调工具，
    但也不能直接把空回答丢给用户 —— 用一次「禁止调用工具」的请求兜底。
    """
    closing_prompt = (
        build_agent_system_prompt(knowledge_hint())
        + "\n\n【本轮特别要求】工具调用次数已达上限，你现在必须直接给出最终回答，"
        "不允许再调用工具。只能依据上文已经返回的资料作答；"
        "如果上文没有可用资料，就回答「资料里没有提到」。"
    )
    messages = build_agent_messages(question, history, observations, system_prompt=closing_prompt)
    payload = build_request_payload(
        messages,
        model=kwargs.get("model", DEFAULT_MODEL),
        temperature=kwargs.get("temperature", AGENT_TEMPERATURE),
        max_tokens=kwargs.get("max_tokens", AGENT_MAX_TOKENS),
    )
    key = kwargs.get("api_key") or get_api_key()
    data = http_post_json(
        kwargs.get("url", DEEPSEEK_API_URL),
        payload,
        build_headers(key),
        timeout=kwargs.get("timeout", 60.0),
        opener=kwargs.get("opener"),
    )
    return str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()


def agent_answer(
    question: str,
    *,
    session_id: str = DEFAULT_SESSION_ID,
    max_steps: int = AGENT_MAX_STEPS,
    history: Iterable[dict] | None = None,
    max_turns: int = MAX_MEMORY_TURNS,
    **kwargs: Any,
) -> dict[str, Any]:
    """【对外主函数】跑完一整轮 Agent 对话。

    流程：取会话记忆 → 循环决策（必要时执行工具）→ 拿到最终回答 → 写回记忆。

    Args:
        question: 用户说的话。
        session_id: 会话 ID，用来隔离记忆。
        max_steps: 最多几轮决策（防止模型一直在调工具）。
        history: 显式指定历史（不传就用服务端记忆）。
        max_turns: 记忆保留轮数。
        kwargs: 透传给 decide()，例如 api_key / model / opener。

    Returns:
        {
          "reply": str,                    # 最终回答
          "trace": [ {...}, ... ],         # ★ 决策过程，前端用它展示「AI 调没调用工具」
          "used_tools": bool,              # 这一轮到底有没有调用工具
          "tools_used": [工具名, ...],
          "search_hit": bool | None,       # 检索是否命中（没调工具时为 None）
          "steps": int,                    # 实际进行了几轮决策
          "turns": int,                    # 记完后会话共有多少轮
          "session_id": str,
          "model": str,
          "usage": {...},
          "elapsed_ms": int,
          "forced_final": bool,            # 是否走了「强制收口」兜底
        }
    """
    text = str(question or "").strip()
    if not text:
        raise ThinkError("没有收到有效的提问内容。")

    started = time.perf_counter()
    history = list(history) if history is not None else get_history(session_id)

    trace: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    tools_used: list[str] = []
    search_hit: bool | None = None
    usage_total: dict[str, int] = {}
    reply = ""
    forced_final = False
    steps_done = 0

    for step in range(1, max(1, int(max_steps)) + 1):
        steps_done = step
        decision = decide(text, history, observations=observations, **kwargs)
        for key, value in (decision.get("usage") or {}).items():
            usage_total[key] = usage_total.get(key, 0) + int(value)

        # --- 记录这一轮的决策 ---
        trace.append(
            {
                "type": "decide",
                "step": step,
                "action": decision["action"],
                "thought": decision.get("thought") or "",
                "elapsed_ms": decision.get("elapsed_ms", 0),
                "model": decision.get("model", DEFAULT_MODEL),
                "tool_names": [c["name"] for c in decision.get("tool_calls") or []],
                "finish_reason": decision.get("finish_reason", ""),
            }
        )

        # --- 模型决定直接回答 ---
        if decision["action"] == "final":
            reply = decision["reply"]

            # 一致性校验：一次工具都没查过，却直接宣称「查不到」？
            # 「查不到」这个结论必须建立在真的检索过的基础上 —— 否则替它补查一次，
            # 再把检索结果塞回去让它重新判断。（这是对「禁止编造/禁止瞎说没有」的兜底）
            if (
                step < max(1, int(max_steps))
                and not observations
                and not tools_used
                and claims_not_found(reply)
                and knowledge_base.knowledge_stats()["documents"] > 0
            ):
                guard_call = make_guard_call(text)
                guard_result = run_tool(SEARCH_TOOL_NAME, guard_call["arguments"])
                observations.append(
                    new_observation(
                        {"thought": "（系统校验：本轮尚未查阅任何资料，先检索一次再下结论）"},
                        guard_call,
                        guard_result,
                    )
                )
                tools_used.append(SEARCH_TOOL_NAME)
                if guard_result.get("hit"):
                    search_hit = True
                trace.append(
                    {
                        "type": "guard",
                        "step": step,
                        "reason": "模型在没查阅资料的情况下断言「查不到」，系统已自动补查一次并要求重新作答",
                        **summarize_tool_result(guard_call, guard_result),
                    }
                )
                reply = ""
                continue        # 带着补查到的资料回到决策环节

            break

        # --- 模型决定调用工具：逐个执行，把结果塞回对话 ---
        for call in decision["tool_calls"]:
            result = run_tool(call["name"], call["arguments"])
            observations.append(new_observation(decision, call, result))

            if call["name"] not in tools_used:
                tools_used.append(call["name"])
            if call["name"] == SEARCH_TOOL_NAME:
                # 只要有一轮命中过，就算命中过（多轮检索取并集）
                search_hit = bool(result.get("hit")) or bool(search_hit)

            trace.append({"type": "tool", "step": step, **summarize_tool_result(call, result)})

    # --- 步数用尽仍没收口：强制收口兜底 ---
    if not reply:
        forced_final = True
        reply = force_final_answer(text, history, observations, **kwargs)
        if not reply:
            reply = "资料里没有提到。"
        trace.append(
            {
                "type": "decide",
                "step": steps_done + 1,
                "action": "final",
                "thought": "已达最大工具调用轮数，强制收口作答。",
                "elapsed_ms": 0,
                "model": kwargs.get("model", DEFAULT_MODEL),
                "tool_names": [],
                "finish_reason": "forced",
            }
        )

    messages = remember(text, reply, session_id=session_id, max_turns=max_turns)

    return {
        "reply": reply,
        "trace": trace,
        "used_tools": bool(tools_used),
        "tools_used": tools_used,
        "search_hit": search_hit,
        "steps": steps_done,
        "turns": count_turns(messages),
        "session_id": session_id,
        "model": kwargs.get("model", DEFAULT_MODEL),
        "usage": usage_total,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "forced_final": forced_final,
    }


# --------------------------------------------------------------------------- #
# 7. 自测入口：python agent_core.py
# --------------------------------------------------------------------------- #
class _FakeResponse:
    """模拟 urllib 的响应对象，支持 with 语法与 read()。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _make_opener(responses: list[dict[str, Any]], calls: list[dict[str, Any]] | None = None) -> Callable[..., Any]:
    """造一个按顺序返回预设响应的假 opener，并记录每次请求体（测试用）。"""
    queue = list(responses)

    def opener(request: Any, timeout: float | None = None) -> _FakeResponse:
        if calls is not None:
            calls.append(json.loads(request.data.decode("utf-8")))
        if not queue:
            raise AssertionError("假 opener 的预设响应已经用完了")
        return _FakeResponse(json.dumps(queue.pop(0), ensure_ascii=False).encode("utf-8"))

    return opener


def _tool_call_response(name: str, arguments: dict[str, Any], call_id: str = "call_1", thought: str = "") -> dict[str, Any]:
    """造一个「模型决定调用工具」的响应。"""
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": thought,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


def _final_response(text: str) -> dict[str, Any]:
    """造一个「模型直接回答」的响应。"""
    return {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }


def _self_test() -> None:
    """离线自测：全程用假 opener，不联网、不需要密钥。"""
    print("=" * 62)
    print("agent_core 离线自测")
    print("=" * 62)

    # 1) 工具箱：必须有 search_knowledge_base，且描述说明非空
    toolbox = describe_toolbox()
    names = [t["name"] for t in toolbox]
    assert SEARCH_TOOL_NAME in names, "工具箱里必须有 search_knowledge_base"
    entry = next(t for t in toolbox if t["name"] == SEARCH_TOOL_NAME)
    assert len(entry["description"]) > 100, "工具描述说明必须足够详细"
    assert "资料里没有提到" in entry["description"]
    assert any(p["name"] == "query" and p["required"] for p in entry["parameters"])
    print(f"[1] 工具箱 OK（{len(toolbox)} 个工具：{'、'.join(names)}）")

    # 2) tool schema 转 OpenAI 格式正确
    schemas = build_tool_schemas()
    assert schemas[0]["type"] == "function"
    assert schemas[0]["function"]["name"] == SEARCH_TOOL_NAME
    assert schemas[0]["function"]["parameters"]["required"] == ["query"]
    assert set(schemas[0]["function"]["parameters"]["properties"]) == {"query", "top_k"}
    print("[2] tool schema 格式 OK")

    # 3) 请求体：必须带上 tools 且 tool_choice=auto（= 让 AI 自己决定）
    payload = build_decide_payload([{"role": "user", "content": "hi"}], tools=schemas, tool_choice="auto")
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0]["function"]["name"] == SEARCH_TOOL_NAME
    assert "tools" not in build_decide_payload([{"role": "user", "content": "hi"}])
    print("[3] 请求体 tools / tool_choice=auto OK")

    # 4) 解析决策：调工具 / 直接回答 / 坏 JSON / 空响应
    d = parse_decision(_tool_call_response(SEARCH_TOOL_NAME, {"query": "年假"}))
    assert d["action"] == "tool" and d["tool_calls"][0]["name"] == SEARCH_TOOL_NAME
    assert d["tool_calls"][0]["arguments"] == {"query": "年假"}

    d = parse_decision(_final_response("你好呀"))
    assert d["action"] == "final" and d["reply"] == "你好呀" and d["tool_calls"] == []

    bad = {"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
        {"id": "c1", "function": {"name": SEARCH_TOOL_NAME, "arguments": "{不是JSON"}}]}}]}
    d = parse_decision(bad)
    assert d["action"] == "tool" and d["tool_calls"][0]["parse_error"], "坏 JSON 应当被标记而不是崩溃"

    for broken in ({}, {"choices": []}, {"choices": [{"message": {"content": ""}}]}):
        try:
            parse_decision(broken)
        except ThinkError:
            pass
        else:
            raise AssertionError(f"{broken} 应当抛 ThinkError")
    print("[4] parse_decision 四种情况 OK（含坏 JSON 与空响应）")

    # 5) 工具分发：未知工具 / 缺参数 / 正常调用
    assert run_tool("不存在的工具", {})["ok"] is False
    assert run_tool(SEARCH_TOOL_NAME, {})["ok"] is False           # 缺 query
    assert run_tool(SEARCH_TOOL_NAME, {"query": ""})["ok"] is False
    # 正常路径：把依赖的检索函数换成假实现，验证参数确实被透传
    original = _TOOL_IMPLS[SEARCH_TOOL_NAME]
    captured: dict[str, Any] = {}

    def fake_search(query: str, top_k: Any = None) -> dict[str, Any]:
        captured.update({"query": query, "top_k": top_k})
        return {"ok": True, "hit": True, "count": 1, "results": [{"source": "a.md", "text": "x"}], "message": "ok"}

    _TOOL_IMPLS[SEARCH_TOOL_NAME] = fake_search
    try:
        out = run_tool(SEARCH_TOOL_NAME, {"query": "年假", "top_k": 2})
        assert out["hit"] is True and captured == {"query": "年假", "top_k": 2}
        # 模型幻觉出参数名时，TypeError 必须被兜成 ok=False，而不是让整轮对话崩掉
        assert run_tool(SEARCH_TOOL_NAME, {"wrong_arg_name": "x"})["ok"] is False
    finally:
        _TOOL_IMPLS[SEARCH_TOOL_NAME] = original
    print("[5] run_tool 分发与容错 OK")

    # 6) 观察结果文本：查不到时必须明确说「资料里没有提到」
    miss = build_observation_text(SEARCH_TOOL_NAME, {"ok": True, "hit": False, "query": "量子纠缠", "scanned_chunks": 7, "count": 0, "results": []})
    assert "资料里没有提到" in miss and "不要编造" in miss and "量子纠缠" in miss

    hit_text = build_observation_text(SEARCH_TOOL_NAME, {
        "ok": True, "hit": True, "query": "年假", "count": 1,
        "results": [{"source": "员工手册.md", "text": "满一年 5 天"}],
    })
    assert "员工手册.md" in hit_text and "满一年 5 天" in hit_text and HONESTY_REMINDER in hit_text
    assert "资料里没有提到" in build_observation_text(SEARCH_TOOL_NAME, {"ok": False, "message": "网络炸了"})
    print("[6] 观察结果文本 OK（命中/未命中/执行失败三种措辞）")

    # 7) 端到端（假 opener）：模型先查资料，再据资料回答
    demo_doc = "员工手册.md"
    knowledge_base.index_document(demo_doc, "公司年假制度：入职满一年可享受 5 天带薪年假，满三年 10 天。")
    try:
        opener = _make_opener([
            _tool_call_response(SEARCH_TOOL_NAME, {"query": "年假"}, thought="需要查员工手册"),
            _final_response("入职满一年有 5 天带薪年假，满三年 10 天（来源：员工手册.md）。"),
        ])
        out = agent_answer("年假有几天", session_id="self-test-1", api_key="sk-test", opener=opener)

        assert out["used_tools"] is True and out["tools_used"] == [SEARCH_TOOL_NAME]
        assert out["search_hit"] is True
        assert "5 天" in out["reply"]
        assert [t["type"] for t in out["trace"]] == ["decide", "tool", "decide"]
        assert out["trace"][0]["action"] == "tool"
        assert out["trace"][1]["hit"] is True and out["trace"][1]["count"] >= 1
        assert out["trace"][2]["action"] == "final"
        assert out["usage"]["total_tokens"] == 270      # 两轮用量累加
        print("[7] 端到端「先查资料再回答」OK")
        print("     trace = " + " -> ".join(
            f"{t['type']}({t.get('action') or t.get('name')})" for t in out["trace"]))

        # 8) 端到端：问知识库里没有的东西 -> 必须诚实说「资料里没有提到」
        opener = _make_opener([
            _tool_call_response(SEARCH_TOOL_NAME, {"query": "黑洞视界半径"}),
            _final_response("资料里没有提到。"),
        ])
        out2 = agent_answer("黑洞的视界半径是多少", session_id="self-test-2", api_key="sk-test", opener=opener)
        assert out2["used_tools"] is True
        assert out2["search_hit"] is False, "知识库里没有黑洞资料，不该命中"
        assert "资料里没有提到" in out2["reply"]
        print("[8] 端到端「查不到就诚实说」OK")

        # 9) 端到端：闲聊时模型可以不调工具（决策权在它手上）
        calls: list[dict[str, Any]] = []
        opener = _make_opener([_final_response("你好！有什么可以帮你的？")], calls)
        out3 = agent_answer("你好呀", session_id="self-test-3", api_key="sk-test", opener=opener)
        assert out3["used_tools"] is False and out3["search_hit"] is None
        assert len(calls) == 1 and calls[0]["tool_choice"] == "auto"
        assert out3["trace"] == [out3["trace"][0]] and out3["trace"][0]["action"] == "final"
        print("[9] 端到端「闲聊不调工具」OK（决策权确实在模型手上）")

        # 10) 兜底：模型一直调工具不收口 -> 强制收口
        opener = _make_opener([
            _tool_call_response(SEARCH_TOOL_NAME, {"query": "年假"}),
            _tool_call_response(SEARCH_TOOL_NAME, {"query": "年假 天数"}),
            _tool_call_response(SEARCH_TOOL_NAME, {"query": "带薪年假"}),
            _final_response("满一年 5 天。"),
        ])
        out4 = agent_answer("年假几天", session_id="self-test-4", api_key="sk-test", opener=opener, max_steps=3)
        assert out4["forced_final"] is True, "步数用尽应当走强制收口"
        assert out4["reply"] == "满一年 5 天。"
        assert len([t for t in out4["trace"] if t["type"] == "tool"]) == 3
        print("[10] 步数上限与强制收口 OK")

        # 11) 一致性校验：一次都没查，却直接断言「查不到」-> 系统自动补查后重新作答
        #     （这是真实联调时暴露的问题：模型会说「我们几点下班」资料里没有提到，
        #       而考勤资料里明明写着 18:00 下班。纯离线用例当初没覆盖到。）
        guard_doc = "_selftest_考勤.md"
        knowledge_base.index_document(guard_doc, "考勤制度：每天上午 9:00 上班，下午 18:00 下班，午休一个半小时。")
        try:
            assert claims_not_found("资料里没有提到。") is True
            assert claims_not_found("这个我不知道") is True
            assert claims_not_found("没有找到相关内容") is True
            assert claims_not_found("入职满一年有 5 天年假。") is False
            assert claims_not_found("") is False

            guard_call = make_guard_call("几点下班")
            assert guard_call["name"] == SEARCH_TOOL_NAME
            assert guard_call["arguments"]["query"] == "几点下班"
            assert json.loads(guard_call["api_call"]["function"]["arguments"])["query"] == "几点下班"

            calls: list[dict[str, Any]] = []
            opener = _make_opener(
                [
                    _final_response("资料里没有提到。"),                              # ① 没查就下结论
                    _final_response("下午 18:00 下班（来源：考勤制度.md）。"),        # ② 补查后重新作答
                ],
                calls,
            )
            out5 = agent_answer("几点下班", session_id="self-test-5", api_key="sk-test", opener=opener)
            assert out5["used_tools"] is True, "应当触发系统补查"
            guards = [t for t in out5["trace"] if t["type"] == "guard"]
            assert len(guards) == 1, "trace 里应当恰好有一条 guard 记录"
            assert guards[0]["hit"] is True, "补查应当命中考勤资料"
            assert "18:00" in out5["reply"]
            assert any(m.get("role") == "tool" for m in calls[1]["messages"]), "第二轮必须把补查结果带给模型"
            print("[11] 一致性校验：未查即断言「查不到」-> 自动补查 OK")

            # 12) 已经查过了再说「查不到」-> 不再补查（避免多余检索，也避免死循环）
            opener = _make_opener([
                _tool_call_response(SEARCH_TOOL_NAME, {"query": "几点下班"}),
                _final_response("资料里没有提到。"),
            ])
            out6 = agent_answer("几点下班", session_id="self-test-6", api_key="sk-test", opener=opener)
            assert out6["used_tools"] is True
            assert not any(t["type"] == "guard" for t in out6["trace"]), "已经查过就不该再补查"
            assert out6["trace"][-1]["action"] == "final"
            print("[12] 已查过则不再补查 OK")
        finally:
            knowledge_base.remove_document(guard_doc)
    finally:
        knowledge_base.remove_document(demo_doc)
        for sid in ("self-test-1", "self-test-2", "self-test-3", "self-test-4", "self-test-5", "self-test-6"):
            reset_history(sid)

    print("-" * 62)
    print("全部自测通过 ✔")


if __name__ == "__main__":
    _self_test()
