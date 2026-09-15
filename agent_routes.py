# -*- coding: utf-8 -*-
"""
agent_routes.py —— Agent 化的 HTTP 接口（以 Blueprint 形式提供）

为什么用 Blueprint
------------------
需求要求「Flask 部分保持原有」。为了不碰 app.py 里已经验证过的 6 个路由，
这里把 Agent 相关的接口全部收在一个独立的 Blueprint 里，由 app.py 在文件
末尾注册一次即可。本模块**不 import app**，所以不存在循环依赖问题。

提供的接口
----------
    GET  /api/agent/toolbox            工具箱清单（含每个工具的描述说明）
    POST /api/agent/think              ★ Agent 版对话：返回回答 + 决策过程 trace
    POST /api/agent/upload             上传 .md / .txt 到知识库（multipart 或 JSON）
    GET  /api/agent/documents          知识库文档列表
    POST /api/agent/documents/delete   删除某个文档
    POST /api/agent/kb/clear           清空知识库
    GET  /api/agent/search             手动检索知识库（调试/演示用）
"""

from __future__ import annotations

import time

from flask import Blueprint, jsonify, request

import agent_core
import knowledge_base
import voice_core

agent_bp = Blueprint("agent", __name__)


# --------------------------------------------------------------------------- #
# 公共小工具（与 app.py 里同名函数保持一致的语义，但不共享代码以避免耦合）
# --------------------------------------------------------------------------- #
def read_session_id(source: dict | None = None) -> str:
    """从请求体 / 查询串里取会话 ID，取不到就用默认会话。"""
    raw = None
    if isinstance(source, dict):
        raw = source.get("session_id")
    if raw is None:
        raw = request.args.get("session_id")
    return str(raw or "").strip() or voice_core.DEFAULT_SESSION_ID


def clamp_int(value: object, default: int, low: int, high: int) -> int:
    """把可能来自前端的任意值安全地转成夹在区间内的整数。"""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = default
    return min(max(number, low), high)


def error(message: str, status: int, **extra: object):
    """统一格式的错误响应。"""
    return jsonify({"ok": False, "error": message, **extra}), status


# --------------------------------------------------------------------------- #
# 工具箱
# --------------------------------------------------------------------------- #
@agent_bp.get("/api/agent/toolbox")
def api_agent_toolbox():
    """返回 AI 手上的工具箱，以及「是否由模型自主决策」的标记。

    Returns:
        {"ok": true, "auto_decision": true, "tools": [...], "knowledge": {...}}
    """
    return jsonify(
        {
            "ok": True,
            "auto_decision": True,          # tool_choice="auto"，调不调由模型决定
            "mode": "function_calling",
            "max_steps": agent_core.AGENT_MAX_STEPS,
            "tools": agent_core.describe_toolbox(),
            "knowledge": knowledge_base.knowledge_stats(),
        }
    )


# --------------------------------------------------------------------------- #
# ★ Agent 对话：decide() 决策 + 工具调用 + 最终回答
# --------------------------------------------------------------------------- #
@agent_bp.post("/api/agent/think")
def api_agent_think():
    """Agent 版对话接口。

    请求体 JSON：
        {"text": "用户说的话", "session_id": "可选", "max_steps": 3,
         "history": "可选，显式指定历史则忽略服务端记忆"}

    成功响应：除最终回答外，还会带上完整的 trace（决策过程），
        前端据此展示「AI 有没有调用工具、调了什么、拿到什么」。

    失败响应（HTTP 400/502/500）：{"ok": false, "error": "中文原因"}
    """
    data = request.get_json(silent=True) or {}
    session_id = read_session_id(data)

    text = str(data.get("text", "")).strip()
    if not text:
        return error("没有收到有效的提问内容。", 400)
    if len(text) > 4000:
        return error("提问内容过长，请精简后再试。", 400)

    history = data.get("history")
    if history is not None and not isinstance(history, list):
        return error("history 必须是数组。", 400)

    max_steps = clamp_int(data.get("max_steps"), agent_core.AGENT_MAX_STEPS, 1, 6)

    kwargs: dict[str, object] = {"max_steps": max_steps}
    if history:
        kwargs["history"] = history

    started = time.perf_counter()
    try:
        result = agent_core.agent_answer(text, session_id=session_id, **kwargs)
    except voice_core.ThinkError as exc:
        status = 400 if not voice_core.has_api_key() else 502
        return error(str(exc), status)
    except Exception as exc:  # noqa: BLE001 —— 兜底，不把堆栈暴露给前端
        return error(f"服务内部错误：{exc}", 500)

    tool_count = sum(1 for item in result["trace"] if item["type"] == "tool")
    return jsonify(
        {
            "ok": True,
            "session_id": session_id,
            "elapsed_ms": result["elapsed_ms"],
            "wall_ms": int((time.perf_counter() - started) * 1000),
            "tool_calls": tool_count,
            "toolbox_size": len(agent_core.TOOLBOX),
            **result,
        }
    )


# --------------------------------------------------------------------------- #
# 知识库：上传 / 列表 / 删除 / 清空 / 检索
# --------------------------------------------------------------------------- #
def _upload_from_files() -> tuple[list[dict], list[dict]]:
    """处理 multipart 表单里的文件（支持一次传多个）。"""
    uploaded: list[dict] = []
    failed: list[dict] = []
    for storage in request.files.getlist("files") or request.files.getlist("file"):
        raw_name = storage.filename or ""
        if not raw_name:
            continue
        try:
            data = storage.read()
            uploaded.append(knowledge_base.save_upload(raw_name, data))
        except knowledge_base.KnowledgeError as exc:
            failed.append({"name": raw_name, "error": str(exc)})
        except OSError as exc:
            failed.append({"name": raw_name, "error": f"写入失败：{exc}"})
        finally:
            storage.close()
    return uploaded, failed


def _upload_from_json() -> tuple[list[dict], list[dict]]:
    """处理 JSON 形式的正文（{"filename": "...", "content": "..."}），方便脚本/测试调用。"""
    data = request.get_json(silent=True) or {}
    raw_name = str(data.get("filename") or data.get("name") or "").strip()
    content = data.get("content")
    if not raw_name or content is None:
        return [], []
    try:
        return [knowledge_base.save_upload(raw_name, content)], []
    except knowledge_base.KnowledgeError as exc:
        return [], [{"name": raw_name, "error": str(exc)}]


@agent_bp.post("/api/agent/upload")
def api_agent_upload():
    """把一个或多个 .md / .txt 文件加入知识库。

    两种调用方式：
        1. multipart/form-data，字段名 files（可重复）—— 前端拖拽上传用这个
        2. application/json，{"filename": "手册.md", "content": "...正文..."}

    Returns:
        {"ok": true, "uploaded": [...], "failed": [...], "stats": {...}}
        只要有文件成功入库就返回 200；全部失败返回 400。
    """
    uploaded, failed = _upload_from_files()
    if not uploaded and not failed:
        uploaded, failed = _upload_from_json()

    if not uploaded and not failed:
        return error("没有收到任何文件。请上传 .md 或 .txt 文件。", 400)
    if not uploaded:
        return jsonify({"ok": False, "error": failed[0]["error"], "uploaded": [], "failed": failed}), 400

    return jsonify(
        {
            "ok": True,
            "uploaded": uploaded,
            "failed": failed,
            "stats": knowledge_base.knowledge_stats(),
        }
    )


@agent_bp.get("/api/agent/documents")
def api_agent_documents():
    """列出知识库里的全部文档。

    Returns:
        {"ok": true, "documents": [...], "stats": {...}}
    """
    return jsonify(
        {
            "ok": True,
            "documents": knowledge_base.list_documents(),
            "stats": knowledge_base.knowledge_stats(),
        }
    )


@agent_bp.post("/api/agent/documents/delete")
def api_agent_delete_document():
    """删除知识库里的一个文档。请求体：{"name": "手册.md"}"""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or data.get("filename") or "").strip()
    if not name:
        return error("没有指定要删除的文件名。", 400)

    removed = knowledge_base.remove_document(name)
    if not removed:
        return error(f"知识库里没有「{name}」这个文件。", 404)
    return jsonify({"ok": True, "removed": name, "stats": knowledge_base.knowledge_stats()})


@agent_bp.post("/api/agent/kb/clear")
def api_agent_clear():
    """清空整个知识库。"""
    removed = knowledge_base.clear_knowledge()
    return jsonify({"ok": True, "removed": removed, "stats": knowledge_base.knowledge_stats()})


@agent_bp.get("/api/agent/search")
def api_agent_search():
    """手动检索知识库（不开对话，直接看命中了什么），便于调试与演示。

    查询串：?q=年假&top_k=3
    """
    query = str(request.args.get("q") or request.args.get("query") or "").strip()
    if not query:
        return error("缺少检索词 q。", 400)
    top_k = clamp_int(request.args.get("top_k"), knowledge_base.KB_DEFAULT_TOP_K, 1, knowledge_base.KB_MAX_TOP_K)
    result = knowledge_base.search_knowledge_base(query, top_k=top_k)
    return jsonify({"ok": True, **result})


# --------------------------------------------------------------------------- #
# 供 app.py 使用的注册函数
# --------------------------------------------------------------------------- #
def register(app) -> None:
    """把本模块的 Blueprint 注册到传入的 Flask 应用上。

    app.py 只需要在末尾调用：agent_routes.register(app)
    """
    if "agent" not in app.blueprints:
        app.register_blueprint(agent_bp)
