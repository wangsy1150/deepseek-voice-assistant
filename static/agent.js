/* ==========================================================================
 * agent.js —— Agent 化前端
 *
 * 这一层负责三件事：
 *   1. 把对话这一步接到 Agent 接口（/api/agent/think），让 AI 走
 *      「自己决定要不要查资料 → 查 → 再决定 → 回答」的链路；
 *   2. 把后端返回的 trace（决策过程）渲染成气泡旁的卡片，
 *      让人一眼看出 AI 这一轮到底调没调用工具、调了什么、查到了什么；
 *   3. 提供知识库面板：上传 .md / .txt、看文档列表、看 AI 手上的工具箱。
 *
 * 整个文件包在 IIFE 里，只对外暴露 window.AgentBridge，
 * 避免和 app.js 里的 $ / state / el 等全局名字冲突。
 *
 * 控制台里可以直接用：
 *      window.AgentBridge.think('年假有几天')   // 走完整的 Agent 链路
 *      window.AgentBridge.openPanel()           // 打开知识库面板
 * ========================================================================== */

(function () {
  'use strict';

  /* ------------------------------------------------------------------------ *
   * 0. 常量、状态与 DOM 引用
   * ---------------------------------------------------------------------- */

  const MODE_KEY = 'voice-app-agent-mode';   // Agent 模式开关存在 localStorage

  const state = {
    toolbox: [],      // AI 手上的工具清单
    documents: [],    // 知识库里的文档
    stats: null,      // 知识库统计
    lastResult: null, // 最近一次 Agent 结果（含 trace），方便在控制台回看
  };

  const dom = {};

  const $ = (id) => document.getElementById(id);

  /** 转义 HTML（本文件自己一份，不依赖 app.js） */
  const esc = (text) =>
    String(text ?? '').replace(/[&<>"']/g, (ch) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));

  function cacheDom() {
    dom.chat = $('chat');
    dom.agentMode = $('agentMode');
    dom.agentPill = $('agentPill');
    dom.kbBtn = $('kbBtn');
    dom.kbCount = $('kbCount');
    dom.kbPanel = $('kbPanel');
    dom.kbCloseBtn = $('kbCloseBtn');
    dom.kbStats = $('kbStats');
    dom.kbToolboxBody = $('kbToolboxBody');
    dom.kbDrop = $('kbDrop');
    dom.kbFile = $('kbFile');
    dom.kbPickBtn = $('kbPickBtn');
    dom.kbList = $('kbList');
    dom.kbRefreshBtn = $('kbRefreshBtn');
    dom.kbClearBtn = $('kbClearBtn');
  }

  /* ------------------------------------------------------------------------ *
   * 1. Agent 模式开关
   * ---------------------------------------------------------------------- */

  function isEnabled() {
    return !dom.agentMode || dom.agentMode.checked;
  }

  function restoreMode() {
    let saved = null;
    try { saved = localStorage.getItem(MODE_KEY); } catch (_) { /* 隐私模式下可能不允许 */ }
    if (dom.agentMode) dom.agentMode.checked = saved !== 'off';
  }

  /* ------------------------------------------------------------------------ *
   * 2. think() —— 与 app.js 的 think() 同签名，但走 Agent 链路
   * ---------------------------------------------------------------------- */

  /**
   * Agent 版 think：POST /api/agent/think。
   *
   * 返回结构与原 think() 完全一致（reply / elapsed_ms / model / turns），
   * 另外多带 trace / used_tools / tools_used / search_hit，供决策过程展示。
   *
   * @param {string} text     用户说的话
   * @param {Array}  history  对话历史
   * @returns {Promise<object>}
   */
  async function agentThink(text, history = []) {
    // 用户关掉了 Agent 模式 -> 老老实实回落到原来的直接问答
    if (!isEnabled()) {
      if (typeof think === 'function') return think(text, history);
      throw new Error('基础 think() 不可用，无法降级。');
    }

    if (typeof setStatus === 'function') {
      setStatus('Agent 决策中…', 'thinking', 'AI 正在判断要不要查资料');
    }

    const response = await fetch('/api/agent/think', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        session_id: typeof getSessionId === 'function' ? getSessionId() : 'default',
        history: Array.isArray(history) ? history : [],
      }),
    });

    let payload = {};
    try {
      payload = await response.json();
    } catch (_) { /* 保持 payload 为空对象，走下面的统一报错 */ }

    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || `Agent 接口返回 HTTP ${response.status}`);
    }

    state.lastResult = payload;
    return payload;
  }

  /* ------------------------------------------------------------------------ *
   * 3. 决策过程可视化
   * ---------------------------------------------------------------------- */

  /**
   * 把一轮 Agent 的 trace 渲染成气泡下方的卡片。
   *
   * 卡片头部直接给出结论：「🔧 调用了 N 次工具」还是「💬 未调用工具，直接回答」。
   *
   * @param {HTMLElement} bubbleEl appendMessage() 返回的气泡元素
   * @param {object}      result   /api/agent/think 的返回体
   */
  function renderTrace(bubbleEl, result) {
    if (!bubbleEl || !result || !Array.isArray(result.trace) || !result.trace.length) return;

    const wrap = bubbleEl.parentNode;
    if (!wrap) return;

    const existing = wrap.querySelector('.trace-card');
    if (existing) existing.remove();

    const toolSteps = result.trace.filter((item) => item.type === 'tool' || item.type === 'guard');
    const guardSteps = toolSteps.filter((item) => item.type === 'guard');
    const usedTools = !!result.used_tools;

    const card = document.createElement('div');
    card.className = 'trace-card';

    // ---- 头部：一眼看出有没有调用工具 ----
    const head = document.createElement('div');
    head.className = 'trace-head';
    head.innerHTML =
      `<span class="trace-badge ${usedTools ? 'is-used' : 'is-skip'}">` +
      (usedTools ? `🔧 调用工具 ${toolSteps.length} 次` : '💬 未调用工具，直接回答') +
      '</span>' +
      `<span class="trace-title">决策过程</span>` +
      (result.forced_final ? '<span class="trace-badge is-warn">⚠ 强制收口</span>' : '') +
      (guardSteps.length ? `<span class="trace-badge is-guard">🛡️ 含 ${guardSteps.length} 次系统补查</span>` : '') +
      '<button class="trace-toggle" type="button">收起</button>';
    card.appendChild(head);

    // ---- 步骤列表 ----
    const list = document.createElement('ol');
    list.className = 'trace-list';
    result.trace.forEach((item) => list.appendChild(renderStep(item)));
    card.appendChild(list);

    head.querySelector('.trace-toggle').addEventListener('click', (event) => {
      const collapsed = card.classList.toggle('is-collapsed');
      event.target.textContent = collapsed ? '展开' : '收起';
    });

    wrap.appendChild(card);
    wrap.classList.add('has-trace');
    if (typeof scrollToBottom === 'function') scrollToBottom();
  }

  /** 渲染 trace 里的单个步骤（一条 decide / tool / guard）。 */
  function renderStep(item) {
    const li = document.createElement('li');

    if (item.type === 'tool' || item.type === 'guard') {
      const isGuard = item.type === 'guard';
      const ok = !!item.ok;
      const hit = !!item.hit;
      li.className = 'trace-step is-tool '
        + (isGuard ? 'is-guard ' : '')
        + (ok ? (hit ? 'is-hit' : 'is-miss') : 'is-error');

      const argsText = Object.entries(item.arguments || {})
        .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
        .join(', ');

      let title;
      if (isGuard) {
        title = `系统补查 <code>${esc(item.name)}</code>(${esc(argsText)})`;
      } else if (ok) {
        title = `调用 <code>${esc(item.name)}</code>(${esc(argsText)})`;
      } else {
        title = `调用 ${esc(item.name)} 失败`;
      }

      let desc;
      if (!ok) {
        desc = `⚠️ ${esc(item.message || '工具执行出错')}`;
      } else if (hit) {
        desc = `✔ 命中 ${item.count} 条 · 来源：${esc((item.sources || []).join('、'))} · 检索耗时 ${item.elapsed_ms}ms`;
      } else {
        desc = '✘ 知识库里没有相关内容 → 将如实回答「资料里没有提到」';
      }

      const reason = isGuard && item.reason
        ? `<div class="trace-step-desc">触发原因：${esc(item.reason)}</div>`
        : '';

      const previews = (item.preview || [])
        .map((p) => `<li><b>${esc(p.source)}</b> <span class="trace-score">相关度 ${p.score}</span>${esc(p.text)}…</li>`)
        .join('');

      li.innerHTML =
        `<span class="trace-icon">${isGuard ? '🛡️' : (ok ? (hit ? '📚' : '🔍') : '⚠️')}</span>` +
        '<div class="trace-body">' +
          `<div class="trace-step-title">${title}</div>` +
          reason +
          `<div class="trace-step-desc">${desc}</div>` +
          (previews ? `<ul class="trace-preview">${previews}</ul>` : '') +
        '</div>';
      return li;
    }

    // ---- decide 步骤 ----
    const wantsTool = item.action === 'tool';
    li.className = 'trace-step is-decide ' + (wantsTool ? 'wants-tool' : 'wants-final');
    const title = wantsTool
      ? `第 ${item.step} 轮决策 · 决定调用工具`
      : `第 ${item.step} 轮决策 · 决定直接回答`;
    const thought = item.thought
      ? `<div class="trace-step-desc">模型说：${esc(item.thought)}</div>`
      : '';
    const forced = item.finish_reason === 'forced'
      ? '<div class="trace-step-desc">已达工具调用上限，强制收口作答</div>'
      : '';

    li.innerHTML =
      `<span class="trace-icon">${wantsTool ? '🤔' : '✅'}</span>` +
      `<div class="trace-body"><div class="trace-step-title">${title}</div>${thought}${forced}</div>`;
    return li;
  }

  /* ------------------------------------------------------------------------ *
   * 4. 工具箱展示
   * ---------------------------------------------------------------------- */

  async function loadToolbox() {
    const response = await fetch('/api/agent/toolbox');
    const data = await response.json();
    state.toolbox = data.tools || [];
    renderToolbox(data);
    updateAgentPill(data);
    return data;
  }

  /** 更新顶栏的 Agent 状态灯。 */
  function updateAgentPill(data) {
    if (!dom.agentPill) return;
    if (!isEnabled()) {
      dom.agentPill.textContent = 'Agent：已关闭';
      dom.agentPill.className = 'pill';
      return;
    }
    const count = state.toolbox.length;
    const mode = data && data.mode === 'function_calling' ? 'Function Calling' : '自主决策';
    dom.agentPill.textContent = `Agent：${mode} · ${count} 个工具`;
    dom.agentPill.className = 'pill is-ok';
    dom.agentPill.title = state.toolbox
      .map((t) => `${t.name}：${t.when}`)
      .join('\n');
  }

  /** 渲染工具箱卡片：把「交给 AI 的描述说明」原样展示出来。 */
  function renderToolbox(data) {
    if (!dom.kbToolboxBody) return;
    if (!state.toolbox.length) {
      dom.kbToolboxBody.innerHTML = '<div class="tool-note">工具箱是空的。</div>';
      return;
    }

    const cards = state.toolbox
      .map((tool) => {
        const params = (tool.parameters || [])
          .map((p) => `<code class="tool-param${p.required ? ' is-required' : ''}" title="${esc(p.description)}">${esc(p.name)}${p.required ? '' : '?'}</code>`)
          .join(' ');
        return (
          '<div class="tool-card">' +
            `<div class="tool-name">${tool.icon || '🔧'} <code>${esc(tool.name)}</code>` +
              `<span class="tool-label">${esc(tool.label || '')}</span></div>` +
            `<div class="tool-line"><b>什么时候用</b>${esc(tool.when || '')}</div>` +
            `<div class="tool-line"><b>返回什么</b>${esc(tool.returns || '')}</div>` +
            `<div class="tool-line"><b>参数</b>${params || '无'}</div>` +
            '<details class="tool-desc"><summary>' +
              `查看交给 AI 的完整描述说明（${String((tool.description || '').length)} 字）` +
            `</summary><div>${esc(tool.description || '')}</div></details>` +
          '</div>'
        );
      })
      .join('');

    const maxSteps = data && data.max_steps ? data.max_steps : 3;
    dom.kbToolboxBody.innerHTML =
      cards +
      `<div class="tool-note">调不调用工具完全由 AI 自己决定（<code>tool_choice = auto</code>），` +
      `一轮对话最多连续决策 ${maxSteps} 轮。</div>`;
  }

  /* ------------------------------------------------------------------------ *
   * 5. 知识库面板
   * ---------------------------------------------------------------------- */

  async function loadDocuments() {
    const response = await fetch('/api/agent/documents');
    const data = await response.json();
    state.documents = data.documents || [];
    state.stats = data.stats || null;
    renderDocuments();
    return data;
  }

  function renderDocuments() {
    const stats = state.stats || { documents: 0, chunks: 0, bytes: 0 };

    if (dom.kbStats) {
      dom.kbStats.textContent = stats.documents
        ? `共 ${stats.documents} 篇 · ${stats.chunks} 个片段 · ${(stats.bytes / 1024).toFixed(1)} KB`
        : '还没有资料';
    }
    if (dom.kbCount) dom.kbCount.textContent = String(stats.documents || 0);

    if (!dom.kbList) return;
    if (!state.documents.length) {
      dom.kbList.innerHTML =
        '<li class="kb-empty">还没有上传任何资料。上传 .md / .txt 后，AI 就能依据它们来回答；' +
        '没上传时它会老实地回答「资料里没有提到」。</li>';
      return;
    }

    dom.kbList.innerHTML = state.documents
      .map((doc) => (
        '<li class="kb-item">' +
          '<div class="kb-item-main">' +
            `<div class="kb-item-name">📄 ${esc(doc.name)}</div>` +
            `<div class="kb-item-meta">${doc.chars} 字 · ${doc.chunks} 个片段 · ` +
              `${(doc.bytes / 1024).toFixed(1)} KB · ${esc(doc.updated_at)}</div>` +
          '</div>' +
          `<button class="link-btn danger" type="button" data-del="${esc(doc.name)}">删除</button>` +
        '</li>'
      ))
      .join('');
  }

  /** 上传一批文件（支持一次多个，逐个显示结果）。 */
  async function uploadFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;

    const accepted = files.filter((f) => /\.(md|markdown|txt|text)$/i.test(f.name));
    if (!accepted.length) {
      showAlert('只支持 .md / .txt 文件，请重新选择。', 'warn');
      return;
    }

    const form = new FormData();
    accepted.forEach((file) => form.append('files', file));

    if (typeof setStatus === 'function') setStatus('正在上传资料…', 'thinking');

    try {
      const response = await fetch('/api/agent/upload', { method: 'POST', body: form });
      const data = await response.json();
      if (!response.ok && !(data.uploaded || []).length) {
        throw new Error(data.error || '上传失败');
      }

      const names = (data.uploaded || []).map((d) => d.name).join('、');
      const failed = data.failed || [];
      let html = `✅ 已入库：${esc(names)}`;
      if (failed.length) {
        html += '<br>⚠️ 跳过：' + esc(failed.map((f) => `${f.name}（${f.error}）`).join('；'));
      }
      showAlert(html, failed.length ? 'warn' : 'info');

      await loadDocuments();
      if (typeof setStatus === 'function') setStatus('资料已入库', 'idle');
    } catch (err) {
      showAlert('上传失败：' + esc(err.message), 'error');
      if (typeof setStatus === 'function') setStatus('上传失败', 'error');
    }
  }

  async function deleteDocument(name) {
    if (!window.confirm(`确定从知识库删除「${name}」吗？`)) return;
    const response = await fetch('/api/agent/documents/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    const data = await response.json();
    if (!data.ok) {
      showAlert('删除失败：' + esc(data.error || '未知原因'), 'error');
      return;
    }
    await loadDocuments();
  }

  async function clearKnowledgeBase() {
    if (!state.documents.length) {
      showAlert('知识库本来就是空的。', 'info');
      return;
    }
    if (!window.confirm(`确定清空知识库里的 ${state.documents.length} 篇资料吗？此操作不可撤销。`)) return;

    const response = await fetch('/api/agent/kb/clear', { method: 'POST' });
    const data = await response.json();
    showAlert(`已清空知识库，删除 ${data.removed} 个文件。`, 'info');
    await loadDocuments();
  }

  async function refreshAll() {
    try {
      await Promise.all([loadToolbox(), loadDocuments()]);
    } catch (err) {
      showAlert('刷新失败：' + esc(err.message), 'error');
    }
  }

  /* ------------------------------------------------------------------------ *
   * 6. 面板开关与事件绑定
   * ---------------------------------------------------------------------- */

  function openPanel() {
    if (!dom.kbPanel) return;
    dom.kbPanel.hidden = false;
    loadDocuments().catch(() => {});
  }

  function closePanel() {
    if (dom.kbPanel) dom.kbPanel.hidden = true;
  }

  function togglePanel() {
    if (dom.kbPanel && dom.kbPanel.hidden) openPanel();
    else closePanel();
  }

  function bindEvents() {
    dom.kbBtn?.addEventListener('click', togglePanel);
    dom.kbCloseBtn?.addEventListener('click', closePanel);
    dom.kbRefreshBtn?.addEventListener('click', refreshAll);
    dom.kbClearBtn?.addEventListener('click', clearKnowledgeBase);
    dom.kbPickBtn?.addEventListener('click', () => dom.kbFile?.click());
    dom.kbFile?.addEventListener('change', (event) => {
      uploadFiles(event.target.files);
      event.target.value = '';       // 允许重复上传同一个文件
    });

    // 拖拽上传
    const drop = dom.kbDrop;
    if (drop) {
      ['dragenter', 'dragover'].forEach((type) => {
        drop.addEventListener(type, (event) => {
          event.preventDefault();
          drop.classList.add('is-over');
        });
      });
      ['dragleave', 'drop'].forEach((type) => {
        drop.addEventListener(type, (event) => {
          event.preventDefault();
          drop.classList.remove('is-over');
        });
      });
      drop.addEventListener('drop', (event) => {
        if (event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files.length) {
          uploadFiles(event.dataTransfer.files);
        }
      });
    }

    // 删除按钮（事件委托，列表重绘也不用重新绑定）
    dom.kbList?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-del]');
      if (button) deleteDocument(button.getAttribute('data-del'));
    });

    // Agent 模式开关
    dom.agentMode?.addEventListener('change', () => {
      try { localStorage.setItem(MODE_KEY, dom.agentMode.checked ? 'on' : 'off'); } catch (_) {}
      updateAgentPill(null);
      showAlert(
        dom.agentMode.checked
          ? '已开启 Agent 模式：AI 会自己判断要不要查知识库，并在回答下方展示决策过程。'
          : '已关闭 Agent 模式：回到原来的直接问答（不再调用工具）。',
        'info'
      );
    });

    // Esc 关闭面板；点击面板外部也关闭
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closePanel();
    });
    document.addEventListener('click', (event) => {
      if (!dom.kbPanel || dom.kbPanel.hidden) return;
      if (dom.kbPanel.contains(event.target) || (dom.kbBtn && dom.kbBtn.contains(event.target))) return;
      closePanel();
    });
  }

  /* ------------------------------------------------------------------------ *
   * 7. 启动
   * ---------------------------------------------------------------------- */

  async function init() {
    cacheDom();
    restoreMode();
    bindEvents();

    try {
      await Promise.all([loadToolbox(), loadDocuments()]);
    } catch (err) {
      console.warn('[agent] 初始化失败', err);
      if (dom.agentPill) {
        dom.agentPill.textContent = 'Agent：不可用';
        dom.agentPill.className = 'pill is-bad';
      }
    }

    console.log(
      '%cAgent 已就绪',
      'color:#7c3aed;font-weight:bold',
      '\n工具箱：' + state.toolbox.map((t) => t.name).join('、') +
      '\n可在控制台单独测试：' +
      '\n  await window.AgentBridge.think("年假有几天")   // 走完整的 Agent 链路' +
      '\n  window.AgentBridge.openPanel()                // 打开知识库面板'
    );
  }

  // 对外只暴露这一个入口，app.js 的 converse() 通过它接管 think 这一步
  window.AgentBridge = {
    think: agentThink,
    renderTrace,
    openPanel,
    closePanel,
    refresh: refreshAll,
    getToolbox: () => state.toolbox.slice(),
    getLastResult: () => state.lastResult,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
