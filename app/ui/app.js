/* 小爱音响控制台 · 控制台前端
   Copyright (c) 2026 HanDou. 许可条款见包内 LICENSE：仅限个人非商业使用，禁止商业分发。
   页面：概览 / 账号 / 小爱音响 / OpenClaw
   数据一律走同源接口：/api/*（hub 自身）与 /proxy/bridge/*（转发到 8790） */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  // 可能挂在 /app/xiaoai-console/ 网关前缀下，也可能直接以 / 打开
  const BASE = location.pathname.replace(/index\.html$/, '').replace(/\/?$/, '/');
  const bridgeUrl = () => `http://${location.hostname}:8790/`;

  const state = {
    meta: {}, probe: null, oc: null, mi: null, st: null, backendModel: '',
    ocAuto: false,
  };

  // ---------------- 图标 ----------------
  function icon(path, size) {
    const s = size || 14;
    return `<svg viewBox="0 0 24 24" width="${s}" height="${s}" fill="none" stroke="currentColor" `
      + `stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;
  }
  const I = {
    play: '<path d="M8 5.6v12.8L18.5 12z"/>',
    pause: '<path d="M9.5 5v14M14.5 5v14"/>',
    prev: '<path d="M18 6v12L8.5 12z"/><path d="M6 5v14"/>',
    next: '<path d="M6 6v12l9.5-6z"/><path d="M18 5v14"/>',
    stop: '<rect x="7" y="7" width="10" height="10" rx="1.2"/>',
    arrow: '<path d="M5 12h12"/><path d="M13 7l5 5-5 5"/>',
    external: '<path d="M14 4h6v6"/><path d="M20 4l-8 8"/><path d="M20 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h5"/>',
    speaker: '<rect x="4" y="6" width="16" height="11" rx="2"/><path d="M9 20h6M12 17v3"/>',
  };

  // ---------------- 基础 ----------------
  async function api(path, body, method) {
    const opt = { method: method || (body ? 'POST' : 'GET'), headers: {} };
    if (body) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    const r = await fetch(BASE + String(path).replace(/^\//, ''), opt);
    const text = await r.text();
    let data;
    try { data = JSON.parse(text); } catch (e) { data = { ok: false, error: text.slice(0, 200) }; }
    if (!r.ok && data.ok !== false) data = { ok: false, error: 'HTTP ' + r.status, raw: data };
    return data;
  }

  let toastTimer;
  function toast(msg, bad) {
    const el = $('toast');
    el.textContent = msg;
    el.className = 'toast' + (bad ? ' bad' : '');
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
  }

  function copyText(text) {
    const done = () => toast('已复制');
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done).catch(() => fallback());
      return;
    }
    fallback();
    function fallback() {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); done(); } catch (e) { toast('复制失败', true); }
      document.body.removeChild(ta);
    }
  }

  const fmtNum = (n) => Number(n || 0).toLocaleString('zh-CN');
  const cut = (s, n) => {
    const v = String(s || '');
    return v.length > n ? v.slice(0, n) + '…' : v;
  };
  // UUID 这类长串：保留头尾，中间省略，比直接截断更好认
  const cutId = (s) => {
    const v = String(s || '');
    return v.length > 14 ? v.slice(0, 8) + '…' + v.slice(-4) : v;
  };
  function fmtUptime(sec) {
    const s = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (h >= 24) return `${Math.floor(h / 24)}天${h % 24}时`;
    if (h) return `${h}时${m}分`;
    return `${m}分`;
  }
  function fmtTs(ts) {
    if (!ts) return '无';
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function setLed(el, cls) {
    const node = typeof el === 'string' ? $(el) : el;
    if (!node) return;
    node.className = 'led' + (cls ? ' ' + cls : '');
  }

  function stamp() {
    const el = $('stamp');
    if (el) el.textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  }

  // ---------------- 数据 ----------------
  async function loadMeta() {
    try {
      state.meta = await api('/api/hub/meta');
      const h = state.meta.host || '';
      $('rail-host').textContent = h || '—';
      $('rail-ver').textContent = 'v' + (state.meta.version || '—');
    } catch (e) {
      $('rail-host').textContent = '连接失败';
    }
  }

  async function probeBridge() {
    try {
      state.probe = await api('/api/probe', { url: bridgeUrl() });
    } catch (e) {
      state.probe = { ok: false, error: '桥接未响应' };
    }
  }

  async function loadOc() {
    try {
      const d = await api('/proxy/bridge/api/openclaw');
      const a = (d && d.agent) || {};
      state.oc = { online: !!(d.reachable || a.online), reachable: !!d.reachable, a, cfg: d.openclaw_config || null };
    } catch (e) {
      state.oc = { online: false, a: {}, cfg: null };
    }
  }

  async function loadState() {
    try {
      state.st = await api('/proxy/bridge/api/state');
    } catch (e) {
      state.st = null;
    }
  }

  async function loadMi() {
    let d = null;
    try { d = await api('/proxy/bridge/api/mi'); } catch (e) { d = null; }
    const a = (d && d.account) || {};
    state.mi = { reachable: !!(d && d.account), ...a };
    return d;
  }

  async function loadBackendModel() {
    try {
      const d = await api('/proxy/bridge/api/openclaw/backend-models');
      if (d && d.ok) state.backendModel = d.current || '';
    } catch (e) { /* 保持旧值 */ }
  }

  // ---------------- 顶部状态 ----------------
  function paintChrome() {
    const st = state.st || {};
    const devOk = !!(st.device && st.device.online);
    const brOk = !!(state.probe && state.probe.ok);
    const gwOk = !!(state.oc && state.oc.online);

    setLed('led-speaker', devOk ? 'ok' : 'bad');
    setLed('led-bridge', brOk ? 'ok' : 'bad');
    setLed('led-gw', gwOk ? 'ok' : 'bad');
    setLed(document.querySelector('[data-dot="overview"]'), (devOk && brOk && gwOk) ? 'ok' : 'bad');
    setLed(document.querySelector('[data-dot="account"]'), (state.mi && state.mi.logged_in) ? 'ok' : 'bad');
    setLed(document.querySelector('[data-dot="xiaoai"]'), devOk ? 'ok' : 'bad');
    setLed(document.querySelector('[data-dot="openclaw"]'), gwOk ? 'ok' : 'bad');
  }

  // ---------------- 概览 ----------------
  function renderOverview() {
    $('view').innerHTML = `
      <div class="kpis">
        <div class="kpi">
          <span class="kpi-label">在线服务</span>
          <div class="kpi-value" id="k-svc">—<small>/3</small></div>
          <div class="kpi-sub">音箱 · 桥接 · 网关</div>
        </div>
        <div class="kpi">
          <span class="kpi-label">桥接延迟</span>
          <div class="kpi-value" id="k-lat">—<small>ms</small></div>
          <div class="kpi-sub">127.0.0.1 : 8790</div>
        </div>
        <div class="kpi">
          <span class="kpi-label">曲库曲目</span>
          <div class="kpi-value" id="k-tracks">—<small>首</small></div>
          <div class="kpi-sub" id="k-roots">—</div>
        </div>
        <div class="kpi">
          <span class="kpi-label">桥接运行</span>
          <div class="kpi-value" id="k-uptime">—</div>
          <div class="kpi-sub" id="k-session">—</div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">信号链</div>
          <div class="right"><span class="pill" id="ch-sum">检测中</span></div>
        </div>
        <div class="panel-body">
          <div class="chain">
            <button class="node" id="ch-speaker" type="button">
              <span class="node-top"><i class="led" id="ch-speaker-led"></i><span class="node-name">小爱音箱</span></span>
              <span class="node-sub" id="ch-speaker-sub">设备</span>
              <span class="node-foot" id="ch-speaker-foot">—</span>
            </button>
            <div class="link">${icon('<path d="M5 12h12"/><path d="M13 8l4 4-4 4"/>', 13)}</div>
            <button class="node" id="ch-bridge" type="button">
              <span class="node-top"><i class="led" id="ch-bridge-led"></i><span class="node-name">小爱桥接</span></span>
              <span class="node-sub">:8790</span>
              <span class="node-foot" id="ch-bridge-foot">—</span>
            </button>
            <div class="link">${icon('<path d="M5 12h12"/><path d="M13 8l4 4-4 4"/>', 13)}</div>
            <button class="node" id="ch-gw" type="button">
              <span class="node-top"><i class="led" id="ch-gw-led"></i><span class="node-name">OpenClaw</span></span>
              <span class="node-sub" id="ch-gw-sub">:48670</span>
              <span class="node-foot" id="ch-gw-foot">—</span>
            </button>
            <div class="link">${icon('<path d="M5 12h12"/><path d="M13 8l4 4-4 4"/>', 13)}</div>
            <button class="node" id="ch-model" type="button">
              <span class="node-top"><i class="led" id="ch-model-led"></i><span class="node-name">模型</span></span>
              <span class="node-sub" id="ch-model-sub">—</span>
              <span class="node-foot" id="ch-model-foot">—</span>
            </button>
          </div>
        </div>
      </div>

      <div class="rack">
        <div class="rack-row">
          <span class="slot">01</span>
          <div class="id">
            <b><i class="led" id="r-mi-led"></i><span>小米账号</span></b>
            <i>登录态 · 音箱设备切换</i>
          </div>
          <div class="stats">
            <div class="stat"><em>状态</em><b id="r-mi-state">—</b></div>
            <div class="stat"><em>用户</em><b class="mono" id="r-mi-user">—</b></div>
            <div class="stat"><em>当前设备</em><b class="mono" id="r-mi-dev">—</b></div>
          </div>
          <button class="go" type="button" data-go="account">进入 ${icon(I.arrow, 12)}</button>
        </div>

        <div class="rack-row">
          <span class="slot">02</span>
          <div class="id">
            <b><i class="led" id="r-br-led"></i><span>小爱音响</span></b>
            <i>桥接服务 · 127.0.0.1:8790</i>
          </div>
          <div class="stats">
            <div class="stat"><em>运行状态</em><b id="r-br-state">—</b></div>
            <div class="stat"><em>响应延迟</em><b class="mono" id="r-br-lat">—</b></div>
            <div class="stat"><em>正在播放</em><b id="r-br-now">—</b></div>
          </div>
          <button class="go" type="button" data-go="xiaoai">进入 ${icon(I.arrow, 12)}</button>
        </div>

        <div class="rack-row">
          <span class="slot">03</span>
          <div class="id">
            <b><i class="led" id="r-oc-led"></i><span>OpenClaw</span></b>
            <i id="r-oc-gw">模型网关 · 会话 · MCP 工具</i>
          </div>
          <div class="stats">
            <div class="stat"><em>运行状态</em><b id="r-oc-state">—</b></div>
            <div class="stat"><em>后端模型</em><b class="mono" id="r-oc-model">—</b></div>
            <div class="stat"><em>响应延迟</em><b class="mono" id="r-oc-lat">—</b></div>
          </div>
          <button class="go" type="button" data-go="openclaw">进入 ${icon(I.arrow, 12)}</button>
        </div>

        <div class="rack-row">
          <span class="slot">04</span>
          <div class="id">
            <b><i class="led" id="r-auth-led"></i><span>访问保护</span></b>
            <i>控制台与桥接的访问口令</i>
          </div>
          <div class="stats">
            <div class="stat"><em>状态</em><b id="r-auth-state">—</b></div>
            <div class="stat"><em>作用于</em><b class="mono">8900 · 8790</b></div>
            <div class="stat"><em>音箱放歌</em><b>不受影响</b></div>
          </div>
          <button class="go" type="button" id="r-auth-btn">设置</button>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">最近交互</div>
          <div class="right"><button class="btn quiet small" type="button" data-go="xiaoai">全部记录 ${icon(I.arrow, 12)}</button></div>
        </div>
        <div class="panel-body tight">
          <div class="list" id="ov-history"><div class="empty">读取中…</div></div>
        </div>
      </div>

      <p class="hint" style="margin-top:14px">
        信号链从上到下只走本机：音箱由小米云下发，桥接与控制台同机，网关走 127.0.0.1。
        点任意节点可跳到对应页面。
      </p>`;

    document.querySelectorAll('[data-go]').forEach((b) => {
      b.onclick = () => { location.hash = '#/' + (b.dataset.go === 'overview' ? '' : b.dataset.go); };
    });
    $('ch-speaker').onclick = () => { location.hash = '#/account'; };
    $('ch-bridge').onclick = () => { location.hash = '#/xiaoai'; };
    $('ch-gw').onclick = () => { location.hash = '#/openclaw'; };
    $('ch-model').onclick = () => { location.hash = '#/openclaw'; };
  }

  function paintOverview() {
    const st = state.st || {};
    const dev = st.device || {};
    const lib = st.library || {};
    const en = st.engine || {};
    const pl = st.player || {};
    const oc = state.oc || {};
    const a = oc.a || {};
    const p = state.probe || {};
    const mi = state.mi || {};

    const devOk = !!dev.online;
    const brOk = !!p.ok;
    const gwOk = !!oc.online;
    const on = [devOk, brOk, gwOk].filter(Boolean).length;

    $('k-svc').innerHTML = `${on}<small>/3</small>`;
    $('k-svc').classList.toggle('amber', on < 3);
    $('k-lat').innerHTML = brOk ? `${p.ms}<small>ms</small>` : `—<small>ms</small>`;
    $('k-tracks').innerHTML = `${fmtNum(lib.count)}<small>首</small>`;
    $('k-roots').textContent = (lib.roots || []).length
      ? `${lib.roots.length} 个目录 · ${(lib.roots_info || []).map((r) => r.count).join(' + ')}`
      : '还没添加音乐目录';
    $('k-uptime').textContent = en.uptime ? fmtUptime(en.uptime) : '—';
    $('k-session').textContent = en.session || '—';

    const sum = $('ch-sum');
    sum.className = 'pill ' + (on === 3 ? 'ok' : on ? '' : 'bad');
    sum.textContent = `${on} / 3 在线`;

    // 信号链节点
    setLed('ch-speaker-led', devOk ? 'ok' : 'bad');
    $('ch-speaker-sub').textContent = [dev.hardware || '设备', dev.name].filter(Boolean).join(' · ');
    $('ch-speaker-foot').textContent = devOk
      ? `在线${typeof dev.volume === 'number' ? ' · 音量 ' + dev.volume : ''}`
      : '离线';

    setLed('ch-bridge-led', brOk ? 'ok' : 'bad');
    $('ch-bridge-foot').textContent = brOk ? `${p.status || 200} · ${p.ms} ms` : '不可达';

    setLed('ch-gw-led', gwOk ? 'ok' : 'bad');
    try { $('ch-gw-sub').textContent = ':' + new URL(a.base_url || '').port; } catch (e) { $('ch-gw-sub').textContent = '—'; }
    $('ch-gw-foot').textContent = gwOk ? `在线 · ${a.last_ms || 0} ms` : (a.last_error || '不可达');

    const model = state.backendModel || a.model || '';
    setLed('ch-model-led', model && gwOk ? 'ok' : '');
    $('ch-model-sub').textContent = model ? 'OpenClaw 后端' : '未配置';
    $('ch-model-foot').textContent = model || '—';

    // 机架行
    setLed('r-mi-led', mi.logged_in ? 'ok' : 'bad');
    $('r-mi-state').innerHTML = `<span class="pill ${mi.logged_in ? 'ok' : 'bad'}">${mi.logged_in ? '已登录' : (mi.reachable ? '未登录' : '读不到')}</span>`;
    $('r-mi-user').textContent = mi.user_id || '—';
    const did = mi.device_id || dev.id || '';
    $('r-mi-dev').textContent = did ? cutId(did) : '—';
    $('r-mi-dev').title = did;

    setLed('r-br-led', brOk ? 'ok' : 'bad');
    $('r-br-state').innerHTML = `<span class="pill ${brOk ? 'ok' : 'bad'}">${brOk ? '在线' : '离线'}</span>`;
    $('r-br-lat').textContent = brOk ? `${p.ms} ms` : '—';
    $('r-br-now').textContent = pl.title ? `${pl.artist || '未知'} · ${pl.title}` : '未在播放';
    $('r-br-now').title = pl.now || '';

    setLed('r-oc-led', gwOk ? 'ok' : 'bad');
    $('r-oc-state').innerHTML = `<span class="pill ${gwOk ? 'ok' : 'bad'}">${gwOk ? '在线' : '离线'}</span>`;
    $('r-oc-model').textContent = state.backendModel || '—';
    $('r-oc-model').title = state.backendModel || '';
    $('r-oc-lat').textContent = a.last_ms ? `${a.last_ms} ms` : '—';
    $('r-oc-gw').textContent = a.base_url ? `模型网关 · ${a.base_url}` : '模型网关 · 会话 · MCP 工具';

    paintChrome();
  }

  async function loadOverviewHistory() {
    const box = $('ov-history');
    if (!box) return;
    let d = null;
    try { d = await api('/proxy/bridge/api/history?limit=6'); } catch (e) { d = null; }
    const items = (d && d.items) || [];
    box.innerHTML = items.length ? items.map((it) => {
      const t = new Date(it.ts * 1000);
      const hh = String(t.getHours()).padStart(2, '0') + ':' + String(t.getMinutes()).padStart(2, '0');
      const q = it.kind === 'command' ? (it.query || it.text || '') : (it.text || '');
      const a = it.reply || it.detail || '';
      return `<div class="row">
        <span class="idx">${hh}</span>
        <span class="name">${esc(q)}</span>
        <span class="sub">${esc(a)}</span>
      </div>`;
    }).join('') : '<div class="empty">还没有交互记录</div>';
  }

  async function refreshOverview() {
    await Promise.all([probeBridge(), loadOc(), loadState(), loadMi(), loadBackendModel(), loadAuth()]);
    if (!$('k-svc')) return;
    paintOverview();
    loadOverviewHistory();
    stamp();
  }

  // ================= 小爱音响 =================
  const XI_FIELDS = {
    play: ['query', 'mode', 'limit'],
    play_folder: ['path', 'query', 'mode', 'limit'],
    volume: ['value', 'delta'],
    mode: ['mode'],
    say: ['text'],
  };
  const XI = { timer: null, cmds: [], actionTypes: [], editing: null, playing: false };
  const xiApi = (path, body, method) => api('/proxy/bridge' + path, body, method);

  function xiStopTimer() {
    if (XI.timer) clearInterval(XI.timer);
    XI.timer = null;
  }

  async function xiState() {
    let d = null;
    try { d = await xiApi('/api/state'); } catch (e) { d = null; }
    if (!d) return;
    state.st = d;
    if (!$('xi-title')) { paintChrome(); return; }

    const dev = d.device || {};
    const p = d.player || {};
    const lib = d.library || {};
    const en = d.engine || {};
    XI.playing = !!p.playing;

    const pill = $('xi-dev');
    pill.className = 'pill ' + (dev.online ? 'ok' : 'bad');
    pill.innerHTML = `<i class="led ${dev.online ? 'ok' : 'bad'}"></i>${dev.online ? esc(dev.name || '设备在线') : '设备离线'}`;
    $('xi-meta').textContent = `${dev.hardware || '—'} · 曲库 ${fmtNum(lib.count)} 首 · 已运行 ${fmtUptime(en.uptime)}`;
    const lc = $('xi-libcount');
    if (lc) lc.textContent = fmtNum(lib.count) + ' 首';

    $('xi-title').textContent = p.title || '未在播放';
    $('xi-sub').textContent = p.title
      ? [p.artist, p.album].filter(Boolean).join(' · ')
      : '选一首歌，或者对小爱说出指令';
    $('xi-pos').textContent = p.total ? `${p.index} / ${p.total}` : '—';
    $('xi-bar').style.width = p.total ? `${((p.index - 1) / p.total) * 100}%` : '0%';
    $('xi-mode-text').textContent = { seq: '顺序播放', shuffle: '随机播放', loop: '单曲循环' }[p.mode] || '—';
    $('xi-qhint').textContent = p.total ? `共 ${p.total} 首` : '';
    const up = $('xi-upnext');
    if (up) {
      const list = p.upcoming || [];
      up.innerHTML = list.length
        ? list.map((t, i) => `<div class="row"><span class="idx">${i + 1}</span><span class="name">${esc(t)}</span></div>`).join('')
        : '<div class="hint" style="margin:0">后面没有排队的歌</div>';
    }
    $('xi-play').innerHTML = icon(XI.playing ? I.pause : I.play, 17);
    $('xi-play').title = XI.playing ? '暂停' : '播放';

    document.querySelectorAll('#xi-modes button').forEach((b) => {
      b.classList.toggle('on', b.dataset.mode === p.mode);
    });
    if (typeof dev.volume === 'number') {
      const v = $('xi-volume');
      if (v && document.activeElement !== v) {
        v.value = dev.volume;
        v.style.setProperty('--fill', dev.volume + '%');
        $('xi-vol-val').textContent = dev.volume;
      }
    }
    const sp = d.speak || {};
    if (typeof sp.speech_volume === 'number') {
      const sv = $('xi-speak-vol');
      if (sv && document.activeElement !== sv) {
        sv.value = sp.speech_volume;
        sv.style.setProperty('--fill', sp.speech_volume + '%');
        $('xi-speak-vol-val').textContent = sp.speech_volume;
      }
    }
    paintChrome();
  }

  async function xiQueue() {
    let d = null;
    try { d = await xiApi('/api/queue'); } catch (e) { return; }
    const box = $('xi-queue');
    if (!box) return;
    const list = (d && d.queue) || [];
    $('xi-qcount').textContent = list.length + ' 首';
    box.innerHTML = list.length ? list.map((t) => `
      <div class="row click ${t.current ? 'cur' : ''}" data-idx="${t.index}">
        <span class="idx">${t.index + 1}</span>
        <span class="name">${esc(t.name)}</span>
        <span class="sub">${esc(t.artist || '')}</span>
      </div>`).join('') : '<div class="empty">队列为空</div>';
  }

  async function xiArtists() {
    const box = $('xi-chips');
    if (!box) return;
    // 索引刚重建完时 /api/artists 可能短暂返回空，重试几次再判定为没有
    let d = null;
    for (let i = 0; i < 3; i += 1) {
      try { d = await xiApi('/api/artists'); } catch (e) { d = null; }
      if (d && (d.artists || []).length) break;
      await new Promise((r) => setTimeout(r, 1200));
    }
    const items = ((d && d.artists) || []).filter((x) => x.name && x.name !== '未知');
    box.innerHTML = items.length
      ? items.slice(0, 60).map((x) =>
        `<button class="chip-btn" data-artist="${esc(x.name)}">${esc(x.name)}<b>${x.count}</b></button>`).join('')
      : '<span class="chip-hint">曲库索引还没回来，点右上「重建索引」再试</span>';
  }

  async function xiSearch() {
    const q = $('xi-search').value.trim();
    const box = $('xi-results');
    if (!q) { box.innerHTML = '<div class="empty">输入关键词搜索</div>'; return; }
    let d = null;
    try { d = await xiApi('/api/search', { query: q, limit: 40 }); } catch (e) { return; }
    const tracks = (d && d.tracks) || [];
    box.innerHTML = tracks.length ? tracks.map((t) => `
      <div class="row click" data-tid="${esc(t.id)}">
        <span class="name">${esc(t.name)}</span>
        <span class="sub">${esc(t.artist || '')}</span>
      </div>`).join('') : '<div class="empty">没有匹配的曲目</div>';
  }

  async function xiRoots() {
    let d = null;
    try { d = await xiApi('/api/library/roots'); } catch (e) { return; }
    const box = $('xi-roots');
    if (!box) return;
    const roots = (d && d.roots) || [];
    box.innerHTML = roots.length ? roots.map((r) => `
      <div class="row">
        <span class="name">${esc(r.path)}</span>
        <span class="sub">${r.exists ? r.count + ' 首' : '未挂进容器'}</span>
        <button class="btn quiet small" data-del="${esc(r.path)}" type="button" style="flex:none">移除</button>
      </div>`).join('') : '<div class="empty">还没有音乐目录</div>';
    box.querySelectorAll('[data-del]').forEach((b) => {
      b.onclick = () => xiRootDel(b.dataset.del);
    });
    // 有目录在当前容器里看不见 → 提示需要重启应用才生效
    const note = $('xi-root-note');
    if (note) note.hidden = !roots.some((r) => !r.exists);
  }

  async function xiRootAdd() {
    const inp = $('xi-root-in');
    const btn = $('xi-root-add');
    const p = ((inp && inp.value) || '').trim();
    if (!p) { toast('先填一个 NAS 上的绝对路径，例如 /vol1/1000/音乐', true); return; }
    if (btn) btn.disabled = true;
    const d = await api('/api/library/roots', { action: 'add', path: p });
    if (btn) btn.disabled = false;
    if (!d || !d.ok) { toast((d && d.error) || '添加失败', true); return; }
    if (inp) inp.value = '';
    await xiRoots();
    xiState();
    if (d.restart_required) toast('已记录，重启应用后才会挂进容器');
    else toast('已添加，共 ' + (d.count || 0) + ' 首');
  }

  async function xiRootDel(p) {
    const d = await api('/api/library/roots', { action: 'remove', path: p });
    if (!d || !d.ok) { toast((d && d.error) || '移除失败', true); return; }
    await xiRoots();
    xiState();
    toast('已移除，重启应用后释放挂载');
  }

  async function xiRootRestart() {
    const btn = $('xi-root-restart');
    if (btn) { btn.disabled = true; btn.textContent = '重启中…'; }
    const d = await api('/api/app/restart', {});
    if (!d || !d.ok) {
      if (btn) { btn.disabled = false; btn.textContent = '重启应用'; }
      toast((d && d.error) || '重启失败', true);
      return;
    }
    toast('应用正在重启，约 20 秒后自动刷新页面');
    setTimeout(() => location.reload(), 22000);
  }

  async function xiWakeLoad() {
    let d = null;
    try { d = await xiApi('/api/wake'); } catch (e) { return; }
    if (!d || !$('xi-wake-names')) return;
    $('xi-wake-names').value = (d.names || []).join(', ');
    $('xi-wake-pats').value = (d.patterns || []).join(', ');
    $('xi-wake-bare').checked = !!d.bare_commands;
  }

  async function xiCmds() {
    let d = null;
    try { d = await xiApi('/api/commands'); } catch (e) { return; }
    XI.cmds = (d && d.commands) || [];
    XI.actionTypes = (d && d.action_types) || [];
    const sel = $('xi-cmd-action');
    if (sel && !sel.options.length) {
      sel.innerHTML = XI.actionTypes.map((t) =>
        `<option value="${t.type}">${esc(t.label || t.type)}</option>`).join('');
      xiSyncFields();
    }
    const box = $('xi-cmd-list');
    box.innerHTML = XI.cmds.length ? XI.cmds.map((c) => {
      // 触发词与名称相同时不再重复一遍，只列真正额外的说法
      const extra = (c.patterns || []).filter((x) => x !== c.name);
      return `
      <div class="row ${c.enabled ? '' : 'off'}" data-id="${esc(c.id)}">
        <span class="name">${esc(c.name)}</span>
        <span class="sub">${esc(extra.join(' / '))}</span>
        <span class="ops">
          <button type="button" data-op="edit">编辑</button>
          <button type="button" data-op="del" class="del">删</button>
        </span>
      </div>`;
    }).join('') : '<div class="empty">还没有自定义指令</div>';
    xiShortcuts();
  }

  // 手机端一键触发：统一用「名字」唤起，跟固定指令同一个写法
  const XI_CTRL = ['暂停播放', '继续播放', '下一首', '上一首', '停止播放'];

  function xiScBase() {
    // 手机要直接打桥接的 8790；控制台只是壳，不能走 8900 的同源代理
    return `http://${location.hostname || 'localhost'}:8790/api/command/run`;
  }

  function xiShortcuts() {
    const box = $('xi-sc-list');
    if (!box) return;
    const base = xiScBase();
    $('xi-sc-base').textContent = base;
    $('xi-sc-copy').onclick = () => copyText(base);
    $('xi-sc-open').onclick = () => window.open(bridgeUrl(), '_blank');

    const items = XI_CTRL.map((label) => ({
      label, control: true, q: `?name=${label}`,
      enc: `?name=${encodeURIComponent(label)}`,
    })).concat(XI.cmds.filter((c) => c.enabled !== false && !XI_CTRL.includes(c.name)).map((c) => {
      const nm = c.name || '';
      // 显示用原文（好认），复制用编码后的（URL 里中文必须转义）
      return {
        label: nm, control: false, q: `?name=${nm}`,
        enc: `?name=${encodeURIComponent(nm)}`,
      };
    }));

    $('xi-sc-count').textContent = `${items.length} 条`;
    box.innerHTML = items.map((it) => `
      <div class="row">
        <span class="name">${esc(it.label)}</span>
        <span class="k">${it.control ? '控制' : '指令'}</span>
        <span class="ops"><button type="button" data-url="${esc(base + (it.enc || it.q))}">复制</button></span>
      </div>`).join('');
    box.querySelectorAll('button[data-url]').forEach((b) => {
      b.onclick = () => copyText(b.dataset.url);
    });
  }

  function xiSyncFields() {
    const type = $('xi-cmd-action').value;
    const need = XI_FIELDS[type] || [];
    ['query', 'mode', 'limit', 'path', 'value', 'delta', 'text'].forEach((k) => {
      const el = $('xi-f-' + k);
      if (el) el.hidden = !need.includes(k);
    });
  }

  function xiFillEditor(c) {
    const a = (c && c.action) || {};
    $('xi-cmd-editor').hidden = false;
    $('xi-cmd-name').value = c ? c.name : '';
    $('xi-cmd-patterns').value = c ? (c.patterns || []).join(', ') : '';
    $('xi-cmd-match').value = (c && c.match) || 'contains';
    const type = a.type || 'play';
    $('xi-cmd-action').value = type;
    $('xi-cmd-query').value = a.query || '';
    $('xi-cmd-mode').value = a.mode || 'seq';
    $('xi-cmd-limit').value = a.limit != null ? a.limit : 30;
    $('xi-cmd-path').value = a.path || '';
    $('xi-cmd-value').value = a.value != null ? a.value : 40;
    $('xi-cmd-delta').value = a.delta != null ? a.delta : 10;
    $('xi-cmd-text').value = a.text || '';
    $('xi-cmd-reply').value = c ? (c.reply || '') : '';
    $('xi-cmd-hint').textContent = '';
    xiSyncFields();
  }

  async function xiCmdSave() {
    const type = $('xi-cmd-action').value;
    const action = { type };
    if (['play', 'play_folder'].includes(type)) {
      action.query = $('xi-cmd-query').value.trim();
      action.mode = $('xi-cmd-mode').value;
      const lim = $('xi-cmd-limit').value.trim();
      action.limit = lim === '' ? 0 : Number(lim) || 0;
    }
    if (type === 'play_folder') action.path = $('xi-cmd-path').value.trim();
    if (type === 'volume') {
      action.value = Number($('xi-cmd-value').value) || 0;
      const d = Number($('xi-cmd-delta').value) || 0;
      if (d) action.delta = d;
    }
    if (type === 'mode') action.mode = $('xi-cmd-mode').value;
    if (type === 'say') action.text = $('xi-cmd-text').value;

    const body = {
      id: XI.editing || undefined,
      name: $('xi-cmd-name').value.trim(),
      patterns: $('xi-cmd-patterns').value.split(/[,，\s]+/).filter(Boolean),
      match: $('xi-cmd-match').value,
      action,
      reply: $('xi-cmd-reply').value.trim(),
      priority: Number($('xi-cmd-priority').value) || 50,
    };
    if (!body.patterns.length) { $('xi-cmd-hint').textContent = '至少填一个触发词'; return; }
    try {
      const d = await xiApi('/api/commands', body);
      if (!d.ok) throw new Error(d.error || '保存失败');
      toast('指令已保存');
      $('xi-cmd-editor').hidden = true;
      XI.editing = null;
      xiCmds();
    } catch (e) {
      $('xi-cmd-hint').textContent = e.message;
    }
  }

  async function xiHistory() {
    let d = null;
    try { d = await xiApi('/api/history?limit=30'); } catch (e) { return; }
    const box = $('xi-history');
    if (!box) return;
    const items = (d && d.items) || [];
    box.innerHTML = items.length ? items.map((it) => {
      const t = new Date(it.ts * 1000);
      const hh = String(t.getHours()).padStart(2, '0') + ':' + String(t.getMinutes()).padStart(2, '0');
      const q = it.kind === 'command' ? (it.query || it.text || '') : (it.text || '');
      const a = it.reply || it.detail || '';
      return `<div class="row">
        <span class="idx">${hh}</span>
        <span class="name">${esc(q)}</span>
        <span class="sub">${esc(a)}</span>
      </div>`;
    }).join('') : '<div class="empty">还没有记录</div>';
  }

  async function xiRouteTest(execute) {
    const text = $('xi-route-text').value.trim();
    const out = $('xi-route-out');
    if (!text) { out.textContent = '先说一句话'; return; }
    out.textContent = '处理中…';
    try {
      const d = await xiApi('/api/route/test', { text, execute });
      if (!d.hit) { out.textContent = '未命中唤醒词：' + (d.note || ''); return; }
      const src = String(d.plan.source || '');
      const name = { local: '本地处理', custom: '自定义指令', openclaw: '交给 OpenClaw', fallback: '本地兜底' }[src] || src;
      out.textContent = `${name} · 指令正文：${d.intent}` + (d.reply ? ` · 播报：${d.reply}` : '');
      if (execute) setTimeout(() => { xiState(); xiHistory(); }, 1500);
    } catch (e) {
      out.textContent = '失败：' + e.message;
    }
  }

  function renderXiaoai() {
    xiStopTimer();
    $('view').innerHTML = `
      <div class="panel strip">
        <div class="panel-body" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span class="pill" id="xi-dev">读取中…</span>
          <span class="hint" id="xi-meta" style="margin:0">—</span>
          <span style="flex:1"></span>
          <button class="btn ghost small" id="xi-open" type="button">${icon(I.external, 12)} 新窗口</button>
        </div>
      </div>

      <div class="cols">
        <div class="col">
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">正在播放</div>
              <div class="right"><span class="pill" id="xi-mode-text">—</span></div>
            </div>
            <div class="deck">
              <h2 class="deck-title" id="xi-title">未在播放</h2>
              <div class="deck-sub" id="xi-sub">—</div>
              <div class="progress"><i id="xi-bar"></i></div>
              <div class="progress-meta"><span id="xi-pos">—</span><span id="xi-qhint"></span></div>
              <div class="transport">
                <button class="tbtn" id="xi-prev" type="button" title="上一首">${icon(I.prev, 16)}</button>
                <button class="tbtn main" id="xi-play" type="button" title="播放">${icon(I.play, 17)}</button>
                <button class="tbtn" id="xi-next" type="button" title="下一首">${icon(I.next, 16)}</button>
                <button class="tbtn stop" id="xi-stop" type="button" title="停止">${icon(I.stop, 15)}</button>
                <span style="flex:1"></span>
                <div class="seg" id="xi-modes">
                  <button type="button" data-mode="seq">顺序</button>
                  <button type="button" data-mode="shuffle">随机</button>
                  <button type="button" data-mode="loop">循环</button>
                </div>
              </div>
              <div class="vol">
                <span class="hint">音量</span>
                <input type="range" id="xi-volume" min="0" max="100" step="1">
                <span class="vol-num" id="xi-vol-val">—</span>
              </div>
              <div class="vol">
                <span class="hint">播报音量</span>
                <input type="range" id="xi-speak-vol" min="0" max="100" step="1">
                <span class="vol-num" id="xi-speak-vol-val">—</span>
              </div>
              <div class="hint" style="margin:-4px 0 10px">播报时临时把音箱抬到这个音量，说完还原；0 表示不改动（只对云端合成通道生效，原生 TTS 用设备当前音量）</div>
              <div class="upnext">
                <div class="upnext-h">接下来</div>
                <div id="xi-upnext"></div>
              </div>
            </div>
          </div>
        </div>

        <div class="col">
          <div class="panel">
            <div class="panel-head"><div class="panel-title">唤醒词</div></div>
            <div class="panel-body">
              <p class="panel-note">先说「小爱同学」，再说下面的名字，后面的内容才归本机。加几个都行，命中一个就生效。</p>
              <label class="field"><span>名字</span><input id="xi-wake-names" placeholder="播放本地, 本地纳斯"></label>
              <label class="field"><span>正则（可选）</span><input id="xi-wake-pats" placeholder="播(放)?本地"></label>
              <label class="check"><input type="checkbox" id="xi-wake-bare"><span>不带唤醒词也响应自定义指令</span></label>
              <div class="btnrow">
                <button class="btn primary small" id="xi-wake-save" type="button">保存唤醒词</button>
                <span class="hint" id="xi-wake-hint" style="margin:0"></span>
              </div>
              <div style="border-top:1px solid var(--line);margin:14px 0 12px"></div>
              <label class="field" style="margin-bottom:10px"><span>试一句话</span>
                <input id="xi-route-text" placeholder="放一首周杰伦">
              </label>
              <div class="btnrow" style="margin-top:0">
                <button class="btn ghost small" id="xi-route-test" type="button">看谁处理</button>
                <button class="btn ghost small" id="xi-route-run" type="button">真跑一次</button>
                <span class="hint" id="xi-route-out" style="margin:0"></span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="cols">
        <div class="col">
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">播放队列 <span class="pill" id="xi-qcount">0 首</span></div>
              <div class="right"><button class="btn quiet small" id="xi-qrefresh" type="button">刷新</button></div>
            </div>
            <div class="panel-body tight"><div class="list" id="xi-queue"></div></div>
          </div>
        </div>

        <div class="col">
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">固定指令</div>
              <div class="right"><button class="btn quiet small" id="xi-cmd-new" type="button">+ 新建</button></div>
            </div>
            <div class="panel-body">
              <div class="list" id="xi-cmd-list"></div>

              <div id="xi-cmd-editor" hidden style="border-top:1px solid var(--line);padding-top:14px;margin-top:12px">
                <label class="field"><span>名称</span><input id="xi-cmd-name" placeholder="播放回忆"></label>
                <label class="field"><span>触发词（逗号分隔）</span><input id="xi-cmd-patterns" placeholder="播放回忆, 放回忆"></label>
                <div class="grid2">
                  <label class="field"><span>匹配</span>
                    <select id="xi-cmd-match">
                      <option value="contains">包含</option>
                      <option value="exact">完全一致</option>
                      <option value="startswith">开头匹配</option>
                      <option value="regex">正则</option>
                    </select>
                  </label>
                  <label class="field"><span>动作</span><select id="xi-cmd-action"></select></label>
                </div>
                <div class="grid2">
                  <label class="field" id="xi-f-query"><span>搜索词</span><input id="xi-cmd-query" placeholder="留空＝整个文件夹"></label>
                  <label class="field" id="xi-f-mode"><span>模式</span>
                    <select id="xi-cmd-mode">
                      <option value="seq">顺序</option>
                      <option value="shuffle">随机</option>
                      <option value="loop">循环</option>
                    </select>
                  </label>
                </div>
                <div class="grid2">
                  <label class="field" id="xi-f-path"><span>文件夹</span><input id="xi-cmd-path" placeholder="/vol1/1000/音乐/我的歌单"></label>
                  <label class="field" id="xi-f-limit"><span>上限（0=全部）</span><input id="xi-cmd-limit" type="number" min="0" value="30"></label>
                </div>
                <div class="grid3">
                  <label class="field" id="xi-f-value"><span>音量</span><input id="xi-cmd-value" type="number" min="0" max="100" value="40"></label>
                  <label class="field" id="xi-f-delta"><span>增减</span><input id="xi-cmd-delta" type="number" min="-50" max="50" value="10"></label>
                  <label class="field" id="xi-f-text"><span>要说的话</span><input id="xi-cmd-text" placeholder="支持 {query} {now}"></label>
                </div>
                <div class="grid2">
                  <label class="field"><span>播报</span><input id="xi-cmd-reply" placeholder="留空用默认话术"></label>
                  <label class="field"><span>优先级</span><input id="xi-cmd-priority" type="number" min="0" value="50"></label>
                </div>
                <div class="btnrow">
                  <button class="btn primary small" id="xi-cmd-save" type="button">保存指令</button>
                  <button class="btn ghost small" id="xi-cmd-cancel" type="button">取消</button>
                  <span class="hint" id="xi-cmd-hint" style="margin:0"></span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">本地曲库 <span class="pill" id="xi-libcount">—</span></div>
          <div class="right">
            <button class="btn quiet small" id="xi-roots-toggle" type="button">音乐路径</button>
            <button class="btn quiet small" id="xi-rescan" type="button">重建索引</button>
          </div>
        </div>
        <div class="panel-body">
          <div class="searchbar">
            <input class="input" id="xi-search" placeholder="搜歌手 / 歌名，回车播放">
            <button class="btn primary small" id="xi-search-btn" type="button">播放</button>
          </div>
          <div id="xi-roots-box" hidden>
            <div class="searchbar">
              <input class="input" id="xi-root-in" placeholder="NAS 上的目录，例如 /vol1/1000/音乐">
              <button class="btn primary small" id="xi-root-add" type="button">添加</button>
            </div>
            <div class="list" id="xi-roots"></div>
            <p class="hint" id="xi-root-note" hidden>
              曲库目录已变更，重启应用后才会挂进容器（约 20 秒）。
              <button class="btn quiet small" id="xi-root-restart" type="button">重启应用</button>
            </p>
          </div>
          <div class="chips" id="xi-chips"></div>
          <div class="list tall" id="xi-results"><div class="empty">输入关键词搜索，点结果直接播</div></div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">快捷指令</div>
          <div class="right">
            <span class="pill" id="xi-sc-count">—</span>
            <button class="btn quiet small" id="xi-sc-open" type="button">完整说明</button>
          </div>
        </div>
        <div class="panel-body">
          <div class="sc-bar">
            <span class="sc-k">URL</span>
            <code id="xi-sc-base">—</code>
            <button class="btn ghost small" id="xi-sc-copy" type="button">复制</button>
          </div>
          <div class="sc-list" id="xi-sc-list"></div>
          <p class="sc-foot">复制整条链接填进手机快捷指令即可，播控与固定指令同一写法。iOS 用 POST，安卓用 GET，手机与 NAS 需同一 Wi-Fi。</p>
        </div>
      </div>

      <div class="panel" style="margin-bottom:0">
        <div class="panel-head">
          <div class="panel-title">最近交互</div>
          <div class="right"><button class="btn quiet small" id="xi-hrefresh" type="button">刷新</button></div>
        </div>
        <div class="panel-body tight"><div class="list" id="xi-history"></div></div>
      </div>`;
    xiBind();
    xiState();
    xiQueue();
    xiArtists();
    xiWakeLoad();
    xiCmds();
    xiHistory();
    XI.timer = setInterval(() => {
      if (location.hash && location.hash !== '#/xiaoai') { xiStopTimer(); return; }
      xiState();
    }, 2500);
  }

  function xiBind() {
    $('xi-open').onclick = () => window.open(bridgeUrl(), '_blank');

    $('xi-prev').onclick = () => xiApi('/api/prev', {});
    $('xi-next').onclick = () => xiApi('/api/next', {});
    $('xi-stop').onclick = () => xiApi('/api/stop', {});
    $('xi-play').onclick = () => {
      xiApi(XI.playing ? '/api/pause' : '/api/resume', {})
        .then(() => setTimeout(xiState, 400));
    };
    document.querySelectorAll('#xi-modes button').forEach((b) => {
      b.onclick = () => xiApi('/api/mode', { mode: b.dataset.mode }).then(() => setTimeout(xiState, 300));
    });
    $('xi-volume').oninput = () => {
      const el = $('xi-volume');
      el.style.setProperty('--fill', el.value + '%');
      $('xi-vol-val').textContent = el.value;
    };
    // 拖动过程中静默生效（120ms 合并），松手再补一次，避免只改数字不动音箱
    let volTimer = null;
    $('xi-volume').onchange = () => {
      const v = Number($('xi-volume').value);
      $('xi-volume').style.setProperty('--fill', v + '%');
      $('xi-vol-val').textContent = v;
      clearTimeout(volTimer);
      volTimer = setTimeout(() => {
        xiApi('/api/volume', { volume: v }).then((d) => {
          if (d && d.ok === false) toast(d.error || '音量设置失败', true);
        });
      }, 120);
    };

    // 播报音量：只影响说话，不影响音乐
    $('xi-speak-vol').oninput = () => {
      const el = $('xi-speak-vol');
      el.style.setProperty('--fill', el.value + '%');
      $('xi-speak-vol-val').textContent = el.value;
    };
    $('xi-speak-vol').onchange = () => {
      const v = Number($('xi-speak-vol').value);
      xiApi('/api/speak', { speech_volume: v }).then((d) => {
        if (d && d.ok === false) toast(d.error || '播报音量保存失败', true);
        else toast(v ? `播报音量 ${v}` : '播报音量：不改动音箱音量');
      });
    };

    $('xi-qrefresh').onclick = xiQueue;
    $('xi-queue').onclick = (e) => {
      const row = e.target.closest('.row');
      if (!row || row.dataset.idx == null) return;
      xiApi('/api/jump', { index: Number(row.dataset.idx) })
        .then(() => setTimeout(() => { xiState(); xiQueue(); }, 600));
    };

    $('xi-search-btn').onclick = xiSearch;
    $('xi-search').onkeydown = (e) => { if (e.key === 'Enter') xiSearch(); };
    $('xi-results').onclick = (e) => {
      const row = e.target.closest('.row');
      if (!row || !row.dataset.tid) return;
      xiApi('/api/play_id', { id: row.dataset.tid })
        .then(() => setTimeout(() => { xiState(); xiQueue(); }, 800));
    };
    $('xi-chips').onclick = (e) => {
      const chip = e.target.closest('.chip-btn');
      if (!chip) return;
      xiApi('/api/play_local', { query: chip.dataset.artist, limit: 30 })
        .then(() => setTimeout(() => { xiState(); xiQueue(); }, 800));
    };
    $('xi-roots-toggle').onclick = () => {
      const box = $('xi-roots-box');
      box.hidden = !box.hidden;
      if (!box.hidden) xiRoots();
    };
    if ($('xi-root-add')) $('xi-root-add').onclick = xiRootAdd;
    if ($('xi-root-restart')) $('xi-root-restart').onclick = xiRootRestart;
    $('xi-rescan').onclick = () => {
      xiApi('/api/library/rescan', {}).then(() => {
        toast('索引重建完成');
        xiState(); xiArtists();
      });
    };

    $('xi-wake-save').onclick = async () => {
      try {
        const d = await xiApi('/api/wake', {
          names: $('xi-wake-names').value.split(/[,，\s]+/).filter(Boolean),
          patterns: $('xi-wake-pats').value.split(/[,，]+/).map((s) => s.trim()).filter(Boolean),
          bare_commands: $('xi-wake-bare').checked,
        });
        if (!d.ok) throw new Error(d.error || '保存失败');
        $('xi-wake-hint').textContent = '已保存 ' + new Date().toLocaleTimeString();
        toast('唤醒词已生效');
      } catch (e) {
        $('xi-wake-hint').textContent = e.message;
      }
    };
    $('xi-route-test').onclick = () => xiRouteTest(false);
    $('xi-route-run').onclick = () => xiRouteTest(true);

    $('xi-cmd-new').onclick = () => { XI.editing = null; xiFillEditor(null); $('xi-cmd-name').focus(); };
    $('xi-cmd-cancel').onclick = () => { $('xi-cmd-editor').hidden = true; XI.editing = null; };
    $('xi-cmd-action').onchange = xiSyncFields;
    $('xi-cmd-save').onclick = xiCmdSave;
    $('xi-cmd-list').onclick = async (e) => {
      const btn = e.target.closest('button[data-op]');
      if (!btn) return;
      const row = btn.closest('.row');
      const cmd = XI.cmds.find((c) => c.id === row.dataset.id);
      if (!cmd) return;
      if (btn.dataset.op === 'edit') {
        XI.editing = cmd.id;
        xiFillEditor(cmd);
        return;
      }
      if (btn.dataset.op === 'del') {
        if (!confirm('删除这条指令？\n' + cmd.name)) return;
        await xiApi('/api/commands/delete', { id: cmd.id });
        xiCmds();
      }
    };

    $('xi-hrefresh').onclick = xiHistory;
  }

  // ================= 账号 =================
  function renderAccount() {
    $('view').innerHTML = `
      <div class="cols even">
        <div class="col">
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">小米账号</div>
              <div class="right"><span class="pill" id="ac-tag">读取中…</span></div>
            </div>
            <div class="panel-body">
              <div class="facts" id="ac-rows"></div>

              <div class="btnrow" style="margin-bottom:4px">
                <button class="btn primary small" id="ac-qr-start" type="button">扫码登录</button>
                <button class="btn ghost small" id="ac-pwd-toggle" type="button">账号密码</button>
              </div>

              <div class="qr-wrap" id="ac-qr" hidden>
                <div class="qr-box" id="ac-qr-svg"></div>
                <div class="qr-side">
                  <div id="ac-qr-msg">用米家 App 扫码</div>
                  <div class="hint" id="ac-qr-tip"></div>
                </div>
              </div>

              <div id="ac-pwd" hidden style="border-top:1px solid var(--line);margin-top:14px;padding-top:14px">
                <label class="field"><span>小米账号（手机号 / 邮箱）</span><input id="ac-account" placeholder="18600000000"></label>
                <label class="field"><span>密码</span><input id="ac-password" type="password" placeholder="密码"></label>
                <div class="btnrow">
                  <button class="btn primary small" id="ac-pwd-btn" type="button">登录</button>
                  <span class="hint" style="margin:0">密码登录常被风控拦截，扫码更稳</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="col">
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">音箱设备</div>
              <div class="right"><button class="btn quiet small" id="ac-dev-refresh" type="button">刷新</button></div>
            </div>
            <div class="panel-body tight">
              <p class="panel-note" style="padding:4px 4px 0">点一行切换小爱音箱当前使用的设备</p>
              <div id="ac-dev-list"><div class="empty">读取中…</div></div>
            </div>
          </div>

          <div class="panel">
            <div class="panel-head"><div class="panel-title">当前设备</div></div>
            <div class="panel-body">
              <div class="facts" style="margin-bottom:0">
                <div class="fact"><em>名称</em><b id="ac-devf-name">—</b></div>
                <div class="fact"><em>型号</em><b class="mono" id="ac-devf-hw">—</b></div>
                <div class="fact"><em>在线状态</em><b id="ac-devf-online">—</b></div>
                <div class="fact"><em>音量</em><b class="mono" id="ac-devf-vol">—</b></div>
              </div>
            </div>
          </div>
        </div>
      </div>`;
    bindAccount();
  }

  function paintAccountExtra() {
    const dev = (state.st && state.st.device) || {};
    const set = (id, v) => {
      const el = $(id);
      if (el) el.textContent = (v === 0 ? '0' : (v || '—'));
    };
    set('ac-devf-name', dev.name);
    set('ac-devf-hw', dev.hardware);
    set('ac-devf-online', dev.online ? '在线' : '离线');
    const on = $('ac-devf-online');
    if (on) on.classList.toggle('bad', !dev.online);
    set('ac-devf-vol', typeof dev.volume === 'number' ? dev.volume : '—');
  }

  let miQrTimer = null;
  function stopMiQr() {
    if (miQrTimer) clearInterval(miQrTimer);
    miQrTimer = null;
  }

  async function miPaint() {
    const d = await loadMi();
    const a = state.mi || {};
    const on = !!a.logged_in;
    const ok = !!state.mi.reachable;
    const tag = $('ac-tag');
    if (tag) {
      tag.className = 'pill ' + (ok ? (on ? 'ok' : 'bad') : 'bad');
      tag.textContent = !ok ? '读不到' : (on ? '已登录' : '未登录');
    }
    const rows = $('ac-rows');
    if (rows) {
      const items = ok ? [
        ['登录状态', on ? '正常' : '需要登录', !on],
        ['用户 ID', a.user_id || '—'],
        ['账号', a.account || '（扫码登录）'],
        ['Token 更新', fmtTs(a.token_updated)],
        ['当前设备', a.device_id ? cutId(a.device_id) : '—'],
        ['登录方式', a.has_password ? '密码已保存' : '仅扫码'],
      ] : [['状态', '桥接服务（8790）没响应', true]];
      rows.innerHTML = items.map(([k, v, bad]) =>
        `<div class="fact"><em>${k}</em><b class="${bad ? 'bad' : ''}" title="${esc(v)}">${esc(v)}</b></div>`).join('');
    }
    return d;
  }

  async function miQrStart() {
    const btn = $('ac-qr-start');
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '获取中…';
    try {
      const d = await api('/proxy/bridge/api/mi/qr', {});
      if (!d.ok) throw new Error(d.error || '获取二维码失败');
      $('ac-qr').hidden = false;
      $('ac-qr-svg').innerHTML = d.qr_svg || '';
      $('ac-qr-msg').textContent = d.tip || '用米家 App 扫码';
      $('ac-qr-tip').textContent = '有效期 ' + (d.expires_in || 0) + ' 秒';
      stopMiQr();
      miQrTimer = setInterval(miQrPoll, 2000);
    } catch (e) {
      toast('获取二维码失败：' + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  async function miQrPoll() {
    let d;
    try { d = await api('/proxy/bridge/api/mi/qr'); } catch (e) { return; }
    if (!d) return;
    if (d.state === 'ok') {
      stopMiQr();
      $('ac-qr').hidden = true;
      toast('登录成功：' + (d.user_id || ''));
      miPaint();
      miDevices();
    } else if (d.state === 'error') {
      stopMiQr();
      $('ac-qr-msg').textContent = d.message || '登录失败';
      $('ac-qr-tip').textContent = '点「扫码登录」重新生成二维码';
    } else {
      $('ac-qr-tip').textContent = '有效期 ' + (d.expires_in || 0) + ' 秒';
    }
  }

  async function miPwdLogin() {
    const btn = $('ac-pwd-btn');
    btn.disabled = true;
    btn.textContent = '登录中…';
    try {
      const d = await api('/proxy/bridge/api/mi/password', {
        account: $('ac-account').value.trim(),
        password: $('ac-password').value,
      });
      if (!d.ok) throw new Error(d.error || '登录失败');
      toast('登录成功');
      $('ac-password').value = '';
      $('ac-pwd').hidden = true;
      miPaint();
      miDevices();
    } catch (e) {
      toast('登录失败：' + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = '登录';
    }
  }

  async function miDevices() {
    const box = $('ac-dev-list');
    if (!box) return;
    try {
      const d = await api('/proxy/bridge/api/mi/devices', {});
      if (!d.ok) throw new Error(d.error || '读取失败');
      const list = d.devices || [];
      box.innerHTML = list.length ? list.map((x) => `
        <button class="dev ${x.current ? 'on' : ''}" type="button"
                data-id="${esc(x.device_id)}" data-hw="${esc(x.hardware)}" data-nm="${esc(x.name)}">
          <i class="led ${x.current ? 'ok' : ''}"></i>
          <span class="dev-name">${esc(x.name || '未命名')}</span>
          <span class="dev-hw">${esc(x.hardware || '')}</span>
        </button>`).join('') : '<div class="empty">账号下没有设备</div>';
    } catch (e) {
      box.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    }
  }

  function bindAccount() {
    $('ac-qr-start').onclick = miQrStart;
    $('ac-pwd-toggle').onclick = () => {
      const box = $('ac-pwd');
      box.hidden = !box.hidden;
      if (!box.hidden && !$('ac-account').value) $('ac-account').focus();
    };
    $('ac-pwd-btn').onclick = miPwdLogin;
    $('ac-dev-refresh').onclick = miDevices;
    $('ac-dev-list').onclick = async (e) => {
      const row = e.target.closest('.dev');
      if (!row) return;
      try {
        const d = await api('/proxy/bridge/api/mi/device', {
          device_id: row.dataset.id,
          hardware: row.dataset.hw,
          name: row.dataset.nm,
        });
        if (!d.ok) throw new Error(d.error || '切换失败');
        toast('已切换到 ' + (row.dataset.nm || row.dataset.id));
        miPaint();
        miDevices();
      } catch (err) {
        toast('切换失败：' + err.message, true);
      }
    };
    miPaint().then(() => {
      const acc = $('ac-account');
      if (acc && !acc.value && state.mi && state.mi.account) acc.value = state.mi.account;
      paintAccountExtra();
    });
    loadState().then(paintAccountExtra);
    miDevices();
    paintChrome();
  }

  // ================= OpenClaw =================
  function renderOpenclawPage() {
    $('view').innerHTML = `
      <div class="cols">
        <div class="col">
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">连接状态</div>
              <div class="right">
                <span class="pill" id="oc-state">读取中…</span>
                <button class="btn quiet small" id="oc-refresh" type="button">刷新</button>
              </div>
            </div>
            <div class="panel-body">
              <div class="facts">
                <div class="fact"><em>网关地址</em><b class="mono" id="oc-base">—</b></div>
                <div class="fact"><em>后端模型</em><b class="mono" id="oc-model">—</b></div>
                <div class="fact"><em>响应延迟</em><b class="mono" id="oc-lat">—</b></div>
                <div class="fact"><em>最近错误</em><b id="oc-err">—</b></div>
              </div>
              <div class="panel-note" style="margin-bottom:6px">MCP 工具</div>
              <div class="chips" id="oc-mcp" style="margin-bottom:0"></div>
            </div>
          </div>

          <div class="panel">
            <div class="panel-head"><div class="panel-title">网关设置</div></div>
            <div class="panel-body">
              <div class="grid2">
                <label class="field"><span>网关地址</span><input id="oc-f-base" placeholder="http://127.0.0.1:48670"></label>
                <label class="field"><span>后端模型</span><select id="oc-f-backend"></select></label>
              </div>
              <p class="tip" style="margin:-4px 0 12px">后端模型选中即写回 openclaw.json，网关约 1 秒热加载。</p>
              <div class="grid2">
                <label class="field"><span>Agent ID</span><input id="oc-f-agent" placeholder="main"></label>
                <label class="field"><span>会话 Key</span>
                  <span class="row-inline">
                    <select id="oc-f-session"></select>
                    <button class="btn ghost small" id="oc-f-session-new" type="button">新增</button>
                  </span>
                  <input id="oc-f-session-custom" class="input" placeholder="新会话 key，如 xiaoai:study" style="display:none;margin-top:6px">
                </label>
              </div>
              <div class="grid3">
                <label class="field"><span>超时（秒，0=不限）</span><input id="oc-f-timeout" type="number" min="0" max="300" value="0"></label>
                <label class="field"><span>启用</span>
                  <select id="oc-f-enabled"><option value="1">启用</option><option value="0">停用</option></select>
                </label>
                <label class="field"><span>访问令牌</span><input id="oc-f-token" type="password" placeholder="留空＝不修改"></label>
              </div>
              <div class="btnrow">
                <button class="btn primary small" id="oc-save" type="button">保存设置</button>
                <span class="hint" id="oc-save-hint" style="margin:0"></span>
              </div>
            </div>
          </div>
        </div>

        <div class="col">
          <div class="panel">
            <div class="panel-head"><div class="panel-title">指令 · 触发词</div></div>
            <div class="panel-body">
              <p class="panel-note">叫到这些词，整句转给 OpenClaw 处理（可写多个，逗号分隔）。和「唤醒词」那套只管本机执行，是两条线，互不影响。</p>
              <label class="field"><span>触发词</span><input id="oc-wakes" placeholder="让小龙虾, 跟小龙虾说"></label>
              <div class="btnrow">
                <button class="btn primary small" id="oc-wakes-save" type="button">保存触发词</button>
                <span class="hint" id="oc-wakes-hint" style="margin:0"></span>
              </div>
            </div>
          </div>

          <div class="panel">
            <div class="panel-head"><div class="panel-title">连通测试</div></div>
            <div class="panel-body">
              <label class="field"><span>发送内容</span><input id="oc-test-text" value="只回两个字：通了"></label>
              <div class="btnrow">
                <button class="btn ghost small" id="oc-test" type="button">发一句试试</button>
                <span class="hint" id="oc-test-hint" style="margin:0">首次调用可能要几十秒（模型冷启动）</span>
              </div>
              <pre class="console" id="oc-test-out" style="margin-top:12px;display:none"></pre>
            </div>
          </div>

          <div class="panel">
            <div class="panel-head"><div class="panel-title">分流统计</div></div>
            <div class="panel-body">
              <div class="facts" style="margin-bottom:0">
                <div class="fact"><em>本地规则</em><b class="mono" id="oc-rt-local">—</b></div>
                <div class="fact"><em>自定义指令</em><b class="mono" id="oc-rt-custom">—</b></div>
                <div class="fact"><em>转 OpenClaw</em><b class="mono" id="oc-rt-oc">—</b></div>
                <div class="fact"><em>本地兜底</em><b class="mono" id="oc-rt-fb">—</b></div>
              </div>
              <p class="tip" id="oc-rt-note" style="margin:12px 0 0">统计自桥接启动</p>
            </div>
          </div>
        </div>
      </div>`;
    bindOpenclaw();
  }

  async function ocLoad() {
    const st = $('oc-state');
    try {
      const d = await api('/proxy/bridge/api/openclaw');
      const a = (d && d.agent) || {};
      const online = !!(d.reachable || a.online);
      if (st) {
        st.className = 'pill ' + (online ? 'ok' : 'bad');
        st.textContent = online ? '在线' : '离线';
      }
      const set = (id, v) => { const el = $(id); if (el) el.textContent = v || '—'; };
      set('oc-base', a.base_url);
      set('oc-lat', a.last_ms ? a.last_ms + ' ms' : '—');
      set('oc-err', a.last_error || '无');
      set('oc-model', state.backendModel || a.model);

      const f = $('oc-f-base');
      if (f) {
        f.value = a.base_url || '';
        $('oc-f-agent').value = a.agent_id || 'main';
        ocSessionsSelect(a.session_key || '');
        $('oc-f-timeout').value = (a.timeout == null ? 0 : a.timeout);
        $('oc-f-enabled').value = a.enabled ? '1' : '0';
      }
      const box = $('oc-mcp');
      if (box) {
        const servers = ((d && d.openclaw_config) || {}).servers || {};
        const keys = Object.keys(servers);
        box.innerHTML = keys.length ? keys.map((k) => {
          const s = servers[k] || {};
          return `<span class="chip" title="${esc(s.url || s.command || '')}"><i class="led ok"></i>${esc(k)} · ${esc(s.transport || 'stdio')}</span>`;
        }).join('') : '<span class="hint" style="margin:0">网关没配 MCP 工具</span>';
      }
      // 地址还是空的（新装或刚清空）：自己识别一次，别让页面一直挂着「未配置」
      if (!a.base_url && !state.ocAuto) {
        state.ocAuto = true;
        ocAutoDiscover();
      }
    } catch (e) {
      if (st) { st.className = 'pill bad'; st.textContent = '桥接未响应'; }
    }
  }

  // 网关地址为空时调桥接的探测接口，把第一个通的地址写进配置
  async function ocAutoDiscover() {
    const hint = $('oc-save-hint');
    if (hint) hint.textContent = '识别网关中…';
    try {
      const d = await api('/proxy/bridge/api/openclaw/discover');
      const ok = ((d && d.gateways) || []).filter((g) => g.ok);
      if (!ok.length) {
        if (hint) hint.textContent = '没识别到网关，请在下面手动填写';
        return;
      }
      const r = await api('/proxy/bridge/api/openclaw/config', { base_url: ok[0].url });
      if (!r.ok) throw new Error(r.error || '写入失败');
      if (hint) hint.textContent = '已识别 ' + ok[0].url;
      toast('已自动识别网关 ' + ok[0].url);
      ocLoad();
    } catch (e) {
      if (hint) hint.textContent = '';
      toast('识别网关失败：' + e.message, true);
    }
  }

  let ocSessions = [];
  async function ocSessionsLoad() {
    let d = null;
    try { d = await api('/proxy/bridge/api/openclaw/sessions'); } catch (e) { d = null; }
    ocSessions = (d && d.sessions) || [];
    const sel = $('oc-f-session');
    if (!sel) return;
    const keep = sel.value;
    sel.innerHTML = '';
    const add = (k) => {
      const op = document.createElement('option');
      op.value = k; op.textContent = k;
      sel.appendChild(op);
    };
    if (!ocSessions.length) add('xiaoai:livingroom');
    ocSessions.forEach(add);
    if (keep && !ocSessions.includes(keep)) add(keep);
    if (keep) sel.value = keep;
  }

  function ocSessionsSelect(cur) {
    const sel = $('oc-f-session');
    if (!sel) return;
    const key = String(cur || '').trim();
    if (key && !Array.from(sel.options).some((o) => o.value === key)) {
      const op = document.createElement('option');
      op.value = key; op.textContent = key;
      sel.insertBefore(op, sel.firstElementChild);
    }
    if (key) sel.value = key;
  }

  async function ocBackendLoad() {
    const sel = $('oc-f-backend');
    if (!sel) return;
    let d = null;
    try { d = await api('/proxy/bridge/api/openclaw/backend-models'); } catch (e) { d = null; }
    if (!d || !d.ok) {
      const m = $('oc-model');
      if (m) m.textContent = (d && d.error) || '读不到';
      return;
    }
    const cur = String(d.current || '').toLowerCase();
    state.backendModel = d.current || '';
    sel.innerHTML = '';
    (d.options || []).forEach((o) => {
      const op = document.createElement('option');
      op.value = o.value;
      op.textContent = o.label;
      if (o.value.toLowerCase() === cur) op.selected = true;
      sel.appendChild(op);
    });
    if (d.current && !(d.options || []).some((o) => o.value.toLowerCase() === cur)) {
      const op = document.createElement('option');
      op.value = d.current;
      op.textContent = d.current + '（当前，不在清单里）';
      op.selected = true;
      sel.insertBefore(op, sel.firstElementChild);
    }
    const m = $('oc-model');
    if (m) m.textContent = d.current || '—';
  }

  async function ocWakesLoad() {
    let d = null;
    try { d = await api('/proxy/bridge/api/router'); } catch (e) { d = null; }
    if (!d) return;
    const el = $('oc-wakes');
    if (el) el.value = (d.openclaw_wakes || []).join(', ');
    state.routerMode = d.openclaw_mode || 'fallback';
    ocRoutePaint();
  }

  function ocRoutePaint() {
    const r = (state.st && state.st.router) || {};
    const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    set('oc-rt-local', r.local || 0);
    set('oc-rt-custom', r.custom || 0);
    set('oc-rt-oc', r.openclaw || 0);
    set('oc-rt-fb', r.fallback || 0);
    const note = $('oc-rt-note');
    if (note) {
      const mode = state.routerMode === 'always' ? '全部转后台' : '按关键词兜底';
      note.textContent = `统计自桥接启动 · 当前模式：${mode}`;
    }
  }

  function bindOpenclaw() {
    $('oc-refresh').onclick = () => {
      ocLoad(); ocBackendLoad(); stamp();
      loadState().then(ocRoutePaint);
    };

    $('oc-save').onclick = async () => {
      const btn = $('oc-save');
      btn.disabled = true;
      $('oc-save-hint').textContent = '保存中…';
      try {
        const body = {
          base_url: $('oc-f-base').value.trim(),
          agent_id: $('oc-f-agent').value.trim() || 'main',
          session_key: ($('oc-f-session-custom').value.trim() || $('oc-f-session').value.trim()),
          timeout: parseInt($('oc-f-timeout').value || '0', 10) || 0,
          enabled: $('oc-f-enabled').value === '1',
        };
        const tk = $('oc-f-token').value.trim();
        if (tk) body.token = tk;
        const d = await api('/proxy/bridge/api/openclaw/config', body);
        if (!d.ok) throw new Error(d.error || '保存失败');
        $('oc-save-hint').textContent = '已保存 ' + new Date().toLocaleTimeString();
        $('oc-f-token').value = '';
        toast('网关设置已保存');
        ocLoad();
      } catch (e) {
        $('oc-save-hint').textContent = '';
        toast('保存失败：' + e.message, true);
      } finally {
        btn.disabled = false;
      }
    };

    $('oc-test').onclick = async () => {
      const btn = $('oc-test');
      const out = $('oc-test-out');
      btn.disabled = true;
      $('oc-test-hint').textContent = '请求中，可能要几十秒…';
      out.style.display = 'block';
      out.textContent = '等待网关返回…';
      try {
        const d = await api('/proxy/bridge/api/openclaw/test',
          { text: $('oc-test-text').value.trim() || '只回两个字：通了' });
        out.textContent = JSON.stringify(d, null, 2);
        $('oc-test-hint').textContent = d.ok ? `通了 · ${d.ms} ms` : '失败';
      } catch (e) {
        out.textContent = '请求失败：' + e.message;
        $('oc-test-hint').textContent = '失败';
      } finally {
        btn.disabled = false;
      }
    };

    $('oc-f-backend').onchange = async () => {
      const sel = $('oc-f-backend');
      const val = sel.value;
      if (!val) return;
      sel.disabled = true;
      try {
        const d = await api('/proxy/bridge/api/openclaw/backend-model', { model: val });
        if (!d.ok) throw new Error(d.error || '切换失败');
        toast('已切到 ' + (d.model || val));
        const m = $('oc-model');
        if (m) m.textContent = d.model || val;
        state.backendModel = d.model || val;
        setTimeout(ocLoad, 1500);
      } catch (e) {
        toast('切换失败：' + e.message, true);
        ocBackendLoad();
      } finally {
        sel.disabled = false;
      }
    };

    $('oc-f-session-new').onclick = () => {
      const i = $('oc-f-session-custom');
      const show = i.style.display === 'none';
      i.style.display = show ? 'block' : 'none';
      if (show) i.focus();
    };

    $('oc-wakes-save').onclick = async () => {
      const btn = $('oc-wakes-save');
      btn.disabled = true;
      $('oc-wakes-hint').textContent = '保存中…';
      try {
        const list = $('oc-wakes').value.split(/[,，\s]+/).filter(Boolean);
        const d = await api('/proxy/bridge/api/router', { openclaw_wakes: list });
        if (!d.ok) throw new Error(d.error || '保存失败');
        $('oc-wakes').value = list.join(', ');
        $('oc-wakes-hint').textContent = '已保存 ' + new Date().toLocaleTimeString();
        toast('触发词已保存');
      } catch (e) {
        $('oc-wakes-hint').textContent = '';
        toast('保存失败：' + e.message, true);
      } finally {
        btn.disabled = false;
      }
    };

    ocLoad();
    ocBackendLoad();
    ocSessionsLoad();
    ocWakesLoad();
    loadState().then(ocRoutePaint);
    paintChrome();
  }

  // ================= 更新 =================
  const UPD = { info: null, busy: false, target: '' };

  const fmtSize = (b) => {
    const n = Number(b || 0);
    if (!n) return '—';
    return n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.round(n / 1024) + ' KB';
  };

  const updTarget = (d) => {
    if (!d) return '';
    if (d.best_local && d.best_local.version) return d.best_local.version;
    if (d.github && d.github.has_update) return d.github.latest || '';
    return '';
  };

  // ---------------- 访问保护 ----------------
  const AUTH = { enabled: false };

  async function loadAuth() {
    try {
      const d = await api('/api/auth/status');
      if (d && d.ok !== false) AUTH.enabled = !!d.enabled;
    } catch (e) { /* 保持旧值，不打断刷新 */ }
  }

  function paintAuth() {
    const st = $('r-auth-state');
    if (!st) return;
    setLed('r-auth-led', AUTH.enabled ? 'ok' : 'bad');
    st.innerHTML = `<span class="pill ${AUTH.enabled ? 'ok' : 'bad'}">${AUTH.enabled ? '已开启' : '未开启'}</span>`;
    const btn = $('r-auth-btn');
    if (btn) btn.textContent = AUTH.enabled ? '修改' : '设置';
  }

  function authStatus(msg, bad) {
    const el = $('auth-status');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'dlg-status' + (bad ? ' bad' : '');
  }

  function openAuthDlg() {
    const pw = $('auth-pw');
    if (pw) pw.value = '';
    authStatus(AUTH.enabled ? '已开启。留空保存等于不修改，要取消请点「关闭保护」。' : '');
    $('auth-clear').hidden = !AUTH.enabled;
    $('auth-save').disabled = false;
    $('auth-mask').hidden = false;
    if (pw) setTimeout(() => pw.focus(), 30);
  }

  function authClose() {
    $('auth-mask').hidden = true;
  }

  async function authSave() {
    const pw = ($('auth-pw').value || '').trim();
    if (pw.length < 4) { authStatus('口令至少 4 位', true); return; }
    $('auth-save').disabled = true;
    const d = await api('/api/auth/save', { password: pw });
    $('auth-save').disabled = false;
    if (!d || !d.ok) { authStatus((d && d.error) || '保存失败', true); return; }
    AUTH.enabled = !!d.enabled;
    authClose();
    paintAuth();
    toast('已开启访问保护，其他设备打开要输入这个口令');
  }

  async function authClear() {
    if (!AUTH.enabled) return;
    $('auth-clear').disabled = true;
    const d = await api('/api/auth/save', { password: '' });
    $('auth-clear').disabled = false;
    if (!d || !d.ok) { authStatus((d && d.error) || '取消失败', true); return; }
    AUTH.enabled = false;
    authClose();
    paintAuth();
    toast('已关闭访问保护，同网段可直接打开');
  }

  function updPaintBtn(d) {
    const btn = $('btn-update');
    const lab = $('upd-label');
    if (!btn || !lab) return;
    const t = updTarget(d);
    if (t) {
      lab.textContent = '更新 v' + t;
      btn.classList.add('hot');
      btn.title = '发现新版本 v' + t;
    } else {
      lab.textContent = d && d.current ? '已是最新' : '检查更新';
      btn.classList.remove('hot');
      btn.title = d && d.current ? '当前 v' + d.current : '检查更新';
    }
  }

  async function updCheck(silent) {
    const btn = $('btn-update');
    if (btn) btn.classList.add('spin');
    try {
      const d = await api('/api/update/check');
      UPD.info = d;
      updPaintBtn(d);
      if (!silent) {
        if (updTarget(d)) updOpen();
        else toast('已是最新版本 v' + (d.current || '—'));
      }
    } catch (e) {
      UPD.info = null;
      updPaintBtn(null);
      if (!silent) toast('检查更新失败', true);
    } finally {
      if (btn) btn.classList.remove('spin');
    }
  }

  function updStatus(msg, bad) {
    const el = $('upd-status');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'dlg-status' + (bad ? ' bad' : '');
  }

  function updOpen() {
    const d = UPD.info || {};
    const loc = d.best_local;
    const gh = d.github || {};
    const t = updTarget(d);
    const rows = [`<div class="kv"><em>当前版本</em><b class="mono">v${esc(d.current || '—')}</b></div>`];

    if (loc) {
      rows.push(`<div class="kv"><em>可更新到</em><b class="mono">v${esc(loc.version)}</b></div>`);
      rows.push(`<div class="kv"><em>安装包</em><b>${esc(loc.name)} · ${fmtSize(loc.size)}</b></div>`);
      rows.push(`<div class="kv"><em>所在位置</em><b class="mono dim">${esc(loc.path)}</b></div>`);
    } else if (t) {
      rows.push(`<div class="kv"><em>可更新到</em><b class="mono">v${esc(t)}</b></div>`);
      rows.push(`<div class="kv"><em>来源</em><b>远端发布 · ${fmtSize(gh.size)}</b></div>`);
    } else {
      rows.push('<div class="kv"><em>状态</em><b>已是最新版本</b></div>');
    }

    let note;
    if (loc) {
      note = '更新会重启应用，约需 20–60 秒。小米登录态、曲库索引与配置都在数据目录里，不会被清掉。';
    } else if (t) {
      note = `远端有 v${esc(t)}，但本机没有对应安装包。把它放进 <span class="mono">${esc((d.dirs || [])[0] || '')}</span> 后再点一次即可。`;
    } else {
      note = `正在监视的目录：<span class="mono">${esc((d.dirs || []).join('  ·  '))}</span>。把新的 fpk 放进去，点按钮就能装。`;
    }

    if ((d.unsafe_dirs || []).length) {
      note += `<br><br>注意：监视目录 <span class="mono">${esc((d.unsafe_dirs || []).join('  ·  '))}</span>`
        + ' 对同组或其他用户可写。放进这个目录的安装包会被以 root 执行，不要指向共享盘。';
    }

    $('upd-body').innerHTML = `<div class="dlg-rows">${rows.join('')}</div><p class="dlg-note">${note}</p>`;
    $('upd-apply').hidden = !loc;
    $('upd-apply').disabled = false;
    $('upd-cancel').disabled = false;
    $('upd-cancel').textContent = '关闭';
    updStatus('');
    $('upd-mask').hidden = false;
  }

  function updClose() {
    $('upd-mask').hidden = true;
  }

  async function updApply() {
    const loc = UPD.info && UPD.info.best_local;
    if (!loc) { toast('没有可安装的本机安装包', true); return; }
    const btn = $('upd-apply');
    btn.disabled = true;
    const r = await api('/api/update/apply', { source: 'local', path: loc.path });
    if (!r.ok) {
      btn.disabled = false;
      updStatus(r.error || '启动更新失败', true);
      return;
    }
    UPD.busy = true;
    UPD.target = r.target;
    btn.hidden = true;
    $('upd-cancel').disabled = true;
    updStatus('正在安装 v' + r.target + '…');
    updPoll(r.target);
  }

  function updPoll(target) {
    const t0 = Date.now();
    let down = false;
    let stillOld = 0;
    const timer = setInterval(async () => {
      if (Date.now() - t0 > 240000) {
        clearInterval(timer);
        updStatus('等待超时，请到应用中心查看安装结果', true);
        $('upd-cancel').disabled = false;
        return;
      }
      let ver = '';
      let alive = false;
      try {
        const r = await fetch(BASE + 'health?t=' + Date.now(), { cache: 'no-store' });
        if (r.ok) { const j = await r.json(); ver = j.version || ''; alive = true; }
      } catch (e) { alive = false; }

      if (!alive) { down = true; updStatus('应用正在重启，等待恢复…'); return; }
      if (ver === target) {
        clearInterval(timer);
        updStatus('已更新到 v' + ver + '，正在刷新页面');
        $('upd-cancel').disabled = false;
        setTimeout(() => location.reload(), 1000);
        return;
      }
      if (down) {
        stillOld += 1;
        if (stillOld >= 3) {
          clearInterval(timer);
          updStatus('已恢复运行，但版本仍是 v' + ver + '，安装未生效', true);
          $('upd-cancel').disabled = false;
        } else {
          updStatus('已恢复运行，等待版本切换…');
        }
        return;
      }
      updStatus('正在安装 v' + target + '…');
    }, 2000);
  }

  // ================= 路由 =================
  const TITLES = {
    overview: ['概览', '这台机器现在在做什么'],
    account: ['账号', '小米账号与音箱设备'],
    xiaoai: ['小爱音响', '播放、唤醒词与曲库'],
    openclaw: ['OpenClaw', '模型网关与分流'],
  };

  function setActiveNav(key) {
    document.querySelectorAll('.nav-item').forEach((el) => {
      el.classList.toggle('active', el.getAttribute('data-nav') === key);
    });
  }

  function route() {
    xiStopTimer();
    const h = location.hash || '#/';
    let key = 'overview';
    if (h === '#/xiaoai') key = 'xiaoai';
    else if (h === '#/account') key = 'account';
    else if (h === '#/openclaw') key = 'openclaw';

    setActiveNav(key);
    stamp();
    $('page-title').textContent = TITLES[key][0];
    $('page-kicker').textContent = TITLES[key][1];

    if (key === 'xiaoai') return renderXiaoai();
    if (key === 'account') return renderAccount();
    if (key === 'openclaw') return renderOpenclawPage();
    renderOverview();
    refreshOverview();
  }

  function refreshCurrent() {
    const btn = $('btn-refresh');
    btn.classList.add('spin');
    setTimeout(() => btn.classList.remove('spin'), 700);
    const h = location.hash || '#/';
    if (h === '#/openclaw') { ocLoad(); ocBackendLoad(); stamp(); return; }
    if (h === '#/account') { miPaint(); miDevices(); stamp(); return; }
    if (h === '#/xiaoai') { xiState(); xiQueue(); xiHistory(); stamp(); return; }
    refreshOverview();
  }

  async function boot() {
    document.querySelectorAll('.nav-item').forEach((el) => {
      el.onclick = () => {
        const key = el.getAttribute('data-nav');
        location.hash = key === 'overview' ? '#/' : '#/' + key;
      };
    });
    $('btn-refresh').onclick = refreshCurrent;
    $('btn-update').onclick = () => {
      if (updTarget(UPD.info)) updOpen();
      else updCheck(false);
    };
    $('upd-close').onclick = updClose;
    $('upd-cancel').onclick = () => { if (!UPD.busy) updClose(); };
    $('upd-apply').onclick = updApply;
    $('upd-mask').onclick = (e) => { if (e.target === $('upd-mask') && !UPD.busy) updClose(); };
    $('auth-close').onclick = authClose;
    $('auth-save').onclick = authSave;
    $('auth-clear').onclick = authClear;
    $('auth-mask').onclick = (e) => { if (e.target === $('auth-mask')) authClose(); };
    window.addEventListener('hashchange', route);

    await loadMeta();
    await Promise.all([probeBridge(), loadOc(), loadState(), loadMi(), loadBackendModel(), loadAuth()]);
    paintChrome();
    route();

    setTimeout(() => updCheck(true), 1500);

    setInterval(() => {
      if (!location.hash || location.hash === '#/') {
        refreshOverview();
      } else if (location.hash === '#/account') {
        miPaint();
      }
    }, 30000);
  }

  boot();
})();
