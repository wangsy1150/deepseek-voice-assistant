# 语音交互助手 Voice Assistant

一个「对着麦克风说话 → DeepSeek 思考 → edge-tts 朗读」的本地网页应用。
前端 HTML + CSS + 原生 JavaScript，后端 Flask，采用函数式架构，每个功能都是独立可测试的函数。

从 **v3 起它还是一个 Agent**：模型自己决定要不要查资料，查不到就如实说「资料里没有提到」，前端把整个决策过程摊开给你看。

**v2 升级亮点**

| 能力 | v1 | v2 |
| --- | --- | --- |
| 朗读 | 浏览器原生 speechSynthesis | **edge-tts 在线合成**（音质好、12 种中文音色可选），失败**自动降级**回 speechSynthesis |
| 记忆 | 前端每次带 8 轮历史 | **`remember()` 维护 messages 列表，保留最近 10 轮**，服务端按会话隔离持久化 |
| 架构 | listen / think / speak | listen / think **架构未改动**，朗读层拆成 `speak_with_edge_tts()` + `speak_fallback()` |

**v3 升级亮点（Agent 化）**

| 需求 | 落地方式 |
| --- | --- |
| ① 给 AI 配工具箱（含工具说明） | `agent_core.TOOLBOX` 声明 `search_knowledge_base`，含 299 字的 `description` —— 这段说明会原样发给模型，也会显示在前端面板里 |
| ② 用 Function Calling 让 AI 自己决定 | 请求体带 `tools` + `tool_choice: "auto"`，**调不调工具由模型说了算**（闲聊就不调，问资料才调） |
| ③ `decide()` 决策 + `search_knowledge_base()` 查资料 | `decide()` 读模型返回的 tool_calls 决定下一步；`search_knowledge_base()` 是工具的真实实现（本地检索） |
| ④ 支持上传 .md / .txt | `POST /api/agent/upload`（支持拖拽 / 文件选择 / 多文件），落盘到 `knowledge_base/` |
| ⑤ 查不到就诚实说「资料里没有提到」 | 三重保险：系统提示词约束 + 工具结果里明确写「没有就回答资料里没有提到」+ **一致性校验**（模型没查就下结论时，系统自动补查一次再让它重答） |
| ⑥ 前端显示决策过程 | 每条回答下方渲染「决策卡」：显示调了几次工具、每次的参数 / 命中与否 / 来源 / 片段预览 |
| ⑦ Flask 原有部分不动 | 新增功能全部走 **Blueprint**（`agent_routes.py`），老路由一行没改，老测试 102 个用例全绿 |

---

## 快速开始

### 方式一：双击启动（推荐）

直接双击项目根目录下的 **`start.bat`**。它会自动完成：

1. 查找可用的 Python（优先 `py -3`，其次 `python`）
2. 首次运行时创建独立虚拟环境 `.venv` 并安装 Flask + edge-tts（只做一次）
3. 检查 `DEEPSEEK_API_KEY` 环境变量（进程没有时，自动去读 Windows 用户级环境变量）
4. 启动 Flask 服务并自动打开浏览器

### 方式二：手动启动

```bash
py -3 -m venv .venv                                  # 首次
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

浏览器访问 <http://127.0.0.1:5000>。

> **麦克风必须用 Chrome 或 Edge 打开**。Firefox、Safari 不支持 `SpeechRecognition`；
> 并且浏览器的语音识别要求页面在 `localhost` 或 https 下运行，直接双击打开 HTML 文件不行。

---

## 配置 API Key

代码中**没有硬编码任何密钥**，全部通过 `os.getenv("DEEPSEEK_API_KEY")` 读取。

**永久设置（推荐）**

1. Win 键搜索「环境变量」，打开「编辑系统环境变量」
2. 点击「环境变量」→ 用户变量「新建」
3. 变量名 `DEEPSEEK_API_KEY`，变量值填你的密钥（形如 `sk-xxxxxxxx`）
4. 确定后**关闭并重新打开**命令行，再双击 `start.bat`

**临时设置（只对当前终端有效）**

```powershell
$env:DEEPSEEK_API_KEY="sk-你的密钥"
.\.venv\Scripts\python.exe app.py
```

密钥申请地址：<https://platform.deepseek.com/api_keys>。页面右上角有「环境自检」指示灯，不用去猜。

---

## 目录结构

```
voice-app/
├── app.py                 # Flask 后端入口：路由 + 参数校验 + 错误翻译（很薄的一层）
├── voice_core.py          # 核心逻辑：think() + speak_with_edge_tts() + remember()
├── knowledge_base.py      # ★ v3 本地知识库：上传 / 分块 / 检索（零外部依赖，自带离线自测）
├── agent_core.py          # ★ v3 Agent 大脑：工具箱 + decide() + 工具分发 + 诚实性校验
├── agent_routes.py        # ★ v3 新增接口，以 Blueprint 形式挂到 app 上（老代码零改动）
├── test_app.py            # 单元测试：102 个用例，不联网、不需要密钥即可跑通
├── requirements.txt       # 依赖清单（Flask + edge-tts）
├── start.bat              # 双击启动脚本
├── knowledge_base/        # ★ v3 上传的资料就存在这里（.md / .txt）
├── templates/
│   └── index.html         # 页面结构
└── static/
    ├── style.css          # 样式
    ├── app.js             # 前端逻辑：listen / think / speak 三层
    └── agent.js           # ★ v3 Agent 桥接层：决策过程渲染 + 知识库管理面板
```

> **依赖没有变化**：`knowledge_base.py` 与 `agent_core.py` 只用 Python 标准库
> （`json` / `math` / `re` / `threading` / `unicodedata`），中文切词用「二元组」自实现，
> 没有引入 jieba 之类的额外包。`requirements.txt` 与 `start.bat` 都无需改动。

---

## 架构：listen → think → speak

### 朗读的三层降级链路（本次升级重点）

```
speak()                    ← 总入口，converse() 只调它
  │
  ├─ speak_with_edge_tts()  ← 首选：POST /api/tts 拿 MP3，用 <audio> 播放
  │     失败（断网/超时/合成报错/自动播放被拒）
  │
  └─ speak_fallback()       ← 保底：浏览器 speechSynthesis，不联网也能出声
```

降级是全自动的：`speak()` 内部捕获异常后直接走保底，用户只会看到顶部一条黄色提示，
以及状态栏里的「朗读：已降级为浏览器朗读」，对话流程完全不受影响。

### 各函数位置与职责

| 函数 | 位置 | 说明 |
| --- | --- | --- |
| `listen()` | `static/app.js` | 浏览器原生 **SpeechRecognition** 录音识别，返回 `Promise<string>`（架构未改动） |
| `think()` | `voice_core.py` + 前端 | 后端读环境变量取密钥调用 DeepSeek；前端 `think()` 请求 `/api/think`（架构未改动） |
| `speak_with_edge_tts(text, voice_name)` | `voice_core.py` + 前端 | **后端**用 edge-tts 合成 MP3（支持音色名参数）；**前端**同名函数负责请求与播放 |
| `speak_fallback(text)` | `static/app.js` | 浏览器原生 speechSynthesis 朗读，保底方案 |
| `speak(text)` | `static/app.js` | 朗读总入口，串联上面两级并处理降级 |
| `remember(user_msg, ai_msg)` | `voice_core.py` + 前端 | 维护 messages 列表，**只保留最近 10 轮** |

三个环节互不依赖，可以分别替换或单独测试：想换语音识别只改 `listen()`，
想换大模型网关只改 `think()` 的 `url`/`model`，想换 TTS 只改 `speak_with_edge_tts()`。

### 记忆（remember）如何工作

```
第 1 轮：think("我叫小林")  ─→ 回答 ─→ remember("我叫小林", "好的小林…")
第 2 轮：think("我叫什么？") ─→ 带上第 1 轮的 messages 一起发给模型 ─→ 正确答出「小林」
```

- **权威记忆在服务端**（`voice_core.remember`），按 `session_id` 隔离，会话 ID 由前端生成并存在 localStorage。
  所以**刷新页面、甚至重启服务后端**，对话上下文都还在。
- **滑动窗口**：写入后立刻裁剪，只保留最近 `MAX_MEMORY_TURNS = 10` 轮（20 条消息），
  最旧的一轮自动丢弃，上下文永远不会无限膨胀。
- 前端另有一份镜像（`static/app.js` 的 `remember()`），用于刷新页面后恢复对话区显示，
  以及方便在控制台检查 `state.history`。
- 顶部的「记忆：N/10 轮」指示实时反映当前轮数。

---

## Agent 化：AI 自己决定要不要查资料

### 一轮问答发生了什么

```
用户提问
   │
   ▼
decide()  ──► 调 DeepSeek（带 tools + tool_choice="auto"）
   │                 │
   │                 ├─ 模型说「我要调 search_knowledge_base」──► run_tool() 在本库检索
   │                 │                                            │
   │                 │                                            ▼
   │                 │                                   把命中的片段作为「观察结果」塞回消息
   │                 │                                            │
   │                 └◄────────────── 再问一次模型 ◄──────────────┘
   │                                    （最多 3 轮）
   ▼
最终回答（附带整条 trace：每步决定了什么、调了什么、命中了什么）
```

**决策权真的在模型手上**：`tool_choice` 设为 `"auto"`，所以「你好呀」这种闲聊模型不会去查库，
只有涉及资料的问题才会触发检索。这一点在 `agent_core.py` 的自测 `[9]` 和真实 e2e 里都验证过。

### 诚实性是怎么被保证的（需求 ⑤）

模型有时会「偷懒」：资料明明在库里，它却直接回一句「资料里没有提到」，压根没去查。
为此加了三道闸：

1. **提示词层面**：系统提示词写死「只能依据工具返回的片段作答，没有就回答『资料里没有提到』，禁止编造」。
2. **工具结果层面**：每次检索返回的文本里都再强调一遍这条规矩，尤其是返回空结果时。
3. **一致性校验（关键）**：如果模型这一轮决定收尾、且说了「没有」，但它**从头到尾一次都没查过**而库里又有资料
   —— 系统会**替它补查一次**，把结果塞回去让它重新决策。trace 里会多出一条 `guard` 步骤，前端标注为「系统补查」。

> 真实案例：问「我们几点下班？」模型一度直接答「资料里没有提到」，
> 系统补查后它才正确回答「下午 18:00 下班」。这类「没查就下结论」的漏查被这道闸兜住了。

### 「查不到」不是异常，是一种正常返回

`search_knowledge_base()` 返回的是 `{"hit": false, ...}` 而不是抛异常。上层拿到 `hit=false`
就应该照实回答。从机制上堵死「编造答案」的路 —— 这是需求 ⑤ 想达到的效果。

检索本身为此做了三层召回：

- **关键词召回**：中文二元组 + 英文词，BM25 风格 IDF 打分。
- **覆盖率只算「库里认识的词」**：口语问句里的无意义二元组（「年假有几天」里的『假有』『有几』）
  不计入分母，否则明明有资料也会被稀释成「查不到」。
- **单字兜底 + 文件名入词表**：用户按文档名提问（问「产品叫什么名字」，正文只写了「本项目名叫『星尘』」）
  时，靠「文件名并入词表 + 单字重合度兜底」把片段捞回来。

### 前端怎么把决策过程摊开给你看（需求 ⑥）

`static/agent.js` 以 `window.AgentBridge` 的形式挂在全局，`app.js` 的 `converse()` 只在两处
opt-in（拿结果、渲染 trace），**没有 if/else 分叉**，Agent 关掉也能照常跑。

每条回答下面会追加一张**决策卡**：

```
┌────────────────────────────────────────────┐
│ 🔧 调用工具 1 次                    1.6s    │
│ [decide] 第1轮 → 决定调用工具                │
│   📚 search_knowledge_base({"query":"产品名称"})  命中 1 条
│      来源：产品说明.txt                       │
│      预览：本项目名叫「星尘」，是一款实时语音… │
└────────────────────────────────────────────┘
```

徽章有三种状态：**🔧 调用工具 N 次** / **💬 未调用工具** / **⚠️ 系统补查 N 次**，
一眼就能看出这一轮 AI 到底查没查资料。

### 知识库管理面板

点击顶栏的「📚 知识库」按钮打开右侧面板，可以：

- 看**工具箱**：每个工具叫什么、什么时候该用、返回什么（就是发给模型的那段说明，所见即所得）
- **拖拽或选择** .md / .txt 文件上传，列表里能看每篇文档的字符数 / 分块数 / 更新时间
- 单篇删除、一键清空

单文件上限 2 MB、整库 20 MB、最多 200 篇；文件名会做安全清洗（挡掉 `../` 路径穿越、
Windows 保留字符），只收 `.md` / `.txt`。

---

## 接口说明

### `GET /api/health`

```json
{
  "ok": true,
  "api_key_configured": true,
  "edge_tts_available": true,
  "default_voice": "zh-CN-XiaoxiaoNeural",
  "memory": { "sessions": 1, "messages": 4, "max_turns": 10 },
  "model": "deepseek-chat",
  "env_var": "DEEPSEEK_API_KEY"
}
```

### `POST /api/think`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `text` | string | 是 | 用户提问，最长 4000 字 |
| `session_id` | string | 否 | 会话标识，用于隔离记忆，默认 `default` |
| `history` | array | 否 | 显式指定历史则忽略服务端记忆（调试用），仍会记进记忆 |
| `temperature` | number | 否 | 0~2，越界自动夹取 |
| `model` | string | 否 | 默认 `deepseek-chat` |

成功：`{"ok": true, "reply": "小林。", "elapsed_ms": 1178, "turns": 2, "session_id": "...", "usage": {...}}`

> `turns` 是记完这一轮后的总轮数，前端用它更新「记忆：N/10 轮」。

### `POST /api/tts`

请求：`{"text": "要朗读的文字", "voice": "zh-CN-YunxiNeural", "rate": 1.0, "volume": 1.0, "pitch": 0}`

- `rate` 0.5~2.0（倍率）、`volume` 0~2.0、`pitch` -50~50（Hz），越界自动夹取
- 成功：返回 `audio/mpeg` 音频流，响应头带 `X-TTS-Voice`（实际音色）和 `X-TTS-Elapsed-Ms`（合成耗时）
- 失败：HTTP 502（上游抖动）/ 503（未安装 edge-tts），响应体是
  `{"ok": false, "error": "中文原因", "fallback": "browser"}` —— **前端看到 `fallback: "browser"` 就降级**

### `GET /api/voices`

返回 12 个内置中文音色：`{"ok": true, "edge_available": true, "default": "zh-CN-XiaoxiaoNeural", "voices": [...]}`

音色清单刻意用**本地内置**而不是实时拉取微软接口，这样网络抖动时前端下拉框依然是满的。

### `GET /api/history?session_id=xxx` / `POST /api/reset`

前者读取某会话的记忆（刷新页面后前端用它恢复对话区），后者清空记忆。

### Agent 接口（全部由 `agent_routes.py` 的 Blueprint 提供，老接口零改动）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/agent/toolbox` | 返回工具箱声明（工具名 / 说明 / 参数 / 是否只读）与知识库概况 |
| POST | `/api/agent/think` | **Agent 对话**：`{"text": "...", "session_id": "..."}`，返回回答 + 完整 trace |
| POST | `/api/agent/upload` | 上传资料：multipart 表单字段 `files`（可多个）或 `file`；也支持 JSON `{"filename","content"}` |
| GET | `/api/agent/documents` | 列出知识库里的文档 + 统计信息 |
| POST | `/api/agent/documents/delete` | 删除单篇：`{"filename": "..."}` |
| POST | `/api/agent/kb/clear` | 清空整个知识库 |
| GET | `/api/agent/search?q=...&top_k=N` | 直接调用检索工具（不经过模型），用于调试 |

`/api/agent/think` 的响应结构（前端就靠 trace 渲染决策卡）：

```json
{
  "ok": true,
  "reply": "这个产品叫「星尘」，是一款实时语音交互助手。（来源：产品说明.txt）",
  "used_tools": true,
  "search_hit": true,
  "steps": 2,
  "elapsed_ms": 1600,
  "trace": [
    {"type": "decide", "step": 1, "action": "tool", "thought": "..."},
    {"type": "tool", "name": "search_knowledge_base",
     "arguments": {"query": "产品名称"}, "hit": true, "count": 1,
     "sources": ["产品说明.txt"], "preview": [...], "elapsed_ms": 0},
    {"type": "decide", "step": 2, "action": "final", "thought": ""}
  ]
}
```

> `trace` 里除 `decide` / `tool` 外，还可能出现 `guard` 类型 —— 即上面说的「系统补查」，
> 前端会把它标成「⚠️ 系统补查」，让你看到这条结论是模型自己查的、还是系统替它兜的底。

---

## 可选音色

| 音色名 | 说明 | 语种 |
| --- | --- | --- |
| `zh-CN-XiaoxiaoNeural` | 晓晓 · 温柔女声（**默认**） | 普通话 |
| `zh-CN-XiaoyiNeural` | 晓伊 · 活泼女声 | 普通话 |
| `zh-CN-YunxiNeural` | 云希 · 阳光男声 | 普通话 |
| `zh-CN-YunjianNeural` | 云健 · 浑厚男声 | 普通话 |
| `zh-CN-YunyangNeural` | 云扬 · 新闻播报 | 普通话 |
| `zh-CN-YunxiaNeural` | 云夏 · 少年音 | 普通话 |
| `zh-CN-liaoning-XiaobeiNeural` | 晓北 · 东北方言 | 东北官话 |
| `zh-CN-shaanxi-XiaoniNeural` | 晓妮 · 陕西方言 | 中原官话 |
| `zh-HK-HiuMaanNeural` / `zh-HK-WanLungNeural` | 曉曼 / 雲龍 | 粤语（中国香港） |
| `zh-TW-HsiaoChenNeural` / `zh-TW-YunJheNeural` | 曉臻 / 雲哲 | 国语（中国台湾） |

直接调函数换音色：

```python
import voice_core
audio = voice_core.speak_with_edge_tts("你好", "zh-CN-YunxiNeural", rate=1.2)
open("demo.mp3", "wb").write(audio)
```

---

## 网络与代理说明

edge-tts 需要访问微软的 `speech.platform.bing.com:443`。如果公司网络必须走代理，可以设置：

```powershell
$env:EDGE_TTS_PROXY="http://127.0.0.1:7890"    # 优先级最高
# 或者直接用标准的 HTTPS_PROXY，代码也会自动识别
```

未设置任何代理变量时直连。配了代理的话，函数会**先走代理、失败再直连**各试一次。

**关于稳定性**：edge-tts 偶发连不上是常见现象，所以 `speak_with_edge_tts()` 内置了
**3 次重试**（`EDGE_TTS_RETRIES`）+ 递增退避；即使全部失败也只会返回 502，
前端立刻降级到浏览器朗读，对话不会中断。想调重试次数：

```python
audio = voice_core.speak_with_edge_tts("你好", retries=5)
```

---

## 测试

```bash
.venv\Scripts\python.exe -m unittest test_app -v     # 全套单元测试（102 个用例，v2 老用例全绿）
.venv\Scripts\python.exe voice_core.py               # 核心模块自带离线自测
.venv\Scripts\python.exe knowledge_base.py           # v3 知识库自测（切词 / 分块 / 命中 / 查不到 / 兜底 / 文件名检索）
.venv\Scripts\python.exe agent_core.py               # v3 Agent 自测（工具箱 / schema / 决策解析 / 诚实性校验）
```

测试**完全离线、不需要 API Key**。关键设计：两个真正访问网络的函数
（`http_post_json()` 和 `_edge_synthesize()`）都支持注入替身，所以能验证完整链路：

- 语音合成：参数换算、音色校验与回落、重试成功、全部失败的错误收口、
  代理优先再直连、空输入/空音频、超长截断，以及**用假 edge_tts 模块跑一遍真实异步链路**
  （含「分片拼接只取 audio」「超时参数必须是 int」这两个易错点）
- 记忆：滑动窗口保留 10 轮、会话隔离、空内容不写入、清空计数、会话数上限淘汰、
  `think_with_memory()` 确实把历史传给了模型
- 接口：`/api/tts` 成功返回 audio/mpeg、失败带 `fallback: browser`、参数夹取，
  以及 `/api/think` 的一轮记忆写入与第二轮历史传递
- 前端：三个朗读函数存在且 `speak()` 真的调用了两个下层函数、`stopSpeaking()` 同时管两条链路

v3 新增的两套离线自测（同样不联网、不需要密钥）：

- **`knowledge_base.py`**：文件名安全清洗、GBK/UTF8 编码探测、markdown 清洗、二元组切词、
  长文分块与重叠、命中 / 查不到、口语化问句召回、单字兜底、**文件名参与检索**、结果字段完整性
- **`agent_core.py`**：工具箱声明、OpenAI 风格的 `tools` schema、`tool_choice="auto"`、
  决策解析（含坏 JSON / 空响应）、工具分发容错、观察结果措辞、端到端「先查再答 / 查不到就诚实说 / 闲聊不调工具」、
  步数上限强制收口、**一致性校验（未查即断言查不到会自动补查）**

---

## 常见问题

**Q：提示「已降级为浏览器朗读」？**
说明 edge-tts 这次没成功（网络抖动或代理问题），功能不受影响。可检查网络后点「🔁 重新朗读」重试，
或在「音色」里切到「浏览器原生音色」分组。

**Q：点击麦克风没反应 / 提示不支持？**
用 Chrome 或 Edge 打开，并且必须是 `http://127.0.0.1:5000` 这种 localhost 地址。首次使用浏览器会弹麦克风授权。

**Q：提示「未找到环境变量 DEEPSEEK_API_KEY」？**
环境变量设置后需要**重启命令行/程序**才生效（`start.bat` 已做用户级变量的兜底读取）。

**Q：模型记不住我说的话？**
记忆按 `session_id` 隔离，只在**同一个浏览器**里延续（ID 存在 localStorage）。
点过「清空对话」会同时清掉服务端记忆。另外记忆上限是 10 轮，超出后最早的内容会被丢弃。

**Q：AI 说「资料里没有提到」，但我明明传了资料？**
先点顶栏「📚 知识库」确认文件在列表里、且状态是「已入库」。若在，多半是**用词对不上**：
资料里写「本项目名叫『星尘』」，而问题用的是完全另一套说法。检索已经做了「文件名入词表 + 单字兜底」，
但仍有极限。可以试着换个说法再问，或直接在文档里补上一句更贴近提问习惯的描述。
想看检索到底给出了什么，可以开控制台调 `GET /api/agent/search?q=...` 单独验证。

**Q：AI 是不是在瞎编？**
看回答下面的决策卡：徽章显示 **💬 未调用工具** 却给了具体事实，才需要警惕。
正常情况下只有 **🔧 调用工具** 或 **⚠️ 系统补查** 才会带出具体信息，且会标注来源文件。

**Q：想换端口 / 关闭 edge-tts？**
设置 `VOICE_APP_PORT`（默认 5000）换端口；把 `requirements.txt` 里的 `edge-tts` 一行删掉重装，
就会一直走浏览器朗读。
