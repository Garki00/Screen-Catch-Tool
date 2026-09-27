/* 屏幕捕获 web 端（P5）
 * 数据来源：Socket.IO 推送（主）+ REST 轮询（兜底）。
 * 只有「实时视图」用服务端预生成的裁剪图（/web/latest.jpeg），点开历史图时用
 * /web/view/<name> 现裁，保证 web 端看到的始终是范围框内的画面。
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const ui = {
    pillCapture: $('pillCapture'),
    pillConn: $('pillConn'),
    viewBadge: $('viewBadge'),
    shotInfo: $('shotInfo'),
    stageView: $('stageView'),
    live: $('live'),
    placeholder: $('placeholder'),
    newHint: $('newHint'),
    btnBackLive: $('btnBackLive'),
    btnPin: $('btnPin'),
    btnDownload: $('btnDownload'),
    btnRefresh: $('btnRefresh'),
    footTarget: $('footTarget'),
    footInterval: $('footInterval'),
    footCrop: $('footCrop'),
    footCache: $('footCache'),
    strip: $('strip'),
    stripEmpty: $('stripEmpty'),
    historyCount: $('historyCount'),
    toast: $('toast'),
  };

  const state = {
    images: [],          // 最新在前
    byName: new Map(),
    latestName: null,
    selected: null,
    pinned: new Set(),
    followLive: true,
    crop: { enabled: false, x: 0, y: 0, width: 0, height: 0 },
    status: {},
    maxSize: 20,
    connected: false,
  };

  const MAX_STRIP = 24;

  // ---------------------------------------------------------------- 工具

  const fmtTime = (iso) => {
    if (!iso) return '—';
    const part = String(iso).split('T');
    return part.length > 1 ? part[1] : String(iso);
  };

  let toastTimer = null;
  function toast(message, bad = false) {
    ui.toast.textContent = message;
    ui.toast.classList.toggle('toast--bad', !!bad);
    ui.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { ui.toast.hidden = true; }, 3200);
  }

  function setConn(text, cls) {
    ui.pillConn.className = `pill ${cls}`;
    ui.pillConn.innerHTML = `<i></i><b>${text}</b>`;
  }

  function setCapturePill(status) {
    const cap = (status && status.capture) || {};
    const running = !!cap.running;
    ui.pillCapture.className = `pill ${running ? 'pill--on' : 'pill--off'}`;
    ui.pillCapture.innerHTML = `<i></i><b>${running ? '捕获中' : '已停止'}</b>`;
    ui.footTarget.textContent = `捕获目标：${(status && status.target) || '—'}`;
    ui.footInterval.textContent = `间隔：${(status && status.interval) || '—'} 秒`;
    const cache = (status && status.cache) || {};
    ui.footCache.textContent = `缓存：${cache.count ?? 0} / ${cache.max ?? state.maxSize}`
      + (cache.pinned ? `（钉住 ${cache.pinned}）` : '');
    if (status && status.crop) { state.crop = status.crop; }
    const crop = state.crop || {};
    ui.footCrop.textContent = (crop.enabled && crop.width > 0)
      ? `范围框：${crop.width}×${crop.height} @ (${crop.x}, ${crop.y})`
      : '范围框：未设置（显示完整画面）';
  }

  /* 只覆盖传进来的字段：部分响应（例如只带 status 的事件）不能把状态区冲成空值 */
  function applyStatus(status) {
    if (!status) return;
    state.status = { ...state.status, ...status };
    setCapturePill(state.status);
    if (state.status.cache && state.status.cache.max) {
      state.maxSize = state.status.cache.max;
    }
    updateActions();
  }

  // ---------------------------------------------------------------- 渲染

  function renderStrip() {
    const frag = document.createDocumentFragment();
    state.images.forEach((shot) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'thumb' + (state.selected === shot.name ? ' thumb--active' : '');
      btn.dataset.name = shot.name;
      btn.setAttribute('role', 'listitem');
      btn.title = `${shot.name}\n${shot.width}×${shot.height} · ${shot.sizeKb} KB`;

      const img = document.createElement('img');
      img.loading = 'lazy';
      img.decoding = 'async';
      img.alt = `历史截图 ${fmtTime(shot.createdAt)}`;
      img.src = shot.url;                       // 缩略图用原图，浏览器自己缩
      img.addEventListener('error', () => {
        // 文件已经不在了（缓存轮转/被删）：直接从列表里摘掉，别留破图
        removeShots([shot.name]);
      });
      btn.appendChild(img);

      const time = document.createElement('span');
      time.className = 'thumb__time';
      time.textContent = `${fmtTime(shot.createdAt)} · ${shot.width}×${shot.height}`;
      btn.appendChild(time);

      if (shot.name === state.latestName) {
        const badge = document.createElement('span');
        badge.className = 'thumb__badge';
        badge.textContent = '最新';
        btn.appendChild(badge);
      }
      if (state.pinned.has(shot.name)) {
        const pin = document.createElement('span');
        pin.className = 'thumb__pin';
        pin.title = '已钉住';
        btn.appendChild(pin);
      }
      btn.addEventListener('click', () => selectShot(shot.name));
      frag.appendChild(btn);
    });
    ui.strip.replaceChildren(frag);
    ui.historyCount.textContent = `${state.images.length} 张`;
    ui.stripEmpty.hidden = state.images.length > 0;
  }

  function showPlaceholder() {
    ui.placeholder.hidden = false;
    ui.live.hidden = true;
  }

  function showImage(src, info) {
    ui.placeholder.hidden = true;
    ui.live.hidden = false;
    ui.live.src = src;
    if (info) ui.shotInfo.textContent = info;
  }

  function updateActions() {
    const name = state.followLive ? state.latestName : state.selected;
    const shot = name ? state.byName.get(name) : null;
    const pinned = !!name && state.pinned.has(name);

    ui.btnDownload.textContent = state.followLive ? '下载最新一张' : '下载这一张';
    ui.btnDownload.disabled = !shot;
    ui.btnPin.textContent = pinned ? '取消钉住' : '钉住';
    ui.btnPin.disabled = !shot;
    ui.btnBackLive.hidden = state.followLive;
    ui.newHint.hidden = state.followLive || !state.latestName;
    ui.viewBadge.className = 'badge ' + (state.followLive ? 'badge--live' : 'badge--history');
    ui.viewBadge.textContent = state.followLive ? '实时' : '历史';
  }

  function refreshStripActive() {
    ui.strip.querySelectorAll('.thumb').forEach((node) => {
      node.classList.toggle('thumb--active', node.dataset.name === state.selected);
    });
  }

  // ---------------------------------------------------------------- 数据

  function applyImages(images, latest, maxSize) {
    const list = Array.isArray(images) ? images : [];
    state.images = list.slice(0, MAX_STRIP);
    state.byName = new Map(state.images.map((s) => [s.name, s]));
    state.latestName = latest ? latest.name : (state.images[0] ? state.images[0].name : null);
    if (typeof maxSize === 'number' && maxSize > 0) state.maxSize = maxSize;
    if (state.selected && !state.byName.has(state.selected)) {
      state.selected = null;
      state.followLive = true;
    }
    renderStrip();
    updateActions();
  }

  function showLive(shot, url) {
    if (!shot) { showPlaceholder(); return; }
    showImage(url || `/web/latest.jpeg?v=${Date.now()}`,
      `${fmtTime(shot.createdAt)} · ${shot.viewWidth || shot.width}×${shot.viewHeight || shot.height}`
      + ` · ${shot.sizeKb} KB${shot.cropped ? ' · 已裁剪' : ''}`);
  }

  function removeShots(names) {
    const gone = new Set(names);
    const before = state.images.length;
    state.images = state.images.filter((s) => !gone.has(s.name));
    gone.forEach((name) => state.byName.delete(name));
    if (state.images.length === before && !gone.size) return;
    if (state.selected && gone.has(state.selected)) {
      state.selected = null;
      state.followLive = true;
    }
    renderStrip();
    updateActions();
    if (state.followLive) refreshLatest();
  }

  function selectShot(name) {
    const shot = state.byName.get(name);
    if (!shot) return;
    state.selected = name;
    state.followLive = (name === state.latestName);
    showImage(shot.viewUrl || shot.url,
      `${fmtTime(shot.createdAt)} · ${shot.width}×${shot.height} · ${shot.sizeKb} KB`);
    ui.shotInfo.textContent += name === state.latestName ? '' : ' · 历史';
    refreshStripActive();
    updateActions();
  }

  function backToLive() {
    state.selected = null;
    state.followLive = true;
    refreshStripActive();
    refreshLatest();
    updateActions();
  }

  async function refreshLatest() {
    try {
      const res = await fetch('/api/latest', { cache: 'no-store' });
      const data = await res.json();
      if (!data || !data.success) return;
      applyStatus(data);
      if (data.image) {
        state.latestName = data.image.name;
        if (!state.byName.has(data.image.name)) {
          state.images.unshift(data.image);
          state.images = state.images.slice(0, MAX_STRIP);
          state.byName.set(data.image.name, data.image);
          renderStrip();
        }
        if (state.followLive) { showLive(data.image, data.webUrl); }
      }
      updateActions();
    } catch (err) {
      /* 轮询失败静默：连接状态由 socket 事件体现 */
    }
  }

  async function refreshList() {
    try {
      const res = await fetch('/api/list', { cache: 'no-store' });
      const data = await res.json();
      if (!data || !data.success) return;
      state.pinned = new Set(data.pinned || []);
      applyImages(data.images, data.images[0], data.maxSize);
      if (data.crop) { state.crop = data.crop; setCapturePill(state.status); }
    } catch (err) { /* 忽略 */ }
  }

  // ---------------------------------------------------------------- 交互

  ui.btnDownload.addEventListener('click', () => {
    const name = state.followLive ? state.latestName : state.selected;
    if (!name) { toast('还没有可下载的截图', true); return; }
    window.location.href = `/api/download/${encodeURIComponent(name)}`;
    toast(`开始下载 ${name}`);
  });

  ui.btnPin.addEventListener('click', () => {
    const name = state.followLive ? state.latestName : state.selected;
    if (!name || !socket || !socket.connected) { toast('连接已断开，无法钉住', true); return; }
    const pinned = state.pinned.has(name);
    // 等服务端确认再提示，避免「看着成功其实那张已被轮转删掉」
    socket.emit(pinned ? 'unpin' : 'pin', { name }, (response) => {
      if (response && response.success) {
        toast(pinned ? `已取消钉住 ${name}` : `已钉住 ${name}（不会被轮转删除）`);
      } else {
        toast(`操作失败：${(response && response.error) || '服务端无响应'}`, true);
        refreshList();
      }
    });
  });

  ui.btnBackLive.addEventListener('click', backToLive);
  ui.newHint.addEventListener('click', backToLive);
  ui.btnRefresh.addEventListener('click', () => {
    if (socket && socket.connected) socket.emit('refresh');
    refreshList();
    refreshLatest();
    toast('已刷新');
  });
  ui.live.addEventListener('error', () => {
    // 视图文件被删/被换掉：提示并自动重取一次
    toast('画面加载失败，正在重取…', true);
    setTimeout(refreshLatest, 800);
  });
  ui.live.addEventListener('click', () => {
    const name = state.followLive ? state.latestName : state.selected;
    if (name) window.open(`/cache/${encodeURIComponent(name)}`, '_blank', 'noopener');
  });

  // ---------------------------------------------------------------- Socket.IO

  const socket = (typeof io === 'function') ? io({
    transports: ['websocket', 'polling'],
    reconnection: true,
    reconnectionDelay: 800,
    reconnectionDelayMax: 5000,
  }) : null;

  if (!socket) {
    setConn('降级为轮询', 'pill--warn');
    toast('Socket.IO 未加载，改用轮询刷新', true);
    setInterval(() => { refreshLatest(); refreshList(); }, 2500);
  } else {
    socket.on('connect', () => {
      state.connected = true;
      setConn(socket.io.engine.transport.name === 'websocket' ? '已连接' : '已连接（轮询）', 'pill--on');
    });
    socket.on('disconnect', () => {
      state.connected = false;
      setConn('重连中…', 'pill--warn');
    });
    socket.on('connect_error', () => setConn('重连中…', 'pill--warn'));

    socket.on('init', (payload) => {
      const data = payload || {};
      state.pinned = new Set(data.pinned || []);
      applyImages(data.images, data.latest, data.maxSize);
      if (data.crop) state.crop = data.crop;
      applyStatus(data.status || {});
      if (data.latest) showLive(data.latest, data.webUrl);
      else showPlaceholder();
      if (!state.selected) state.followLive = true;
      updateActions();
    });

    socket.on('frame', (payload) => {
      const shot = payload || {};
      if (!shot.name) return;
      // 列表：去重后插到最前，并顶掉超出额度的
      state.images = [shot, ...state.images.filter((s) => s.name !== shot.name)]
        .slice(0, MAX_STRIP);
      state.byName.set(shot.name, shot);
      const previousLatest = state.latestName;
      state.latestName = shot.name;
      renderStrip();
      if (state.followLive) {
        showImage(shot.webUrl || `/web/latest.jpeg?v=${Date.now()}`,
          `${fmtTime(shot.createdAt)} · ${shot.viewWidth || shot.width}×${shot.viewHeight || shot.height}`
          + ` · ${shot.sizeKb} KB${shot.cropped ? ' · 已裁剪' : ''}`);
      } else {
        ui.newHint.hidden = false;
        if (previousLatest !== shot.name) toast('收到新截图');
      }
      updateActions();
    });

    socket.on('updated', (payload) => {
      // 范围框变了：实时视图重取
      if (state.followLive) {
        showImage((payload && payload.webUrl) || `/web/latest.jpeg?v=${Date.now()}`, ui.shotInfo.textContent);
      }
    });

    socket.on('removed', (payload) => {
      // 缓存轮转删掉的文件：立刻从列表里摘掉，否则缩略图会 404 变成破图
      const names = (payload && payload.names) || [];
      if (!names.length) return;
      removeShots(names);
    });

    socket.on('pinned', (payload) => {
      const name = payload && payload.name;
      if (payload && payload.pinned) state.pinned = new Set(payload.pinned);
      else if (name) { payload.isPinned ? state.pinned.add(name) : state.pinned.delete(name); }
      const shot = state.byName.get(name);
      if (shot) shot.isPinned = !!(payload && payload.isPinned);
      renderStrip();
      updateActions();
    });

    socket.on('status', (payload) => {
      applyStatus(payload || {});
      if (payload && payload.event === 'closed') toast('捕获对象已关闭', true);
      if (payload && payload.event === 'error') toast(payload.error || '捕获出错', true);
    });

    setInterval(() => {
      if (!state.connected) { refreshLatest(); }
    }, 3000);
    setInterval(refreshList, 15000);
  }

  // 首屏先用 REST 铺一遍，避免连上 socket 前空白
  refreshLatest();
  refreshList();
  setConn('连接中…', 'pill--warn');
})();
