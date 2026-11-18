#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC2MB agent 离线自检（不连服务器，不做破坏性操作）。

覆盖内容
--------
1. DPI awareness 是否生效（决定抓到的分辨率和光标坐标系是否一致）；
2. mss 抓主显示器 + GDI+ JPEG 编码：保存 ``agent/_selftest.jpg``，
   再用 GDI+ 把 jpg 解码回来校验尺寸 / 非全黑 / JPEG 标记，并统计单帧耗时；
3. 鼠标注入安全测试：记录原坐标 -> 移到屏幕中心 / (100,100) -> 校验 -> 复位；
   （只移动光标，不点击、不输入，避免对真实桌面产生副作用）
4. 键盘映射纯函数校验（protocol 里出现的所有 key，不真的按键）；
5. SendInput 结构体大小校验（x64 下 INPUT=40 字节）；
5b. 安全键盘注入：单独按/放 Ctrl（无副作用）并用 GetAsyncKeyState 校验，
    再验证 release_all() 能释放"卡住"的 Shift；
6. 配置优先级校验：命令行 > 环境变量 > agent.env。

退出码：0 = 全部通过，1 = 有失败项，3 = 非 Windows 平台。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import agent as A  # noqa: E402  (需要先补 sys.path)

SELFTEST_JPEG = os.path.join(HERE, "_selftest.jpg")

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" ... {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n== {title} ==")


# ---------------------------------------------------------------------------
# JPEG 解码（用同一个 GDI+ 实例）
# ---------------------------------------------------------------------------
class BitmapData(ctypes.Structure):
    _fields_ = [
        ("Width", ctypes.c_uint32),
        ("Height", ctypes.c_uint32),
        ("Stride", ctypes.c_int32),
        ("PixelFormat", ctypes.c_int32),
        ("Scan0", ctypes.c_void_p),
        ("Reserved", ctypes.c_size_t),
    ]


def decode_jpeg(encoder: A.GdiPlusJpegEncoder, path: str) -> dict:
    """用 GDI+ 解码 jpg，返回尺寸和平均亮度（采样）。"""
    gp = encoder._gdiplus
    gp.GdipCreateBitmapFromFile.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    gp.GdipCreateBitmapFromFile.restype = ctypes.c_int
    gp.GdipGetImageWidth.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    gp.GdipGetImageWidth.restype = ctypes.c_int
    gp.GdipGetImageHeight.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    gp.GdipGetImageHeight.restype = ctypes.c_int
    gp.GdipBitmapLockBits.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wt.RECT),
        ctypes.c_uint32,
        ctypes.c_int32,
        ctypes.POINTER(BitmapData),
    ]
    gp.GdipBitmapLockBits.restype = ctypes.c_int
    gp.GdipBitmapUnlockBits.argtypes = [ctypes.c_void_p, ctypes.POINTER(BitmapData)]
    gp.GdipBitmapUnlockBits.restype = ctypes.c_int
    gp.GdipDisposeImage.argtypes = [ctypes.c_void_p]
    gp.GdipDisposeImage.restype = ctypes.c_int

    bitmap = ctypes.c_void_p()
    st = gp.GdipCreateBitmapFromFile(path, ctypes.byref(bitmap))
    if st != 0:
        raise RuntimeError(f"GdipCreateBitmapFromFile failed: status={st}")
    try:
        width = ctypes.c_uint32(0)
        height = ctypes.c_uint32(0)
        gp.GdipGetImageWidth(bitmap, ctypes.byref(width))
        gp.GdipGetImageHeight(bitmap, ctypes.byref(height))
        rect = wt.RECT(0, 0, width.value, height.value)
        data = BitmapData()
        st = gp.GdipBitmapLockBits(
            bitmap,
            ctypes.byref(rect),
            1,  # ImageLockModeRead
            A.PIXEL_FORMAT_32BPP_ARGB,
            ctypes.byref(data),
        )
        if st != 0:
            raise RuntimeError(f"GdipBitmapLockBits failed: status={st}")
        try:
            stride = data.Stride
            pixels = ctypes.string_at(data.Scan0, stride * height.value)
            total, samples = 0, 0
            step = max(1, (width.value * height.value) // 20000)
            for index in range(0, width.value * height.value, step):
                y, x = divmod(index, width.value)
                offset = y * stride + x * 4
                b, g, r = pixels[offset], pixels[offset + 1], pixels[offset + 2]
                total += r + g + b
                samples += 3
            mean = total / samples if samples else 0.0
            return {"width": width.value, "height": height.value, "mean_luma": round(mean, 1)}
        finally:
            gp.GdipBitmapUnlockBits(bitmap, ctypes.byref(data))
    finally:
        gp.GdipDisposeImage(bitmap)


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------
def check_dpi() -> str:
    section("1. DPI awareness")
    mode = A.enable_dpi_awareness()
    ok = mode in ("per-monitor", "system")
    record("enable_dpi_awareness", ok, mode)
    return mode


def check_capture(capture: A.ScreenCapture) -> dict:
    section("2. screen capture -> JPEG")
    src_w, src_h = capture.source_size()
    out_w, out_h = capture.output_size()
    print(f"  primary monitor: {src_w}x{src_h} -> frame {out_w}x{out_h} (max_width={capture.max_width})")
    ok_scale = (out_w == src_w) or (out_w == capture.max_width)

    frames = []
    for index in range(3):
        started = time.perf_counter()
        frame = capture.grab_jpeg()
        elapsed = (time.perf_counter() - started) * 1000.0
        frames.append((frame, elapsed))
        print(
            f"  frame {index + 1}: {frame.width}x{frame.height} "
            f"{len(frame.jpeg)} bytes ({frame.size_kb:.1f} KB), "
            f"grab {frame.grab_ms:.1f} ms, encode {frame.encode_ms:.1f} ms, "
            f"total {elapsed:.1f} ms"
        )
    frame, elapsed = frames[-1]  # 跳过第一帧的初始化抖动

    with open(SELFTEST_JPEG, "wb") as handle:
        handle.write(frame.jpeg)
    record("jpeg saved", os.path.isfile(SELFTEST_JPEG), SELFTEST_JPEG)
    record("jpeg markers (SOI/EOI)", frame.jpeg[:2] == b"\xff\xd8" and frame.jpeg[-2:] == b"\xff\xd9")
    record("frame dimensison matches", (frame.width, frame.height) == (out_w, out_h), f"{frame.width}x{frame.height}")
    record("scale rule (width<=1600)", ok_scale, f"{src_w}->{out_w}")

    encoder = capture._encoder
    assert encoder is not None
    info = decode_jpeg(encoder, SELFTEST_JPEG)
    print(f"  decoded back: {info['width']}x{info['height']}, mean luma {info['mean_luma']}")
    record("decoded size matches", (info["width"], info["height"]) == (out_w, out_h), f"{info['width']}x{info['height']}")
    record("image is not black", info["mean_luma"] > 8.0, f"mean luma {info['mean_luma']} (0=black, 255=white)")
    record("single frame time < 500 ms", elapsed < 500.0, f"{elapsed:.1f} ms")
    return {
        "jpeg": SELFTEST_JPEG,
        "frame": f"{out_w}x{out_h}",
        "bytes": len(frame.jpeg),
        "single_frame_ms": round(elapsed, 1),
        "mean_luma": info["mean_luma"],
    }


def check_mouse(injector: A.InputInjector) -> dict:
    section("3. mouse injection (move only, no clicks)")
    original = injector.cursor_position()
    print(f"  original cursor: {original}")
    ok_restore = False
    center = None
    try:
        # 归一化坐标 -> 屏幕中心（走协议 m 事件，验证归一化换算）
        px, py = injector.mouse_move(0.5, 0.5)
        time.sleep(0.05)
        center = injector.cursor_position()
        expect = (round(0.5 * injector.screen_w), round(0.5 * injector.screen_h))
        record(
            "m event moves cursor to center",
            abs(center[0] - expect[0]) <= 1 and abs(center[1] - expect[1]) <= 1,
            f"expected {expect}, got {center}",
        )

        # 绝对坐标 (100,100)：任务要求的安全位置
        A._user32().SetCursorPos(100, 100)
        time.sleep(0.05)
        pos_100 = injector.cursor_position()
        record("SetCursorPos(100,100)", abs(pos_100[0] - 100) <= 1 and abs(pos_100[1] - 100) <= 1, str(pos_100))

        # 再走一次协议事件路径，确认 handle_event 的 m 分支
        injector.handle_event({"t": "m", "x": 0.25, "y": 0.25})
        time.sleep(0.05)
        quarter = injector.cursor_position()
        expect_q = (round(0.25 * injector.screen_w), round(0.25 * injector.screen_h))
        record(
            "handle_event m (0.25,0.25)",
            abs(quarter[0] - expect_q[0]) <= 1 and abs(quarter[1] - expect_q[1]) <= 1,
            f"expected {expect_q}, got {quarter}",
        )
    finally:
        A._user32().SetCursorPos(original[0], original[1])
        time.sleep(0.05)
        restored = injector.cursor_position()
        ok_restore = abs(restored[0] - original[0]) <= 1 and abs(restored[1] - original[1]) <= 1
        record("cursor restored", ok_restore, f"restored {restored}, original {original}")
    return {"original": original, "center": center, "restored": ok_restore}


def check_key_mapping() -> dict:
    section("4. key mapping (pure function, no real key press)")
    cases = {
        "a": ("char", None),
        "A": ("char", None),
        "1": ("char", None),
        "你": ("char", None),
        "Enter": ("special", 0x0D),
        "Backspace": ("special", 0x08),
        "Tab": ("special", 0x09),
        "Escape": ("special", 0x1B),
        "Delete": ("special", 0x2E),
        "ArrowUp": ("special", 0x26),
        "ArrowDown": ("special", 0x28),
        "ArrowLeft": ("special", 0x25),
        "ArrowRight": ("special", 0x27),
        "Home": ("special", 0x24),
        "End": ("special", 0x23),
        "PageUp": ("special", 0x21),
        "PageDown": ("special", 0x22),
        "Insert": ("special", 0x2D),
        "F1": ("special", 0x70),
        "F12": ("special", 0x7B),
        "Control": ("modifier", 0x11),
        "Shift": ("modifier", 0x10),
        "Alt": ("modifier", 0x12),
        "Meta": ("modifier", 0x5B),
    }
    failed = []
    for key, expected in cases.items():
        got = A.resolve_key(key)
        if got != expected:
            failed.append(f"{key!r}: expected {expected}, got {got}")
    record("protocol key table", not failed, f"{len(cases) - len(failed)}/{len(cases)} ok" + (f"; {failed}" if failed else ""))
    for weird in ("MediaPlayPause", "", "Dead", None, 123):
        kind, vk = A.resolve_key(weird)  # type: ignore[arg-type]
        if kind != "unknown" or vk is not None:
            record(f"unknown key {weird!r} ignored", False, f"{kind},{vk}")
            break
    else:
        record("unknown keys ignored", True, "5 weird inputs -> unknown")
    return {"cases": len(cases)}


def check_structs() -> dict:
    section("5. SendInput structs")
    size_input = ctypes.sizeof(A.INPUT)
    size_kb = ctypes.sizeof(A.KEYBDINPUT)
    size_mi = ctypes.sizeof(A.MOUSEINPUT)
    print(f"  sizeof(INPUT)={size_input} MOUSEINPUT={size_mi} KEYBDINPUT={size_kb}")
    record("INPUT size (x64 = 40)", size_input == 40, str(size_input))
    record("KEYBDINPUT size (x64 = 24)", size_kb == 24, str(size_kb))
    record("MOUSEINPUT size (x64 = 32)", size_mi == 32, str(size_mi))
    # 构造一次事件但不发送：确认字段编码正确（wheel 的负数放在 DWORD 里）
    wheel_input = A._mouse_input(A.MOUSEEVENTF_WHEEL, data=-120)
    record("wheel data encoded as DWORD", wheel_input.mi.mouseData == 0xFFFFFF88, hex(wheel_input.mi.mouseData))
    unicode_input = A._keyboard_input(0, ord("中"), A.KEYEVENTF_UNICODE)
    record("unicode scan code", unicode_input.ki.wScan == ord("中"), hex(unicode_input.ki.wScan))
    # release_all 在空状态下不应崩溃
    injector = A.InputInjector(10, 10)
    injector.release_all()
    record("release_all on empty state", True, "no exception")
    injector.text("")
    record("text('') no-op", True, "no exception")
    return {"input": size_input}


def check_keyboard_state(injector: A.InputInjector) -> dict:
    """安全的键盘注入测试：只按/放 Control、Shift（单独按修饰键对桌面无副作用）。"""
    section("5b. safe keyboard injection (modifier press/release only)")
    user32 = A._user32()
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short

    def is_down(vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    if is_down(A.VK_CONTROL) or is_down(A.VK_SHIFT):
        record("modifier injection", True, "physical Ctrl/Shift already held - skipped to avoid interference")
        return {}

    try:
        injector.key_down("Control")
        time.sleep(0.06)
        down_ok = is_down(A.VK_CONTROL)
        injector.key_up("Control")
        time.sleep(0.06)
        up_ok = not is_down(A.VK_CONTROL)
        record("kd Control -> key is down", down_ok, f"GetAsyncKeyState=0x11 down={down_ok}")
        record("ku Control -> key is up", up_ok, f"GetAsyncKeyState=0x11 down={not up_ok}")

        # 模拟"ku 丢失"：按下 Shift 后直接 release_all，必须能自动释放
        injector.key_down("Shift")
        time.sleep(0.05)
        stuck_before = is_down(A.VK_SHIFT)
        injector.release_all()
        time.sleep(0.06)
        freed = stuck_before and not is_down(A.VK_SHIFT)
        record("release_all frees stuck modifier", freed, f"shift stuck_before={stuck_before} freed={freed}")
    finally:
        injector.release_all()
    return {"control_ok": down_ok and up_ok, "release_all_ok": True}


def check_config() -> dict:
    section("6. config precedence (cli > env > agent.env)")
    temp_dir = tempfile.mkdtemp(prefix="pc2mb-selftest-")
    env_path = os.path.join(temp_dir, "agent.env")
    with open(env_path, "w", encoding="utf-8") as handle:
        handle.write(
            "# comment line\n"
            'export PC2MB_TOKEN="file-token"\n'
            "PC2MB_SERVER=ws://file/ws/agent\n"
            "PC2MB_MAX_WIDTH=1200\n"
        )
    problems = []
    try:
        # 1) 环境变量覆盖 agent.env
        cfg, _ = A.resolve_config(
            ["--env-file", env_path], environ={"PC2MB_TOKEN": "env-token"}
        )
        if cfg.token != "env-token":
            problems.append(f"env should win over file: {cfg.token!r}")
        if cfg.server != "ws://file/ws/agent":
            problems.append(f"file server not used: {cfg.server!r}")
        if cfg.max_width != 1200:
            problems.append(f"file max_width not used: {cfg.max_width}")

        # 2) 命令行覆盖环境变量 + 文件
        cfg, _ = A.resolve_config(
            [
                "--env-file", env_path,
                "--token", "cli-token",
                "--server", "ws://cli/ws/agent",
                "--max-width", "1000",
                "--quality", "75",
            ],
            environ={"PC2MB_TOKEN": "env-token", "PC2MB_SERVER": "ws://env/ws/agent"},
        )
        if (cfg.token, cfg.server, cfg.max_width, cfg.quality) != (
            "cli-token",
            "ws://cli/ws/agent",
            1000,
            75,
        ):
            problems.append(f"cli precedence wrong: {cfg.redacted()} q={cfg.quality}")

        # 3) 只有文件
        cfg, _ = A.resolve_config(["--env-file", env_path], environ={})
        if cfg.token != "file-token":
            problems.append(f"file token not used: {cfg.token!r}")

        # 4) 缺 token 必须报 ConfigError
        try:
            A.resolve_config(["--env-file", os.path.join(temp_dir, "nope.env")], environ={})
            problems.append("missing token should raise ConfigError")
        except A.ConfigError:
            pass

        # 5) token 拼进 query 且覆盖旧值
        uri = A.build_uri("ws://h:18091/ws/agent?token=old&x=1", "new token")
        if "token=new+token" not in uri or "token=old" in uri or "x=1" not in uri:
            problems.append(f"build_uri wrong: {uri}")
        record("build_uri", "token=new+token" in uri, uri)

        # 6) 非法 URL 必须报错
        for bad in ("http://h/ws/agent", "ws:///ws/agent"):
            try:
                A.build_uri(bad, "t")
                problems.append(f"invalid URL accepted: {bad}")
            except A.ConfigError:
                pass
    finally:
        for name in os.listdir(temp_dir):
            os.remove(os.path.join(temp_dir, name))
        os.rmdir(temp_dir)

    record("config precedence", not problems, "; ".join(problems) if problems else "cli > env > file ok")
    return {}


# ---------------------------------------------------------------------------
def main() -> int:
    print("PC2MB agent selftest - " + time.strftime("%Y-%m-%d %H:%M:%S"))
    if not A.IS_WINDOWS:
        print("FAIL: Windows only")
        return 3

    summary: dict = {}
    capture = None
    try:
        summary["dpi"] = check_dpi()
        capture = A.ScreenCapture()
        summary.update(check_capture(capture))
        injector = A.InputInjector(*capture.source_size())
        summary.update(check_mouse(injector))
        check_key_mapping()
        check_structs()
        summary.update(check_keyboard_state(injector))
        check_config()
    except Exception:  # noqa: BLE001
        print("\nUNEXPECTED EXCEPTION:")
        traceback.print_exc()
        RESULTS.append(("unexpected exception", False, "see traceback"))
    finally:
        if capture is not None:
            capture.close()

    passed = sum(1 for _name, ok, _detail in RESULTS if ok)
    failed = [(name, detail) for name, ok, detail in RESULTS if not ok]
    print("\n" + "=" * 64)
    print(f"SUMMARY: {passed}/{len(RESULTS)} checks passed")
    if failed:
        for name, detail in failed:
            print(f"  FAILED: {name} {detail}")
        print("RESULT: FAIL")
        return 1
    print(f"  selftest jpeg : {summary.get('jpeg')}")
    print(f"  frame         : {summary.get('frame')} / {summary.get('bytes')} bytes / {summary.get('single_frame_ms')} ms")
    print(f"  cursor        : original={summary.get('original')} center={summary.get('center')} restored={summary.get('restored')}")
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
