# -*- coding: utf-8 -*-
"""
knowledge_base.py —— 本地知识库（上传 / 分块 / 检索）

职责
----
把用户上传的 .md / .txt 文件变成「可被检索的资料片段」，并提供一个
search_knowledge_base() 做相关性检索。它是 Agent 工具箱里唯一落地实现的工具。

设计要点
--------
1. **零外部依赖**：中文切词不用 jieba，而是「二元组(bigram) + 英文/数字词」。
   对问答式短查询的召回效果足够，而且完全可解释、可断言。
2. **纯函数优先**：切词、分块、打分都是纯函数，只有 index_document() /
   remove_document() / clear_knowledge() 三个函数会碰磁盘。
3. **索引常驻内存**：上传后立刻重建索引，检索时不再读盘。
4. **「查不到」是正常结果而不是异常**：返回 hit=False，让上层能诚实地
   告诉用户「资料里没有提到」，从机制上堵住编造答案的路。
5. **文件名也是一条检索线索**：用户经常按文档名提问（「产品说明里写了啥」），
   而正文未必出现「产品」二字。分块时把文件名并入片段词表，按名字提问即可命中。

命令行自测
----------
    python knowledge_base.py       # 跑内置离线自测，不联网
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# 1. 配置常量
# --------------------------------------------------------------------------- #

#: 知识库落盘目录（与代码同级的 knowledge_base/）
KB_DIR = Path(__file__).resolve().parent / "knowledge_base"

#: 允许上传的扩展名 —— 需求指定只收 .md 和 .txt
KB_ALLOWED_SUFFIXES: tuple[str, ...] = (".md", ".markdown", ".txt", ".text")

#: 单文件 / 整库体积上限，防止一次上传把内存打满
KB_MAX_FILE_BYTES = 2 * 1024 * 1024        # 2 MB
KB_MAX_TOTAL_BYTES = 20 * 1024 * 1024      # 20 MB
KB_MAX_DOCUMENTS = 200

#: 分块参数：每块目标长度，以及相邻块之间的重叠字数（避免答案被切断）
KB_CHUNK_CHARS = 400
KB_CHUNK_OVERLAP = 80

#: 检索参数
KB_DEFAULT_TOP_K = 4
KB_MAX_TOP_K = 8
#: 关键词命中比例门槛。取值偏低是刻意的：漏召回（该命中却没命中）会直接
#: 导致 AI 误答「资料里没有提到」，比多召回几条无关片段严重得多。
KB_MIN_COVERAGE = 0.4
KB_KEEP_TOP_RATIO = 0.3     # 只保留分数不低于最高分 30% 的片段

#: 单字兜底召回的门槛。只在关键词检索「一条都没命中」时启用，用于兜住
#: 「口语提问用词与文档用词对不上」的情况（问「叫什么名字」，文档写「名叫」）。
#: 门槛不宜过低，否则会把只沾了一个常见字的无关片段也拉进来。
KB_FALLBACK_COVERAGE = 0.35

#: 返回给模型的单条片段最长字符数（避免一次把上下文撑爆）
KB_SNIPPET_CHARS = 500


class KnowledgeError(ValueError):
    """上传/删除知识库文件时的可预期错误（文件名非法、体积超限等）。"""


# --------------------------------------------------------------------------- #
# 2. 文本清洗（纯函数）
# --------------------------------------------------------------------------- #

#: 中文里几乎不携带检索信息的虚词，作为单字/二元组形式的停用词过滤掉
KB_STOPWORDS = frozenset(
    """
    的 了 和 与 及 或 是 在 有 为 对 把 被 从 到 上 下 中 内 外 之 其 该 这 那 这些 那些
    什么 怎么 怎样 如何 为何 为什么 哪些 哪个 多少 是否 能否 可否 可以 需要 应该 请问
    一个 一些 我们 你们 他们 它们 自己 以及 并且 但是 不过 因为 所以 如果 那么 就是
    请问 一下 帮我 告诉 知道 介绍 说明 解释 关于 方面 情况 时候 目前 现在 还是 或者
    么是 是什 请 你 我 他 她 它 们 吗 呢 吧 啊 呀 哦 嗯 之 于 并 而 则 就 都 也 很 更 最
    什 么 怎 样 哪 谁 几 个 些 来 去 好
    """.split()
)

#: 连续汉字 / 英文单词 / 数字 的切分正则
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[a-z][a-z0-9_.\-]{0,30}|\d+(?:\.\d+)?")

#: 连续空白、markdown 语法符号
_WS_RE = re.compile(r"[ \t\u3000]+")
_MD_NOISE_RE = re.compile(r"(`{1,3}|^\s{0,3}#{1,6}\s*|\*\*|__|\*|~~|^\s{0,3}[-*+]\s+|^\s{0,3}>\s*|!?\[([^\]]*)\]\([^)]*\))", re.MULTILINE)


def normalize_text(raw: str) -> str:
    """统一换行、扔掉控制字符，让后续切词/分块稳定（纯函数）。"""
    text = unicodedata.normalize("NFKC", str(raw or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 去掉不可见控制字符（保留换行和制表符）
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or not unicodedata.category(ch).startswith("C"))
    # 三个以上连续换行压成两个
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def strip_markdown(text: str) -> str:
    """把 markdown 语法符号去掉，只留下可检索的正文（纯函数）。

    注意这里**保留链接文字**（`[标题](url)` -> `标题`），因为标题往往才是关键词。
    代码块围栏会被去掉，但块里的内容保留 —— 文档里的配置示例通常也需要能被搜到。
    """
    return _MD_NOISE_RE.sub(lambda m: m.group(2) or "", text)


def clean_for_search(text: str) -> str:
    """正文清洗：去 markdown 符号 + 压缩空白，供切词使用（纯函数）。"""
    return _WS_RE.sub(" ", strip_markdown(normalize_text(text))).strip()


def decode_bytes(data: bytes) -> str:
    """把上传的字节流解码成文本（纯函数）。

    依次尝试 UTF-8(SIG) -> GB18030 -> Latin-1 兜底，覆盖 Windows 上
    记事本存成 ANSI/GBK 的常见情况。
    """
    if isinstance(data, str):
        return data
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    # 最后兜底：替换掉非法字节，保证不抛异常
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# 3. 文件名校验与磁盘读写
# --------------------------------------------------------------------------- #

_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def normalize_filename(raw: str) -> str:
    """把用户给的文件名整理成安全的磁盘文件名（纯函数）。

    会挡掉目录穿越（`../`）、路径分隔符、Windows 保留字符等。

    Raises:
        KnowledgeError: 文件名为空或扩展名不在白名单内。
    """
    name = str(raw or "").strip().replace("\\", "/")
    # 只取最后一段，天然挡掉 ../../etc/passwd 这类路径穿越
    name = name.split("/")[-1]
    name = _UNSAFE_NAME_RE.sub("_", name).strip(" .")
    if not name:
        raise KnowledgeError("文件名无效，请重新选择文件。")

    suffix = Path(name).suffix.lower()
    if suffix not in KB_ALLOWED_SUFFIXES:
        allowed = "、".join(sorted({s.lstrip(".") for s in KB_ALLOWED_SUFFIXES}))
        raise KnowledgeError(f"只支持 {allowed} 格式的文件，收到的是「{suffix or '无扩展名'}」。")

    # 统一扩展名写法，并限制总长度
    stem = Path(name).stem[:80] or "document"
    canonical = {".markdown": ".md", ".text": ".txt"}.get(suffix, suffix)
    return f"{stem}{canonical}"


def ensure_kb_dir() -> Path:
    """确保知识库目录存在，返回该目录路径。"""
    KB_DIR.mkdir(parents=True, exist_ok=True)
    return KB_DIR


def read_document(path: Path) -> str:
    """读取知识库里的一个文档并解码成文本。"""
    return normalize_text(decode_bytes(path.read_bytes()))


def list_documents() -> list[dict[str, Any]]:
    """列出知识库里的所有文档（按文件名排序）。

    Returns:
        [{"name": str, "bytes": int, "chars": int, "chunks": int, "updated_at": str}, ...]
    """
    ensure_kb_dir()
    docs: list[dict[str, Any]] = []
    for path in sorted(KB_DIR.iterdir()):
        if not path.is_file() or path.suffix.lower() not in KB_ALLOWED_SUFFIXES:
            continue
        try:
            stat = path.stat()
            text = read_document(path)
        except OSError:
            continue
        docs.append(
            {
                "name": path.name,
                "bytes": stat.st_size,
                "chars": len(text),
                "chunks": len(chunk_text(text, path.name)),
                "updated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
            }
        )
    return docs


def total_bytes() -> int:
    """当前知识库占用的总字节数。"""
    return sum(int(d["bytes"]) for d in list_documents())


def save_upload(filename: str, data: bytes | str) -> dict[str, Any]:
    """把上传内容写入知识库目录，并返回该文档的信息。

    Args:
        filename: 原始文件名（会被 normalize_filename 清洗）。
        data: 文件内容，bytes 或 str 都可以。

    Returns:
        dict: list_documents() 里对应文档的那一项，外加 "overwritten" 标记。

    Raises:
        KnowledgeError: 文件名非法、内容为空或体积超限时抛出。
    """
    name = normalize_filename(filename)
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if not raw.strip():
        raise KnowledgeError("文件内容是空的，没有可入库的内容。")
    if len(raw) > KB_MAX_FILE_BYTES:
        limit_mb = KB_MAX_FILE_BYTES / 1024 / 1024
        raise KnowledgeError(f"单个文件不能超过 {limit_mb:.0f} MB，当前 {len(raw) / 1024 / 1024:.1f} MB。")

    ensure_kb_dir()
    path = KB_DIR / name
    overwritten = path.exists()

    # 体积总量保护：新文件才计入
    if not overwritten and total_bytes() + len(raw) > KB_MAX_TOTAL_BYTES:
        raise KnowledgeError(f"知识库总容量已达上限（{KB_MAX_TOTAL_BYTES / 1024 / 1024:.0f} MB），请先清理旧文件。")
    if not overwritten and len(list_documents()) >= KB_MAX_DOCUMENTS:
        raise KnowledgeError(f"知识库最多保存 {KB_MAX_DOCUMENTS} 个文件，请先清理旧文件。")

    path.write_bytes(raw)
    index_document(name, read_document(path))
    for doc in list_documents():
        if doc["name"] == name:
            return {**doc, "overwritten": overwritten}
    return {"name": name, "bytes": len(raw), "chars": 0, "chunks": 0, "updated_at": "", "overwritten": overwritten}


def remove_document(filename: str) -> bool:
    """从知识库删除一个文档（同时更新内存索引）。

    Returns:
        True 表示确实删掉了，False 表示文件本来就不存在。
    """
    try:
        name = normalize_filename(filename)
    except KnowledgeError:
        return False
    path = KB_DIR / name
    if not path.exists():
        return False
    path.unlink()
    _drop_from_index(name)
    return True


def clear_knowledge() -> int:
    """清空整个知识库。

    Returns:
        被删除的文件数量。
    """
    ensure_kb_dir()
    removed = 0
    for path in list(KB_DIR.iterdir()):
        if path.is_file() and path.suffix.lower() in KB_ALLOWED_SUFFIXES:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    reset_index()
    return removed


# --------------------------------------------------------------------------- #
# 4. 切词与分块（纯函数）
# --------------------------------------------------------------------------- #

def tokenize(text: str) -> list[str]:
    """把文本切成检索用 token（含重复，用于统计词频）。纯函数。

    策略（针对中文短查询优化）：
        * 连续汉字段 -> 相邻二元组（"量子纠缠" -> 量子/子纠/纠缠）
        * 单字汉字段 -> 该单字
        * 英文/数字 -> 整词（小写）
        * 过滤停用词与单字噪声

    这样做的好处：不需要结巴分词那样的外部依赖，且「关键词出现」这件事
    完全可解释 —— 检索命中与否能一条条断言出来。
    """
    cleaned = clean_for_search(text).lower()
    tokens: list[str] = []

    for seg in _CJK_RE.findall(cleaned):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))

    tokens.extend(_WORD_RE.findall(cleaned))

    return [t for t in tokens if t and t not in KB_STOPWORDS]


def split_paragraphs(text: str) -> list[str]:
    """按空行切成段落，并把 markdown 标题单独成段（纯函数）。"""
    norm = normalize_text(text)
    pieces: list[str] = []
    for block in re.split(r"\n\s*\n", norm):
        block = block.strip()
        if not block:
            continue
        # 标题行单独拆出来，这样「## 报销流程」能作为一个独立片段被检索到
        if re.match(r"^\s{0,3}#{1,6}\s", block):
            lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
            pieces.extend(lines)
        else:
            pieces.append(block)
    return pieces


def chunk_text(
    text: str,
    source: str,
    max_chars: int = KB_CHUNK_CHARS,
    overlap: int = KB_CHUNK_OVERLAP,
) -> list[dict[str, Any]]:
    """把一篇文档切成若干片段（纯函数）。

    做法：先按段落聚合到接近 max_chars，再单独处理超长段落；
    相邻片段之间保留 overlap 个字符的重叠，避免答案正好被切在边界上。

    Returns:
        [{"id": "文件::序号", "source": str, "index": int, "text": str}, ...]
    """
    paragraphs = split_paragraphs(text)
    blocks: list[str] = []
    buffer = ""

    for para in paragraphs:
        # 单段就超长 -> 硬切
        while len(para) > max_chars:
            head, para = para[:max_chars], para[max_chars - overlap:]
            blocks.append(head)
        if not buffer:
            buffer = para
        elif len(buffer) + len(para) + 1 <= max_chars:
            buffer = f"{buffer}\n{para}"
        else:
            blocks.append(buffer)
            tail = buffer[-overlap:] if overlap > 0 else ""
            buffer = f"{tail}\n{para}".strip() if tail else para

    if buffer.strip():
        blocks.append(buffer)

    chunks: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        content = block.strip()
        if not content:
            continue
        chunks.append({"id": f"{source}::{index}", "source": source, "index": index, "text": content})
    return chunks


# --------------------------------------------------------------------------- #
# 5. 索引与检索
# --------------------------------------------------------------------------- #
# 说明：索引常驻内存，结构如下
#   _INDEX = {
#       "chunks":  [{"id","source","index","text","title","tokens","tf","chars"}],
#       "df":      {token: 出现在多少个 chunk 里},
#       "sources": [文件名, ...],
#   }
# 每次上传/删除都会重建索引，检索时只做内存计算。
# --------------------------------------------------------------------------- #

_INDEX: dict[str, Any] = {"chunks": [], "df": {}, "sources": []}
_INDEX_LOCK = threading.RLock()

#: 索引是否已经从磁盘加载过。进程刚启动时为 False，第一次检索才会读盘。
_INDEX_LOADED = False


def _idf(df: int, total: int) -> float:
    """BM25 风格的 IDF：越罕见的词权重越高（纯函数）。

    用 +0.5 平滑，并夹一个 0.1 的下界，保证常见词也还有一点点正向权重。
    """
    if total <= 0:
        return 0.1
    value = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
    return max(0.1, value)


def _chunk_char_set(text: str) -> set[str]:
    """片段里所有「有检索意义的单个汉字」（纯函数），供单字兜底召回使用。"""
    return {ch for ch in str(text) if "\u4e00" <= ch <= "\u9fff" and ch not in KB_STOPWORDS}


#: 从文件名里剥出标题时的分隔符（下划线、连字符、点号、空白）
_TITLE_SPLIT_RE = re.compile(r"[_\-.·\s]+")


def title_of(source: str) -> str:
    """从文件名里取出「标题」用于检索（纯函数）。

    文件名本身就是一条重要的检索线索。例如正文写的是
    「本项目名叫『星尘』」，通篇没有「产品」二字，但文件名叫
    「产品说明.txt」——把标题并入片段词表后，用户问「产品叫什么名字」
    就能直接命中，而不必依赖单字兜底这条后路。
    """
    name = str(source or "").strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", name)     # 去掉扩展名
    return _TITLE_SPLIT_RE.sub(" ", name).strip()


def _analyze_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    """给一个片段补上 tokens / 词频表 / 汉字集合（纯函数，返回新字典）。

    文件名（标题）会被一并并入词表与汉字集合，这样「按文档名提问」也能命中；
    片段正文本身（chunk["text"]）保持原样，便于前端原样展示。
    """
    text = str(chunk.get("text") or "")
    tokens = tokenize(text)
    chars = _chunk_char_set(text)

    title = title_of(chunk.get("source", ""))
    title_tokens = tokenize(title) if title else []
    if title_tokens:
        tokens = tokens + title_tokens
        chars = chars | _chunk_char_set(title)

    tf: dict[str, int] = {}
    for token in tokens:
        tf[token] = tf.get(token, 0) + 1
    return {**chunk, "tokens": tokens, "tf": tf, "chars": chars, "title": title}


def build_index(chunks: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """由片段列表构建索引（纯函数，便于单测）。

    Returns:
        {"chunks": [...], "df": {...}, "sources": [...]}
    """
    analyzed = [_analyze_chunk(c) for c in chunks]
    df: dict[str, int] = {}
    for chunk in analyzed:
        for token in chunk["tf"]:
            df[token] = df.get(token, 0) + 1
    sources = sorted({c["source"] for c in analyzed})
    return {"chunks": analyzed, "df": df, "sources": sources}


def index_document(filename: str, text: str) -> dict[str, Any]:
    """把一篇文档加入内存索引（同名文档 = 覆盖重建）。"""
    name = str(filename or "").strip()
    get_index()                     # 确保磁盘上的老文档已经在索引里
    with _INDEX_LOCK:
        kept = [c for c in _INDEX["chunks"] if c.get("source") != name]
        kept.extend(chunk_text(text, name))
        _INDEX.clear()
        _INDEX.update(build_index(kept))
        return {"source": name, "chunks": len(_INDEX["chunks"])}


def _drop_from_index(filename: str) -> None:
    """把一篇文档从内存索引里移除。"""
    name = str(filename or "").strip()
    get_index()
    with _INDEX_LOCK:
        kept = [c for c in _INDEX["chunks"] if c.get("source") != name]
        _INDEX.clear()
        _INDEX.update(build_index(kept))


def rebuild_index(force: bool = False) -> dict[str, Any]:
    """从磁盘重建整个索引。

    Args:
        force: 为 False 且索引已加载过时直接复用，避免每次检索都读盘。
    """
    global _INDEX_LOADED
    with _INDEX_LOCK:
        if _INDEX_LOADED and not force:
            return knowledge_stats()
        chunks: list[dict[str, Any]] = []
        for doc in list_documents():
            try:
                text = read_document(KB_DIR / doc["name"])
            except OSError:
                continue
            chunks.extend(chunk_text(text, doc["name"]))
        _INDEX.clear()
        _INDEX.update(build_index(chunks))
        _INDEX_LOADED = True
        return knowledge_stats()


def reset_index() -> None:
    """清空内存索引（清空知识库时调用）。"""
    global _INDEX_LOADED
    with _INDEX_LOCK:
        _INDEX.clear()
        _INDEX.update({"chunks": [], "df": {}, "sources": []})
        _INDEX_LOADED = True


def get_index() -> dict[str, Any]:
    """只读地取到当前索引（进程启动后首次调用会从磁盘懒加载一次）。"""
    with _INDEX_LOCK:
        if not _INDEX_LOADED:
            rebuild_index(force=True)      # 同一把可重入锁，安全
    return _INDEX


def score_chunk(
    query_tokens: Iterable[str],
    chunk: dict[str, Any],
    df: dict[str, int],
    total: int,
    vocab: set[str] | None = None,
) -> tuple[float, float]:
    """给「查询 vs 片段」打分（纯函数）。

    打分 = Σ IDF(命中词) × (1 + log(1+词频))，再乘一个长度惩罚
    （长片段天然容易命中，稍微压一压）。

    coverage 的语义很关键：分母只算**知识库认识的关键词**
    （即出现在全局词表里的词），完全没在库里出现过的词不计入分母。
    否则口语化问句里那些无意义的二元组（「年假有几天」里的『假有』『有几』）
    会把覆盖率稀释掉，导致明明能查到的问题被误判成「查不到」。

    Args:
        query_tokens: 查询切出来的 token。
        chunk: 带 tf 的片段。
        df: 全局文档频率表（token -> 出现在几个片段里）。
        total: 片段总数。
        vocab: 全局词表；不传则用 df 的键。

    Returns:
        (score, coverage)。完全无法匹配时返回 (0.0, 0.0)。
    """
    query_unique = {t for t in query_tokens if t}
    if not query_unique:
        return 0.0, 0.0

    known = query_unique & (vocab if vocab is not None else set(df))
    if not known:
        # 问题里的词知识库一个都不认识 -> 必然查不到
        return 0.0, 0.0

    chunk_tf = chunk.get("tf") or {}
    hits = [t for t in known if t in chunk_tf]
    if not hits:
        return 0.0, 0.0

    coverage = len(hits) / len(known)
    raw = sum(_idf(df.get(t, 0), total) * (1.0 + math.log(1.0 + chunk_tf[t])) for t in hits)

    # 长度惩罚：片段越长越可能是「碰巧包含」，给一点阻尼
    length = max(1, len(chunk.get("tokens") or []))
    penalty = 1.0 / (1.0 + max(0.0, (length - 60) / 240.0))
    return raw * penalty, coverage


def fallback_search(
    query: str,
    chunks: Iterable[dict[str, Any]],
    limit: int = KB_DEFAULT_TOP_K,
    min_coverage: float = KB_FALLBACK_COVERAGE,
) -> list[dict[str, Any]]:
    """单字兜底召回（纯函数）。

    只在关键词检索一条都没命中时才启用。动机很实际：口语提问的用词经常和
    文档对不上 —— 用户问「这个产品叫什么名字」，而文档里写的是「本项目名叫」，
    「产品」「名字」两个词在词表里根本不存在，关键词检索必然全军覆没。
    这时退一步按「有意义的单字重合度」做一次宽松召回，
    宁可多给模型几条线索，也不要让它轻率地下「资料里没有」的结论。

    Returns:
        与 search_knowledge_base() 的 results 同构的列表，额外带 "fallback": True。
    """
    query_chars = _chunk_char_set(query)
    if len(query_chars) < 2:      # 只有一个字时噪声太大，不做兜底
        return []

    scored: list[dict[str, Any]] = []
    for chunk in chunks:
        overlap = query_chars & set(chunk.get("chars") or ())
        if not overlap:
            continue
        coverage = len(overlap) / len(query_chars)
        if coverage < min_coverage:
            continue
        scored.append(
            {
                "source": chunk["source"],
                "chunk_id": chunk["id"],
                "score": round(coverage, 4),
                "coverage": round(coverage, 4),
                "matched": sorted(overlap),
                "fallback": True,
                "text": chunk["text"][:KB_SNIPPET_CHARS],
            }
        )

    scored.sort(key=lambda item: (-item["score"], item["source"]))
    return scored[:limit]


def search_knowledge_base(
    query: str,
    top_k: Any = KB_DEFAULT_TOP_K,
    *,
    min_coverage: float = KB_MIN_COVERAGE,
) -> dict[str, Any]:
    """【核心工具】在本地知识库里检索与 query 最相关的资料片段。

    这是 Agent 工具箱中 search_knowledge_base 工具的真正实现，设计上
    把「查不到」当成一种正常返回值（hit=False），而不是抛异常 ——
    上层拿到 hit=False 就应该如实回答「资料里没有提到」。

    Args:
        query: 检索词或问题。可以带口语词，切词时会自动过滤停用词。
        top_k: 最多返回几段，1~8，超出会自动夹住。
        min_coverage: 关键词命中比例门槛，低于它认为「没查到」。

    Returns:
        {
          "ok": bool,                  # 工具是否正常执行
          "hit": bool,                 # 是否找到相关内容
          "query": str,
          "count": int,
          "message": str,              # 给模型/人看的一句话结论
          "results": [
            {"source": 文件名, "chunk_id": str, "score": float,
             "coverage": float, "text": 片段正文}, ...
          ],
          "scanned_chunks": int,       # 共扫描了多少片段
          "elapsed_ms": int,
        }
    """
    started = time.perf_counter()
    text = str(query or "").strip()

    index = get_index()
    chunks: list[dict[str, Any]] = index.get("chunks") or []
    df: dict[str, int] = index.get("df") or {}
    total = max(1, len(chunks))

    def _result(
        hit: bool,
        results: list[dict[str, Any]],
        message: str,
        terms: list[str] | None = None,
        fallback: bool = False,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "hit": hit,
            "query": text,
            "count": len(results),
            "results": results,
            "message": message,
            "known_terms": terms or [],
            "fallback": fallback,
            "scanned_chunks": len(chunks),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    if not text:
        return _result(False, [], "检索词为空，没有可查询的内容。")
    if not chunks:
        return _result(False, [], "知识库目前是空的，还没有上传任何资料。")

    # top_k 容错：模型可能传字符串、传 0、传很大的数
    try:
        limit = int(top_k)
    except (TypeError, ValueError):
        limit = KB_DEFAULT_TOP_K
    limit = min(max(limit, 1), KB_MAX_TOP_K)

    query_tokens = tokenize(text)
    vocab = set(df)
    # 只保留知识库「认识」的查询词，用于判断问题是否真的被资料覆盖
    known_terms = sorted({t for t in set(query_tokens) if t in vocab})

    scored: list[dict[str, Any]] = []
    for chunk in chunks:
        score, coverage = score_chunk(query_tokens, chunk, df, total, vocab=vocab)
        if score <= 0 or coverage < min_coverage:
            continue
        chunk_tf = chunk.get("tf") or {}
        matched = sorted({t for t in known_terms if t in chunk_tf})
        scored.append(
            {
                "source": chunk["source"],
                "chunk_id": chunk["id"],
                "score": round(score, 4),
                "coverage": round(coverage, 4),
                "matched": matched,
                "text": chunk["text"][:KB_SNIPPET_CHARS],
            }
        )

    if not scored:
        # 关键词一条都没命中 -> 退一步做单字兜底召回（兜住「用词对不上」的情况）
        rescued = fallback_search(text, chunks, limit)
        if rescued:
            sources = sorted({item["source"] for item in rescued})
            return _result(
                True,
                rescued,
                f"关键词没有直接命中，改按单字匹配找到 {len(rescued)} 条可能相关的片段"
                f"（来源：{'、'.join(sources)}）。请判断这些片段是否真的回答了问题；"
                "如果答不上来，就如实回答「资料里没有提到」。",
                known_terms,
                fallback=True,
            )
        return _result(False, [], f"知识库中没有找到与「{text}」相关的资料。", known_terms)

    # 只保留分数接近最高分的片段，避免把弱相关的也塞给模型
    scored.sort(key=lambda item: (-item["score"], item["source"]))
    threshold = scored[0]["score"] * KB_KEEP_TOP_RATIO
    top = [item for item in scored if item["score"] >= threshold][:limit]

    sources = sorted({item["source"] for item in top})
    return _result(
        True,
        top,
        f"在 {len(sources)} 篇资料中找到 {len(top)} 条相关内容（来源：{'、'.join(sources)}）。",
        known_terms,
    )


# --------------------------------------------------------------------------- #
# 6. 统计信息
# --------------------------------------------------------------------------- #

def knowledge_stats() -> dict[str, Any]:
    """知识库概况，供健康检查 / 前端面板展示。"""
    docs = list_documents()
    with _INDEX_LOCK:
        chunk_count = len(_INDEX["chunks"])
        indexed_sources = list(_INDEX["sources"])
    return {
        "documents": len(docs),
        "chunks": chunk_count,
        "bytes": sum(int(d["bytes"]) for d in docs),
        "chars": sum(int(d["chars"]) for d in docs),
        "sources": indexed_sources,
        "empty": not docs,
        "max_file_mb": round(KB_MAX_FILE_BYTES / 1024 / 1024, 1),
    }


# --------------------------------------------------------------------------- #
# 7. 自测入口：python knowledge_base.py
# --------------------------------------------------------------------------- #
def _self_test() -> None:
    """离线自测：不联网、不碰真实知识库目录，验证切词/分块/检索是否正确。"""
    print("=" * 62)
    print("knowledge_base 离线自测")
    print("=" * 62)

    # 1) 文件名清洗与校验
    assert normalize_filename("../../etc/passwd.txt") == "passwd.txt"
    assert normalize_filename("产品:手册.md") == "产品_手册.md"
    assert normalize_filename("说明.markdown") == "说明.md"
    for bad in ("logo.png", "data.csv", ""):
        try:
            normalize_filename(bad)
        except KnowledgeError:
            pass
        else:
            raise AssertionError(f"{bad!r} 应当被拒绝")
    print("[1] 文件名清洗与扩展名白名单 OK")

    # 2) 编码探测
    assert decode_bytes("中文内容".encode("utf-8")) == "中文内容"
    assert decode_bytes("中文内容".encode("gb18030")) == "中文内容"
    assert decode_bytes(b"\xff\xfe\x00bad")  # 不抛异常即可
    print("[2] GBK/UTF-8 编码探测 OK")

    # 3) markdown 清洗：链接保留文字、围栏去掉
    cleaned = strip_markdown("## 标题\n[官网](https://x.com) 请访问\n```py\nprint(1)\n```")
    assert "标题" in cleaned and "官网" in cleaned and "print(1)" in cleaned and "```" not in cleaned
    print("[3] markdown 清洗 OK")

    # 4) 切词：中文出二元组、英文整词、停用词被过滤
    tokens = tokenize("什么是量子纠缠 quantum entanglement")
    assert "量子" in tokens and "纠缠" in tokens
    assert "quantum" in tokens and "entanglement" in tokens
    assert "什么" not in tokens      # 停用词
    print(f"[4] 切词 OK（示例 {len(tokens)} 个 token）")

    # 5) 分块：长文档能切开，且相邻块有重叠
    long_text = "\n\n".join(f"第{i}段：" + "内容" * 60 for i in range(6))
    chunks = chunk_text(long_text, "demo.md")
    assert len(chunks) > 1, "长文档应当被切成多块"
    assert all(c["source"] == "demo.md" for c in chunks)
    short = chunk_text("就一句话。", "tiny.txt")
    assert len(short) == 1
    print(f"[5] 分块 OK（长文切成 {len(chunks)} 块，短文 1 块）")

    # 6) 检索：命中与「查不到」都要正确
    demo_chunks = chunk_text(
        "公司年假制度：入职满一年可享受 5 天带薪年假，满三年 10 天。\n\n"
        "报销流程：先提交发票，再由部门主管审批，财务在 5 个工作日内打款。\n\n"
        "产品名称叫做「星尘」，主打实时语音交互。",
        "员工手册.md",
    ) + chunk_text("本项目使用 Flask + DeepSeek 构建后端。", "技术说明.txt")

    index = build_index(demo_chunks)
    df, total = index["df"], max(1, len(index["chunks"]))

    def find(query: str) -> list[tuple[float, float]]:
        qt = tokenize(query)
        return [score_chunk(qt, c, df, total) for c in index["chunks"]]

    # 6.1 命中：问年假，应当召回带年假的那一段
    q = tokenize("年假有几天")
    best = max(range(len(index["chunks"])), key=lambda i: score_chunk(q, index["chunks"][i], df, total)[0])
    assert "年假" in index["chunks"][best]["text"], "应当召回年假相关片段"
    print("[6.1] 命中检索 OK（「年假有几天」-> 年假段落）")

    # 6.2 命中：问报销
    q = tokenize("报销需要多久到账")
    best = max(range(len(index["chunks"])), key=lambda i: score_chunk(q, index["chunks"][i], df, total)[0])
    assert "报销" in index["chunks"][best]["text"]
    print("[6.2] 命中检索 OK（「报销需要多久到账」-> 报销段落）")

    # 6.3 查不到：问文档里没有的概念，最高分应当很弱 / coverage 很低
    q = tokenize("黑洞的视界半径是多少")
    score, coverage = max((score_chunk(q, c, df, total) for c in index["chunks"]), key=lambda x: x[0])
    assert score == 0.0 and coverage == 0.0, "问题里的词一个都不认识时，应当完全打不出分"
    print("[6.3] 「查不到」判定 OK（词表完全不认识 -> score=0）")

    # 6.4 端到端检索（走真实索引）：口语化问句必须能命中。
    #     ★ 这条用例是补上来的：起初 coverage 用的是「占全部 query 词的比例」，
    #       口语问句里的无意义二元组（假有/有几/几天）把分母撑大，导致
    #       「年假有几天」明明有资料却查不到 —— 纯函数层面的用例发现不了它。
    demo_name = "_selftest_员工手册.md"
    try:
        save_upload(
            demo_name,
            "公司年假制度：入职满一年可享受 5 天带薪年假，满三年 10 天。\n\n"
            "报销流程：先提交发票，再由部门主管审批，财务在 5 个工作日内打款。\n\n"
            "考勤：每天上午 9:00 上班，下午 18:00 下班。\n\n"
            "本项目名叫「星尘」，主打实时语音交互。",
        )

        for query in ("年假有几天", "报销多久能到账", "报销需要主管审批吗", "几点下班"):
            got = search_knowledge_base(query)
            assert got["hit"], f"「{query}」应当命中，实际：{got['message']}"
        assert any("年假" in r["text"] for r in search_knowledge_base("年假有几天")["results"])
        print("[6.4] 端到端口语化问句检索 OK（4 个问句全部命中）")

        # 6.5 端到端「查不到」：库外概念必须返回 hit=False 且结果为空
        for query in ("黑洞的视界半径是多少", "食堂几点开门", "如何申请专利"):
            got = search_knowledge_base(query)
            assert not got["hit"] and got["results"] == [], f"「{query}」不该命中，实际：{got['message']}"
        print("[6.5] 端到端「查不到」判定 OK（3 个库外问题全部未命中，兜底召回也没误伤）")

        # 6.5b 单字兜底：关键词一条都命中不了时，仍要给出线索。
        #      这里故意用纯函数 + 受控片段来测，不依赖真实知识库里有什么：
        #      问「它叫什么名字」，文档里写的是「名叫」——「名字」这个词词表里没有，
        #      关键词检索必然全军覆没，只能靠单字重合度把它捞回来。
        fb_chunks = build_index(
            chunk_text("本项目名叫「星尘」，主打实时语音交互。", "说明.txt")
        )["chunks"]
        rescued = fallback_search("它叫什么名字", fb_chunks)
        assert rescued, "关键词无法命中时，单字兜底应当给出线索"
        assert any("星尘" in r["text"] for r in rescued)
        print(f"[6.5b] 单字兜底召回 OK（matched={rescued[0]['matched']}）")

        # 6.5c 文件名参与检索：用户按文档名提问时必须能直接命中。
        #      动机：「产品说明.txt」的正文写的是「本项目名叫『星尘』」，
        #      通篇没有「产品」二字；不索引文件名的话，问「产品叫什么名字」
        #      只能靠单字兜底这条后路，而模型改写查询时（「产品名称」）
        #      一条字都兜不住 —— 这是唯一一个真实环境里复现出来的漏召回。
        title_name = "_selftest_产品说明.txt"
        save_upload(title_name, "本项目名叫「星尘」，是一款实时语音交互助手。")
        try:
            got = search_knowledge_base("产品叫什么名字")
            assert got["hit"], f"按文件名提问应当命中，实际：{got['message']}"
            assert any("星尘" in r["text"] for r in got["results"]), "应当召回写着名字的那一段"
            assert title_name in {r["source"] for r in got["results"]}, "命中的来源应是产品说明那份文档"
            print(f"[6.5c] 文件名参与检索 OK（matched={got['results'][0]['matched']}）")
        finally:
            remove_document(title_name)

        # 6.6 检索结果里带上命中关键词与来源，便于上层解释与前端展示
        got = search_knowledge_base("年假有几天")
        first = got["results"][0]
        assert first["source"] == demo_name and "年假" in first["matched"]
        assert got["fallback"] is False, "关键词直接命中的不应标记为兜底"
        print(f"[6.6] 检索结果字段 OK（matched={first['matched']}，来源={first['source']}）")
    finally:
        remove_document(demo_name)

    # 7) score_chunk 的纯函数性质：空查询、字典外词汇都返回 0
    assert score_chunk([], index["chunks"][0], df, total) == (0.0, 0.0)
    assert score_chunk(["完全不存在的词"], index["chunks"][0], df, total) == (0.0, 0.0)
    print("[7] 打分的边界情况 OK")

    print("-" * 62)
    print("全部自测通过 ✔")


if __name__ == "__main__":
    _self_test()
