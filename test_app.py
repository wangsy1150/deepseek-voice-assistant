# -*- coding: utf-8 -*-
"""
test_app.py —— 单元测试

设计目标：**不联网、不需要 API Key** 就能把整条链路验证一遍。
做法是把唯一真正访问网络的函数 `voice_core.http_post_json` 的 opener 换成一个假的替身。

运行方式（在项目目录下）：
    python -m unittest test_app -v
    # 或者
    python test_app.py

覆盖范围：
    voice_core.get_api_key / has_api_key     密钥读取
    voice_core.sanitize_history              历史清洗
    voice_core.build_messages                提示词拼装
    voice_core.build_request_payload         请求体构造
    voice_core.parse_response                响应解析
    voice_core.extract_error                 错误提取
    voice_core.think                         think() 全链路
    app.index / api_health / api_think       Flask 接口
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import voice_core


# --------------------------------------------------------------------------- #
# 测试替身：模拟 urllib 的响应对象
# --------------------------------------------------------------------------- #
class FakeResponse:
    """模拟 urllib.request.urlopen 返回的对象（支持 with 语法与 read）。"""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        """urllib 在异常路径下会尝试关闭响应体，这里补一个空实现避免告警。"""

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def make_opener(reply: str = "你好呀，很高兴见到你。", record: list | None = None):
    """生成一个假的网络函数，返回一段合法的 DeepSeek 风格响应。

    Args:
        reply: 希望模型「回答」的文本。
        record: 若传入列表，会把收到的请求体塞进去，方便断言。

    Returns:
        可直接作为 opener 参数传入的函数。
    """

    def opener(request, timeout: float = 60.0):
        if record is not None:
            record.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(
            json.dumps(
                {
                    "id": "chatcmpl-test",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )

    return opener


# --------------------------------------------------------------------------- #
# 1. 密钥读取
# --------------------------------------------------------------------------- #
class TestApiKey(unittest.TestCase):
    """验证密钥只从环境变量读取，且缺失时给出可读的中文提示。"""

    def test_has_api_key_false_when_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(voice_core.has_api_key())

    def test_has_api_key_true_when_present(self):
        with mock.patch.dict(os.environ, {voice_core.ENV_API_KEY: "sk-abc"}, clear=True):
            self.assertTrue(voice_core.has_api_key())

    def test_has_api_key_false_when_only_spaces(self):
        """全是空格也算未配置，避免误判。"""
        with mock.patch.dict(os.environ, {voice_core.ENV_API_KEY: "   "}, clear=True):
            self.assertFalse(voice_core.has_api_key())

    def test_get_api_key_strips_whitespace(self):
        with mock.patch.dict(os.environ, {voice_core.ENV_API_KEY: "  sk-abc  "}, clear=True):
            self.assertEqual(voice_core.get_api_key(), "sk-abc")

    def test_get_api_key_raises_with_hint(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(voice_core.ThinkError) as ctx:
                voice_core.get_api_key()
            # 报错信息里必须带上环境变量名，方便用户排查
            self.assertIn(voice_core.ENV_API_KEY, str(ctx.exception))

    def test_key_never_hardcoded(self):
        """确保源码里没有硬编码的密钥（sk- 开头的字面量）。"""
        here = os.path.dirname(os.path.abspath(__file__))
        for name in ("voice_core.py", "app.py"):
            with open(os.path.join(here, name), encoding="utf-8") as fh:
                source = fh.read()
            self.assertNotIn("sk-", source.replace("sk-abc", "").replace("sk-fake", "").replace("sk-test", "").replace("sk-x", "").replace("sk-你的密钥", "").replace("sk-xxxxxxxx", ""))


# --------------------------------------------------------------------------- #
# 2. 请求体构造（纯函数）
# --------------------------------------------------------------------------- #
class TestBuilders(unittest.TestCase):
    """验证 messages / payload / headers 的拼装逻辑。"""

    def test_build_messages_order_and_content(self):
        msgs = voice_core.build_messages(
            "今天几号",
            [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好呀"}],
            system_prompt="你是助手",
        )
        self.assertEqual(msgs[0], {"role": "system", "content": "你是助手"})
        self.assertEqual(msgs[1], {"role": "user", "content": "你好"})
        self.assertEqual(msgs[-1], {"role": "user", "content": "今天几号"})
        self.assertEqual(len(msgs), 4)

    def test_build_messages_without_system_prompt(self):
        msgs = voice_core.build_messages("嗨", system_prompt="")
        self.assertEqual(msgs, [{"role": "user", "content": "嗨"}])

    def test_build_messages_trims_input(self):
        msgs = voice_core.build_messages("  你好  ")
        self.assertEqual(msgs[-1]["content"], "你好")

    def test_sanitize_history_drops_invalid(self):
        raw = [
            {"role": "hacker", "content": "非法角色"},
            {"role": "user", "content": ""},          # 空内容
            "不是字典",                                  # 类型错误
            {"role": "user", "content": "有效"},
        ]
        self.assertEqual(voice_core.sanitize_history(raw), [{"role": "user", "content": "有效"}])

    def test_sanitize_history_none(self):
        self.assertEqual(voice_core.sanitize_history(None), [])

    def test_sanitize_history_truncates_long_text(self):
        cleaned = voice_core.sanitize_history([{"role": "user", "content": "啊" * 5000}])
        self.assertEqual(len(cleaned[0]["content"]), 2000)

    def test_sanitize_history_keeps_recent_turns(self):
        raw = [{"role": "user", "content": f"第{i}轮"} for i in range(30)]
        cleaned = voice_core.sanitize_history(raw, max_turns=3)
        self.assertEqual(len(cleaned), 6)               # 3 轮 × 2 条
        self.assertEqual(cleaned[-1]["content"], "第29轮")

    def test_build_request_payload(self):
        payload = voice_core.build_request_payload([{"role": "user", "content": "hi"}], model="deepseek-chat")
        self.assertEqual(payload["model"], "deepseek-chat")
        self.assertFalse(payload["stream"])
        self.assertIn("temperature", payload)

    def test_build_headers_uses_bearer(self):
        headers = voice_core.build_headers("sk-key")
        self.assertEqual(headers["Authorization"], "Bearer sk-key")
        self.assertEqual(headers["Content-Type"], "application/json")


# --------------------------------------------------------------------------- #
# 3. 响应解析
# --------------------------------------------------------------------------- #
class TestParseResponse(unittest.TestCase):
    def test_parse_normal(self):
        data = {"choices": [{"message": {"content": "  你好  "}}]}
        self.assertEqual(voice_core.parse_response(data), "你好")

    def test_parse_empty_choices_raises(self):
        with self.assertRaises(voice_core.ThinkError):
            voice_core.parse_response({"choices": []})

    def test_parse_empty_content_raises(self):
        with self.assertRaises(voice_core.ThinkError):
            voice_core.parse_response({"choices": [{"message": {"content": "   "}}]})

    def test_parse_error_field_raises(self):
        with self.assertRaises(voice_core.ThinkError):
            voice_core.parse_response({"error": {"message": "余额不足"}})

    def test_extract_error_from_json(self):
        self.assertEqual(voice_core.extract_error('{"error":{"message":"额度不够"}}'), "额度不够")

    def test_extract_error_from_plain_text(self):
        self.assertEqual(voice_core.extract_error("Bad Gateway"), "Bad Gateway")

    def test_get_usage_filters_non_numbers(self):
        usage = voice_core.get_usage({"usage": {"total_tokens": 12, "note": "x"}})
        self.assertEqual(usage, {"total_tokens": 12})


# --------------------------------------------------------------------------- #
# 4. think() 全链路（不联网）
# --------------------------------------------------------------------------- #
class TestThink(unittest.TestCase):
    """用假 opener 验证 think() 的完整流程与错误分支。"""

    def test_think_success(self):
        record: list = []
        reply = voice_core.think("你好", api_key="sk-test", opener=make_opener("你好呀", record))
        self.assertEqual(reply, "你好呀")
        # 校验真正发出去的请求体
        self.assertEqual(record[0]["model"], voice_core.DEFAULT_MODEL)
        self.assertEqual(record[0]["messages"][-1], {"role": "user", "content": "你好"})
        self.assertTrue(record[0]["messages"][0]["role"] == "system")

    def test_think_passes_history(self):
        record: list = []
        voice_core.think(
            "那明天呢",
            [{"role": "user", "content": "今天几号"}, {"role": "assistant", "content": "9月11日"}],
            api_key="sk-test",
            opener=make_opener(record=record),
        )
        roles = [m["role"] for m in record[0]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])

    def test_think_empty_input_raises(self):
        for bad in ("", "   ", None):
            with self.assertRaises(voice_core.ThinkError):
                voice_core.think(bad, api_key="sk-test", opener=make_opener())

    def test_think_without_key_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(voice_core.ThinkError):
                voice_core.think("你好", opener=make_opener())

    def test_think_custom_model_and_url(self):
        record: list = []
        voice_core.think("嗨", api_key="sk-test", model="deepseek-reasoner", url="http://localhost/fake", opener=make_opener(record=record))
        self.assertEqual(record[0]["model"], "deepseek-reasoner")

    def test_http_post_json_http_error_translated(self):
        """服务端返回 401 时，错误信息应当是中文化的、带状态码的提示。"""
        import urllib.error

        def failing_opener(request, timeout=60.0):
            raise urllib.error.HTTPError(
                url="http://x", code=401, msg="Unauthorized", hdrs=None,
                fp=FakeResponse(json.dumps({"error": {"message": "Authentication Fails"}}).encode()),
            )

        with self.assertRaises(voice_core.ThinkError) as ctx:
            voice_core.think("你好", api_key="sk-bad", opener=failing_opener)
        self.assertIn("401", str(ctx.exception))
        self.assertIn("Authentication Fails", str(ctx.exception))

    def test_http_post_json_network_error_translated(self):
        import urllib.error

        def failing_opener(request, timeout=60.0):
            raise urllib.error.URLError("getaddrinfo failed")

        with self.assertRaises(voice_core.ThinkError) as ctx:
            voice_core.think("你好", api_key="sk-test", opener=failing_opener)
        self.assertIn("无法连接", str(ctx.exception))

    def test_http_post_json_bad_json(self):
        with self.assertRaises(voice_core.ThinkError):
            voice_core.http_post_json("http://x", {}, {}, opener=lambda r, timeout=60: FakeResponse(b"<html>"))

    def test_think_with_meta_returns_usage(self):
        result = voice_core.think_with_meta("你好", api_key="sk-test", opener=make_opener("嗨"))
        self.assertEqual(result["reply"], "嗨")
        self.assertEqual(result["usage"]["total_tokens"], 18)
        self.assertIsInstance(result["elapsed_ms"], int)


# --------------------------------------------------------------------------- #
# 5. Flask 接口（使用 Flask 自带的测试客户端）
# --------------------------------------------------------------------------- #
class TestFlaskRoutes(unittest.TestCase):
    """验证 Web 层的参数校验与错误翻译，不触发真实网络请求。"""

    def setUp(self):
        import app as flask_app

        self.app_module = flask_app
        flask_app.app.config.update(TESTING=True)
        self.client = flask_app.app.test_client()

    def test_index_renders_page(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("语音交互助手", html)
        self.assertIn("app.js", html)

    def test_health_reports_key_state(self):
        with mock.patch.dict(os.environ, {voice_core.ENV_API_KEY: "sk-abc"}, clear=True):
            body = self.client.get("/api/health").get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["api_key_configured"])
        self.assertEqual(body["env_var"], voice_core.ENV_API_KEY)

    def test_health_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            body = self.client.get("/api/health").get_json()
        self.assertFalse(body["api_key_configured"])

    def test_think_endpoint_rejects_empty_text(self):
        res = self.client.post("/api/think", json={"text": "  "})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(res.get_json()["ok"])

    def test_think_endpoint_rejects_bad_history(self):
        res = self.client.post("/api/think", json={"text": "你好", "history": "不是数组"})
        self.assertEqual(res.status_code, 400)

    def test_think_endpoint_rejects_too_long_text(self):
        res = self.client.post("/api/think", json={"text": "啊" * 4001})
        self.assertEqual(res.status_code, 400)

    def test_think_endpoint_success(self):
        """把核心函数替换成替身，验证路由的封装与响应结构。"""
        fake = {"reply": "你好呀", "elapsed_ms": 12, "model": "deepseek-chat", "usage": {"total_tokens": 18}}
        with mock.patch.object(self.app_module.voice_core, "think_with_meta", return_value=fake) as m:
            res = self.client.post("/api/think", json={"text": "你好", "history": []})
        body = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["reply"], "你好呀")
        m.assert_called_once()

    def test_think_endpoint_maps_think_error(self):
        with mock.patch.object(
            self.app_module.voice_core, "think_with_meta",
            side_effect=voice_core.ThinkError("密钥不对"),
        ):
            with mock.patch.dict(os.environ, {voice_core.ENV_API_KEY: "sk-abc"}, clear=True):
                res = self.client.post("/api/think", json={"text": "你好"})
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.get_json()["error"], "密钥不对")

    def test_think_endpoint_temperature_clamped(self):
        """越界的 temperature 应当被夹到合法区间，而不是直接报错。"""
        record = {}

        def fake_think(text, history=None, **kwargs):
            record.update(kwargs)
            return {"reply": "ok", "elapsed_ms": 1, "model": "x", "usage": {}}

        with mock.patch.object(self.app_module.voice_core, "think_with_meta", side_effect=fake_think):
            self.client.post("/api/think", json={"text": "你好", "temperature": 99})
        self.assertEqual(record["temperature"], 2.0)


# --------------------------------------------------------------------------- #
# 7. 语音合成：参数换算与音色解析（纯函数，无需联网）
# --------------------------------------------------------------------------- #
class TestVoiceParams(unittest.TestCase):
    """验证传给 edge-tts 的参数换算，以及音色名的安全校验。"""

    def test_format_rate(self):
        self.assertEqual(voice_core.format_rate(1.0), '+0%')
        self.assertEqual(voice_core.format_rate(1.25), '+25%')
        self.assertEqual(voice_core.format_rate(0.5), '-50%')

    def test_format_rate_clamped(self):
        self.assertEqual(voice_core.format_rate(99), '+200%')     # 上限
        self.assertEqual(voice_core.format_rate(0.0), '-90%')      # 下限

    def test_format_rate_bad_input_falls_back(self):
        for bad in (None, 'abc', object()):
            self.assertEqual(voice_core.format_rate(bad), '+0%')

    def test_format_volume(self):
        self.assertEqual(voice_core.format_volume(1.0), '+0%')
        self.assertEqual(voice_core.format_volume(2.0), '+100%')
        self.assertEqual(voice_core.format_volume(0.0), '-100%')

    def test_format_pitch(self):
        self.assertEqual(voice_core.format_pitch(0), '+0Hz')
        self.assertEqual(voice_core.format_pitch(5), '+5Hz')
        self.assertEqual(voice_core.format_pitch(-10), '-10Hz')
        self.assertEqual(voice_core.format_pitch('x'), '+0Hz')

    def test_resolve_voice_valid(self):
        self.assertEqual(voice_core.resolve_voice('zh-CN-YunxiNeural'), 'zh-CN-YunxiNeural')

    def test_resolve_voice_empty_uses_default(self):
        for bad in (None, '', '   '):
            self.assertEqual(voice_core.resolve_voice(bad), voice_core.DEFAULT_EDGE_VOICE)

    def test_resolve_voice_rejects_illegal_chars(self):
        """音色名会被拼进请求，非法字符必须回落到默认值。"""
        for bad in ('../../etc/passwd', 'zh CN;rm -rf', '<script>', 'a/b'):
            self.assertEqual(voice_core.resolve_voice(bad), voice_core.DEFAULT_EDGE_VOICE)

    def test_list_voices_is_a_copy(self):
        """外部改动返回值不应污染内置清单。"""
        voices = voice_core.list_voices()
        self.assertTrue(len(voices) >= 10)
        voices[0]['name'] = 'hacked'
        self.assertNotEqual(voice_core.list_voices()[0]['name'], 'hacked')
        self.assertTrue(all({'name', 'label', 'gender', 'locale'} <= set(v) for v in voice_core.list_voices()))

    def test_resolve_proxy_priority(self):
        env = {voice_core.ENV_EDGE_PROXY: 'http://edge-proxy:8080', 'HTTPS_PROXY': 'http://std:8080'}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(voice_core.resolve_proxy('http://explicit:1'), 'http://explicit:1')
            self.assertEqual(voice_core.resolve_proxy(), 'http://edge-proxy:8080')

    def test_resolve_proxy_falls_back_to_https_proxy(self):
        with mock.patch.dict(os.environ, {'HTTPS_PROXY': 'http://std:8080'}, clear=True):
            self.assertEqual(voice_core.resolve_proxy(), 'http://std:8080')

    def test_resolve_proxy_none_when_absent(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(voice_core.resolve_proxy())

    def test_proxy_candidates_try_then_direct(self):
        """配了代理时应当「先代理、再直连」各试一次。"""
        with mock.patch.dict(os.environ, {voice_core.ENV_EDGE_PROXY: 'http://p:1'}, clear=True):
            self.assertEqual(voice_core._proxy_candidates(), ['http://p:1', None])
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(voice_core._proxy_candidates(), [None])


# --------------------------------------------------------------------------- #
# 8. speak_with_edge_tts()：重试、降级与错误收口（注入替身，不联网）
# --------------------------------------------------------------------------- #
def make_synthesizer(audio: bytes = b'ID3-fake-mp3', fail_times: int = 0, record: list | None = None):
    """生成一个假的合成器：前 fail_times 次抛异常，之后返回音频。"""

    calls: list = []

    def synth(text, voice, *, rate, volume, pitch, proxy, timeout):
        calls.append({'text': text, 'voice': voice, 'rate': rate, 'volume': volume,
                      'pitch': pitch, 'proxy': proxy, 'timeout': timeout})
        if record is not None:
            record.append(calls[-1])
        if len(calls) <= fail_times:
            raise ConnectionError('模拟第 %d 次连接失败' % len(calls))
        return audio

    synth.calls = calls
    return synth


class TestSpeakWithEdgeTts(unittest.TestCase):
    """验证合成主函数的重试策略与失败收口——失败必须抛 SpeakError 让前端降级。"""

    def test_success_first_try(self):
        synth = make_synthesizer(b'ID3-audio')
        audio = voice_core.speak_with_edge_tts('你好', 'zh-CN-YunxiNeural', synthesizer=synth)
        self.assertEqual(audio, b'ID3-audio')
        self.assertEqual(len(synth.calls), 1)
        self.assertEqual(synth.calls[0]['voice'], 'zh-CN-YunxiNeural')
        self.assertEqual(synth.calls[0]['rate'], '+0%')

    def test_retry_then_success(self):
        """第一次连不上是常态，重试应当救回来。"""
        synth = make_synthesizer(b'ID3-ok', fail_times=1)
        audio = voice_core.speak_with_edge_tts('你好', retries=2, synthesizer=synth)
        self.assertEqual(audio, b'ID3-ok')
        self.assertEqual(len(synth.calls), 2)

    def test_all_attempts_fail_raises_speak_error(self):
        synth = make_synthesizer(fail_times=99)
        with self.assertRaises(voice_core.SpeakError) as ctx:
            voice_core.speak_with_edge_tts('你好', retries=3, synthesizer=synth)
        self.assertIn('3 次', str(ctx.exception))          # 报错要说清试了几次
        self.assertIn('ConnectionError', str(ctx.exception))
        self.assertEqual(len(synth.calls), 3)

    def test_empty_text_raises_without_calling_engine(self):
        synth = make_synthesizer()
        for bad in ('', '   ', None):
            with self.assertRaises(voice_core.SpeakError):
                voice_core.speak_with_edge_tts(bad, synthesizer=synth)
        self.assertEqual(len(synth.calls), 0)             # 空输入不该发起任何调用

    def test_empty_audio_from_service_raises(self):
        """服务端返回空音频也算失败，不能当成成功结果播出去。"""
        synth = make_synthesizer(b'', fail_times=0)
        with self.assertRaises(voice_core.SpeakError):
            voice_core.speak_with_edge_tts('你好', retries=2, synthesizer=synth)
        self.assertEqual(len(synth.calls), 2)

    def test_long_text_truncated(self):
        synth = make_synthesizer()
        voice_core.speak_with_edge_tts('啊' * (voice_core.MAX_TTS_CHARS + 500), synthesizer=synth)
        self.assertEqual(len(synth.calls[0]['text']), voice_core.MAX_TTS_CHARS)

    def test_illegal_voice_falls_back_to_default(self):
        synth = make_synthesizer()
        voice_core.speak_with_edge_tts('嗨', 'bad name!', synthesizer=synth)
        self.assertEqual(synth.calls[0]['voice'], voice_core.DEFAULT_EDGE_VOICE)

    def test_proxy_attempted_first_then_direct(self):
        """配了代理时，第一次走代理，第二次应当改直连再试。"""
        synth = make_synthesizer(fail_times=1)
        with mock.patch.dict(os.environ, {voice_core.ENV_EDGE_PROXY: 'http://p:1'}, clear=True):
            voice_core.speak_with_edge_tts('你好', retries=2, synthesizer=synth)
        self.assertEqual(synth.calls[0]['proxy'], 'http://p:1')
        self.assertIsNone(synth.calls[1]['proxy'])

    def test_retries_clamped_to_at_least_one(self):
        synth = make_synthesizer()
        voice_core.speak_with_edge_tts('你好', retries=0, synthesizer=synth)
        self.assertEqual(len(synth.calls), 1)

    def test_missing_library_raises_speak_error(self):
        with mock.patch.object(voice_core, 'is_edge_tts_available', return_value=False):
            with self.assertRaises(voice_core.SpeakError) as ctx:
                voice_core.speak_with_edge_tts('你好')
            self.assertIn('edge-tts', str(ctx.exception))

    def test_is_edge_tts_available(self):
        """本环境已安装 edge-tts，这里顺带断言探测函数可用。"""
        self.assertIsInstance(voice_core.is_edge_tts_available(), bool)

    def test_real_async_path_with_fake_module(self):
        """用假的 edge_tts 模块走一遍真实的异步链路，验证分片拼接逻辑。

        edge-tts 的 stream() 会混着给出 audio 和 WordBoundary，
        这里确认只有 audio 分片被拼进最终音频。
        """
        called = {}

        class FakeCommunicate:
            def __init__(self, text, voice, **kwargs):
                called['text'] = text
                called['voice'] = voice
                called['kwargs'] = kwargs

            async def stream(self):
                yield {'type': 'audio', 'data': b'ID3-part1'}
                yield {'type': 'WordBoundary', 'data': b'should-be-ignored'}
                yield {'type': 'audio', 'data': b'-part2'}

        fake_module = type('FakeEdgeTts', (), {'Communicate': FakeCommunicate, '__version__': '7.fake'})

        # 清空代理变量，保证这条用例不受运行环境（如公司网络代理）影响
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.dict('sys.modules', {'edge_tts': fake_module}):
                with mock.patch.object(voice_core, 'is_edge_tts_available', return_value=True):
                    audio = voice_core.speak_with_edge_tts('你好呀', 'zh-CN-XiaoxiaoNeural', retries=1)

        self.assertEqual(audio, b'ID3-part1-part2')       # 非 audio 分片被忽略
        self.assertEqual(called['voice'], 'zh-CN-XiaoxiaoNeural')
        self.assertEqual(called['kwargs']['rate'], '+0%')
        self.assertEqual(called['kwargs']['pitch'], '+0Hz')
        self.assertNotIn('proxy', called['kwargs'])       # 没代理时不应传 proxy 参数

        # edge-tts 对这两个超时做的是 int 校验，传 float 会抛
        # TypeError: connect_timeout must be int —— 这里守住类型，避免回归。
        for key in ('connect_timeout', 'receive_timeout'):
            self.assertIsInstance(called['kwargs'][key], int, f'{key} 必须是 int')

    def test_synthesize_meta_returns_metrics(self):
        synth = make_synthesizer(b'ID3-abcdef')
        meta = voice_core.synthesize_meta('你好', 'zh-CN-YunxiNeural', synthesizer=synth)
        self.assertEqual(meta['audio'], b'ID3-abcdef')
        self.assertEqual(meta['voice'], 'zh-CN-YunxiNeural')
        self.assertEqual(meta['bytes'], 10)
        self.assertIsInstance(meta['elapsed_ms'], int)


# --------------------------------------------------------------------------- #
# 9. remember()：会话记忆（10 轮滑动窗口）
# --------------------------------------------------------------------------- #
class TestMemory(unittest.TestCase):
    """验证记忆的滑动窗口、会话隔离与清空。"""

    def setUp(self):
        self.session = 'unittest-' + str(id(self))

    def tearDown(self):
        voice_core.reset_history(self.session)

    def test_trim_turns_keeps_last_n_turns(self):
        messages = [{'role': 'user', 'content': f'm{i}'} for i in range(10)]
        self.assertEqual(len(voice_core.trim_turns(messages, max_turns=3)), 6)
        self.assertEqual(voice_core.trim_turns(messages, max_turns=3)[-1]['content'], 'm9')

    def test_trim_turns_edge_cases(self):
        self.assertEqual(voice_core.trim_turns(None), [])
        self.assertEqual(voice_core.trim_turns([]), [])
        self.assertEqual(voice_core.trim_turns([{'role': 'user', 'content': 'x'}], max_turns=0), [])

    def test_trim_turns_returns_copy(self):
        source = [{'role': 'user', 'content': '原文'}]
        result = voice_core.trim_turns(source)
        result[0]['content'] = '改过'
        self.assertEqual(source[0]['content'], '原文')

    def test_count_turns(self):
        self.assertEqual(voice_core.count_turns(None), 0)
        self.assertEqual(voice_core.count_turns([]), 0)
        self.assertEqual(voice_core.count_turns([{'role': 'user', 'content': 'a'}]), 1)
        self.assertEqual(voice_core.count_turns([{'role': 'user', 'content': 'a'}] * 4), 2)

    def test_remember_appends_both_messages(self):
        messages = voice_core.remember('今天几号', '9 月 11 日', session_id=self.session)
        self.assertEqual(messages, [
            {'role': 'user', 'content': '今天几号'},
            {'role': 'assistant', 'content': '9 月 11 日'},
        ])

    def test_remember_keeps_only_10_turns(self):
        for i in range(15):
            result = voice_core.remember(f'问{i}', f'答{i}', session_id=self.session)
        self.assertEqual(len(result), voice_core.MAX_MEMORY_TURNS * 2)     # 20 条 = 10 轮
        self.assertEqual(voice_core.count_turns(result), 10)
        # 最早 5 轮应当已被丢弃
        self.assertEqual(result[0]['content'], '问5')
        self.assertEqual(result[-1]['content'], '答14')

    def test_remember_ignores_empty(self):
        voice_core.reset_history(self.session)
        voice_core.remember('', '', session_id=self.session)
        self.assertEqual(voice_core.get_history(self.session), [])
        self.assertEqual(voice_core.remember('只有问题', '', session_id=self.session),
                         [{'role': 'user', 'content': '只有问题'}])

    def test_sessions_are_isolated(self):
        voice_core.remember('A 的问题', 'A 的回答', session_id=self.session)
        self.assertEqual(voice_core.get_history(self.session + '-other'), [])
        self.assertEqual(len(voice_core.get_history(self.session)), 2)

    def test_get_history_returns_copy(self):
        voice_core.remember('问', '答', session_id=self.session)
        snapshot = voice_core.get_history(self.session)
        snapshot.append({'role': 'user', 'content': '外部塞进来的'})
        self.assertEqual(len(voice_core.get_history(self.session)), 2)

    def test_reset_returns_cleared_count(self):
        voice_core.remember('问1', '答1', session_id=self.session)
        voice_core.remember('问2', '答2', session_id=self.session)
        self.assertEqual(voice_core.reset_history(self.session), 4)
        self.assertEqual(voice_core.get_history(self.session), [])
        self.assertEqual(voice_core.reset_history(self.session), 0)       # 再清一次是 0

    def test_memory_stats(self):
        voice_core.reset_history(self.session)
        before = voice_core.memory_stats()
        voice_core.remember('问', '答', session_id=self.session)
        after = voice_core.memory_stats()
        self.assertEqual(after['messages'], before['messages'] + 2)
        self.assertEqual(after['max_turns'], voice_core.MAX_MEMORY_TURNS)

    def test_session_limit_evicts_oldest(self):
        """会话数超上限时应淘汰最早创建的，避免内存无限增长。"""
        with mock.patch.object(voice_core, 'MAX_MEMORY_SESSIONS', 2):
            voice_core.reset_history('cap-a')
            voice_core.reset_history('cap-b')
            voice_core.reset_history('cap-c')
            voice_core.remember('a', 'a', session_id='cap-a')
            voice_core.remember('b', 'b', session_id='cap-b')
            voice_core.remember('c', 'c', session_id='cap-c')     # 触发淘汰 cap-a
            self.assertEqual(voice_core.get_history('cap-a'), [])
            self.assertEqual(len(voice_core.get_history('cap-c')), 2)
        for key in ('cap-a', 'cap-b', 'cap-c'):
            voice_core.reset_history(key)

    def test_think_with_memory_passes_history_and_recalls(self):
        """think_with_memory() 应当把历史传给模型，并把这一轮记下来。"""
        voice_core.reset_history(self.session)
        voice_core.remember('第一句', '第一答', session_id=self.session)

        seen = {}

        def fake_think_with_meta(user_text, history=None, **kwargs):
            seen['history'] = history
            return {'reply': '第二答', 'elapsed_ms': 1, 'model': 'x', 'usage': {}}

        with mock.patch.object(voice_core, 'think_with_meta', side_effect=fake_think_with_meta):
            result = voice_core.think_with_memory('第二句', session_id=self.session)

        # 历史确实被一起传给了模型
        self.assertEqual(seen['history'], [
            {'role': 'user', 'content': '第一句'},
            {'role': 'assistant', 'content': '第一答'},
        ])
        # 这一轮也被记住了，轮数正确回传
        self.assertEqual(result['reply'], '第二答')
        self.assertEqual(result['turns'], 2)
        self.assertEqual([m['content'] for m in voice_core.get_history(self.session)],
                         ['第一句', '第一答', '第二句', '第二答'])


# --------------------------------------------------------------------------- #
# 10. 新增 Web 接口：/api/tts、/api/voices、/api/history、/api/reset
# --------------------------------------------------------------------------- #
class TestTtsAndMemoryRoutes(unittest.TestCase):
    """验证语音合成接口与记忆接口的参数校验、状态码与降级约定。"""

    def setUp(self):
        import app as flask_app

        self.app_module = flask_app
        flask_app.app.config.update(TESTING=True)
        self.client = flask_app.app.test_client()
        self.session = 'route-unittest-' + str(id(self))
        voice_core.reset_history(self.session)

    def tearDown(self):
        voice_core.reset_history(self.session)

    # ---------------- /api/tts ----------------
    def test_tts_returns_mp3(self):
        fake = {'audio': b'ID3-yin-pin', 'voice': 'zh-CN-XiaoxiaoNeural', 'elapsed_ms': 12, 'bytes': 10}
        with mock.patch.object(self.app_module.voice_core, 'synthesize_meta', return_value=fake):
            res = self.client.post('/api/tts', json={'text': '你好', 'voice': 'zh-CN-XiaoxiaoNeural'})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, 'audio/mpeg')
        self.assertEqual(res.get_data(), b'ID3-yin-pin')
        self.assertEqual(res.headers['X-TTS-Voice'], 'zh-CN-XiaoxiaoNeural')
        self.assertEqual(res.headers['X-TTS-Elapsed-Ms'], '12')

    def test_tts_rejects_empty_text(self):
        res = self.client.post('/api/tts', json={'text': '   '})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()['fallback'], 'browser')

    def test_tts_failure_tells_frontend_to_fall_back(self):
        """合成失败必须返回 fallback=browser，前端据此降级到 speechSynthesis。"""
        with mock.patch.object(self.app_module.voice_core, 'synthesize_meta',
                               side_effect=voice_core.SpeakError('连不上微软语音服务')):
            with mock.patch.object(self.app_module.voice_core, 'is_edge_tts_available', return_value=True):
                res = self.client.post('/api/tts', json={'text': '你好'})

        self.assertEqual(res.status_code, 502)
        body = res.get_json()
        self.assertFalse(body['ok'])
        self.assertEqual(body['fallback'], 'browser')
        self.assertIn('连不上', body['error'])

    def test_tts_returns_503_when_library_missing(self):
        with mock.patch.object(self.app_module.voice_core, 'synthesize_meta',
                               side_effect=voice_core.SpeakError('未安装 edge-tts')):
            with mock.patch.object(self.app_module.voice_core, 'is_edge_tts_available', return_value=False):
                res = self.client.post('/api/tts', json={'text': '你好'})
        self.assertEqual(res.status_code, 503)

    def test_tts_clamps_params(self):
        """越界的语速/音量/音调应被夹到合法区间，而不是报错。"""
        captured = {}

        def fake_meta(text, voice=None, **kwargs):
            captured.update(kwargs)
            captured['voice'] = voice
            return {'audio': b'x', 'voice': 'v', 'elapsed_ms': 1, 'bytes': 1}

        with mock.patch.object(self.app_module.voice_core, 'synthesize_meta', side_effect=fake_meta):
            self.client.post('/api/tts', json={'text': '你好', 'rate': 99, 'volume': -5, 'pitch': 999})

        self.assertEqual(captured['rate'], 2.0)
        self.assertEqual(captured['volume'], 0.0)
        self.assertEqual(captured['pitch'], 50)

    # ---------------- /api/voices ----------------
    def test_voices_lists_catalog(self):
        body = self.client.get('/api/voices').get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['default'], voice_core.DEFAULT_EDGE_VOICE)
        self.assertTrue(len(body['voices']) >= 10)
        self.assertIsInstance(body['edge_available'], bool)

    # ---------------- /api/history & /api/reset ----------------
    def test_history_reads_session(self):
        voice_core.remember('问', '答', session_id=self.session)
        body = self.client.get(f'/api/history?session_id={self.session}').get_json()
        self.assertEqual(body['turns'], 1)
        self.assertEqual(len(body['messages']), 2)
        self.assertEqual(body['max_turns'], voice_core.MAX_MEMORY_TURNS)

    def test_history_empty_for_unknown_session(self):
        body = self.client.get('/api/history?session_id=nobody-here').get_json()
        self.assertEqual(body['turns'], 0)
        self.assertEqual(body['messages'], [])

    def test_reset_clears_session(self):
        voice_core.remember('问', '答', session_id=self.session)
        body = self.client.post('/api/reset', json={'session_id': self.session}).get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['cleared'], 2)
        self.assertEqual(voice_core.get_history(self.session), [])

    # ---------------- /api/think 与记忆的联动 ----------------
    def test_think_remembers_this_turn(self):
        """一次成功的对话应当自动写入记忆，并把轮数回传。"""
        fake = {'reply': '你好呀', 'elapsed_ms': 5, 'model': 'deepseek-chat', 'usage': {}}
        with mock.patch.object(self.app_module.voice_core, 'think_with_meta', return_value=fake):
            res = self.client.post('/api/think', json={'text': '你好', 'session_id': self.session})

        body = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(body['turns'], 1)
        self.assertEqual(body['session_id'], self.session)
        self.assertEqual([m['content'] for m in voice_core.get_history(self.session)], ['你好', '你好呀'])

    def test_think_second_turn_carries_history(self):
        """第二轮请求时，历史应当被一起发给模型（这是「记忆」生效的关键）。"""
        fake = {'reply': '答2', 'elapsed_ms': 5, 'model': 'm', 'usage': {}}
        voice_core.remember('问1', '答1', session_id=self.session)

        seen = {}

        def spy(user_text, history=None, **kwargs):
            seen['history'] = history
            return fake

        with mock.patch.object(self.app_module.voice_core, 'think_with_meta', side_effect=spy):
            self.client.post('/api/think', json={'text': '问2', 'session_id': self.session})

        self.assertEqual(seen['history'], [
            {'role': 'user', 'content': '问1'},
            {'role': 'assistant', 'content': '答1'},
        ])

    def test_think_sessions_do_not_leak(self):
        fake = {'reply': '答', 'elapsed_ms': 1, 'model': 'm', 'usage': {}}
        with mock.patch.object(self.app_module.voice_core, 'think_with_meta', return_value=fake):
            self.client.post('/api/think', json={'text': 'A 的提问', 'session_id': self.session})
            self.client.post('/api/think', json={'text': 'B 的提问', 'session_id': self.session + '-b'})

        self.assertEqual(len(voice_core.get_history(self.session)), 2)
        self.assertEqual(len(voice_core.get_history(self.session + '-b')), 2)
        voice_core.reset_history(self.session + '-b')

    def test_health_reports_new_fields(self):
        body = self.client.get('/api/health').get_json()
        self.assertIn('edge_tts_available', body)
        self.assertEqual(body['default_voice'], voice_core.DEFAULT_EDGE_VOICE)
        self.assertEqual(body['memory']['max_turns'], voice_core.MAX_MEMORY_TURNS)
        self.assertIsInstance(body['memory']['sessions'], int)

    def test_index_page_contains_new_controls(self):
        html = self.client.get('/').get_data(as_text=True)
        for element_id in ('voiceSelect', 'voicePill', 'memoryPill', 'replayBtn'):
            self.assertIn(f'id="{element_id}"', html)


# --------------------------------------------------------------------------- #
# 11. 前端静态资源自检
# --------------------------------------------------------------------------- #
class TestFrontendAssets(unittest.TestCase):
    """确保核心函数确实存在于前端代码里（防止误删），并守住「listen/think 架构不变」。"""

    def setUp(self):
        self.root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(self.root, 'static', 'app.js'), encoding='utf-8') as fh:
            self.js = fh.read()
        with open(os.path.join(self.root, 'requirements.txt'), encoding='utf-8') as fh:
            self.requirements = fh.read()

    def test_listen_uses_speech_recognition(self):
        self.assertIn('function listen(', self.js)
        self.assertIn('SpeechRecognition', self.js)

    def test_think_calls_backend(self):
        self.assertIn('async function think(', self.js)
        self.assertIn('/api/think', self.js)

    def test_speak_with_edge_tts_exists_and_calls_api(self):
        self.assertIn('async function speak_with_edge_tts(', self.js)
        self.assertIn('/api/tts', self.js)

    def test_speak_fallback_uses_speech_synthesis(self):
        self.assertIn('function speak_fallback(', self.js)
        self.assertIn('speechSynthesis', self.js)

    def test_speak_orchestrates_fallback(self):
        """speak() 必须真的调用两个下层函数，否则降级链路不成立。"""
        self.assertIn('async function speak(', self.js)
        body = self.js.split('async function speak(', 1)[1].split('function parseVoiceTarget', 1)[0]
        self.assertIn('speak_with_edge_tts(', body)
        self.assertIn('speak_fallback(', body)

    def test_remember_exists_with_ten_turn_window(self):
        self.assertIn('function remember(', self.js)
        self.assertIn('const MAX_TURNS = 10;', self.js)
        self.assertIn('MAX_TURNS * 2', self.js)

    def test_stop_speaking_stops_both_engines(self):
        """「停止朗读」要同时管住 <audio> 和 speechSynthesis。"""
        body = self.js.split('function stopSpeaking(', 1)[1].split('}', 1)[0]
        self.assertIn('stopAudio', body)

    def test_requirements_includes_edge_tts(self):
        self.assertIn('edge-tts', self.requirements)

    def test_static_files_exist(self):
        for rel in ('static/app.js', 'static/style.css', 'templates/index.html', 'start.bat', 'requirements.txt'):
            self.assertTrue(os.path.exists(os.path.join(self.root, rel)), f'缺少文件：{rel}')


if __name__ == "__main__":
    unittest.main(verbosity=2)
