/* ==========================================================================
 * pc2mb 前端：登录 + 远程桌面控制台
 * 原生 HTML / CSS / JS，无第三方库、无 eval、无 innerHTML 注入。
 * 接口契约见 docs/PROTOCOL.md（v1）。
 * ========================================================================== */
(function () {
  'use strict';

  /* ------------------------------ 常量 ------------------------------ */
  var THEMES = ['light', 'gray', 'dark'];
  var THEME_KEY = 'pc2mb-theme';

  var MAX_RECONNECT_DELAY = 8000; /* 指数退避上限：1s → 8s */
  var PING_INTERVAL = 25000;
  var FRAME_WATCHDOG_MS = 5000; /* server 5s 收不到帧会丢弃该次 req，超时补发 */
  var FPS_WINDOW_MS = 500;

  var LONG_PRESS_MS = 500;
  var TOUCH_SLOP = 10; /* 移动超过该 CSS 像素才算拖动 */
  var TOUCH_WHEEL_STEP = 12; /* 双指累计位移 12px 折合一格滚轮 */
  var WHEEL_NOTCH = 120; /* 协议：dy>0 向下滚，一格 120 */
  var WHEEL_NOTCH_PX = 100; /* 系统滚轮一格通常约 100 CSS px，累计折算成 120 */
  var KEY_HOLD_MS = 70;

  var FONT_STACK = 'system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif';

  /* ------------------------------ 状态 ------------------------------ */
  var dom = {};

  var S = {
    loggedIn: false,
    ws: null,
    connecting: false,
    everOpened: false,
    reconnectAttempt: 0,
    reconnectTimer: 0,
    pingTimer: 0,
    stopReconnect: false,
    lastErrorCode: '',

    awaitingFrame: false,
    awaitingSince: 0,
    agentOnline: false,
    screen: null, /* {w,h} 主显示器分辨率（state 消息） */
    frameW: 0, /* 当前画面像素尺寸（meta / 实际帧） */
    frameH: 0,
    lastBitmap: null,
    fpsFrames: 0,
    fpsStart: 0,

    colors: { stage: '#e4e8ee', dim: '#697180' },

    heldButtons: {}, /* 鼠标按下的键 */
    heldKeys: Object.create(null),
    modDown: Object.create(null),
    landscapeLocked: false, /* 已通过 ScreenOrientation.lock 切到横屏 */
    pseudoLandscape: false, /* 浏览器不支持方向锁定时用 CSS 旋转兜底 */
    composing: false
  };

  /* ------------------------------ 小工具 ------------------------------ */
  function $(id) {
    return document.getElementById(id);
  }

  function clamp01(v) {
    return v < 0 ? 0 : v > 1 ? 1 : v;
  }

  function round4(v) {
    return Math.round(v * 10000) / 10000;
  }

  function closeBitmap(bmp) {
    if (bmp && typeof bmp.close === 'function') {
      try {
        bmp.close();
      } catch (e) {
        /* ignore */
      }
    }
  }

  var toastTimer = 0;
  function showToast(text, ms) {
    if (!dom.toast) return;
    dom.toast.textContent = text;
    dom.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      dom.toast.hidden = true;
    }, ms || 2600);
  }

  /* ------------------------------ 主题 ------------------------------ */
  function currentTheme() {
    var t = document.documentElement.getAttribute('data-theme');
    return THEMES.indexOf(t) >= 0 ? t : 'light';
  }

  function refreshThemeColors() {
    var cs = getComputedStyle(document.documentElement);
    S.colors.stage = cs.getPropertyValue('--stage-bg').trim() || '#000000';
    S.colors.dim = cs.getPropertyValue('--text-dim').trim() || '#888888';
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
      var bg = cs.getPropertyValue('--bg').trim();
      if (bg) meta.setAttribute('content', bg);
    }
  }

  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    try {
      localStorage.setItem(THEME_KEY, t);
    } catch (e) {
      /* 隐私模式下忽略 */
    }
    refreshThemeColors();
    redraw();
  }

  function cycleTheme() {
    var i = THEMES.indexOf(currentTheme());
    applyTheme(THEMES[(i + 1) % THEMES.length]);
  }

  /* ------------------------------ 登录 / 视图 ------------------------------ */
  function setLoginError(msg) {
    dom.loginError.textContent = msg || '';
  }

  function setLoginBusy(busy) {
    dom.loginSubmit.disabled = !!busy;
    dom.loginSubmit.textContent = busy ? '登录中…' : '登录';
  }

  function showLogin(errorText) {
    S.loggedIn = false;
    S.stopReconnect = true;
    closeWS();
    clearTimeout(S.reconnectTimer);
    S.reconnectTimer = 0;
    clearInterval(S.pingTimer);
    S.pingTimer = 0;
    S.connecting = false;
    S.agentOnline = false;
    S.screen = null;
    S.frameW = 0;
    S.frameH = 0;
    S.awaitingFrame = false;
    S.heldButtons = {};
    S.heldKeys = Object.create(null);
    S.modDown = Object.create(null);
    if (S.lastBitmap) {
      closeBitmap(S.lastBitmap);
      S.lastBitmap = null;
    }
    dom.overlay.hidden = true;
    releaseModifiers();
    dom.keybar.hidden = true;
    dom.btnKeyboard.setAttribute('aria-pressed', 'false');
    if (S.landscapeLocked || S.pseudoLandscape) exitLandscape();
    if (dom.ime.value) dom.ime.value = '';
    S.fpsFrames = 0;
    S.fpsStart = 0;
    dom.fps.textContent = '— fps';
    dom.loginView.hidden = false;
    dom.consoleView.hidden = true;
    document.body.setAttribute('data-view', 'login');
    dom.loginPass.value = '';
    setLoginError(errorText || '');
    setTimeout(function () {
      try {
        dom.loginUser.focus({ preventScroll: true });
      } catch (e) {
        /* ignore */
      }
    }, 30);
  }

  function enterConsole(initial) {
    S.loggedIn = true;
    S.stopReconnect = false;
    S.reconnectAttempt = 0;
    S.lastErrorCode = '';
    S.everOpened = false;
    if (initial) {
      S.agentOnline = !!initial.agent;
      if (initial.screen && initial.screen.w > 0 && initial.screen.h > 0) {
        S.screen = { w: initial.screen.w, h: initial.screen.h };
      }
    }
    dom.loginView.hidden = true;
    dom.consoleView.hidden = false;
    document.body.setAttribute('data-view', 'console');
    setLoginError('');
    refreshThemeColors();
    resizeCanvas();
    updateStatus();
    connect();
    focusCanvas();
  }

  function onLoginSubmit(e) {
    e.preventDefault();
    if (S.loggedIn) return;
    var u = dom.loginUser.value.trim();
    var p = dom.loginPass.value;
    if (!u || !p) {
      setLoginError('请输入账号和密码');
      return;
    }
    setLoginError('');
    setLoginBusy(true);
    fetch('./api/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ u: u, p: p })
    })
      .then(function (res) {
        return res
          .json()
          .catch(function () {
            return null;
          })
          .then(function (data) {
            return { res: res, data: data };
          });
      })
      .then(function (r) {
        if (r.res.ok && r.data && r.data.ok) {
          dom.loginPass.value = '';
          enterConsole(null);
          return;
        }
        var code = r.data && r.data.error;
        if (r.res.status === 429 || code === 'locked') {
          setLoginError('尝试次数过多，请 60 秒后再试');
        } else if (r.res.status === 401 || code === 'invalid') {
          setLoginError('账号或密码错误');
        } else {
          setLoginError('登录失败，请稍后再试');
        }
      })
      .catch(function () {
        setLoginError('无法连接服务器，请检查网络');
      })
      .then(function () {
        setLoginBusy(false);
      });
  }

  function checkSession() {
    fetch('./api/state', {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' }
    })
      .then(function (res) {
        if (!res.ok) {
          showLogin('');
          return null;
        }
        return res
          .json()
          .catch(function () {
            return null;
          })
          .then(function (data) {
            if (data && typeof data === 'object') enterConsole(data);
            else showLogin('');
          });
      })
      .catch(function () {
        showLogin('');
      });
  }

  function logout() {
    var ws = S.ws;
    S.loggedIn = false;
    S.stopReconnect = true;
    closeWS();
    clearTimeout(S.reconnectTimer);
    S.reconnectTimer = 0;
    if (ws) {
      /* closeWS 已经断开；这里只确保不再调度重连 */
    }
    fetch('./api/logout', { method: 'POST', credentials: 'same-origin' }).catch(function () {
      /* 即使服务端失败也回到登录页 */
    });
    showLogin('');
    setLoginError('');
  }

  /* ------------------------------ WebSocket ------------------------------ */
  function basePath() {
    var p = location.pathname || '/';
    if (p.charAt(p.length - 1) === '/') return p;
    var slash = p.lastIndexOf('/');
    var last = p.slice(slash + 1);
    if (last.indexOf('.') >= 0) return p.slice(0, slash + 1);
    return p + '/';
  }

  function wsViewerURL() {
    if (!location.host) throw new Error('no host');
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + location.host + basePath() + 'ws/viewer';
  }

  function send(obj) {
    var ws = S.ws;
    if (!ws || ws.readyState !== 1) return;
    try {
      ws.send(JSON.stringify(obj));
    } catch (e) {
      /* ignore */
    }
  }

  function closeWS() {
    var ws = S.ws;
    S.ws = null;
    S.awaitingFrame = false;
    clearInterval(S.pingTimer);
    S.pingTimer = 0;
    if (ws) {
      try {
        ws.onopen = null;
        ws.onmessage = null;
        ws.onerror = null;
        ws.onclose = null;
      } catch (e) {
        /* ignore */
      }
      try {
        ws.close(1000, 'bye');
      } catch (e) {
        /* ignore */
      }
    }
  }

  function connect() {
    if (!S.loggedIn) return;
    clearTimeout(S.reconnectTimer);
    S.reconnectTimer = 0;
    closeWS();
    S.stopReconnect = false;
    S.connecting = true;
    updateStatus();
    if (!S.everOpened) setOverlay('正在连接桌面…', { spinner: true });

    var url;
    try {
      url = wsViewerURL();
    } catch (e) {
      S.connecting = false;
      scheduleReconnect();
      return;
    }

    var ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      S.connecting = false;
      scheduleReconnect();
      return;
    }
    ws.binaryType = 'arraybuffer';
    S.ws = ws;
    ws.onopen = onOpen;
    ws.onmessage = onMessage;
    ws.onclose = onClose;
    ws.onerror = onError;
  }

  function onOpen() {
    S.connecting = false;
    S.everOpened = true;
    S.reconnectAttempt = 0;
    S.stopReconnect = false;
    S.awaitingFrame = false;
    hideOverlay();
    updateStatus();
    startPing();
    requestFrame();
  }

  function onError() {
    /* 之后必然触发 close，由 onClose 统一处理重连 */
    updateStatus();
  }

  function onClose(ev) {
    S.ws = null;
    clearInterval(S.pingTimer);
    S.pingTimer = 0;
    S.connecting = false;
    S.awaitingFrame = false;
    if (!S.loggedIn) return;

    if (ev && ev.code === 4401) {
      S.stopReconnect = true;
      showLogin('登录已失效，请重新登录');
      return;
    }
    if (S.stopReconnect) {
      updateStatus();
      return;
    }
    scheduleReconnect();
  }

  function scheduleReconnect() {
    if (!S.loggedIn || S.stopReconnect) return;
    clearTimeout(S.reconnectTimer);
    var attempt = S.reconnectAttempt;
    if (attempt < 10) S.reconnectAttempt = attempt + 1;
    var delay = Math.min(1000 * Math.pow(2, attempt), MAX_RECONNECT_DELAY);
    var text = S.lastErrorCode === 'agent_offline' ? '桌面离线，正在重连…' : '连接中断，正在重连…';
    setOverlay(text, { spinner: true });
    updateStatus();
    S.reconnectTimer = setTimeout(function () {
      S.reconnectTimer = 0;
      connect();
    }, delay);
  }

  function startPing() {
    clearInterval(S.pingTimer);
    S.pingTimer = setInterval(function () {
      var ws = S.ws;
      if (ws && ws.readyState === 1) send({ t: 'ping' });
    }, PING_INTERVAL);
  }

  function onMessage(ev) {
    var data = ev.data;
    if (typeof data === 'string') {
      handleText(data);
    } else {
      handleFrame(data);
    }
  }

  function handleText(raw) {
    var msg;
    try {
      msg = JSON.parse(raw);
    } catch (e) {
      return;
    }
    if (!msg || typeof msg !== 'object') return;

    if (msg.t === 'state') {
      var was = S.agentOnline;
      S.agentOnline = !!msg.agent;
      if (msg.screen && msg.screen.w > 0 && msg.screen.h > 0) {
        S.screen = { w: msg.screen.w, h: msg.screen.h };
      }
      updateStatus();
      if (!was && S.agentOnline) {
        S.awaitingFrame = false;
        requestFrame();
      }
    } else if (msg.t === 'meta') {
      if (msg.w > 0 && msg.h > 0) {
        S.frameW = msg.w;
        S.frameH = msg.h;
        updateRes();
      }
    } else if (msg.t === 'pong') {
      /* 心跳回应，无需处理 */
    } else if (msg.t === 'error') {
      onServerError(msg);
    }
  }

  function onServerError(msg) {
    var code = msg.code || '';
    var text = typeof msg.msg === 'string' && msg.msg ? msg.msg : '连接异常';
    S.lastErrorCode = code;
    if (code === 'replaced') {
      S.stopReconnect = true;
      setOverlay(text, { spinner: false, retry: true });
    } else if (code === 'agent_offline') {
      S.agentOnline = false;
      /* 不停止重连：桌面端重新上线后继续出图 */
    } else {
      setOverlay(text, { spinner: false, retry: true });
    }
    updateStatus();
  }

  /* ------------------------------ 帧渲染 / 流控 ------------------------------ */
  function decodeBitmap(blob) {
    if (typeof createImageBitmap === 'function') {
      return createImageBitmap(blob);
    }
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(blob);
      var img = new Image();
      img.onload = function () {
        URL.revokeObjectURL(url);
        resolve(img);
      };
      img.onerror = function () {
        URL.revokeObjectURL(url);
        reject(new Error('decode failed'));
      };
      img.src = url;
    });
  }

  function handleFrame(data) {
    S.awaitingFrame = false;
    var blob;
    if (typeof Blob !== 'undefined' && data instanceof Blob) {
      blob = data;
    } else {
      blob = new Blob([data], { type: 'image/jpeg' });
    }
    decodeBitmap(blob)
      .then(function (bmp) {
        var old = S.lastBitmap;
        S.lastBitmap = bmp;
        if (bmp.width > 0 && bmp.height > 0) {
          if (bmp.width !== S.frameW || bmp.height !== S.frameH) {
            S.frameW = bmp.width;
            S.frameH = bmp.height;
            updateRes();
          }
        }
        redraw();
        if (old && old !== bmp) closeBitmap(old);
        S.fpsFrames += 1;
        if (!S.fpsStart) S.fpsStart = performance.now();
        requestFrameSoon();
      })
      .catch(function () {
        /* 解码失败也要继续要下一帧，避免卡死 */
        requestFrameSoon();
      });
  }

  function requestFrame() {
    if (!S.loggedIn || document.hidden) return;
    var ws = S.ws;
    if (!ws || ws.readyState !== 1) return;
    if (S.awaitingFrame) return;
    S.awaitingFrame = true;
    S.awaitingSince = performance.now();
    send({ t: 'req' });
  }

  function requestFrameSoon() {
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(requestFrame);
    } else {
      setTimeout(requestFrame, 0);
    }
  }

  function frameWatchdog() {
    if (!S.loggedIn || document.hidden) return;
    var ws = S.ws;
    if (!ws || ws.readyState !== 1) return;
    if (S.awaitingFrame && performance.now() - S.awaitingSince > FRAME_WATCHDOG_MS) {
      S.awaitingFrame = false;
      requestFrame();
    }
  }

  function drawBitmap(ctx, bmp) {
    var cw = dom.canvas.width;
    var ch = dom.canvas.height;
    var scale = Math.min(cw / bmp.width, ch / bmp.height);
    var w = bmp.width * scale;
    var h = bmp.height * scale;
    var x = (cw - w) / 2;
    var y = (ch - h) / 2;
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(bmp, x, y, w, h);
  }

  function drawIdle(ctx) {
    var canvas = dom.canvas;
    var cw = canvas.width;
    var ch = canvas.height;
    var dpr = canvas.clientWidth ? cw / canvas.clientWidth : window.devicePixelRatio || 1;
    var label = '等待画面…';
    if (S.everOpened && !S.agentOnline) label = '桌面离线';
    ctx.fillStyle = S.colors.dim;
    ctx.font = '500 ' + Math.round(14 * dpr) + 'px ' + FONT_STACK;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, cw / 2, ch / 2);
  }

  function redraw() {
    var canvas = dom.canvas;
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    if (!ctx) return;
    var cw = canvas.width;
    var ch = canvas.height;
    if (!cw || !ch) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = S.colors.stage;
    ctx.fillRect(0, 0, cw, ch);
    if (S.lastBitmap) drawBitmap(ctx, S.lastBitmap);
    else drawIdle(ctx);
  }

  function resizeCanvas() {
    var canvas = dom.canvas;
    if (!canvas) return;
    /* 用 canvas 自身的布局尺寸（伪横屏时舞台被旋转，getBoundingClientRect 会返回交换后的外接框） */
    var cssW = canvas.clientWidth;
    var cssH = canvas.clientHeight;
    if (!cssW || !cssH) {
      if (!dom.stage) return;
      var rect = dom.stage.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      cssW = rect.width;
      cssH = rect.height;
    }
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var w = Math.max(1, Math.round(cssW * dpr));
    var h = Math.max(1, Math.round(cssH * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    redraw();
  }

  /* ------------------------------ 状态 / 遮罩 UI ------------------------------ */
  function setOverlay(text, opts) {
    opts = opts || {};
    dom.overlay.hidden = false;
    dom.overlayText.textContent = text;
    dom.overlaySpinner.hidden = opts.spinner === false;
    dom.overlayRetry.hidden = !opts.retry;
  }

  function hideOverlay() {
    dom.overlay.hidden = true;
  }

  function updateStatus() {
    var ws = S.ws;
    var wsOpen = !!ws && ws.readyState === 1;
    var state;
    var text;
    if (wsOpen) {
      if (S.agentOnline) {
        state = 'on';
        text = '已连接';
      } else {
        state = 'wait';
        text = '已连接 · 桌面离线';
      }
    } else if (S.connecting) {
      state = 'wait';
      text = '连接中…';
    } else if (S.loggedIn) {
      state = 'err';
      text = S.stopReconnect ? '连接已断开' : '正在重连…';
    } else {
      state = 'off';
      text = '未连接';
    }
    dom.connDot.setAttribute('data-state', state);
    dom.connText.textContent = text;
    updateRes();
  }

  function updateRes() {
    if (S.frameW > 0 && S.frameH > 0) {
      dom.res.textContent = S.frameW + '×' + S.frameH;
    } else if (S.screen) {
      dom.res.textContent = S.screen.w + '×' + S.screen.h;
    } else {
      dom.res.textContent = '';
    }
  }

  function focusCanvas() {
    try {
      dom.canvas.focus({ preventScroll: true });
    } catch (e) {
      /* ignore */
    }
  }

  /* ------------------------------ 坐标换算 ------------------------------ */
  function aspectDims() {
    if (S.frameW > 0 && S.frameH > 0) return [S.frameW, S.frameH];
    if (S.screen && S.screen.w > 0 && S.screen.h > 0) return [S.screen.w, S.screen.h];
    if (S.lastBitmap) return [S.lastBitmap.width, S.lastBitmap.height];
    return [0, 0];
  }

  /* 画面在页面中“等比适配”后的矩形（CSS 像素） */
  function fitRectCss() {
    var rect = dom.canvas.getBoundingClientRect();
    var dims = aspectDims();
    if (!rect.width || !rect.height || !dims[0] || !dims[1]) return null;
    var scale = Math.min(rect.width / dims[0], rect.height / dims[1]);
    var w = dims[0] * scale;
    var h = dims[1] * scale;
    return {
      left: rect.left + (rect.width - w) / 2,
      top: rect.top + (rect.height - h) / 2,
      width: w,
      height: h
    };
  }

  /* 客户端坐标 → 归一化桌面坐标（0..1，相对主显示器） */
  function normalizePoint(clientX, clientY) {
    if (S.pseudoLandscape) {
      /* 舞台顺时针旋转 90°：屏幕坐标反推舞台局部坐标 */
      var canvas = dom.canvas;
      var dims = aspectDims();
      if (!canvas || !dims[0] || !dims[1]) return null;
      var cw = canvas.clientWidth;
      var ch = canvas.clientHeight;
      if (!cw || !ch) return null;
      var localX = clientY;
      var localY = window.innerWidth - clientX;
      var scale = Math.min(cw / dims[0], ch / dims[1]);
      var drawW = dims[0] * scale;
      var drawH = dims[1] * scale;
      var offX = (cw - drawW) / 2;
      var offY = (ch - drawH) / 2;
      return {
        x: clamp01((localX - offX) / drawW),
        y: clamp01((localY - offY) / drawH)
      };
    }
    var fit = fitRectCss();
    if (!fit) return null;
    var x = (clientX - fit.left) / fit.width;
    var y = (clientY - fit.top) / fit.height;
    return { x: clamp01(x), y: clamp01(y) };
  }

  function sendMouseMove(clientX, clientY) {
    var p = normalizePoint(clientX, clientY);
    if (!p) return;
    send({ t: 'm', x: round4(p.x), y: round4(p.y) });
  }

  /* ------------------------------ 桌面：鼠标 / 滚轮 / 键盘 ------------------------------ */
  var movePending = null;
  var moveRaf = 0;
  var mouseWheelAcc = 0;

  function queueMouseMove(clientX, clientY) {
    movePending = { x: clientX, y: clientY };
    if (moveRaf) return;
    moveRaf = requestAnimationFrame(function () {
      moveRaf = 0;
      var p = movePending;
      movePending = null;
      if (p) sendMouseMove(p.x, p.y);
    });
  }

  function onMouseDown(e) {
    focusCanvas();
    e.preventDefault();
    sendMouseMove(e.clientX, e.clientY);
    if (e.button >= 0 && e.button <= 2) {
      S.heldButtons[e.button] = true;
      send({ t: 'd', b: e.button });
    }
  }

  function releaseMouseButton(button) {
    if (!S.heldButtons[button]) return;
    delete S.heldButtons[button];
    send({ t: 'u', b: button });
  }

  function onWindowMouseUp(e) {
    releaseMouseButton(e.button);
  }

  function onWheel(e) {
    e.preventDefault();
    var raw = e.deltaY;
    if (e.deltaMode === 1) raw *= 40; /* 行模式 */
    else if (e.deltaMode === 2) raw *= 800; /* 页模式 */
    if (!raw) return;
    mouseWheelAcc += raw;
    /* 无论系统一格是 100 还是 120px，都折算成协议里的 ±120 */
    for (var guard = 0; Math.abs(mouseWheelAcc) >= WHEEL_NOTCH_PX && guard < 10; guard++) {
      var dir = mouseWheelAcc > 0 ? 1 : -1;
      send({ t: 'w', dy: dir * WHEEL_NOTCH });
      mouseWheelAcc -= dir * WHEEL_NOTCH_PX;
    }
    if (guard >= 10) mouseWheelAcc = 0;
  }

  function normalizeKey(e) {
    var k = e.key;
    if (typeof k !== 'string' || !k) return '';
    if (k.length === 1) return k.toLowerCase();
    return k;
  }

  function onKeyDown(e) {
    if (e.isComposing || e.keyCode === 229) return; /* 本地 IME 组字中不转发 */
    var k = normalizeKey(e);
    if (!k || k === 'Dead' || k === 'Unidentified') return;
    e.preventDefault();
    S.heldKeys[k] = true;
    send({ t: 'kd', k: k });
  }

  function onKeyUp(e) {
    var k = normalizeKey(e);
    if (!k || k === 'Dead' || k === 'Unidentified') return;
    if (S.heldKeys[k]) {
      delete S.heldKeys[k];
      e.preventDefault();
      send({ t: 'ku', k: k });
    }
  }

  function sendKeyTap(k) {
    send({ t: 'kd', k: k });
    setTimeout(function () {
      send({ t: 'ku', k: k });
    }, KEY_HOLD_MS);
  }

  function releaseAllInput() {
    var b;
    for (b in S.heldButtons) {
      if (Object.prototype.hasOwnProperty.call(S.heldButtons, b)) {
        send({ t: 'u', b: Number(b) });
      }
    }
    S.heldButtons = {};
    var k;
    for (k in S.heldKeys) {
      if (Object.prototype.hasOwnProperty.call(S.heldKeys, k)) {
        send({ t: 'ku', k: k });
      }
    }
    S.heldKeys = Object.create(null);
    releaseModifiers();
  }

  /* 手机特殊键条上的 Ctrl/Alt 是“开关式”按下，页面失焦/收起键盘时统一抬起，避免远程卡键 */
  function releaseModifiers() {
    var k;
    for (k in S.modDown) {
      if (Object.prototype.hasOwnProperty.call(S.modDown, k)) {
        send({ t: 'ku', k: k });
      }
    }
    S.modDown = Object.create(null);
    if (dom.keybar) {
      var btns = dom.keybar.querySelectorAll('button.key.active');
      for (var i = 0; i < btns.length; i++) btns[i].classList.remove('active');
    }
  }

  /* ------------------------------ 手机：触摸 ------------------------------ */
  var touch = null; /* 单指状态 */
  var twoFinger = false;
  var twoFingerLastY = 0;
  var wheelAcc = 0;

  function findTouch(list, id) {
    if (!list) return null;
    for (var i = 0; i < list.length; i++) {
      if (list[i].identifier === id) return list[i];
    }
    return null;
  }

  function startSingle(t) {
    touch = {
      id: t.identifier,
      startX: t.clientX,
      startY: t.clientY,
      moved: false,
      dragging: false,
      longPressed: false,
      timer: 0
    };
    sendMouseMove(t.clientX, t.clientY);
    var id = t.identifier;
    touch.timer = setTimeout(function () {
      if (!touch || touch.id !== id) return;
      if (touch.moved || touch.dragging || touch.longPressed) return;
      touch.longPressed = true;
      send({ t: 'd', b: 2 }); /* 长按 = 右键按下，抬起在 touchend */
    }, LONG_PRESS_MS);
  }

  function cancelSingle() {
    if (!touch) return;
    clearTimeout(touch.timer);
    if (touch.dragging) send({ t: 'u', b: 0 });
    else if (touch.longPressed) send({ t: 'u', b: 2 });
    touch = null;
  }

  function resetTouchState() {
    if (touch) clearTimeout(touch.timer);
    touch = null;
    twoFinger = false;
    wheelAcc = 0;
  }

  function onTouchStart(e) {
    e.preventDefault();
    var touches = e.touches;
    if (!touches || !touches.length) return;

    if (touches.length >= 2) {
      cancelSingle();
      twoFinger = true;
      twoFingerLastY = (touches[0].clientY + touches[1].clientY) / 2;
      wheelAcc = 0;
      return;
    }

    if (!touch) startSingle(touches[0]);
  }

  function onTouchMove(e) {
    e.preventDefault();
    var touches = e.touches;
    if (!touches || !touches.length) return;

    if (twoFinger) {
      if (touches.length >= 2) {
        var cy = (touches[0].clientY + touches[1].clientY) / 2;
        var dyPx = twoFingerLastY - cy;
        twoFingerLastY = cy;
        wheelAcc += dyPx;
        while (Math.abs(wheelAcc) >= TOUCH_WHEEL_STEP) {
          var dir = wheelAcc > 0 ? 1 : -1;
          send({ t: 'w', dy: dir * WHEEL_NOTCH });
          wheelAcc -= dir * TOUCH_WHEEL_STEP;
        }
      } else {
        twoFinger = false;
        startSingle(touches[0]);
      }
      return;
    }

    if (!touch) return;
    var t = findTouch(touches, touch.id);
    if (!t) return;

    var dx = t.clientX - touch.startX;
    var dy = t.clientY - touch.startY;

    if (!touch.moved && (Math.abs(dx) > TOUCH_SLOP || Math.abs(dy) > TOUCH_SLOP)) {
      touch.moved = true;
      clearTimeout(touch.timer);
      if (!touch.longPressed) {
        touch.dragging = true;
        send({ t: 'd', b: 0 });
      }
    }
    sendMouseMove(t.clientX, t.clientY);
  }

  function onTouchEnd(e) {
    e.preventDefault();
    var touches = e.touches || [];

    if (twoFinger) {
      if (touches.length >= 2) return;
      twoFinger = false;
      wheelAcc = 0;
      if (touches.length === 1) startSingle(touches[0]);
      return;
    }

    if (!touch) {
      if (touches.length === 1) startSingle(touches[0]);
      return;
    }

    if (findTouch(touches, touch.id)) return; /* 该指还在按着 */
    clearTimeout(touch.timer);
    var longPressed = touch.longPressed;
    var dragging = touch.dragging;
    var moved = touch.moved;
    touch = null;

    if (longPressed) {
      send({ t: 'u', b: 2 });
    } else if (dragging) {
      send({ t: 'u', b: 0 });
    } else if (!moved) {
      var ct = e.changedTouches && e.changedTouches[0];
      if (ct) sendMouseMove(ct.clientX, ct.clientY);
      send({ t: 'd', b: 0 });
      send({ t: 'u', b: 0 });
    }
  }

  function onTouchCancel(e) {
    e.preventDefault();
    cancelSingle();
    twoFinger = false;
    wheelAcc = 0;
  }

  /* ------------------------------ 手机：软键盘 / IME / 特殊键 ------------------------------ */
  function flushIme() {
    var v = dom.ime.value;
    if (!v) return;
    dom.ime.value = '';
    /* 分片发送：server 对单条 JSON 有大小上限（约 8KB），避免粘贴长文本被整条丢弃 */
    var MAX_CHUNK = 400;
    while (v.length) {
      var chunk = v.slice(0, MAX_CHUNK);
      /* 不把代理对（emoji 等）从中间切开 */
      if (chunk.length === MAX_CHUNK) {
        var code = chunk.charCodeAt(chunk.length - 1);
        if (code >= 0xd800 && code <= 0xdbff) chunk = chunk.slice(0, -1);
      }
      v = v.slice(chunk.length);
      send({ t: 'txt', s: chunk });
    }
  }

  function onImeInput(e) {
    if (S.composing || (e && e.isComposing)) return;
    flushIme();
  }

  function onImeCompositionStart() {
    S.composing = true;
  }

  function onImeCompositionEnd() {
    S.composing = false;
    flushIme();
  }

  function onImeKeyDown(e) {
    if (e.isComposing || S.composing) return;
    if (e.key === 'Enter') {
      e.preventDefault();
      flushIme();
      sendKeyTap('Enter');
    } else if (e.key === 'Backspace' && !dom.ime.value) {
      e.preventDefault();
      sendKeyTap('Backspace');
    }
  }

  function openKeyboard() {
    dom.keybar.hidden = false;
    dom.btnKeyboard.setAttribute('aria-pressed', 'true');
    try {
      dom.ime.value = '';
      dom.ime.focus({ preventScroll: true });
    } catch (e) {
      /* ignore */
    }
  }

  function closeKeyboard() {
    dom.keybar.hidden = true;
    dom.btnKeyboard.setAttribute('aria-pressed', 'false');
    releaseModifiers();
    try {
      dom.ime.blur();
    } catch (e) {
      /* ignore */
    }
  }

  function toggleKeyboard() {
    if (!dom.keybar.hidden) closeKeyboard();
    else openKeyboard();
  }

  function onKeybarClick(e) {
    var btn = e.target && e.target.closest ? e.target.closest('button[data-key]') : null;
    if (!btn) return;
    var k = btn.getAttribute('data-key');
    if (!k) return;

    if (k === 'Control' || k === 'Alt' || k === 'Shift') {
      if (S.modDown[k]) {
        delete S.modDown[k];
        btn.classList.remove('active');
        send({ t: 'ku', k: k });
      } else {
        S.modDown[k] = true;
        btn.classList.add('active');
        send({ t: 'kd', k: k });
      }
    } else {
      sendKeyTap(k);
    }
    /* 特殊键条使用中保持软键盘可用 */
    try {
      dom.ime.focus({ preventScroll: true });
    } catch (err) {
      /* ignore */
    }
  }

  /* ------------------------------ 横屏 ------------------------------ */
  function supportsOrientationLock() {
    return !!(window.screen && screen.orientation && typeof screen.orientation.lock === 'function');
  }

  function updateRotateButton() {
    if (!dom.btnRotate) return;
    var on = S.landscapeLocked || S.pseudoLandscape;
    dom.btnRotate.classList.toggle('is-active', on);
    dom.btnRotate.setAttribute('aria-pressed', on ? 'true' : 'false');
  }

  /* CSS 伪横屏：旋转整个舞台 90°，不依赖浏览器旋转锁定（iOS Safari 等） */
  function applyPseudoLandscape(on) {
    S.pseudoLandscape = !!on;
    document.documentElement.classList.toggle('pseudo-landscape', !!on);
    closeKeyboard();
    resizeCanvas();
    updateRotateButton();
    if (on) showToast('横屏模式：把手机转 90° 即可横屏观看');
  }

  function enterNativeLandscape() {
    var root = document.documentElement;
    var fullscreen;
    if (document.fullscreenElement) fullscreen = Promise.resolve();
    else if (root.requestFullscreen) fullscreen = root.requestFullscreen();
    else fullscreen = Promise.reject(new Error('fullscreen unsupported'));

    fullscreen
      .then(function () {
        if (!supportsOrientationLock()) throw new Error('orientation lock unsupported');
        return screen.orientation.lock('landscape');
      })
      .then(function () {
        S.landscapeLocked = true;
        updateRotateButton();
        showToast('已切换横屏，再点一次恢复竖屏');
      })
      .catch(function () {
        applyPseudoLandscape(true);
      });
  }

  function exitLandscape() {
    var wasLandscape = S.landscapeLocked || S.pseudoLandscape;
    if (S.landscapeLocked) {
      S.landscapeLocked = false;
      try {
        if (screen.orientation && typeof screen.orientation.unlock === 'function') screen.orientation.unlock();
      } catch (e) {
        /* ignore */
      }
    }
    if (S.pseudoLandscape) applyPseudoLandscape(false);
    /* 进入横屏时可能顺带进了全屏，退出横屏时一并退出 */
    if (wasLandscape && document.fullscreenElement && document.exitFullscreen) {
      var p = document.exitFullscreen();
      if (p && p.catch) p.catch(function () {});
    }
    updateRotateButton();
  }

  function toggleLandscape() {
    if (S.landscapeLocked || S.pseudoLandscape) {
      exitLandscape();
      return;
    }
    enterNativeLandscape();
  }

  /* ------------------------------ 全屏 ------------------------------ */
  function toggleFullscreen() {
    var root = document.documentElement;
    if (document.fullscreenElement) {
      var p = document.exitFullscreen ? document.exitFullscreen() : null;
      if (p && p.catch) p.catch(function () {});
      return;
    }
    if (root.requestFullscreen) {
      var q = root.requestFullscreen();
      if (q && q.catch) q.catch(function () {});
    }
  }

  function onFullscreenChange() {
    document.documentElement.classList.toggle('is-fullscreen', !!document.fullscreenElement);
    if (!document.fullscreenElement && S.landscapeLocked) {
      S.landscapeLocked = false;
      try {
        if (screen.orientation && typeof screen.orientation.unlock === 'function') screen.orientation.unlock();
      } catch (e) {
        /* ignore */
      }
      updateRotateButton();
    }
    resizeCanvas();
  }

  /* ------------------------------ 绑定 ------------------------------ */
  function bindUI() {
    dom.loginForm.addEventListener('submit', onLoginSubmit);
    dom.loginTheme.addEventListener('click', function () {
      cycleTheme();
    });
    dom.btnTheme.addEventListener('click', function () {
      cycleTheme();
      focusCanvas();
    });
    dom.btnFullscreen.addEventListener('click', function () {
      toggleFullscreen();
      focusCanvas();
    });
    dom.btnRotate.addEventListener('click', function () {
      toggleLandscape();
      focusCanvas();
    });
    dom.btnLogout.addEventListener('click', logout);
    dom.btnKeyboard.addEventListener('click', toggleKeyboard);
    dom.keybar.addEventListener('click', onKeybarClick);
    dom.overlayRetry.addEventListener('click', function () {
      if (!S.loggedIn) return;
      S.stopReconnect = false;
      S.lastErrorCode = '';
      S.reconnectAttempt = 0;
      hideOverlay();
      connect();
    });
    dom.ime.addEventListener('input', onImeInput);
    dom.ime.addEventListener('compositionstart', onImeCompositionStart);
    dom.ime.addEventListener('compositionend', onImeCompositionEnd);
    dom.ime.addEventListener('keydown', onImeKeyDown);

    document.addEventListener('fullscreenchange', onFullscreenChange);

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) requestFrame();
    });

    window.addEventListener('mouseup', onWindowMouseUp);
    window.addEventListener('blur', releaseAllInput);

    dom.stage.addEventListener('contextmenu', function (e) {
      e.preventDefault();
    });

    var resizeRaf = 0;
    function onResize() {
      if (resizeRaf) return;
      resizeRaf = requestAnimationFrame(function () {
        resizeRaf = 0;
        resizeCanvas();
      });
    }
    if (typeof ResizeObserver === 'function') {
      new ResizeObserver(onResize).observe(dom.stage);
    }
    window.addEventListener('resize', onResize);
    window.addEventListener('orientationchange', onResize);
  }

  function bindCanvas() {
    var canvas = dom.canvas;

    canvas.addEventListener('mousemove', function (e) {
      queueMouseMove(e.clientX, e.clientY);
    });
    canvas.addEventListener('mousedown', onMouseDown);
    canvas.addEventListener('wheel', onWheel, { passive: false });
    canvas.addEventListener('keydown', onKeyDown);
    canvas.addEventListener('keyup', onKeyUp);
    canvas.addEventListener('dragstart', function (e) {
      e.preventDefault();
    });

    canvas.addEventListener('touchstart', onTouchStart, { passive: false });
    canvas.addEventListener('touchmove', onTouchMove, { passive: false });
    canvas.addEventListener('touchend', onTouchEnd, { passive: false });
    canvas.addEventListener('touchcancel', onTouchCancel, { passive: false });

    /* 工具栏以外点击画布时把焦点还给 canvas，保证桌面键盘可转发 */
    canvas.addEventListener('focus', function () {
      /* 仅用于桌面键盘通道 */
    });
  }

  /* ------------------------------ 启动 ------------------------------ */
  function boot() {
    dom = {
      loginView: $('login-view'),
      loginForm: $('login-form'),
      loginUser: $('login-user'),
      loginPass: $('login-pass'),
      loginError: $('login-error'),
      loginSubmit: $('login-submit'),
      loginTheme: $('login-theme'),
      consoleView: $('console-view'),
      stage: $('stage'),
      canvas: $('screen'),
      overlay: $('overlay'),
      overlayText: $('overlay-text'),
      overlaySpinner: $('overlay-spinner'),
      overlayRetry: $('overlay-retry'),
      connDot: $('conn-dot'),
      connText: $('conn-text'),
      res: $('res'),
      fps: $('fps'),
      keybar: $('keybar'),
      ime: $('ime'),
      btnKeyboard: $('btn-keyboard'),
      btnRotate: $('btn-rotate'),
      btnFullscreen: $('btn-fullscreen'),
      btnTheme: $('btn-theme'),
      btnLogout: $('btn-logout'),
      toast: $('toast')
    };

    bindUI();
    bindCanvas();
    refreshThemeColors();
    resizeCanvas();
    setInterval(function () {
      var now = performance.now();
      var fps = 0;
      if (S.fpsStart) {
        var elapsed = now - S.fpsStart;
        if (elapsed > 0) fps = (S.fpsFrames * 1000) / elapsed;
      }
      S.fpsFrames = 0;
      S.fpsStart = now;
      dom.fps.textContent = (fps > 0 ? Math.round(fps) : '—') + ' fps';
    }, FPS_WINDOW_MS);
    setInterval(frameWatchdog, 1000);
    checkSession();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
