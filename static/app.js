/* ==========================================================================
 * app.js —— 前端语音交互逻辑
 *
 * 架构：函数式，一个功能一个独立函数，互相之间只通过参数/返回值通信。
 *
 *      listen()              麦克风录音 + 语音识别（浏览器 SpeechRecognition API，架构未改动）
 *      think()               把文字发给后端 /api/think，拿回大模型回答（架构未改动）
 *      speak_with_edge_tts() 向后端 /api/tts 要 MP3，用 <audio> 播放（音质更好，可指定音色）
 *      speak_fallback()      浏览器 speechSynthesis 朗读（edge-tts 失败时的保底）
 *      speak()               朗读总入口：优先 edge-tts，失败自动降级到 speak_fallback()
 *      remember()            维护 messages 列表（前端镜像），只保留最近 10 轮
 *      converse()            把上面几步串起来，构成一轮完整对话
 *
 * 降级链路（重点）：
 *      speak() ──> speak_with_edge_tts() ──失败──> speak_fallback() ──再失败──> 静默跳过
 *
 * 每个函数都可以在浏览器控制台里单独调用测试：
 *      await listen()                                        // 识别一句话
 *      await think('你好')                                    // 只问不读
 *      await speak_with_edge_tts('测试', 'zh-CN-XiaoxiaoNeural')  // 指定音色
 *      await speak_fallback('测试')                           // 只走浏览器朗读
 *      await speak('测试')                                    // 带降级的完整朗读
 *      remember('你好', '你也好')                              // 手动记一轮
 * ========================================================================== */

'use strict';

/* --------------------------------------------------------------------------
 * 0. 全局状态与 DOM 引用
 * ------------------------------------------------------------------------ */

/** 记忆保留的对话轮数，必须与后端 voice_core.MAX_MEMORY_TURNS 保持一致 */
const MAX_TURNS = 10;

/** 音色下拉框里表示「走哪条朗读链路」的前缀 */
const VOICE_PREFIX_EDGE = 'edge:';
const VOICE_PREFIX_BROWSER = 'browser:';

/** 应用运行状态（集中存放，便于阅读；不参与函数间通信） */
const state = {
  history: [],            // 对话历史的「前端镜像」：[{role, content}]（真正的记忆在后端）
  sessionId: '',          // 会话 ID，服务端按它隔离记忆
  recognition: null,      // 当前正在运行的 SpeechRecognition 实例
  listening: false,       // 是否正在录音
  busy: false,            // 是否正在思考/朗读（防止重入）
  browserVoices: [],      // 浏览器可用语音列表
  edgeVoices: [],         // 服务端返回的 edge-tts 音色列表
  edgeAvailable: false,   // 服务端 edge-tts 是否可用
  defaultVoice: '',       // 服务端建议的默认音色
  currentAudio: null,     // 当前正在播放的 <audio>
  stopAudio: null,        // 由 playAudioUrl() 注册的中断回调，供「停止朗读」调用
  currentUtterance: null, // 当前朗读的 utterance（浏览器保底链路）
  lastReply: '',          // 上一条回答，供「重新朗读」用
  fallbackWarned: false,  // 降级提示只弹一次，避免刷屏
};

const $ = (id) => document.getElementById(id);

const el = {
  chat: $('chat'),
  empty: $('emptyState'),
  micBtn: $('micBtn'),
  micLabel: $('micLabel'),
  textInput: $('textInput'),
  sendBtn: $('sendBtn'),
  stopSpeakBtn: $('stopSpeakBtn'),
  replayBtn: $('replayBtn'),
  clearBtn: $('clearBtn'),
  voiceSelect: $('voiceSelect'),
  rateRange: $('rateRange'),
  rateValue: $('rateValue'),
  autoSpeak: $('autoSpeak'),
  continuousMode: $('continuousMode'),
  statusDot: $('statusDot'),
  statusText: $('statusText'),
  statusExtra: $('statusExtra'),
  keyPill: $('keyPill'),
  voicePill: $('voicePill'),
  memoryPill: $('memoryPill'),
  alertBox: $('alertBox'),
};

/* --------------------------------------------------------------------------
 * 1. 基础工具函数
 * ------------------------------------------------------------------------ */

/** 把 HH:MM:SS 时间戳格式化成 HH:MM */
function formatTime(date = new Date()) {
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
}

/** 转义 HTML，避免把外部文本（错误原因等）直接塞进 innerHTML */
function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** 更新底部状态栏；kind ∈ idle | listening | thinking | speaking | error */
function setStatus(text, kind = 'idle', extra = '') {
  el.statusText.textContent = text;
  el.statusDot.className = 'status-dot' + (kind === 'idle' ? '' : ' ' + kind);
  el.statusExtra.textContent = extra;
}

/** 显示顶部提示条；level ∈ info | warn | error */
function showAlert(html, level = 'info') {
  el.alertBox.className = 'alert ' + level;
  el.alertBox.innerHTML = html;
  el.alertBox.hidden = false;
}

/** 隐藏顶部提示条 */
function hideAlert() {
  el.alertBox.hidden = true;
}

/**
 * 往对话区添加一条消息气泡。
 * @param {'user'|'ai'} role
 * @param {string} content
 * @param {{meta?: string, isError?: boolean, isTyping?: boolean}} [opts]
 * @returns {HTMLElement} 气泡的 .bubble 元素，便于后续更新内容
 */
function appendMessage(role, content, opts = {}) {
  el.empty && (el.empty.hidden = true);

  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'user' ? '🙋' : '🤖';

  const bubble = document.createElement('div');
  bubble.className = 'bubble' + (opts.isError ? ' error' : '');

  const body = document.createElement('span');
  body.className = 'bubble-body';
  if (opts.isTyping) {
    body.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
  } else {
    body.textContent = content;
  }

  const meta = document.createElement('span');
  meta.className = 'meta';
  meta.textContent = opts.meta || formatTime();

  bubble.appendChild(body);
  bubble.appendChild(meta);
  wrap.appendChild(avatar);
  wrap.appendChild(bubble);
  el.chat.appendChild(wrap);
  scrollToBottom();

  return bubble;
}

/** 把某个气泡的内容替换掉（用于把「思考中」替换成真正的回答） */
function updateBubble(bubbleEl, content, meta) {
  const body = bubbleEl.querySelector('.bubble-body');
  if (body) body.textContent = content;
  if (meta) bubbleEl.querySelector('.meta').textContent = meta;
  scrollToBottom();
}

/** 滚动到底部 */
function scrollToBottom() {
  requestAnimationFrame(() => {
    el.chat.scrollTop = el.chat.scrollHeight;
  });
}

/** 给按钮设置禁用态并同步文案 */
function setMicButton(recording) {
  el.micBtn.classList.toggle('recording', recording);
  el.micLabel.textContent = recording ? '停止录音' : '按住说话';
  el.micBtn.querySelector('.mic-icon').textContent = recording ? '⏹' : '🎤';
}

/* ==========================================================================
 * 2. listen() —— 麦克风录音 + 语音识别
 * ========================================================================== */

/** 检测浏览器是否支持语音识别 */
function isSpeechRecognitionSupported() {
  return Boolean(window.SpeechRecognition || window.webkitSpeechRecognition);
}

/**
 * 录音并做语音识别。
 *
 * 使用浏览器原生 SpeechRecognition：
 *   - 内部会自动申请麦克风权限（首次使用浏览器会弹窗）
 *   - 识别在云端完成，需要联网
 *   - 必须在 localhost 或 https 下才能拿到麦克风权限
 *
 * @param {object}  [options]
 * @param {string}  [options.lang='zh-CN']   识别语言
 * @param {boolean} [options.interim=true]   是否回调中间结果（边说边显示）
 * @param {number}  [options.timeout=12000]  最长录音毫秒数，超时自动停止
 * @param {boolean} [options.continuous=false] 是否持续识别（连续对话模式用）
 * @param {Function}[options.onPartial]      中间结果回调 (text) => void
 * @param {Function}[options.onStart]        开始录音回调
 * @param {Function}[options.onEnd]          录音结束回调
 *
 * @returns {Promise<string>} 识别出的最终文字（识别不到内容时返回空字符串）
 * @throws {Error} 浏览器不支持 / 麦克风被拒绝 / 网络异常 / 未检测到语音
 */
function listen(options = {}) {
  const {
    lang = 'zh-CN',
    interim = true,
    timeout = 12000,
    continuous = false,
    onPartial,
    onStart,
    onEnd,
  } = options;

  return new Promise((resolve, reject) => {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) {
      reject(new Error('当前浏览器不支持语音识别，请使用 Chrome 或 Edge。'));
      return;
    }

    // --- 每次识别都新建实例：SpeechRecognition 实例不可重复 start ---
    const recognition = new Recognition();
    recognition.lang = lang;
    recognition.interimResults = interim;
    recognition.continuous = continuous;
    recognition.maxAlternatives = 1;

    let finalText = '';      // 已确认的识别结果
    let settled = false;     // 防止 resolve/reject 被触发两次
    let timer = null;

    const cleanup = () => {
      if (timer) clearTimeout(timer);
      state.recognition = null;
      state.listening = false;
      setMicButton(false);
      onEnd && onEnd();
    };

    const fail = (err) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(err);
    };

    const done = (text) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(text);
    };

    // 录音过程中的中间/最终结果
    recognition.onresult = (event) => {
      let interimText = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        const transcript = result[0].transcript;
        if (result.isFinal) {
          finalText += transcript;
        } else {
          interimText += transcript;
        }
      }
      const shown = (finalText + interimText).trim();
      if (shown) onPartial && onPartial(shown);
    };

    recognition.onerror = (event) => {
      const messages = {
        'not-allowed': '麦克风权限被拒绝。请点击地址栏的锁图标，允许本站使用麦克风后重试。',
        'service-not-allowed': '浏览器禁止了语音识别服务，请检查浏览器设置。',
        'no-speech': '没有检测到语音，请靠近麦克风再试一次。',
        'audio-capture': '没有找到可用的麦克风设备。',
        'network': '语音识别需要联网，请检查网络连接。',
        'aborted': '',   // 用户主动停止，不算错误
      };
      const msg = messages[event.error];
      if (event.error === 'aborted' || msg === '') {
        done(finalText.trim());       // 主动停止时，把已有结果返回
      } else {
        fail(new Error(msg || `语音识别失败：${event.error}`));
      }
    };

    // 识别自然结束（用户停顿）
    recognition.onend = () => {
      if (continuous) {
        // 连续模式：交给上层控制，直接返回当前结果
        done(finalText.trim());
      } else {
        done(finalText.trim());
      }
    };

    // --- 启动 ---
    try {
      recognition.start();
    } catch (err) {
      fail(new Error('无法启动录音，可能上一次录音还没结束：' + err.message));
      return;
    }

    state.recognition = recognition;
    state.listening = true;
    setMicButton(true);
    onStart && onStart();

    // 超时保护：避免用户忘了说话导致一直挂着
    if (timeout > 0) {
      timer = setTimeout(() => {
        try { recognition.stop(); } catch (_) { /* 已结束则忽略 */ }
        if (!finalText.trim()) {
          fail(new Error('录音超时，没有检测到语音。'));
        }
      }, timeout);
    }
  });
}

/** 主动停止当前录音（把已识别到的内容作为结果返回） */
function stopListening() {
  if (state.recognition) {
    try { state.recognition.stop(); } catch (_) { /* 忽略 */ }
  }
}

/* ==========================================================================
 * 3. think() —— 调用后端大模型接口
 * ========================================================================== */

/**
 * 把用户提问发给后端 /api/think，取回大模型的文字回答。
 *
 * @param {string} text           用户提问
 * @param {Array}  [history=[]]   历史对话 [{{role, content}}, ...]
 * @returns {Promise<{reply: string, elapsed_ms: number, model: string}>}
 * @throws {Error} 网络失败或后端返回错误时抛出（message 已是中文）
 */
async function think(text, history = []) {
  const response = await fetch('/api/think', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      text,
      history,                      // 记忆升级：把最近的对话历史一起传给后端
      session_id: getSessionId(),   // 后端按会话 ID 维护权威记忆（10 轮滑动窗口）
      temperature: 1.0,
    }),
  });

  let data = null;
  try {
    data = await response.json();
  } catch (_) {
    throw new Error(`服务端返回了非法响应（HTTP ${response.status}）。`);
  }

  if (!response.ok || !data.ok) {
    throw new Error((data && data.error) || `请求失败（HTTP ${response.status}）。`);
  }
  return data;
}

/* ==========================================================================
 * 4. speak_with_edge_tts() / speak_fallback() / speak() —— 文字转语音
 * ==========================================================================
 * 朗读一共三层，职责分明：
 *   speak_with_edge_tts()  首选：后端 edge-tts 合成 MP3，音质好、音色可选
 *   speak_fallback()       保底：浏览器 speechSynthesis，不联网也能出声
 *   speak()                总入口：先试首选，失败自动降级到保底
 * ========================================================================== */

/** 浏览器是否支持语音合成（保底链路的能力检测） */
function isSpeechSynthesisSupported() {
  return 'speechSynthesis' in window;
}

/** 拉取并缓存浏览器可用语音列表（部分浏览器是异步加载的） */
function loadVoices() {
  if (!isSpeechSynthesisSupported()) return [];
  const voices = window.speechSynthesis.getVoices() || [];
  state.browserVoices = voices;
  return voices;
}

/**
 * 挑选一个最合适的中文语音（仅保底链路使用）。
 * 优先级：用户手动选择 > 中文语音 > 系统默认。
 */
function pickVoice(preferredURI) {
  const voices = state.browserVoices.length ? state.browserVoices : loadVoices();
  if (preferredURI) {
    const hit = voices.find((v) => v.voiceURI === preferredURI);
    if (hit) return hit;
  }
  // 优先找中文（含普通话）
  const zh = voices.find((v) => /^zh(-|_)?(CN|Hans)?/i.test(v.lang));
  return zh || voices[0] || null;
}

/**
 * 播放一段音频 URL，播放结束或被中断时收尾。
 *
 * 单独抽出来是为了让 speak_with_edge_tts() 保持线性易读；
 * 同时注册 state.stopAudio，让「停止朗读」能真正打断播放，
 * 而不是留下一个永远不 resolve 的 Promise。
 *
 * @param {string} url 由 Blob 生成的临时对象 URL
 * @returns {Promise<boolean>}
 */
function playAudioUrl(url) {
  return new Promise((resolve, reject) => {
    const audio = new Audio(url);
    let settled = false;

    const finish = (err) => {
      if (settled) return;          // 防止 ended / 主动停止 重复收尾
      settled = true;
      state.currentAudio = null;
      state.stopAudio = null;
      if (err) reject(err); else resolve(true);
    };

    audio.onended = () => finish(null);
    audio.onerror = () => finish(new Error('音频解码或播放失败。'));

    // 供 stopSpeaking() 调用：主动中断也算正常结束
    state.stopAudio = () => {
      try { audio.pause(); } catch (_) { /* 已经停了就忽略 */ }
      finish(null);
    };

    state.currentAudio = audio;
    // 若被浏览器的「自动播放策略」拦下，这里会抛错，交给 speak() 去降级
    audio.play().catch((e) => finish(new Error('浏览器阻止了自动播放：' + e.message)));
  });
}

/**
 * 【首选】调用后端 /api/tts，用 edge-tts 合成语音并播放。
 *
 * 支持传入音色名参数，例如 'zh-CN-XiaoxiaoNeural'（晓晓）、
 * 'zh-CN-YunxiNeural'（云希）等，完整清单见 GET /api/voices。
 *
 * @param {string} text                要朗读的文字
 * @param {string} [voiceName='']      edge-tts 音色名，留空则后端用默认音色
 * @param {object} [options]
 * @param {number} [options.rate=1]    语速倍率 0.5~2
 * @param {number} [options.volume=1]  音量倍率
 * @param {number} [options.pitch=0]   音调偏移（Hz）
 * @returns {Promise<{voice: string, elapsedMs: number}>}
 * @throws {Error} 网络不通 / 合成失败 / 播放被拒 —— 调用方（speak）据此降级
 */
async function speak_with_edge_tts(text, voiceName = '', options = {}) {
  const content = (text || '').trim();
  if (!content) throw new Error('没有需要朗读的内容。');

  const { rate = 1, volume = 1, pitch = 0 } = options;

  const response = await fetch('/api/tts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: content, voice: voiceName || null, rate, volume, pitch }),
  });

  if (!response.ok) {
    // 后端会用 fallback: 'browser' 明确告诉我们该降级了
    let message = `语音合成失败（HTTP ${response.status}）`;
    try {
      const data = await response.json();
      if (data && data.error) message = data.error;
    } catch (_) { /* 响应不是 JSON，用默认提示 */ }
    throw new Error(message);
  }

  const blob = await response.blob();
  if (!blob.size) throw new Error('服务端返回了空音频。');

  const url = URL.createObjectURL(blob);
  try {
    await playAudioUrl(url);
  } finally {
    URL.revokeObjectURL(url);   // 播放结束就释放，避免内存泄漏
  }

  return {
    voice: response.headers.get('X-TTS-Voice') || voiceName,
    elapsedMs: Number(response.headers.get('X-TTS-Elapsed-Ms') || 0),
  };
}

/**
 * 【保底】用浏览器原生 speechSynthesis 朗读：不联网、不需要密钥、没有额外依赖。
 * edge-tts 不可用时由 speak() 自动调用，也可以在控制台单独调用测试。
 *
 * @param {string} text  要朗读的文字
 * @param {object} [options]
 * @param {number} [options.rate=1]       语速 0.5~2
 * @param {number} [options.pitch=1]      音调 0~2
 * @param {number} [options.volume=1]     音量 0~1
 * @param {string} [options.lang='zh-CN'] 语言
 * @param {string} [options.voiceURI]     指定浏览器语音
 * @returns {Promise<boolean>} 是否朗读成功（浏览器不支持时返回 false，不抛错）
 */
function speak_fallback(text, options = {}) {
  const { rate = 1, pitch = 1, volume = 1, lang = 'zh-CN', voiceURI = null } = options;

  return new Promise((resolve) => {
    const content = (text || '').trim();
    if (!content) { resolve(false); return; }

    if (!isSpeechSynthesisSupported()) {
      console.warn('[speak_fallback] 当前浏览器不支持 speechSynthesis，跳过朗读。');
      resolve(false);
      return;
    }

    // 打断上一段朗读，避免叠加
    window.speechSynthesis.cancel();

    const utterance = new SpeechSynthesisUtterance(content);
    utterance.lang = lang;
    utterance.rate = rate;
    utterance.pitch = pitch;
    utterance.volume = volume;

    const voice = pickVoice(voiceURI);
    if (voice) utterance.voice = voice;

    const finish = (ok) => {
      state.currentUtterance = null;
      resolve(ok);
    };

    utterance.onend = () => finish(true);
    utterance.onerror = (e) => {
      // interrupted / canceled 属于正常打断，不视为失败
      finish(e.error === 'interrupted' || e.error === 'canceled');
    };

    state.currentUtterance = utterance;
    window.speechSynthesis.speak(utterance);
  });
}

/**
 * 【总入口】把文字朗读出来：优先 edge-tts，失败自动降级到浏览器朗读。
 *
 * 这是 converse() 唯一需要调用的朗读函数，降级逻辑全部收在这里，
 * 所以 listen() / think() 的架构完全不用改。
 *
 * @param {string} text  要朗读的文字
 * @param {object} [options]
 * @param {number} [options.rate=1]          语速
 * @param {string} [options.voiceTarget]     下拉框取值，如 'edge:zh-CN-YunxiNeural'
 * @returns {Promise<{ok: boolean, engine: 'edge'|'browser'|'none'}>}
 */
async function speak(text, options = {}) {
  const content = (text || '').trim();
  if (!content) return { ok: false, engine: 'none' };

  const { rate = 1 } = options;
  const rawTarget = options.voiceTarget !== undefined ? options.voiceTarget : el.voiceSelect.value;
  const target = parseVoiceTarget(rawTarget);

  // ---------- 第一层：edge-tts 在线合成 ----------
  if (target.engine === 'edge') {
    setStatus('正在合成语音…', 'speaking', `edge-tts · ${shortVoiceName(target.name)}`);
    el.micBtn.disabled = true;
    try {
      const info = await speak_with_edge_tts(content, target.name, { rate });
      el.micBtn.disabled = false;
      setVoicePill('edge', `edge-tts · ${shortVoiceName(info.voice)}`);
      return { ok: true, engine: 'edge' };
    } catch (err) {
      // 关键降级点：断网 / 合成超时 / 播放被拒，一律退回浏览器朗读
      el.micBtn.disabled = false;
      console.warn('[speak] edge-tts 失败，自动降级到浏览器朗读：', err);
      warnFallbackOnce(err.message);
      setVoicePill('browser', '已降级为浏览器朗读');
    }
  }

  // ---------- 第二层：浏览器 speechSynthesis 保底 ----------
  setStatus('正在朗读…', 'speaking', '浏览器原生语音');
  el.micBtn.disabled = true;
  const ok = await speak_fallback(content, { rate, voiceURI: target.browserVoiceURI });
  el.micBtn.disabled = false;

  if (ok && target.engine === 'browser') setVoicePill('browser', '浏览器原生语音');
  return { ok, engine: 'browser' };
}

/**
 * 解析音色下拉框的取值，判断该走哪条朗读链路。
 *
 * @param {string} value 'edge:zh-CN-XiaoxiaoNeural' / 'browser:<voiceURI>' / ''
 * @returns {{engine: 'edge'|'browser', name: string, browserVoiceURI: string|null}}
 */
function parseVoiceTarget(value) {
  const raw = String(value || '');

  if (raw.startsWith(VOICE_PREFIX_EDGE)) {
    return { engine: 'edge', name: raw.slice(VOICE_PREFIX_EDGE.length), browserVoiceURI: null };
  }
  if (raw.startsWith(VOICE_PREFIX_BROWSER)) {
    const uri = raw.slice(VOICE_PREFIX_BROWSER.length);
    return { engine: 'browser', name: '', browserVoiceURI: uri || null };
  }
  // 没选 / 值不认识：edge 可用就用 edge，否则走浏览器保底
  return state.edgeAvailable
    ? { engine: 'edge', name: state.defaultVoice, browserVoiceURI: null }
    : { engine: 'browser', name: '', browserVoiceURI: null };
}

/** 把 'zh-CN-XiaoxiaoNeural' 简写成『晓晓』之类，便于在状态栏展示 */
function shortVoiceName(voiceName) {
  const hit = state.edgeVoices.find((v) => v.name === voiceName);
  if (hit) return hit.label.split(' · ')[0];
  return voiceName || '';
}

/** 更新顶部的「朗读引擎」指示 */
function setVoicePill(kind, text) {
  if (!el.voicePill) return;
  el.voicePill.textContent = '朗读：' + text;
  el.voicePill.className = 'pill ' + (kind === 'edge' ? 'is-ok' : 'is-warn');
}

/** edge-tts 降级提示只弹一次，避免连续对话时刷屏 */
function warnFallbackOnce(reason) {
  if (state.fallbackWarned) return;
  state.fallbackWarned = true;
  showAlert(
    '🔇 <b>edge-tts 配音失败，已自动降级为浏览器朗读</b>（功能不受影响，音质会差一些）。<br>' +
    '原因：' + escapeHtml(String(reason || '未知')) + '<br>' +
    '可检查网络后重新选择 edge-tts 音色，或在下方「语音」里改用浏览器音色。',
    'warn'
  );
}

/** 立即停止朗读（两条链路的音频都要停） */
function stopSpeaking() {
  // 第一层：中断正在播放的 MP3
  if (state.stopAudio) {
    state.stopAudio();
  } else if (state.currentAudio) {
    try { state.currentAudio.pause(); } catch (_) { /* 忽略 */ }
    state.currentAudio = null;
  }

  // 第二层：中断浏览器合成语音
  if (isSpeechSynthesisSupported()) {
    window.speechSynthesis.cancel();
    state.currentUtterance = null;
  }
}

/* ==========================================================================
 * 5. remember() —— 对话记忆
 * ==========================================================================
 * 需要说明清楚：**权威记忆在后端**（voice_core.remember），这里维护的是前端镜像，
 * 它有两个作用：
 *   1. 刷新页面后能立刻把对话区恢复出来（配合 GET /api/history）；
 *   2. 方便在浏览器控制台里检查当前上下文（state.history）。
 * 两边遵守同一条规则：只保留最近 MAX_TURNS(=10) 轮，超出就丢掉最旧的一轮。
 * ========================================================================== */

/** 取会话 ID：一个浏览器固定一个，存 localStorage，刷新后记忆能延续 */
function getSessionId() {
  if (state.sessionId) return state.sessionId;

  let id = '';
  try { id = localStorage.getItem('voice_session_id') || ''; } catch (_) { /* 隐私模式可能禁用 */ }

  if (!id) {
    const uuid = window.crypto && crypto.randomUUID ? crypto.randomUUID() : Date.now() + '-' + Math.random().toString(16).slice(2);
    id = 'sess-' + uuid;
    try { localStorage.setItem('voice_session_id', id); } catch (_) { /* 忽略 */ }
  }

  state.sessionId = id;
  return id;
}

/**
 * 把一轮问答记进 messages 列表，只保留最近 MAX_TURNS 轮（滑动窗口）。
 * 与后端 voice_core.remember(user_msg, ai_msg) 的行为保持一致。
 *
 * @param {string} userMsg 用户这一轮说的话
 * @param {string} aiMsg   助手这一轮的回答
 * @returns {Array<{role: string, content: string}>} 更新后的 messages 列表
 */
function remember(userMsg, aiMsg) {
  const user = String(userMsg || '').trim();
  const ai = String(aiMsg || '').trim();

  if (user) state.history.push({ role: 'user', content: user });
  if (ai) state.history.push({ role: 'assistant', content: ai });

  // 滑动窗口：超过 10 轮就丢掉最旧的一轮
  if (state.history.length > MAX_TURNS * 2) {
    state.history = state.history.slice(-(MAX_TURNS * 2));
  }

  updateMemoryPill();
  return state.history;
}

/**
 * 更新顶部的「记忆」指示。
 * @param {number} [turns] 后端返回的轮数；不传则按前端镜像自己算
 */
function updateMemoryPill(turns) {
  if (!el.memoryPill) return;
  const value = turns === undefined ? Math.ceil(state.history.length / 2) : turns;
  el.memoryPill.textContent = `记忆：${value}/${MAX_TURNS} 轮`;
}

/* ==========================================================================
 * 6. converse() —— 一轮完整对话：listen → think → speak
 * ========================================================================== */

/**
 * 执行一轮完整语音对话。
 *
 * @param {object} [options]
 * @param {string} [options.text]  直接指定输入文字（跳过录音，用于文字输入）
 */
async function converse(options = {}) {
  if (state.busy) return;
  state.busy = true;
  el.sendBtn.disabled = true;

  try {
    // ---------- 步骤 1：listen() ----------
    let userText = (options.text || '').trim();

    if (!userText) {
      setStatus('正在聆听… 请说话', 'listening');
      userText = (
        await listen({
          timeout: 15000,
          onPartial: (partial) => setStatus('正在聆听…', 'listening', partial),
        })
      ).trim();
    }

    if (!userText) {
      setStatus('没有听清，请再说一次', 'idle');
      return;
    }

    // 把用户的话显示出来
    appendMessage('user', userText, { meta: '语音输入 ' + formatTime() });

    // ---------- 步骤 2：think() ----------
    setStatus('正在思考…', 'thinking', '请求 DeepSeek');
    const placeholder = appendMessage('ai', '', { isTyping: true });

    let reply;
    try {
      // Agent 升级：加载了 agent.js 就把这一步交给它（内部是 decide() + 工具调用链路），
      // 没加载则回落到原来的 think()。两者返回结构完全一致，所以这里不需要分支。
      const result = await (window.AgentBridge?.think || think)(userText, state.history);
      reply = result.reply;
      updateBubble(placeholder, reply, `耗时 ${(result.elapsed_ms / 1000).toFixed(2)}s · ${result.model}`);
      // 把决策过程渲染成气泡旁的卡片，让人看得见 AI 到底调没调工具
      window.AgentBridge?.renderTrace?.(placeholder, result);
      setStatus('已收到回答', 'idle');

      // 记忆升级：记下这一轮（后端也会同步记进它的权威记忆里）
      remember(userText, reply);
      updateMemoryPill(result.turns);
      state.lastReply = reply;
    } catch (err) {
      placeholder.classList.add('error');
      updateBubble(placeholder, '出错了：' + err.message, formatTime());
      setStatus('请求失败', 'error');
      showAlert('⚠️ ' + escapeHtml(err.message), 'error');
      return;
    }

    // ---------- 步骤 3：speak() ----------
    // 朗读总入口：优先 edge-tts（可指定音色），失败自动降级到浏览器 speechSynthesis
    if (el.autoSpeak.checked) {
      setStatus('正在朗读…', 'speaking');
      await speak(reply, {
        rate: parseFloat(el.rateRange.value),
        voiceTarget: el.voiceSelect.value,
      });
      setStatus('准备就绪', 'idle');
    }

    // ---------- 连续对话模式：自动开始下一轮 ----------
    if (el.continuousMode.checked) {
      setTimeout(() => converse(), 350);
    }
  } catch (err) {
    // 录音阶段的错误（权限、超时、不支持等）
    setStatus('录音失败', 'error');
    showAlert('🎤 ' + err.message, 'warn');
  } finally {
    state.busy = false;
    el.sendBtn.disabled = false;
    el.sendBtn.textContent = '发送';
  }
}

/* ==========================================================================
 * 7. 界面初始化与事件绑定
 * ========================================================================== */

/** 读取上次选中的音色（存 localStorage，刷新后沿用） */
function readStoredVoice() {
  try { return localStorage.getItem('voice_target') || ''; } catch (_) { return ''; }
}

/** 记住用户选中的音色 */
function storeVoice(value) {
  try { localStorage.setItem('voice_target', value || ''); } catch (_) { /* 忽略 */ }
}

/** 从后端拉取 edge-tts 音色清单，然后刷新下拉框 */
async function loadVoicesFromServer() {
  try {
    const res = await fetch('/api/voices');
    const data = await res.json();
    state.edgeVoices = data.voices || [];
    state.edgeAvailable = Boolean(data.edge_available);
    state.defaultVoice = data.default || '';
  } catch (err) {
    console.warn('[voices] 拉取音色清单失败，本次只提供浏览器音色：', err);
    state.edgeVoices = [];
    state.edgeAvailable = false;
  }
  renderVoiceOptions();
}

/**
 * 填充「语音」下拉框，分两组：
 *   第一组 Edge 在线音色 —— 走 edge-tts，音质好、音色多（首选）
 *   第二组 浏览器原生音色 —— 走 speechSynthesis（保底，永远可用）
 */
function renderVoiceOptions() {
  const previous = readStoredVoice() || el.voiceSelect.value;
  el.voiceSelect.innerHTML = '';

  // ---- 第一组：edge-tts 在线音色 ----
  if (state.edgeVoices.length) {
    const group = document.createElement('optgroup');
    group.label = state.edgeAvailable ? 'Edge 在线音色（推荐）' : 'Edge 在线音色（当前不可用）';
    state.edgeVoices.forEach((v) => {
      const opt = document.createElement('option');
      opt.value = VOICE_PREFIX_EDGE + v.name;
      opt.textContent = `${v.label}（${v.locale}）`;
      group.appendChild(opt);
    });
    el.voiceSelect.appendChild(group);
  }

  // ---- 第二组：浏览器原生音色（保底）----
  const voices = loadVoices();
  const zhVoices = voices.filter((v) => /^zh/i.test(v.lang));
  const list = zhVoices.length ? zhVoices : voices;

  const fallbackGroup = document.createElement('optgroup');
  fallbackGroup.label = '浏览器原生音色（保底）';

  const autoOpt = document.createElement('option');
  autoOpt.value = VOICE_PREFIX_BROWSER;
  autoOpt.textContent = '系统默认语音';
  fallbackGroup.appendChild(autoOpt);

  list.forEach((v) => {
    const opt = document.createElement('option');
    opt.value = VOICE_PREFIX_BROWSER + v.voiceURI;
    opt.textContent = `${v.name} (${v.lang})`;
    fallbackGroup.appendChild(opt);
  });
  el.voiceSelect.appendChild(fallbackGroup);

  // ---- 恢复上次的选择 ----
  const wanted = previous || (state.defaultVoice ? VOICE_PREFIX_EDGE + state.defaultVoice : '');
  const exists = Array.from(el.voiceSelect.options).some((o) => o.value === wanted);
  if (wanted && exists) {
    el.voiceSelect.value = wanted;
  } else if (el.voiceSelect.options.length) {
    el.voiceSelect.selectedIndex = 0;
  }
}

/** 恢复服务端记忆里的对话（刷新页面后对话区不会空白） */
async function restoreHistory() {
  try {
    const res = await fetch('/api/history?session_id=' + encodeURIComponent(getSessionId()));
    const data = await res.json();
    if (!data.ok || !data.messages || !data.messages.length) {
      updateMemoryPill(0);
      return;
    }

    state.history = data.messages;
    data.messages.forEach((msg) => {
      appendMessage(msg.role === 'user' ? 'user' : 'ai', msg.content, {
        meta: msg.role === 'user' ? '历史记录' : '历史回答',
      });
    });
    const lastAi = [...data.messages].reverse().find((m) => m.role === 'assistant');
    if (lastAi) state.lastReply = lastAi.content;

    updateMemoryPill(data.turns);
  } catch (err) {
    console.warn('[history] 恢复历史失败：', err);
  }
}

/** 清空对话：前端界面 + 后端记忆一起清 */
async function clearConversation() {
  stopSpeaking();
  state.history = [];
  state.lastReply = '';
  el.chat.innerHTML = '';
  el.chat.appendChild(el.empty);
  el.empty.hidden = false;
  updateMemoryPill(0);

  try {
    const res = await fetch('/api/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: getSessionId() }),
    });
    const data = await res.json();
    setStatus(data.cleared ? `已清空对话（含 ${data.cleared} 条记忆）` : '对话已清空', 'idle');
  } catch (_) {
    setStatus('对话已清空（后端记忆未能清空）', 'idle');
  }
}

/** 向后端做一次环境自检：密钥是否配置、edge-tts 是否可用 */
async function checkHealth() {
  try {
    const res = await fetch('/api/health');
    const data = await res.json();

    // --- 密钥状态 ---
    if (data.api_key_configured) {
      el.keyPill.textContent = '密钥已配置 ✔';
      el.keyPill.className = 'pill is-ok';
      hideAlert();
    } else {
      el.keyPill.textContent = '密钥未配置 ✘';
      el.keyPill.className = 'pill is-bad';
      showAlert(
        '未检测到环境变量 <code>' + escapeHtml(data.env_var) + '</code>，对话会失败。<br>' +
        '请在 Windows 中设置该环境变量后重启程序（或双击 <code>start.bat</code>，脚本里有设置指引）。',
        'warn'
      );
    }

    // --- 朗读引擎状态 ---
    state.edgeAvailable = Boolean(data.edge_tts_available);
    if (state.edgeAvailable) {
      setVoicePill('edge', `edge-tts · ${shortVoiceName(data.default_voice)}`);
    } else {
      setVoicePill('browser', '浏览器原生（edge-tts 未安装）');
    }
  } catch (_) {
    el.keyPill.textContent = '后端未连接 ✘';
    el.keyPill.className = 'pill is-bad';
    setVoicePill('browser', '浏览器原生');
  }
}

/** 检查浏览器能力并给出提示 */
function checkBrowserSupport() {
  const problems = [];
  if (!isSpeechRecognitionSupported()) {
    problems.push('语音识别（SpeechRecognition）不可用 —— 请改用 Chrome 或 Edge，其他功能仍可用文字输入。');
  }
  if (!isSpeechSynthesisSupported() && !state.edgeAvailable) {
    // 只有当 edge-tts 也不可用时，缺少 speechSynthesis 才是真的问题
    problems.push('语音朗读不可用（edge-tts 与 speechSynthesis 都不可用）—— 回答将只以文字显示。');
  }
  if (location.protocol !== 'https:' && !['localhost', '127.0.0.1'].includes(location.hostname)) {
    problems.push('当前不是 localhost/https，浏览器会拒绝麦克风权限。');
  }
  if (problems.length) showAlert('⚠️ ' + problems.map(escapeHtml).join('<br>⚠️ '), 'warn');
  return problems.length === 0;
}

/** 绑定所有 DOM 事件 */
function bindEvents() {
  // 麦克风主按钮：录音 / 停止录音
  el.micBtn.addEventListener('click', () => {
    if (state.listening) {
      stopListening();
    } else {
      converse();
    }
  });

  // 文字输入：回车发送
  el.textInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) {
      const text = el.textInput.value.trim();
      if (!text) return;
      el.textInput.value = '';
      converse({ text });
    }
  });

  el.sendBtn.addEventListener('click', () => {
    const text = el.textInput.value.trim();
    if (!text) { el.textInput.focus(); return; }
    el.textInput.value = '';
    converse({ text });
  });

  el.stopSpeakBtn.addEventListener('click', () => {
    stopSpeaking();
    setStatus('已停止朗读', 'idle');
  });

  // 重新朗读上一条回答 —— 换音色后想对比效果时很好用
  el.replayBtn.addEventListener('click', async () => {
    if (!state.lastReply) {
      setStatus('还没有可朗读的回答', 'idle');
      return;
    }
    setStatus('正在重新朗读…', 'speaking');
    await speak(state.lastReply, {
      rate: parseFloat(el.rateRange.value),
      voiceTarget: el.voiceSelect.value,
    });
    setStatus('准备就绪', 'idle');
  });

  // 清空对话（同时清掉后端该会话的记忆）
  el.clearBtn.addEventListener('click', clearConversation);

  // 记住选择的音色，刷新后继续用
  el.voiceSelect.addEventListener('change', () => {
    storeVoice(el.voiceSelect.value);
    const target = parseVoiceTarget(el.voiceSelect.value);
    if (target.engine === 'edge') {
      setVoicePill('edge', `edge-tts · ${shortVoiceName(target.name)}`);
    } else {
      setVoicePill('browser', '浏览器原生语音');
    }
    state.fallbackWarned = false;   // 换了音色，降级提示可以再提醒一次
  });

  el.rateRange.addEventListener('input', () => {
    el.rateValue.textContent = parseFloat(el.rateRange.value).toFixed(1);
  });

  // 连续对话模式下，打断朗读应当同时停止后续自动聆听
  el.continuousMode.addEventListener('change', () => {
    if (!el.continuousMode.checked) stopSpeaking();
  });
}

/** 启动入口 */
async function main() {
  bindEvents();
  updateMemoryPill(0);

  // 先拿音色清单（顺便确认 edge-tts 是否可用），再自检、再恢复历史
  await loadVoicesFromServer();
  checkHealth();
  checkBrowserSupport();
  await restoreHistory();

  // Chrome 的语音列表是异步加载的，加载完再刷新下拉框
  if (isSpeechSynthesisSupported()) {
    window.speechSynthesis.onvoiceschanged = renderVoiceOptions;
  }

  setStatus('准备就绪', 'idle');
  console.log(
    '%c语音交互助手已就绪',
    'color:#4f6ef7;font-weight:bold',
    '\n朗读链路：speak() → speak_with_edge_tts() → (失败自动) speak_fallback()' +
    '\n可在控制台单独测试：' +
    '\n  await listen()' +
    '\n  await think("你好")' +
    '\n  await speak_with_edge_tts("测试", "zh-CN-XiaoxiaoNeural")   // 指定音色' +
    '\n  await speak_fallback("测试")                                // 只走浏览器朗读' +
    '\n  remember("你好", "你也好")                                  // 手动记一轮记忆'
  );
}

document.addEventListener('DOMContentLoaded', main);
