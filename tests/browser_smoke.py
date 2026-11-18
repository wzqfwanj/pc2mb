#!/usr/bin/env python3
"""pc2mb 浏览器端到端冒烟测试（需要真实服务端 + 已连上的 Windows Agent）。

覆盖：
  - 登录页 → 控制台
  - canvas 收到并渲染真实 JPEG 帧（采样像素）
  - 鼠标移动端到端注入（浏览器 → server → agent → SetCursorPos，测试后复位）
  - 明 / 灰 / 暗三主题切换
  - 手机视口（390x844, touch）布局与软键盘按钮

用法：
  python tests/browser_smoke.py [BASE_URL]
  # 默认 http://127.0.0.1:18091；线上可传 https://wkeeli.cn/pc2mb
环境变量：PC2MB_TEST_USER / PC2MB_TEST_PASSWORD
"""

from __future__ import annotations

import ctypes
import io
import json
import os
import sys
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18091").rstrip("/")
USER = os.environ.get("PC2MB_TEST_USER", "admin")
PASSWORD = os.environ.get("PC2MB_TEST_PASSWORD", "test1234")
OUT = Path(__file__).resolve().parent / "output"
OUT.mkdir(exist_ok=True)

WS_INIT_SCRIPT = """
(() => {
  window.__wslog = [];
  window.__sent = [];
  const Orig = window.WebSocket;
  function W(url, protocols) {
    const ws = protocols === undefined ? new Orig(url) : new Orig(url, protocols);
    window.__wslog.push({ ev: 'new', url: String(url) });
    const origSend = ws.send.bind(ws);
    ws.send = function (data) {
      try {
        if (typeof data === 'string') window.__sent.push(data);
      } catch (e) {}
      // __blockInput = true 时只记录、不真正发给服务端，避免测试误操作真实桌面
      if (window.__blockInput && typeof data === 'string' && /"t":"(m|d|u|w|kd|ku|txt|vk)"/.test(data)) {
        return undefined;
      }
      return origSend(data);
    };
    ws.addEventListener('open', () => window.__wslog.push({ ev: 'open', url: String(url) }));
    ws.addEventListener('close', (e) => window.__wslog.push({ ev: 'close', code: e.code, reason: e.reason }));
    ws.addEventListener('error', () => window.__wslog.push({ ev: 'error' }));
    return ws;
  }
  W.prototype = Orig.prototype;
  W.CONNECTING = 0; W.OPEN = 1; W.CLOSING = 2; W.CLOSED = 3;
  window.WebSocket = W;
})();
"""

# 让浏览器认为不支持 ScreenOrientation.lock，从而稳定覆盖 CSS 伪横屏回退路径
LANDSCAPE_STUB = """
(() => {
  try {
    if (window.screen && window.screen.orientation) {
      Object.defineProperty(window.screen.orientation, 'lock', {
        configurable: true,
        value: function () { return Promise.reject(new Error('stub: unsupported')); }
      });
    }
  } catch (e) {}
})();
"""


def dump_diag(page, tag: str) -> None:
    try:
        print(f"[diag {tag}] conn={page.locator('#conn-text').text_content()!r} "
              f"fps={page.locator('#fps').text_content()!r} res={page.locator('#res').text_content()!r} "
              f"overlay={page.locator('#overlay-text').text_content()!r}")
        print(f"[diag {tag}] canvas={page.evaluate('() => { const c=document.querySelector(\"#screen\"); return [c.width,c.height,c.clientWidth,c.clientHeight]; }')}")
        print(f"[diag {tag}] colors={canvas_variance(page)}")
        print(f"[diag {tag}] wslog={page.evaluate('() => window.__wslog')}")
        page.screenshot(path=str(OUT / f"diag-{tag}.png"))
    except Exception as exc:  # noqa: BLE001
        print(f"[diag {tag}] failed: {exc}")

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  <- ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(name)


def cursor_pos() -> tuple[int, int]:
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def set_cursor_pos(x: int, y: int) -> None:
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def physical_screen() -> tuple[int, int]:
    """物理分辨率（先声明 Per-Monitor DPI aware，否则拿到的是缩放后逻辑值）。"""
    return (
        ctypes.windll.user32.GetSystemMetrics(0),
        ctypes.windll.user32.GetSystemMetrics(1),
    )


def canvas_png_colors(page) -> int:
    """截取 canvas 元素本身，统计缩放后不同颜色数：真实画面有大量颜色，空白画面只有个位数。"""
    try:
        raw = page.locator("#screen").screenshot()
    except Exception:
        return 0
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    image = image.resize((160, 120))
    return len(image.getcolors(maxcolors=1 << 20) or [])


def wait_canvas_content(page, timeout_ms: int = 45000, tag: str = "") -> int:
    deadline = time.time() + timeout_ms / 1000
    colors = 0
    while time.time() < deadline:
        colors = canvas_png_colors(page)
        if colors > 50:
            return colors
        time.sleep(0.5)
    print(f"[diag] {tag} canvas 颜色数={colors}（等待超时）")
    return colors


def canvas_variance(page) -> int:
    """采样 canvas 11x11 个点，返回不同颜色数量（>5 说明画面不是纯色/纯背景）。"""
    return page.evaluate(
        """() => {
          const c = document.querySelector('#screen');
          if (!c || c.width < 10 || c.height < 10) return 0;
          const ctx = c.getContext('2d');
          const colors = new Set();
          for (let i = 1; i < 12; i++) {
            for (let j = 1; j < 12; j++) {
              const d = ctx.getImageData(Math.floor(c.width * i / 12), Math.floor(c.height * j / 12), 1, 1).data;
              colors.add(d[0] + ',' + d[1] + ',' + d[2] + ',' + d[3]);
            }
          }
          return colors.size;
        }"""
    )


def login_and_wait(page, timeout: int = 45000) -> None:
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#login-view:not([hidden]), #console-view:not([hidden])", timeout=20000)
    if page.locator("#login-view").is_visible():
        page.fill("#login-user", USER)
        page.fill("#login-pass", PASSWORD)
        page.click("#login-submit")
    page.wait_for_selector("#console-view:not([hidden])", timeout=timeout)
    page.wait_for_function(
        "() => document.querySelector('#conn-text').textContent.includes('已连接')", timeout=timeout
    )
    colors = wait_canvas_content(page, timeout, tag="boot")
    if colors <= 50:
        raise TimeoutError(f"canvas 未渲染出真实画面（颜色数={colors}）")


def desktop_checks(browser) -> None:
    print("\n== 桌面视口 1280x800 ==")
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    context.add_init_script(WS_INIT_SCRIPT)
    page = context.new_page()
    console_errors: list[str] = []

    def on_console(msg):
        # 未登录时 /api/state 的 401 属于预期流程，忽略该资源加载错误
        if msg.type == "error" and "Failed to load resource" not in msg.text:
            console_errors.append(msg.text)

    page.on("console", on_console)
    page.on("pageerror", lambda e: console_errors.append(str(e)))

    login_and_wait(page)
    page.wait_for_timeout(1500)

    res = page.locator("#res").text_content() or ""
    conn = page.locator("#conn-text").text_content() or ""
    fps = page.locator("#fps").text_content() or ""
    check("登录后进入控制台并已连接", "已连接" in conn, conn)
    check("画面分辨率已显示", "×" in res, res)
    check("FPS 在走", "fps" in fps and "—" not in fps, fps)
    variance = canvas_png_colors(page)
    check("canvas 渲染出真实画面(颜色>50)", variance > 50, f"colors={variance}")

    page.screenshot(path=str(OUT / "desktop-light.png"))
    check("light 主题截图", (OUT / "desktop-light.png").stat().st_size > 5000)
    check("桌面端隐藏横屏按钮", not page.locator("#btn-rotate").is_visible())

    # ---- 鼠标移动端到端（只移动，不点击；测试后复位）----
    physical = physical_screen()
    before = cursor_pos()
    try:
        box = page.locator("#screen").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        # 本机可能有人在真实使用鼠标，采样一段时间的窗口取最小偏差，避免人工干扰导致误判
        expected = (physical[0] // 2, physical[1] // 2)
        best = None
        after = expected
        for _ in range(15):
            time.sleep(0.1)
            pos = cursor_pos()
            dist = abs(pos[0] - expected[0]) + abs(pos[1] - expected[1])
            if best is None or dist < best:
                best = dist
                after = pos
            if dist <= 80:
                break
        dist = best if best is not None else 10**9
        sent = page.evaluate("() => (window.__sent || []).filter(s => s.indexOf('\"t\":\"m\"') >= 0)")
        last = json.loads(sent[-1]) if sent else {}
        check(
            "鼠标移动端到端注入(物理桌面中心±80px)",
            physical[0] > 0 and dist <= 80,
            f"before={before} after={after} expected≈{expected} physical={physical} dist={dist} sent={last}",
        )
    finally:
        set_cursor_pos(*before)

    # ---- 三主题 ----
    for theme, shot in (("gray", "desktop-gray.png"), ("dark", "desktop-dark.png"), ("light", "desktop-light2.png")):
        page.click("#btn-theme")
        page.wait_for_timeout(400)
        current = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        page.screenshot(path=str(OUT / shot))
        check(f"主题切换到 {theme}", current == theme, f"actual={current}")
    page.wait_for_timeout(200)
    stored = page.evaluate("() => localStorage.getItem('pc2mb-theme')")
    check("主题写入 localStorage", stored in ("light", "gray", "dark"), str(stored))

    check("无前端 JS 报错", not console_errors, "; ".join(console_errors[:3]))
    context.close()


def mobile_checks(browser) -> None:
    print("\n== 手机视口 390x844 ==")
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
        device_scale_factor=3,
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
    )
    page = context.new_page()
    context.add_init_script(WS_INIT_SCRIPT)
    context.add_init_script(LANDSCAPE_STUB)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        login_and_wait(page)
    except Exception:
        dump_diag(page, "mobile")
        raise
    page.wait_for_timeout(1500)
    page.screenshot(path=str(OUT / "mobile-light.png"))
    variance = canvas_png_colors(page)
    check("手机端登录并出画面", variance > 50, f"colors={variance}")

    keyboard_display = page.evaluate("() => getComputedStyle(document.querySelector('#btn-keyboard')).display")
    check("手机端显示键盘按钮", keyboard_display != "none", keyboard_display)

    # 键盘按钮 → 展开特殊键条并聚焦 IME 输入框
    page.tap("#btn-keyboard")
    page.wait_for_timeout(300)
    keybar_visible = page.locator("#keybar").is_visible()
    ime_focused = page.evaluate("() => document.activeElement && document.activeElement.id === 'ime'")
    check("点击键盘按钮展开键条/输入框", keybar_visible or ime_focused, f"keybar={keybar_visible} ime={ime_focused}")
    page.tap("#btn-keyboard")  # 收起键条，避免遮挡后续横屏测试
    page.wait_for_timeout(200)

    # ---- 横屏：优先系统方向锁定，不支持时退回 CSS 伪横屏 ----
    rotate_visible = page.locator("#btn-rotate").is_visible()
    check("手机端显示横屏按钮", rotate_visible)
    if rotate_visible:
        saved_cursor = cursor_pos()
        try:
            page.tap("#btn-rotate")
            page.wait_for_timeout(900)
            state = page.evaluate(
                "() => ({pseudo: document.documentElement.classList.contains('pseudo-landscape'),"
                " padMode: document.documentElement.classList.contains('pad-mode'),"
                " fs: !!document.fullscreenElement,"
                " type: (window.screen && window.screen.orientation && window.screen.orientation.type) || '',"
                " pressed: document.querySelector('#btn-rotate').getAttribute('aria-pressed')})"
            )
            landscape_on = state["pseudo"] or state["fs"] or state["type"].startswith("landscape")
            check("点击横屏按钮进入横屏", landscape_on and state["pressed"] == "true", str(state))
            page.screenshot(path=str(OUT / "mobile-landscape.png"))

            if state["pseudo"] and not state["padMode"]:
                # 无两侧控制区时（空白太窄），画面仍走直接触摸/鼠标坐标映射
                res_text = (page.locator("#res").text_content() or "").replace(" ", "")
                try:
                    fw, fh = [int(v) for v in res_text.split("×")]
                except Exception:
                    fw, fh = 1600, 1000
                page.mouse.move(100, 300)
                page.wait_for_timeout(400)
                sent = page.evaluate(
                    "() => (window.__sent || []).filter(s => s.indexOf('\"t\":\"m\"') >= 0)"
                )
                last = json.loads(sent[-1]) if sent else {}
                cw = page.evaluate("() => document.querySelector('#screen').clientWidth")
                ch = page.evaluate("() => document.querySelector('#screen').clientHeight")
                vw = page.evaluate("() => window.innerWidth")
                # 舞台顺时针旋转 90° 的逆映射：localX = clientY, localY = innerWidth - clientX
                local_x = 300
                local_y = vw - 100
                scale = min(cw / fw, ch / fh)
                draw_w, draw_h = fw * scale, fh * scale
                off_x, off_y = (cw - draw_w) / 2, (ch - draw_h) / 2
                expect_x = min(1.0, max(0.0, (local_x - off_x) / draw_w))
                expect_y = min(1.0, max(0.0, (local_y - off_y) / draw_h))
                got_x, got_y = last.get("x"), last.get("y")
                ok = (
                    got_x is not None
                    and abs(got_x - expect_x) < 0.03
                    and abs(got_y - expect_y) < 0.03
                )
                check("伪横屏触控坐标映射正确", ok, f"sent={last} expect=({expect_x:.3f},{expect_y:.3f})")

            if state["pseudo"] and state["padMode"]:
                # 触摸/按键只记录 WebSocket 消息、不真正注入远端桌面，避免测试误操作
                page.evaluate("() => { window.__blockInput = true; }")
                pad_state = page.evaluate(
                    "() => ({mode: document.documentElement.classList.contains('pad-mode'),"
                    " keys: getComputedStyle(document.querySelector('#pad-keys')).display,"
                    " touch: getComputedStyle(document.querySelector('#pad-touch')).display,"
                    " buttons: document.querySelectorAll('#pad-keys button.t9').length,"
                    " padW: getComputedStyle(document.documentElement).getPropertyValue('--pad-w').trim(),"
                    " keysW: document.querySelector('#pad-keys').clientWidth,"
                    " touchW: document.querySelector('#pad-touch').clientWidth})"
                )
                check(
                    "横屏显示两侧控制区",
                    pad_state["mode"] and pad_state["keys"] == "grid" and pad_state["touch"] == "flex",
                    str(pad_state),
                )
                check("横屏两侧面板等宽且贴着两侧", pad_state["keysW"] == pad_state["touchW"] > 0, str(pad_state))
                check("九宫格共 12 个按键", pad_state["buttons"] == 12, str(pad_state["buttons"]))

                # 九宫格：连点 ABC 两次 = b（800ms 后自动提交）
                page.tap("#pad-keys button[data-letters='abc']")
                page.wait_for_timeout(120)
                page.tap("#pad-keys button[data-letters='abc']")
                page.wait_for_timeout(1100)
                sent = page.evaluate("() => window.__sent || []")
                check(
                    "九宫格多次按键发送字母(vk)",
                    any('"t":"vk"' in s and '"s":"b"' in s for s in sent[-8:]),
                    str(sent[-5:]),
                )

                # 触摸板：拖动 = 相对移动（不应误触发点击）
                box = page.locator("#pad-touch").bounding_box()
                cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                before_count = len(page.evaluate("() => window.__sent || []"))
                page.mouse.move(cx, cy)
                page.mouse.down()
                page.mouse.move(cx - 30, cy - 50, steps=8)
                page.mouse.up()
                page.wait_for_timeout(300)
                moved = page.evaluate("() => window.__sent || []")[before_count:]
                m_events = [json.loads(s) for s in moved if '"t":"m"' in s]
                clicks = [s for s in moved if '"t":"d"' in s or '"t":"u"' in s]
                check(
                    "触摸板拖动产生相对移动",
                    bool(m_events) and (abs(m_events[0]["x"] - 0.5) > 0.005 or abs(m_events[0]["y"] - 0.5) > 0.005),
                    str(moved[:4]),
                )
                check("拖动不误触发点击", not clicks, str(clicks[:3]))

                # 触摸板轻点 = 左键单击
                before_count = len(page.evaluate("() => window.__sent || []"))
                page.mouse.move(cx, cy)
                page.mouse.down()
                page.mouse.up()
                page.wait_for_timeout(300)
                tapped = page.evaluate("() => window.__sent || []")[before_count:]
                check(
                    "触摸板轻点 = 左键单击",
                    any('"t":"d","b":0' in s for s in tapped) and any('"t":"u","b":0' in s for s in tapped),
                    str(tapped[:4]),
                )

                # 画面直接触摸应被禁用
                before_count = len(page.evaluate("() => window.__sent || []"))
                page.touchscreen.tap(195, 422)
                page.wait_for_timeout(400)
                touch_inputs = [
                    s
                    for s in page.evaluate("() => window.__sent || []")[before_count:]
                    if any(f'"t":"{kind}"' in s for kind in ("m", "d", "u", "w", "kd", "ku", "txt", "vk"))
                ]
                check("触摸板模式下画面不响应直接触摸", not touch_inputs, str(touch_inputs[:3]))
                page.evaluate("() => { window.__blockInput = false; }")

            page.tap("#btn-rotate")
            page.wait_for_timeout(700)
            off = page.evaluate(
                "() => ({pseudo: document.documentElement.classList.contains('pseudo-landscape'),"
                " fs: !!document.fullscreenElement,"
                " pressed: document.querySelector('#btn-rotate').getAttribute('aria-pressed')})"
            )
            check(
                "再次点击恢复竖屏",
                (not off["pseudo"]) and (not off["fs"]) and off["pressed"] == "false",
                str(off),
            )
        finally:
            set_cursor_pos(*saved_cursor)

    check("手机端无 JS 报错", not errors, "; ".join(errors[:3]))
    context.close()


def main() -> int:
    print(f"== pc2mb 浏览器冒烟测试 {BASE} ==")
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 与 Agent 一致，按物理像素比较光标
    except Exception:  # noqa: BLE001
        pass
    with sync_playwright() as p:
        # 本机未安装 playwright 自带浏览器时，复用系统 Chrome/Edge
        launch_kwargs = {"headless": True}
        channel = os.environ.get("PC2MB_BROWSER_CHANNEL", "chrome")
        try:
            browser = p.chromium.launch(channel=channel, **launch_kwargs)
        except Exception as exc:  # noqa: BLE001
            print(f"提示: channel={channel} 启动失败({exc})，尝试内置 chromium")
            browser = p.chromium.launch(**launch_kwargs)
        try:
            desktop_checks(browser)
            mobile_checks(browser)
        finally:
            browser.close()
    print()
    if failures:
        print(f"{len(failures)} 项失败: {failures}")
        return 1
    print(f"全部通过，截图在 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
