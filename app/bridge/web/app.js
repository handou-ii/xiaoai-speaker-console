/* 小爱音箱控制台 —— 无依赖前端
   Copyright (c) 2026 HanDou. 许可条款见包内 LICENSE：仅限个人非商业使用，禁止商业分发。 */
(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    playing: false,
    paused: false,
    mode: 'shuffle',
    artistLoaded: false,
    qrTimer: null,
    miLoaded: false,
    ocLoaded: false,
    ocConfigured: false,
    editing: null,
  };

  // ---------- 基础 ----------
  async function api(path, body, method) {
    const opt = { method: method || (body === undefined ? 'GET' : 'POST') };
    if (body !== undefined) {
      opt.headers = { 'Content-Type': 'application/json' };
      opt.body = JSON.stringify(body);
    }
    const r = await fetch(path, opt);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  let toastTimer;
  function toast(msg, isErr) {
    const el = $('toast');
    el.textContent = msg;
    el.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.className = 'toast'), 3000);
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  function fmtTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function setRow(k, v, cls) {
    return `<div class="row2"><span class="k">${esc(k)}</span>` +
      `<span class="v ${cls || ''}">${esc(v)}</span></div>`;
  }

  // ---------- 状态刷新 ----------
  async function refreshState() {
    let d;
    try {
      d = await api('/api/state');
    } catch (e) {
      $('badge-device').dataset.state = 'bad';
      $('badge-device').querySelector('span').textContent = '服务异常';
      return;
    }

    const dev = $('badge-device');
    dev.dataset.state = d.device.online ? 'ok' : 'bad';
    dev.querySelector('span').textContent = d.device.online
      ? (d.device.name || '设备在线') : '设备离线';

    const lib = $('badge-library');
    lib.dataset.state = d.library.count ? 'ok' : 'warn';
    lib.querySelector('span').textContent = '曲库 ' + d.library.count + ' 首';

    const acc = $('badge-account');
    acc.dataset.state = d.account.logged_in ? 'ok' : 'bad';
    acc.querySelector('span').textContent = d.account.logged_in ? '账号已登录' : '账号未登录';

    const oc = $('badge-openclaw');
    if (!d.openclaw.configured) {
      oc.removeAttribute('data-state');
      oc.querySelector('span').textContent = '后台未配置';
    } else if (!d.openclaw.enabled) {
      oc.removeAttribute('data-state');
      oc.querySelector('span').textContent = '后台已关闭';
    } else if (d.openclaw.online === true) {
      oc.dataset.state = 'ok';
      oc.querySelector('span').textContent = '后台通';
    } else {
      oc.dataset.state = 'warn';
      oc.querySelector('span').textContent = '后台未通';
    }

    const up = d.engine.uptime;
    $('uptime').textContent = '已运行 ' + (up > 3600
      ? Math.floor(up / 3600) + ' 小时 ' + Math.floor((up % 3600) / 60) + ' 分'
      : Math.floor(up / 60) + ' 分 ' + (up % 60) + ' 秒');

    // 播放器
    const p = d.player;
    state.playing = p.playing;
    state.paused = p.paused;
    state.mode = p.mode;
    $('now-title').textContent = p.title || '未在播放';
    $('now-meta').textContent = p.title
      ? [p.artist, p.album].filter(Boolean).join(' · ')
      : '选一首歌，或者对小爱说「让小龙虾放周杰伦」';
    const modeName = { seq: '顺序', shuffle: '随机', loop: '循环', one: '单曲' }[p.mode] || p.mode;
    $('queue-pos').textContent = p.total
      ? p.index + ' / ' + p.total + ' · ' + modeName : '—';
    $('progress-fill').style.width = p.total
      ? ((p.index / p.total) * 100).toFixed(1) + '%' : '0%';
    $('progress-row').style.display = p.total ? '' : 'none';

    document.querySelectorAll('.modebtn').forEach((b) => {
      b.classList.toggle('active', b.dataset.mode === p.mode);
    });

    $('disc').classList.toggle('spinning', !!p.playing);
    $('ico-pause').style.display = p.playing ? '' : 'none';
    $('ico-play').style.display = p.playing ? 'none' : '';

    if (d.device.volume != null && document.activeElement !== $('volume')) {
      $('volume').value = d.device.volume;
      $('volume-val').textContent = d.device.volume;
    }

    refreshQueue();
    refreshHistory();
  }

  async function refreshQueue() {
    let d;
    try { d = await api('/api/queue'); } catch (e) { return; }
    const box = $('queue-list');
    $('queue-total').textContent = (d.queue.length || 0) + ' 首';
    if (!d.queue.length) {
      box.innerHTML = '<div class="empty">队列为空</div>';
      return;
    }
    box.innerHTML = d.queue.map((t) => `
      <div class="row ${t.current ? 'current' : ''}" data-idx="${t.index}">
        <span class="idx">${t.index + 1}</span>
        <div class="body">
          <div class="t">${esc(t.name)}</div>
          <div class="s">${esc(t.artist || '未知歌手')}${t.album ? ' · ' + esc(t.album) : ''}</div>
        </div>
      </div>`).join('');
  }

  async function refreshHistory() {
    let d;
    try { d = await api('/api/history?limit=40'); } catch (e) { return; }
    const box = $('history-list');
    $('hist-count').textContent = d.items.length + ' 条';
    if (!d.items.length) {
      box.innerHTML = '<div class="empty">还没有记录</div>';
      return;
    }
    const routeName = { local: '本地', openclaw: '后台', fallback: '本地兜底' };
    box.innerHTML = d.items.map((it) => {
      const t = new Date(it.ts * 1000);
      const hh = String(t.getHours()).padStart(2, '0') + ':' + String(t.getMinutes()).padStart(2, '0');
      const isCmd = it.kind === 'command';
      const q = isCmd ? (it.query || '') : (it.text || '');
      let a = '';
      if (isCmd && it.reply) a = '↳ ' + it.reply;
      if (!isCmd && it.detail) a = '↳ ' + it.detail;
      let badge = isCmd ? '语音' : '操作';
      if (isCmd && it.route) {
        const key = String(it.route).startsWith('custom') ? 'custom' : it.route;
        badge = routeName[key] || '本地';
      }
      const ms = it.ms ? (it.ms / 1000).toFixed(1) + 's' : '';
      return `<div class="log-item">
        <span class="log-time">${hh}</span>
        <span class="log-badge ${isCmd ? 'cmd' : 'act'}">${esc(badge)}</span>
        <div class="log-body">
          <div class="log-q">${esc(q)}</div>
          ${a ? `<div class="log-a">${esc(a)}</div>` : ''}
        </div>
        <span class="log-ms ${it.ms > 12000 ? 'slow' : ''}">${ms}</span>
      </div>`;
    }).join('');
  }

  // ---------- 控制 ----------
  async function act(path, body, okMsg) {
    try {
      const d = await api(path, body === undefined ? {} : body);
      if (d && d.ok === false) toast(d.error || '操作失败', true);
      else if (okMsg) toast(okMsg);
      await refreshState();
      return d;
    } catch (e) {
      toast('请求失败：' + e.message, true);
    }
  }

  // ---------- 搜索 ----------
  let searchTimer;
  async function doSearch() {
    const q = $('search').value.trim();
    const box = $('search-results');
    if (!q) { box.innerHTML = '<div class="empty">输入关键词搜索</div>'; return; }
    let d;
    try { d = await api('/api/search', { query: q, limit: 40 }); }
    catch (e) { toast('搜索失败', true); return; }
    if (!d.tracks.length) {
      box.innerHTML = '<div class="empty">没有匹配的曲目</div>';
      return;
    }
    box.innerHTML = d.tracks.map((t) => `
      <div class="row" data-tid="${t.id}">
        <div class="body">
          <div class="t">${esc(t.name)}</div>
          <div class="s">${esc(t.artist || '未知歌手')}${t.album ? ' · ' + esc(t.album) : ''}</div>
        </div>
      </div>`).join('');
  }

  async function loadArtists() {
    if (state.artistLoaded) return;
    let d;
    try { d = await api('/api/artists'); } catch (e) { return; }
    state.artistLoaded = true;
    const box = $('artist-chips');
    const items = d.artists.filter((a) => a.name && a.name !== '未知');
    box.innerHTML = items.slice(0, 80).map((a) => `
      <span class="chip" data-artist="${esc(a.name)}">${esc(a.name)}<b>${a.count}</b></span>`).join('');
  }

  // ---------- 音乐路径 ----------
  async function loadRoots() {
    let d;
    try { d = await api('/api/library/roots'); } catch (e) { return; }
    const box = $('roots-list');
    if (!d.roots.length) {
      box.innerHTML = '<div class="empty">还没有音乐目录</div>';
      return;
    }
    box.innerHTML = d.roots.map((r) => `
      <div class="pathrow">
        <span class="p" title="${esc(r.path)}">${esc(r.path)}</span>
        <span class="n">${r.exists ? r.count + ' 首' : '不可见'}</span>
        <button class="x" data-del="${esc(r.path)}" title="移除">×</button>
      </div>`).join('');
  }

  $('btn-roots-toggle').onclick = () => {
    const p = $('roots-panel');
    p.hidden = !p.hidden;
    if (!p.hidden) loadRoots();
  };

  $('btn-add-root').onclick = async () => {
    const path = $('root-input').value.trim();
    if (!path) return toast('先填路径', true);
    try {
      const d = await api('/api/library/roots', { path });
      if (!d.ok) return toast(d.error, true);
      $('root-input').value = '';
      toast('已添加，曲库 ' + d.count + ' 首');
      state.artistLoaded = false;
      loadRoots(); loadArtists(); refreshState();
    } catch (e) { toast('添加失败：' + e.message, true); }
  };

  $('roots-list').onclick = async (e) => {
    const b = e.target.closest('[data-del]');
    if (!b) return;
    const path = b.dataset.del;
    if (!confirm('移除音乐目录？\n' + path)) return;
    try {
      const d = await api('/api/library/roots/remove', { path });
      if (!d.ok) return toast(d.error, true);
      toast('已移除，曲库 ' + d.count + ' 首');
      state.artistLoaded = false;
      loadRoots(); loadArtists(); refreshState();
    } catch (err) { toast('移除失败：' + err.message, true); }
  };

  // ---------- 小米账号 ----------
  // 账号模块已搬到飞牛控制台的独立页面，桥接这边只在有卡片时才渲染
  function bindClick(id, fn) { const el = $(id); if (el) el.onclick = fn; }

  async function loadMi() {
    if (!$('mi-info')) return;
    let d;
    try { d = await api('/api/mi'); } catch (e) { return; }
    const a = d.account;
    $('mi-tag').textContent = a.logged_in ? '已登录' : '未登录';
    $('mi-info').innerHTML = [
      setRow('状态', a.logged_in ? '正常' : '需要登录', a.logged_in ? 'ok' : 'bad'),
      setRow('用户', a.user_id || '—', 'mono'),
      setRow('账号', a.account || '（扫码登录）'),
      setRow('设备', a.device_id || '—', 'mono'),
      setRow('Token', a.token_updated ? fmtTime(a.token_updated) + ' 更新' : '无'),
    ].join('');
    if ($('mi-account') && a.account) $('mi-account').value = a.account;
    state.miLoaded = true;
  }

  function stopQrPoll() {
    if (state.qrTimer) clearInterval(state.qrTimer);
    state.qrTimer = null;
  }

  bindClick('btn-qr', async () => {
    const b = $('btn-qr');
    b.disabled = true; b.textContent = '获取中…';
    try {
      const d = await api('/api/mi/qr', {});
      if (!d.ok) { toast(d.error || '获取二维码失败', true); return; }
      $('qr-box').hidden = false;
      $('qr-svg').innerHTML = d.qr_svg;
      $('qr-msg').textContent = d.tip || '用米家 App 扫码';
      $('qr-tip').textContent = '有效期 ' + d.expires_in + ' 秒';
      stopQrPoll();
      state.qrTimer = setInterval(pollQr, 2000);
    } catch (e) {
      toast('获取二维码失败：' + e.message, true);
    } finally {
      b.disabled = false; b.textContent = '扫码登录';
    }
  });

  async function pollQr() {
    let d;
    try { d = await api('/api/mi/qr'); } catch (e) { return; }
    if (d.state === 'ok') {
      stopQrPoll();
      $('qr-box').hidden = true;
      toast('登录成功：' + (d.user_id || ''));
      loadMi(); refreshState();
    } else if (d.state === 'error') {
      stopQrPoll();
      $('qr-msg').textContent = d.message || '登录失败';
      $('qr-tip').textContent = '点「扫码登录」重新生成二维码';
    } else {
      $('qr-tip').textContent = '有效期 ' + d.expires_in + ' 秒';
    }
  }

  bindClick('btn-pwd-toggle', () => {
    const box = $('pwd-box');
    box.hidden = !box.hidden;
    if (!box.hidden && !$('mi-account').value) $('mi-account').focus();
  });

  bindClick('btn-pwd-login', async () => {
    const b = $('btn-pwd-login');
    b.disabled = true; b.textContent = '登录中…';
    try {
      const d = await api('/api/mi/password', {
        account: $('mi-account').value.trim(),
        password: $('mi-password').value,
      });
      if (!d.ok) { toast(d.error || '登录失败', true); return; }
      toast('登录成功');
      $('mi-password').value = '';
      $('pwd-box').hidden = true;
      loadMi(); refreshState();
    } catch (e) {
      toast('登录失败：' + e.message, true);
    } finally {
      b.disabled = false; b.textContent = '登录';
    }
  });

  bindClick('btn-devices', async () => {
    const box = $('device-list');
    try {
      const d = await api('/api/mi/devices', {});
      if (!d.ok) return toast(d.error || '读取失败', true);
      box.hidden = false;
      box.innerHTML = d.devices.map((x) => `
        <div class="devrow ${x.current ? 'current' : ''}"
             data-id="${esc(x.device_id)}" data-hw="${esc(x.hardware)}" data-nm="${esc(x.name)}">
          <span class="nm">${esc(x.name || '未命名')}</span>
          <span class="hw">${esc(x.hardware)}</span>
        </div>`).join('') || '<div class="empty">账号下没有设备</div>';
    } catch (e) { toast('读取失败：' + e.message, true); }
  });

  bindClick('device-list', async (e) => {
    const row = e.target.closest('.devrow');
    if (!row) return;
    try {
      const d = await api('/api/mi/device', {
        device_id: row.dataset.id, hardware: row.dataset.hw, name: row.dataset.nm,
      });
      if (!d.ok) return toast(d.error, true);
      toast('已切换到 ' + (row.dataset.nm || row.dataset.id));
      loadMi(); refreshState();
    } catch (err) { toast('切换失败：' + err.message, true); }
  });

  // ---------- 唤醒词 ----------
  // 标签式编辑：一个名字一个标签，回车/「添加」收录，× 删除，空输入框退格删最后一个
  function tagEditor(listId, inputId, btnId) {
    let items = [];
    const list = $(listId);
    const input = $(inputId);

    function render() {
      list.innerHTML = items.map((p, i) =>
        `<span class="kwtag">${esc(p)}` +
        `<button type="button" data-i="${i}" title="删除">×</button></span>`).join('');
    }

    function add(raw) {
      String(raw || '').split(/[,，、;；\n]+/).map((s) => s.trim()).filter(Boolean)
        .forEach((p) => {
          if (items.length >= 8) return;
          if (!items.includes(p)) items.push(p);
        });
      render();
    }

    function commit() {
      if (input.value.trim()) {
        add(input.value);
        input.value = '';
      }
    }

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        commit();
      } else if (e.key === 'Backspace' && !input.value && items.length) {
        items.pop();
        render();
      }
    });

    list.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-i]');
      if (!b) return;
      items.splice(Number(b.dataset.i), 1);
      render();
    });

    const btn = $(btnId);
    if (btn) btn.onclick = () => { commit(); input.focus(); };

    return {
      get: () => items.slice(),
      set(v) { items = (v || []).slice(); render(); },
      commit,
    };
  }

  const wakeTags = tagEditor('wake-list', 'wake-input', 'btn-add-wake');
  const wakePatTags = tagEditor('wake-pat-list', 'wake-pat-input', 'btn-add-wake-pat');

  function renderWake(d) {
    wakeTags.set(d.names);
    wakePatTags.set(d.patterns);
    $('wake-bare').checked = !!d.bare_commands;
    $('wake-tag').textContent = (d.effective || []).join(' / ') || '—';
    if (d.sample) $('wake-test-text').placeholder = `试一句：${d.sample}`;
  }

  async function loadWake() {
    try { renderWake(await api('/api/wake')); } catch (e) { /* 老版本后端没有就忽略 */ }
  }

  $('btn-wake-save').onclick = async () => {
    wakeTags.commit();
    wakePatTags.commit();
    $('wake-err').textContent = '';
    try {
      const d = await api('/api/wake', {
        names: wakeTags.get(),
        patterns: wakePatTags.get(),
        bare_commands: $('wake-bare').checked,
      });
      if (!d.ok) {
        $('wake-err').textContent = d.error || '保存失败';
        return;
      }
      renderWake(d);
      toast('唤醒词已生效，直接说话就行');
    } catch (e) {
      $('wake-err').textContent = '保存失败：' + e.message;
    }
  };

  async function wakeTest() {
    const text = $('wake-test-text').value.trim();
    if (!text) return toast('先说一句话', true);
    const out = $('wake-out');
    out.innerHTML = '<span class="hint">识别中…</span>';
    try {
      const d = await api('/api/wake/test', { text });
      if (!d.ok) {
        out.innerHTML = `<span class="hint">${esc(d.error || '失败')}</span>`;
        return;
      }
      out.innerHTML = d.hit
        ? `<span class="pill ${d.bare ? 'custom' : 'local'}">` +
          `${d.bare ? '没叫名字也能处理' : '命中唤醒词'}</span>` +
          `后面的指令正文：${esc(d.intent)}`
        : `<span class="pill fallback">没命中</span>这句话不会被处理，` +
          `当前唤醒词：${esc((d.names || []).join(' / '))}` +
          (d.bare_enabled ? '' : '（想不叫名字也能触发自定义指令，勾上上面的开关）');
    } catch (e) {
      out.innerHTML = `<span class="hint">失败：${esc(e.message)}</span>`;
    }
  }
  $('btn-wake-test').onclick = wakeTest;
  $('btn-wake-run').onclick = async () => {
    const text = $('wake-test-text').value.trim();
    if (!text) return toast('先说一句话', true);
    const b = $('btn-wake-run');
    b.disabled = true;
    b.textContent = '执行中…';
    try {
      const d = await api('/api/simulate', { text });
      if (!d.ok) return toast(d.error || '失败', true);
      $('wake-out').innerHTML = '<span class="hint">已交给链路处理，看下面的「最近交互」和音箱。</span>';
      setTimeout(refreshState, 1500);
    } catch (e) {
      toast('失败：' + e.message, true);
    } finally {
      b.disabled = false;
      b.textContent = '真跑一遍';
    }
  };
  $('wake-test-text').onkeydown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      wakeTest();
    }
  };

  // ---------- 自定义指令 ----------
  const ACTION_FIELDS = {
    play: ['query', 'mode', 'limit'],
    play_folder: ['path', 'query', 'mode', 'limit'],
    pause: [], resume: [], next: [], prev: [], stop: [],
    volume: ['value', 'delta'],
    mode: ['mode'],
    say: ['text'],
    status: [], rescan: [], openclaw: [],
  };
  const ACTION_LABEL = {
    play: '播放（搜索词）', play_folder: '只播某个文件夹',
    pause: '暂停', resume: '继续', next: '下一首', prev: '上一首',
    stop: '停止', volume: '音量', mode: '播放模式', say: '只说一句',
    status: '报当前播放', rescan: '重建曲库索引', openclaw: '转给 OpenClaw',
  };
  let actionTypes = [];

  function syncFields() {
    const type = $('cmd-action').value;
    const need = ACTION_FIELDS[type] || [];
    const map = {
      path: 'f-path', query: 'f-query', mode: 'f-mode', limit: 'f-limit',
      value: 'f-value', delta: 'f-delta', text: 'f-text',
    };
    Object.entries(map).forEach(([k, id]) => {
      $(id).hidden = !need.includes(k);
    });
  }

  async function loadCommands() {
    let d;
    try { d = await api('/api/commands'); } catch (e) { return; }
    actionTypes = d.action_types.filter((t) => ACTION_FIELDS[t.type]);
    if (!$('cmd-action').options.length) {
      $('cmd-action').innerHTML = actionTypes.map((t) =>
        `<option value="${t.type}">${esc(t.label || ACTION_LABEL[t.type] || t.type)}</option>`).join('');
      syncFields();
    }
    const box = $('cmd-list');
    if (!d.commands.length) {
      box.innerHTML = '<div class="empty">还没有自定义指令</div>';
      return;
    }
    box.innerHTML = d.commands.map((c) => {
      const a = c.action || {};
      const desc = Object.entries(a).filter(([k]) => k !== 'type')
        .map(([k, v]) => k + '=' + v).join(' ');
      return `<div class="cmdrow ${c.enabled ? '' : 'off'}" data-id="${esc(c.id)}">
        <div class="body">
          <div class="t">${esc(c.name)}</div>
          <div class="s">${esc(c.patterns.join(' / '))} → ${esc(a.type || 'play')} ${esc(desc)}</div>
        </div>
        <div class="ops">
          <button data-op="toggle">${c.enabled ? '停用' : '启用'}</button>
          <button data-op="edit">编辑</button>
          <button data-op="del" class="del">删除</button>
        </div>
      </div>`;
    }).join('');
    state.commands = d.commands;
  }

  // 触发词：标签式编辑，可一个个加/删（一条指令可挂多个触发词，命中任一即可）
  let cmdPatterns = [];

  function renderPatterns() {
    $('cmd-pattern-list').innerHTML = cmdPatterns.map((p, i) =>
      `<span class="kwtag">${esc(p)}<button type="button" data-i="${i}" title="删除">×</button></span>`
    ).join('');
  }

  function addPatterns(raw) {
    String(raw || '').split(/[,，、;；]+/).map((s) => s.trim()).filter(Boolean)
      .forEach((p) => {
        if (cmdPatterns.length >= 12) return;
        if (!cmdPatterns.includes(p)) cmdPatterns.push(p);
      });
    renderPatterns();
  }

  function commitPatternInput() {
    const el = $('cmd-patterns');
    if (el.value.trim()) {
      addPatterns(el.value);
      el.value = '';
    }
  }

  $('btn-add-pattern').onclick = () => {
    commitPatternInput();
    $('cmd-patterns').focus();
  };

  $('cmd-patterns').onkeydown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      commitPatternInput();
    } else if (e.key === 'Backspace' && !$('cmd-patterns').value && cmdPatterns.length) {
      cmdPatterns.pop();                    // 输入框空了再退格 → 删掉最后一个标签
      renderPatterns();
    }
  };

  $('cmd-pattern-list').onclick = (e) => {
    const b = e.target.closest('button[data-i]');
    if (!b) return;
    cmdPatterns.splice(Number(b.dataset.i), 1);
    renderPatterns();
  };

  function fillEditor(c) {
    $('cmd-name').value = c ? c.name : '';
    cmdPatterns = c && c.patterns ? c.patterns.slice() : [];
    renderPatterns();
    $('cmd-patterns').value = '';
    $('cmd-match').value = c ? (c.match || 'contains') : 'contains';
    const a = (c && c.action) || {};
    const type = a.type && ACTION_FIELDS[a.type] ? a.type : 'play';
    $('cmd-action').value = type;
    $('cmd-path').value = a.path || '';
    $('cmd-query').value = a.query || '';
    $('cmd-mode').value = a.mode || 'seq';
    $('cmd-limit').value = a.limit != null ? a.limit : 30;
    $('cmd-value').value = a.value != null ? a.value : 40;
    $('cmd-delta').value = a.delta != null ? a.delta : 10;
    $('cmd-text').value = a.text || '';
    $('cmd-reply').value = c ? (c.reply || '') : '';
    $('cmd-priority').value = c ? c.priority : 50;
    $('cmd-err').textContent = '';
    syncFields();
  }

  $('btn-cmd-new').onclick = () => {
    state.editing = null;
    $('cmd-editor').hidden = false;
    fillEditor(null);
    $('cmd-name').focus();
  };
  $('btn-cmd-cancel').onclick = () => { $('cmd-editor').hidden = true; state.editing = null; };
  $('cmd-action').onchange = syncFields;

  $('btn-cmd-save').onclick = async () => {
    const type = $('cmd-action').value;
    const action = { type };
    const num = (id) => Number($(id).value);
    commitPatternInput();          // 输入框里还没点「添加」的词也算数
    if (type === 'play' || type === 'play_folder') {
      action.query = $('cmd-query').value.trim();
      action.mode = $('cmd-mode').value;
      const raw = $('cmd-limit').value.trim();
      const lim = raw === '' ? 0 : Number(raw);   // 0 / 留空 = 不限，整个文件夹
      action.limit = Number.isFinite(lim) ? lim : 0;
    }
    if (type === 'play_folder') action.path = $('cmd-path').value.trim();
    if (type === 'volume') {
      action.value = num('cmd-value');
      const delta = num('cmd-delta');
      if (delta) action.delta = delta;
    }
    if (type === 'mode') action.mode = $('cmd-mode').value;
    if (type === 'say') action.text = $('cmd-text').value;

    const body = {
      id: state.editing || undefined,
      name: $('cmd-name').value.trim(),
      patterns: cmdPatterns.slice(),
      match: $('cmd-match').value,
      action,
      reply: $('cmd-reply').value.trim(),
      priority: num('cmd-priority') || 50,
    };
    if (!body.patterns.length) { $('cmd-err').textContent = '至少一个触发词'; return; }
    try {
      const d = await api('/api/commands', body);
      if (!d.ok) { $('cmd-err').textContent = d.error || '保存失败'; return; }
      toast('已保存');
      $('cmd-editor').hidden = true;
      state.editing = null;
      loadCommands();
    } catch (e) { $('cmd-err').textContent = e.message; }
  };

  $('cmd-list').onclick = async (e) => {
    const btn = e.target.closest('button[data-op]');
    if (!btn) return;
    const row = btn.closest('.cmdrow');
    const id = row.dataset.id;
    const cmd = (state.commands || []).find((c) => c.id === id);
    if (!cmd) return;
    const op = btn.dataset.op;
    if (op === 'edit') {
      state.editing = id;
      $('cmd-editor').hidden = false;
      fillEditor(cmd);
      $('cmd-editor').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      return;
    }
    try {
      if (op === 'toggle') {
        await api('/api/commands', { id, enabled: !cmd.enabled });
      } else if (op === 'del') {
        if (!confirm('删除这条指令？\n' + cmd.name)) return;
        await api('/api/commands/delete', { id });
      }
      loadCommands();
    } catch (err) { toast('操作失败：' + err.message, true); }
  };

  // ---------- 路由测试 ----------
  async function routeTest(execute) {
    const text = $('route-text').value.trim();
    if (!text) return toast('先说一句话', true);
    const out = $('route-out');
    out.innerHTML = '<span class="hint">处理中…</span>';
    try {
      const d = await api('/api/route/test', { text, execute });
      if (!d.hit) {
        out.innerHTML = `<span class="pill fallback">未命中唤醒词</span>${esc(d.note || '')}`;
        return;
      }
      const src = d.plan.source;
      const kind = src.startsWith('custom') ? 'custom' : src;
      const name = { local: '本地处理', custom: '自定义指令', openclaw: '交给 OpenClaw', fallback: '本地兜底' }[kind] || kind;
      let html = (d.bare ? '<span class="pill custom">未带唤醒词</span>' : '') +
        `<span class="pill ${kind}">${esc(name)}</span>` +
        (d.plan.note ? esc(d.plan.note) : '') +
        `<div class="hint">指令正文：${esc(d.intent)}</div>`;
      if (d.plan.reply) html += `<div>播报：${esc(d.plan.reply)}</div>`;
      if (d.executed) html += `<div>执行结果：${esc(d.reply || '（无）')}</div>`;
      out.innerHTML = html;
      if (d.executed) refreshState();
    } catch (e) {
      out.innerHTML = `<span class="hint">失败：${esc(e.message)}</span>`;
    }
  }
  $('btn-route-test').onclick = () => routeTest(false);
  $('btn-route-run').onclick = () => routeTest(true);

  // OpenClaw 触发词（独立配置，和本地唤醒词互不相干）
  async function fillWakeSplit() {
    let r;
    try { r = await api('/api/router'); } catch (e) { return; }
    const R = r.router || r || {};
    $('rt-wakes').value = (R.openclaw_wakes || []).join(', ');
  }

  $('btn-rt-save').onclick = async () => {
    const split = (v) => v.split(/[,，\s]+/).filter(Boolean);
    try {
      const d = await api('/api/router', {
        openclaw_wakes: split($('rt-wakes').value),
      });
      if (!d.ok) return toast(d.error || '保存失败', true);
      toast('触发词已保存');
      fillWakeSplit();
    } catch (e) { toast('保存失败：' + e.message, true); }
  };

  // ---------- OpenClaw 网关 ----------

  // 没有网关地址时，Token / Agent / 会话 / 后端模型 / 超时 / 日志 先不展开
  function syncOcMore() {
    const on = !!(state.ocConfigured || ($('oc-base').value || '').trim());
    const more = $('oc-more');
    if (more) more.hidden = !on;
    const logFold = $('oc-log-fold');
    if (logFold) logFold.hidden = !on;
  }

  async function loadOpenclaw() {
    let d;
    try { d = await api('/api/openclaw'); } catch (e) { return; }
    const a = d.agent;
    $('oc-tag').textContent = !a.configured ? '未配置'
      : (d.reachable ? '已连通' : '不可达');
    $('oc-info').innerHTML = [
      setRow('状态', !a.configured ? '未配置（纯本地）'
        : (d.reachable ? '可达 · ' + (a.last_ms || '-') + 'ms' : (a.last_error || '不可达')),
        !a.configured ? '' : (d.reachable ? 'ok' : 'bad')),
      setRow('地址', a.base_url || '—'),
      // 没填网关地址时，Agent / 会话 / Token 都还没意义，先不显示
      ...(a.configured ? [
        setRow('Agent', a.agent_id || '—'),
        setRow('会话', a.session_key || '—'),
        setRow('Token', a.has_token ? (a.token_auto ? '已自动读取' : '已配置') : '未配置'),
      ] : []),
      setRow('本地/后台', (d.router.stats.local + d.router.stats.custom)
        + ' / ' + d.router.stats.openclaw + ' 次'),
    ].join('');

    state.ocConfigured = !!a.configured;
    syncOcMore();

    if (!state.ocLoaded) {
      $('oc-base').value = a.base_url || '';
      $('oc-agent').value = a.agent_id || '';
      $('oc-timeout').value = a.timeout || 0;
      $('oc-enabled').value = a.enabled ? '1' : '0';
      state.ocLoaded = true;
      await fillOcChoices(a);
      await fillWakeSplit();
      if (!a.configured && $('btn-oc-discover')) runDiscover(true);
    }

    const oc = d.openclaw_config || {};
    $('oc-log').textContent = (d.log_tail && d.log_tail.length)
      ? d.log_tail.join('\n')
      : `日志不可读（${oc.path || '/oc/gateway.log'}）。可在 compose 里挂载后查看。`;
    return d;
  }

  // 会话下拉：从网关自动识别已有会话，也可以自己填新的
  async function fillOcChoices(a) {
    let sessions = [];
    try { sessions = (await api('/api/openclaw/sessions')).sessions || []; } catch (e) {}
    const curSession = (a && a.session_key) || '';

    const selS = $('oc-session');
    selS.innerHTML = '';
    if (curSession && !sessions.includes(curSession)) sessions.unshift(curSession);
    (sessions.length ? sessions : ['xiaoai:livingroom']).forEach(k => {
      const o = document.createElement('option');
      o.value = k; o.textContent = k;
      if (k === curSession) o.selected = true;
      selS.appendChild(o);
    });
  }

  // 后端真实模型：读 openclaw.json 的 models.providers，选中即写回 primary
  async function loadBackendModels() {
    const sel = $('oc-backend');
    if (!sel) return;
    let d;
    try { d = await api('/api/openclaw/backend-models'); } catch (e) { return; }
    if (!d || !d.ok) {
      const hint = $('oc-backend-hint');
      if (hint) hint.textContent = (d && d.error) || '读不到 openclaw.json';
      return;
    }
    const cur = String(d.current || '').toLowerCase();
    sel.innerHTML = '';
    (d.options || []).forEach((o) => {
      const op = document.createElement('option');
      op.value = o.value;
      op.textContent = o.label;
      if (o.value.toLowerCase() === cur) op.selected = true;
      sel.appendChild(op);
    });
    if (d.current && !(d.options || []).some(o => o.value.toLowerCase() === cur)) {
      const op = document.createElement('option');
      op.value = d.current;
      op.textContent = d.current + '（当前，不在清单里）';
      op.selected = true;
      sel.insertBefore(op, sel.firstElementChild);
    }
    const hint = $('oc-backend-hint');
    if (hint) hint.textContent = d.current
      ? '当前：' + d.current + '（改后网关自动热加载）'
      : '还没设置后端模型';
  }

  $('oc-backend').onchange = async () => {
    const sel = $('oc-backend');
    const val = sel.value;
    if (!val) return;
    sel.disabled = true;
    try {
      const d = await api('/api/openclaw/backend-model', { model: val });
      if (!d.ok) throw new Error(d.error || '切换失败');
      toast('已切到 ' + (d.model || val));
      const hint = $('oc-backend-hint');
      if (hint) hint.textContent = '当前：' + (d.model || val) + '（网关热加载中，几秒生效）';
      setTimeout(loadOpenclaw, 2000);
    } catch (e) {
      toast('切换失败：' + e.message, true);
      loadBackendModels();
    } finally {
      sel.disabled = false;
    }
  };

  $('oc-base').addEventListener('input', syncOcMore);

  // auto = 页面初次加载时的静默探测：只显示结果，不替你填地址
  async function runDiscover(auto) {
    const b = $('btn-oc-discover');
    b.disabled = true; b.textContent = '识别中…';
    try {
      const d = await api('/api/openclaw/discover');
      const gs = d.gateways || [];
      $('oc-gateways').innerHTML = gs.map(g =>
        '<span class="gw ' + (g.ok ? 'ok' : 'bad') + '">' + g.url +
        (g.ok ? ' · ' + g.ms + 'ms' : ' · 不可达') + '</span>').join(' ');
      const ok = gs.filter(g => g.ok);
      if (ok.length) {
        if (auto) {
          // 只提示找到了，地址由用户点「自动识别」自己填
          $('oc-gateways').innerHTML += ' <span class="hint">点「自动识别」填入</span>';
        } else {
          $('oc-base').value = ok[0].url;
          syncOcMore();
          toast('发现 ' + ok.length + ' 个可用网关');
        }
      } else if (!auto) {
        toast('没发现可用网关', true);
      }
    } catch (e) {
      if (!auto) toast('识别失败：' + e.message, true);
    } finally {
      b.disabled = false; b.textContent = '自动识别';
    }
  }

  $('btn-oc-discover').onclick = () => runDiscover(false);

  $('btn-oc-new-session').onclick = () => {
    const i = $('oc-session-new');
    i.style.display = i.style.display === 'none' ? 'block' : 'none';
    if (i.style.display === 'block') i.focus();
  };

  $('btn-oc-save').onclick = async () => {
    const body = {
      base_url: $('oc-base').value.trim(),
      agent_id: $('oc-agent').value.trim(),
      session_key: ($('oc-session-new').value.trim() || $('oc-session').value.trim()),
      timeout: Number($('oc-timeout').value) || 0,
      enabled: $('oc-enabled').value === '1',
    };
    const tok = $('oc-token').value.trim();
    if (tok) body.token = tok;
    try {
      const d = await api('/api/openclaw/config', body);
      if (!d.ok) { $('oc-err').textContent = d.error || '保存失败'; return; }
      $('oc-token').value = '';
      $('oc-err').textContent = '';
      toast('配置已保存');
      state.ocLoaded = false;
      loadOpenclaw(); refreshState();
    } catch (e) { $('oc-err').textContent = e.message; }
  };

  $('btn-oc-test').onclick = async () => {
    const b = $('btn-oc-test');
    b.disabled = true; b.textContent = '测试中…';
    try {
      const d = await api('/api/openclaw/test', {});
      if (d.ok) toast('连通 ' + d.ms + 'ms：' + (d.reply || '').slice(0, 20));
      else toast(d.error || '不可达', true);
      loadOpenclaw(); refreshState();
    } catch (e) {
      toast('测试失败：' + e.message, true);
    } finally {
      b.disabled = false; b.textContent = '测试连通';
    }
  };

  async function loadMcp() {
    let d;
    try { d = await api('/api/mcp'); } catch (e) { return; }
    $('mcp-state').innerHTML = d.linked
      ? '已挂到 OpenClaw（xiaoai），agent 可以直接调下面的工具。'
      : '还没挂到 OpenClaw。把下面的配置贴进 openclaw.json 的 mcp.servers 即可。';
    $('mcp-tools').innerHTML = d.tools.map((t) =>
      `<span class="tool" title="${esc(t.description)}">${esc(t.name)}</span>`).join('');
    $('mcp-snippet').textContent = JSON.stringify(d.snippet, null, 2);
  }

  document.querySelectorAll('details.fold').forEach((el) => {
    el.addEventListener('toggle', () => {
      if (!el.open) return;
      const title = el.querySelector('summary').textContent;
      if (title.includes('MCP')) loadMcp();
      if (title.includes('网关日志')) loadOpenclaw();
    });
  });

  // ---------- 绑定 ----------
  $('btn-prev').onclick = () => act('/api/prev', {});
  $('btn-next').onclick = () => act('/api/next', {});
  $('btn-stop').onclick = () => act('/api/stop', {}, '已停止');
  $('btn-playpause').onclick = () => state.playing
    ? act('/api/pause', {}, '已暂停')
    : act('/api/resume', {}, '继续播放');
  $('btn-refresh').onclick = async () => {
    $('btn-refresh').classList.add('spin');
    await refreshState();
    loadMi();
    setTimeout(() => $('btn-refresh').classList.remove('spin'), 400);
  };

  $('mode-group').onclick = (e) => {
    const b = e.target.closest('.modebtn');
    if (!b) return;
    act('/api/mode', { mode: b.dataset.mode }, '模式：' + b.textContent);
  };

  let volTimer;
  $('volume').oninput = (e) => { $('volume-val').textContent = e.target.value; };
  $('volume').onchange = (e) => {
    const v = Number(e.target.value);
    clearTimeout(volTimer);
    volTimer = setTimeout(async () => {
      const d = await act('/api/volume', { volume: v });
      if (d && d.ok) toast('音量 ' + v);
    }, 120);
  };

  $('queue-list').onclick = (e) => {
    const row = e.target.closest('.row');
    if (!row || !row.dataset.idx) return;
    act('/api/jump', { index: Number(row.dataset.idx) });
  };

  $('search-results').onclick = (e) => {
    const row = e.target.closest('.row');
    if (!row || !row.dataset.tid) return;
    act('/api/play_id', { id: row.dataset.tid }, '开始播放');
  };

  $('artist-chips').onclick = (e) => {
    const c = e.target.closest('.chip');
    if (!c) return;
    const name = c.dataset.artist;
    $('search').value = name;
    act('/api/play_local', { query: name, mode: state.mode, limit: 30 },
        '播放 ' + name);
    doSearch();
  };

  $('search').oninput = () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(doSearch, 320);
  };
  $('search').onkeydown = (e) => {
    if (e.key === 'Enter') {
      const q = $('search').value.trim();
      if (q) act('/api/play_local', { query: q, mode: state.mode, limit: 30 },
                 '播放「' + q + '」');
    }
  };
  $('btn-play-search').onclick = () => {
    const q = $('search').value.trim();
    if (!q) return toast('先输入关键词', true);
    act('/api/play_local', { query: q, mode: state.mode, limit: 30 }, '播放「' + q + '」');
  };

  $('btn-rescan').onclick = () => act('/api/library/rescan', {}, '索引重建完成');

  // ---------- iOS 快捷指令 ----------
  async function loadShortcuts() {
    let d;
    try { d = await api('/api/shortcuts'); } catch (e) { return; }
    $('sc-base').textContent = d.base;
    const list = d.items || [];
    if (!list.length) {
      $('sc-list').innerHTML = '<div class="empty">还没有指令</div>';
      return;
    }
    $('sc-list').innerHTML = list.map((it) => {
      // 控制类和固定指令一个写法（都按 name），标题就是名字，不再额外显示参数
      return `<div class="sc-item">
        <span class="t">${esc(it.siri)}</span>
        ${it.control ? '<span class="tag-ctrl">控制</span>' : ''}
      </div>`;
    }).join('');
  }

  // ---------- 启动 ----------
  refreshState();
  loadArtists();
  loadMi();
  loadCommands();
  loadWake();
  fillWakeSplit();
  loadOpenclaw();
  loadBackendModels();
  loadShortcuts();
  setInterval(refreshState, 2500);
  setInterval(loadArtists, 60000);
})();
