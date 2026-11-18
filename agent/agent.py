#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC2MB 被控端 Agent（Windows）。

职责
----
1. 通过 WSS 出站连接 PC2MB 服务端 ``/ws/agent?token=...``；
2. 收到 ``{"t":"req"}`` 时用 mss 抓取主显示器一帧，JPEG(quality=60) 编码后以 binary 回发；
3. 收到 ``{"t":"input","ev":{...}}`` 时用纯 ctypes（user32.SendInput / SetCursorPos）
   注入鼠标、滚轮、键盘事件。

依赖只有 mss + websockets。JPEG 编码使用 Windows 自带的 GDI+（gdiplus.dll），
因此不需要 Pillow。协议见 docs/PROTOCOL.md，本文件与协议 v1 对齐。

仅支持 Windows。日志写 stdout，带时间戳。
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

LOG = logging.getLogger("pc2mb.agent")

IS_WINDOWS = sys.platform == "win32"
APP_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_SERVER = "ws://127.0.0.1:18091/ws/agent"
DEFAULT_ENV_FILE = os.path.join(APP_DIR, "agent.env")
DEFAULT_MAX_WIDTH = 1600
DEFAULT_JPEG_QUALITY = 60
BACKOFF_SECONDS = (1, 2, 4, 8, 15)
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2  # token 错误 / 配置错误：不重连，直接退出


# ---------------------------------------------------------------------------
# DPI awareness
# ---------------------------------------------------------------------------
def enable_dpi_awareness() -> str:
    """把进程设为 Per-Monitor DPI aware，使 mss / SetCursorPos 使用物理像素。

    否则在 125% 缩放的机器上抓到的画面和鼠标坐标都会和真实桌面对不上。
    返回实际生效的模式名，便于日志与自检确认。
    """
    if not IS_WINDOWS:
        return "non-windows"
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Windows 8.1+)
        hr = ctypes.windll.shcore.SetProcessDpiAwareness(2)
        if hr == 0:
            return "per-monitor"
    except Exception:  # noqa: BLE001 - 老系统没有 shcore
        pass
    try:
        # Vista+ 回退：system DPI aware
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system"
    except Exception:  # noqa: BLE001
        pass
    return "none"


# ---------------------------------------------------------------------------
# GDI+ JPEG 编码（免 Pillow）
# ---------------------------------------------------------------------------
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def to_bytes(self) -> bytes:
        return bytes(self)


def _guid(text: str) -> GUID:
    u = uuid.UUID(text)
    g = GUID()
    g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", wt.BOOL),
        ("SuppressExternalCodecs", wt.BOOL),
    ]


class ImageCodecInfo(ctypes.Structure):
    _fields_ = [
        ("Clsid", GUID),
        ("FormatID", GUID),
        ("CodecName", ctypes.c_wchar_p),
        ("DllName", ctypes.c_wchar_p),
        ("FormatDescription", ctypes.c_wchar_p),
        ("FilenameExtension", ctypes.c_wchar_p),
        ("MimeType", ctypes.c_wchar_p),
        ("Flags", ctypes.c_uint32),
        ("Version", ctypes.c_uint32),
        ("SigCount", ctypes.c_uint32),
        ("SigSize", ctypes.c_uint32),
        ("SigPattern", ctypes.c_void_p),
        ("SigMask", ctypes.c_void_p),
    ]


class EncoderParameter(ctypes.Structure):
    _fields_ = [
        ("Guid", GUID),
        ("NumberOfValues", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("Value", ctypes.c_void_p),
    ]


class EncoderParameters(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint32), ("Parameter", EncoderParameter * 1)]


class _IStream(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(ctypes.c_void_p))]


PIXEL_FORMAT_32BPP_ARGB = 0x0026200A
ENCODER_QUALITY = "1d5be4b5-fa4a-452d-9cdd-5db35105e7eb"  # Encoder.Quality
ENCODER_VALUE_TYPE_LONG = 4

# 缩小用的插值模式。实测 2880x1800 -> 1600x1000（本机 200% DPI）：
#   HighQualityBicubic(7)   ~91 ms   (画质最好但太慢)
#   HighQualityBilinear(5)  ~23 ms   (体积相近，文字清晰度足够，选它)
#   Bilinear(3)             ~42 ms
#   NearestNeighbor(4)     ~101 ms  (又慢又差)
# 帧率对远程桌面比画质更敏感，所以用 HighQualityBilinear。
INTERPOLATION_MODE = 5  # InterpolationModeHighQualityBilinear


class GdiPlusJpegEncoder:
    """把 BGRA 原始像素直接编码成 JPEG，可顺便等比缩放到目标宽度。

    mss 的 ``ScreenShot.bgra`` 字节序是 B,G,R,A，与 GDI+ PixelFormat32bppARGB
    在内存中的布局一致，因此可以零拷贝地交给 ``GdipCreateBitmapFromScan0``。
    """

    def __init__(self) -> None:
        if not IS_WINDOWS:
            raise RuntimeError("GDI+ JPEG encoder is Windows-only")
        self._gdiplus = ctypes.WinDLL("gdiplus", use_last_error=True)
        self._ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure()
        self._token = ctypes.c_size_t(0)
        startup = GdiplusStartupInput()
        startup.GdiplusVersion = 1
        startup.DebugEventCallback = None
        startup.SuppressBackgroundThread = 0
        startup.SuppressExternalCodecs = 0
        status = self._gdiplus.GdiplusStartup(
            ctypes.byref(self._token), ctypes.byref(startup), None
        )
        if status != 0:
            raise RuntimeError(f"GdiplusStartup failed: status={status}")
        self._started = True
        self._jpeg_clsid = self._find_encoder_clsid("image/jpeg")

    # -- win32 原型 -------------------------------------------------------
    def _configure(self) -> None:
        gp = self._gdiplus
        gp.GdiplusStartup.argtypes = [
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(GdiplusStartupInput),
            ctypes.c_void_p,
        ]
        gp.GdiplusStartup.restype = ctypes.c_int
        gp.GdiplusShutdown.argtypes = [ctypes.c_size_t]
        gp.GdiplusShutdown.restype = None
        gp.GdipCreateBitmapFromScan0.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        gp.GdipCreateBitmapFromScan0.restype = ctypes.c_int
        gp.GdipGetImageGraphicsContext.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        gp.GdipGetImageGraphicsContext.restype = ctypes.c_int
        gp.GdipSetInterpolationMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        gp.GdipSetInterpolationMode.restype = ctypes.c_int
        gp.GdipDrawImageRectI.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        gp.GdipDrawImageRectI.restype = ctypes.c_int
        gp.GdipDeleteGraphics.argtypes = [ctypes.c_void_p]
        gp.GdipDeleteGraphics.restype = ctypes.c_int
        gp.GdipDisposeImage.argtypes = [ctypes.c_void_p]
        gp.GdipDisposeImage.restype = ctypes.c_int
        gp.GdipGetImageEncodersSize.argtypes = [
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        gp.GdipGetImageEncodersSize.restype = ctypes.c_int
        gp.GdipGetImageEncoders.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ImageCodecInfo),
        ]
        gp.GdipGetImageEncoders.restype = ctypes.c_int
        gp.GdipSaveImageToStream.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(GUID),
            ctypes.POINTER(EncoderParameters),
        ]
        gp.GdipSaveImageToStream.restype = ctypes.c_int

        ole = self._ole32
        ole.CreateStreamOnHGlobal.argtypes = [
            ctypes.c_void_p,
            wt.BOOL,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        ole.CreateStreamOnHGlobal.restype = ctypes.c_long
        ole.GetHGlobalFromStream.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        ole.GetHGlobalFromStream.restype = ctypes.c_long

        k32 = self._kernel32
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        k32.GlobalUnlock.restype = wt.BOOL
        k32.GlobalSize.argtypes = [ctypes.c_void_p]
        k32.GlobalSize.restype = ctypes.c_size_t

    # -- 编码 -------------------------------------------------------------
    def _find_encoder_clsid(self, mime: str) -> GUID:
        count = ctypes.c_uint32(0)
        size = ctypes.c_uint32(0)
        st = self._gdiplus.GdipGetImageEncodersSize(
            ctypes.byref(count), ctypes.byref(size)
        )
        if st != 0 or count.value == 0:
            raise RuntimeError(f"GdipGetImageEncodersSize failed: status={st}")
        buf = (ctypes.c_ubyte * size.value)()
        st = self._gdiplus.GdipGetImageEncoders(
            count.value, size.value, ctypes.cast(buf, ctypes.POINTER(ImageCodecInfo))
        )
        if st != 0:
            raise RuntimeError(f"GdipGetImageEncoders failed: status={st}")
        infos = ctypes.cast(buf, ctypes.POINTER(ImageCodecInfo))
        for i in range(count.value):
            if (infos[i].MimeType or "").lower() == mime.lower():
                return infos[i].Clsid
        raise RuntimeError(f"no GDI+ image encoder found for {mime}")

    def encode(
        self,
        bgra: bytes,
        width: int,
        height: int,
        quality: int = DEFAULT_JPEG_QUALITY,
        out_width: Optional[int] = None,
    ) -> bytes:
        """BGRA -> JPEG bytes。out_width 小于 width 时用高质量双三次等比缩小。"""
        expected = width * height * 4
        if len(bgra) < expected:
            raise ValueError(f"bgra buffer too small: {len(bgra)} < {expected}")
        src_buf = ctypes.create_string_buffer(bgra, expected)
        src = ctypes.c_void_p()
        st = self._gdiplus.GdipCreateBitmapFromScan0(
            width,
            height,
            width * 4,
            PIXEL_FORMAT_32BPP_ARGB,
            ctypes.cast(src_buf, ctypes.c_void_p),
            ctypes.byref(src),
        )
        if st != 0:
            raise RuntimeError(f"GdipCreateBitmapFromScan0 failed: status={st}")

        target = src
        target_w, target_h = width, height
        scaled: Optional[ctypes.c_void_p] = None
        try:
            if out_width and 0 < out_width < width:
                target_w = int(out_width)
                target_h = max(1, round(height * target_w / width))
                scaled = ctypes.c_void_p()
                st = self._gdiplus.GdipCreateBitmapFromScan0(
                    target_w,
                    target_h,
                    0,
                    PIXEL_FORMAT_32BPP_ARGB,
                    None,
                    ctypes.byref(scaled),
                )
                if st != 0:
                    raise RuntimeError(f"create scaled bitmap failed: status={st}")
                graphics = ctypes.c_void_p()
                st = self._gdiplus.GdipGetImageGraphicsContext(
                    scaled, ctypes.byref(graphics)
                )
                if st != 0:
                    raise RuntimeError(f"GdipGetImageGraphicsContext failed: status={st}")
                try:
                    self._gdiplus.GdipSetInterpolationMode(graphics, INTERPOLATION_MODE)
                    st = self._gdiplus.GdipDrawImageRectI(
                        graphics, src, 0, 0, target_w, target_h
                    )
                    if st != 0:
                        raise RuntimeError(f"GdipDrawImageRectI failed: status={st}")
                finally:
                    self._gdiplus.GdipDeleteGraphics(graphics)
                target = scaled
            return self._save_jpeg(target, quality)
        finally:
            if scaled is not None:
                self._gdiplus.GdipDisposeImage(scaled)
            self._gdiplus.GdipDisposeImage(src)

    def _save_jpeg(self, image: ctypes.c_void_p, quality: int) -> bytes:
        stream = ctypes.c_void_p()
        hr = self._ole32.CreateStreamOnHGlobal(None, True, ctypes.byref(stream))
        if hr != 0:
            raise RuntimeError(f"CreateStreamOnHGlobal failed: hr=0x{hr & 0xFFFFFFFF:08x}")
        try:
            params = EncoderParameters()
            params.Count = 1
            q = ctypes.c_uint32(int(quality))
            params.Parameter[0].Guid = _guid(ENCODER_QUALITY)
            params.Parameter[0].NumberOfValues = 1
            params.Parameter[0].Type = ENCODER_VALUE_TYPE_LONG
            params.Parameter[0].Value = ctypes.cast(
                ctypes.byref(q), ctypes.c_void_p
            )
            st = self._gdiplus.GdipSaveImageToStream(
                image, stream, ctypes.byref(self._jpeg_clsid), ctypes.byref(params)
            )
            if st != 0:
                raise RuntimeError(f"GdipSaveImageToStream failed: status={st}")
            hglobal = ctypes.c_void_p()
            hr = self._ole32.GetHGlobalFromStream(stream, ctypes.byref(hglobal))
            if hr != 0:
                raise RuntimeError(f"GetHGlobalFromStream failed: hr={hr}")
            ptr = self._kernel32.GlobalLock(hglobal)
            if not ptr:
                raise RuntimeError("GlobalLock failed")
            try:
                size = self._kernel32.GlobalSize(hglobal)
                return ctypes.string_at(ptr, size)
            finally:
                self._kernel32.GlobalUnlock(hglobal)
        finally:
            self._release_stream(stream)

    def _release_stream(self, stream: ctypes.c_void_p) -> None:
        if not stream:
            return
        try:
            st = ctypes.cast(stream, ctypes.POINTER(_IStream)).contents
            release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
                st.lpVtbl[2]  # IUnknown::Release
            )
            release(stream)
        except Exception:  # noqa: BLE001 - 释放失败不影响主流程
            pass

    def close(self) -> None:
        if getattr(self, "_started", False):
            self._started = False
            try:
                self._gdiplus.GdiplusShutdown(self._token)
            except Exception:  # noqa: BLE001
                pass

    def __del__(self) -> None:  # pragma: no cover - 解释器退出时兜底
        self.close()


# ---------------------------------------------------------------------------
# 屏幕采集
# ---------------------------------------------------------------------------
@dataclass
class Frame:
    jpeg: bytes
    width: int            # 编码后（可能已缩小）的宽
    height: int
    src_width: int        # 主显示器物理宽
    src_height: int
    grab_ms: float
    encode_ms: float

    @property
    def size_kb(self) -> float:
        return len(self.jpeg) / 1024.0


class ScreenCapture:
    """mss 单实例循环复用 + GDI+ JPEG 编码。

    线程约束：mss 和 GDI+ 实例都必须在**同一个线程**里创建和使用。
    Agent 通过只有一个 worker 的线程池串行调用，保证这一点；
    自检脚本直接在同线程调用即可。
    """

    def __init__(
        self,
        max_width: int = DEFAULT_MAX_WIDTH,
        quality: int = DEFAULT_JPEG_QUALITY,
        monitor_index: int = 1,
    ) -> None:
        self.max_width = max_width
        self.quality = quality
        self.monitor_index = monitor_index
        self._sct: Any = None
        self._encoder: Optional[GdiPlusJpegEncoder] = None
        self._frame_no = 0

    def _ensure(self) -> None:
        if self._sct is None:
            import mss  # 延迟导入，--help 时不需要依赖

            # mss 10.2 起 mss.mss() 标记 deprecated，优先用 mss.MSS()
            factory = getattr(mss, "MSS", None) or mss.mss
            self._sct = factory()
            self._encoder = GdiPlusJpegEncoder()

    def source_size(self) -> tuple[int, int]:
        """主显示器物理分辨率（光标坐标空间）。"""
        self._ensure()
        mon = self._sct.monitors[self.monitor_index]
        return int(mon["width"]), int(mon["height"])

    def output_size(self) -> tuple[int, int]:
        """编码后帧尺寸（宽超过 max_width 时等比缩小）。"""
        sw, sh = self.source_size()
        if sw > self.max_width > 0:
            return self.max_width, max(1, round(sh * self.max_width / sw))
        return sw, sh

    def grab_jpeg(self) -> Frame:
        self._ensure()
        t0 = time.perf_counter()
        mon = self._sct.monitors[self.monitor_index]
        shot = self._sct.grab(mon)
        t1 = time.perf_counter()
        sw, sh = int(shot.width), int(shot.height)
        out_w = self.max_width if (self.max_width and sw > self.max_width) else sw
        jpeg = self._encoder.encode(shot.bgra, sw, sh, self.quality, out_w)
        t2 = time.perf_counter()
        self._frame_no += 1
        out_h = max(1, round(sh * out_w / sw))
        return Frame(
            jpeg=jpeg,
            width=out_w,
            height=out_h,
            src_width=sw,
            src_height=sh,
            grab_ms=(t1 - t0) * 1000.0,
            encode_ms=(t2 - t1) * 1000.0,
        )

    def close(self) -> None:
        if self._encoder is not None:
            self._encoder.close()
            self._encoder = None
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:  # noqa: BLE001
                pass
            self._sct = None


# ---------------------------------------------------------------------------
# 输入注入（纯 ctypes user32.SendInput）
# ---------------------------------------------------------------------------
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_LWIN = 0x5B

MODIFIER_VK = {
    "control": VK_CONTROL,
    "shift": VK_SHIFT,
    "alt": VK_MENU,
    "meta": VK_LWIN,
    "win": VK_LWIN,
}

SPECIAL_VK = {
    "enter": 0x0D,
    "backspace": 0x08,
    "tab": 0x09,
    "escape": 0x1B,
    "delete": 0x2E,
    "insert": 0x2D,
    "arrowup": 0x26,
    "arrowdown": 0x28,
    "arrowleft": 0x25,
    "arrowright": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    " ": 0x20,
    "space": 0x20,
    "spacebar": 0x20,
    "capslock": 0x14,
    "contextmenu": 0x5D,
    "printscreen": 0x2C,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "pause": 0x13,
}
SPECIAL_VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})

# 这些键用 SendInput 时必须带 KEYEVENTF_EXTENDEDKEY，否则部分应用（远程桌面/
# 游戏）识别成小键盘键。
EXTENDED_VKS = {
    0x26, 0x28, 0x25, 0x27,  # arrows
    0x2D, 0x2E, 0x24, 0x23,  # insert delete home end
    0x21, 0x22,              # page up/down
    0x5B, 0x5C,              # win keys
    0x5D,                    # apps
    0xA3, 0xA5,              # right ctrl / right alt
}

MOUSE_BUTTONS = {
    0: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    1: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    2: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wt.DWORD),
        ("wParamL", wt.WORD),
        ("wParamH", wt.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi = MOUSEINPUT(
        dx=int(dx),
        dy=int(dy),
        mouseData=int(data) & 0xFFFFFFFF,
        dwFlags=int(flags),
        time=0,
        dwExtraInfo=0,
    )
    return inp


def _keyboard_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(
        wVk=int(vk) & 0xFFFF,
        wScan=int(scan) & 0xFFFF,
        dwFlags=int(flags),
        time=0,
        dwExtraInfo=0,
    )
    return inp


def _user32() -> Any:
    if not IS_WINDOWS:
        raise RuntimeError("input injection is Windows-only")
    lib = getattr(_user32, "_lib", None)
    if lib is None:
        lib = ctypes.WinDLL("user32", use_last_error=True)
        lib.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        lib.SendInput.restype = wt.UINT
        lib.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.SetCursorPos.restype = wt.BOOL
        lib.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
        lib.GetCursorPos.restype = wt.BOOL
        lib.VkKeyScanW.argtypes = [wt.WCHAR]
        lib.VkKeyScanW.restype = ctypes.c_short
        _user32._lib = lib  # type: ignore[attr-defined]
    return lib


def resolve_key(key: str) -> tuple[str, Optional[int]]:
    """协议里的 KeyboardEvent.key -> ("modifier"|"special"|"char"|"unknown", vk)。

    纯函数，不产生副作用（自检脚本会直接校验它）。
    """
    if not isinstance(key, str) or not key:
        return "unknown", None
    lowered = key.lower()
    if lowered in MODIFIER_VK:
        return "modifier", MODIFIER_VK[lowered]
    if lowered in SPECIAL_VK:
        return "special", SPECIAL_VK[lowered]
    if len(key) == 1:
        return "char", None
    return "unknown", None


class InputInjector:
    """把协议里的 input 事件翻译成 SendInput 调用。

    修饰键状态表 ``_down`` 用于：
      * kd 决定走 Unicode 还是 VK 路径（Ctrl/Alt/Meta 按下时字母要当快捷键）；
      * ku 用同一个 VK 抬起，避免"键卡住"；
      * 断线/退出时 release_all() 统一释放。
    """

    def __init__(self, screen_width: int = 1440, screen_height: int = 900) -> None:
        self.screen_w = int(screen_width)
        self.screen_h = int(screen_height)
        self._down: dict[str, Optional[int]] = {}

    # -- 基础发送 ---------------------------------------------------------
    def _send(self, *inputs: INPUT) -> int:
        user32 = _user32()
        n = len(inputs)
        if not n:
            return 0
        arr = (INPUT * n)(*inputs)
        sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
        if sent != n:
            LOG.warning(
                "SendInput inserted %s/%s events (GetLastError=%s)",
                sent,
                n,
                ctypes.get_last_error(),
            )
        return sent

    def _keystroke_unicode(self, ch: str) -> int:
        code = ord(ch)
        if code > 0xFFFF:
            LOG.debug("skip non-BMP char U+%04X", code)
            return 0
        return self._send(
            _keyboard_input(0, code, KEYEVENTF_UNICODE),
            _keyboard_input(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        )

    def _vk_scan(self, ch: str) -> Optional[int]:
        """用 VkKeyScanW 找到字符对应的 VK（考虑当前键盘布局）。"""
        try:
            scan = _user32().VkKeyScanW(ch)
        except Exception:  # noqa: BLE001
            return None
        if scan == -1:
            return None
        vk = scan & 0xFF
        return vk if vk != 0xFF else None

    # -- 协议事件 ---------------------------------------------------------
    def mouse_move(self, x: float, y: float) -> tuple[int, int]:
        px = int(round(float(x) * self.screen_w))
        py = int(round(float(y) * self.screen_h))
        px = min(max(px, 0), max(self.screen_w - 1, 0))
        py = min(max(py, 0), max(self.screen_h - 1, 0))
        if not _user32().SetCursorPos(px, py):
            LOG.warning("SetCursorPos(%s,%s) failed: %s", px, py, ctypes.get_last_error())
        return px, py

    def mouse_button(self, button: int, down: bool) -> None:
        flags = MOUSE_BUTTONS.get(int(button))
        if flags is None:
            LOG.debug("unknown mouse button %r, ignored", button)
            return
        self._send(_mouse_input(flags[0] if down else flags[1]))

    def wheel(self, dy: float) -> None:
        try:
            value = int(round(float(dy)))
        except (TypeError, ValueError):
            LOG.debug("invalid wheel delta %r, ignored", dy)
            return
        self._send(_mouse_input(MOUSEEVENTF_WHEEL, data=value))

    def key_down(self, key: str) -> None:
        kind, vk = resolve_key(key)
        if kind == "unknown":
            LOG.debug("unknown key down %r, ignored", key)
            return
        if kind in ("modifier", "special"):
            self._send(_keyboard_input(vk, 0, self._vk_flags(vk)))
            self._down[key.lower()] = vk
            return
        # 单字符
        if self._has_command_modifier():
            vk = self._vk_scan(key)
            if vk is None:
                LOG.debug("char %r has no VK mapping, ignored", key)
                return
            self._send(_keyboard_input(vk, 0, self._vk_flags(vk)))
            self._down[key.lower()] = vk
        else:
            # 没有 Ctrl/Alt/Meta 时走 Unicode 注入：直接下发"按下+抬起"一对，
            # 保证一次按键只产生一个字符（IME/软键盘/中文标点都正确）。
            # 随后的 ku 通过 _down[key] is None 被吞掉。
            self._keystroke_unicode(key)
            self._down[key.lower()] = None

    def key_up(self, key: str) -> None:
        lowered = key.lower() if isinstance(key, str) else ""
        vk = self._down.pop(lowered, "missing")
        if vk == "missing":
            # 没记录过按下（比如断线重连后只收到 ku）：不补发，避免误触发。
            return
        if vk is None:  # Unicode 路径已经成对发过
            return
        self._send(_keyboard_input(vk, 0, self._vk_flags(vk) | KEYEVENTF_KEYUP))

    def text(self, text: str) -> None:
        for ch in text or "":
            self._keystroke_unicode(ch)
        # IME 输入框需要一次回车/确认？协议是直接输入文本，逐字符 Unicode 最可靠。

    def release_all(self) -> None:
        """断线 / 退出时释放所有按下的键（避免 Ctrl 卡住）。"""
        pending = list(self._down.items())
        self._down.clear()
        for _key, vk in reversed(pending):
            if vk is None:
                continue
            self._send(_keyboard_input(vk, 0, self._vk_flags(vk) | KEYEVENTF_KEYUP))

    def _vk_flags(self, vk: Optional[int]) -> int:
        return KEYEVENTF_EXTENDEDKEY if (vk is not None and vk in EXTENDED_VKS) else 0

    def _has_command_modifier(self) -> bool:
        command = {VK_CONTROL, VK_MENU, VK_LWIN}
        return any(vk in command for vk in self._down.values() if vk is not None)

    # -- 协议原始事件 -----------------------------------------------------
    def handle_event(self, ev: dict) -> None:
        """ev 就是浏览器发来的 JSON 原文（PROTOCOL.md 第 2 节）。"""
        if not isinstance(ev, dict):
            LOG.debug("non-dict input event ignored: %r", ev)
            return
        kind = ev.get("t")
        try:
            if kind == "m":
                if ev.get("x") is None or ev.get("y") is None:
                    return
                self.mouse_move(float(ev["x"]), float(ev["y"]))
            elif kind == "d":
                self.mouse_button(ev.get("b", 0), True)
            elif kind == "u":
                self.mouse_button(ev.get("b", 0), False)
            elif kind == "w":
                self.wheel(ev.get("dy", 0))
            elif kind == "kd":
                self.key_down(ev.get("k", ""))
            elif kind == "ku":
                self.key_up(ev.get("k", ""))
            elif kind == "txt":
                self.text(ev.get("s", ""))
            else:
                LOG.debug("unknown input event %r, ignored", kind)
        except Exception:  # noqa: BLE001 - 单个事件失败不能拖垮连接
            LOG.exception("inject failed for event %r", kind)

    def cursor_position(self) -> tuple[int, int]:
        if not IS_WINDOWS:
            return (0, 0)
        point = wt.POINT()
        if _user32().GetCursorPos(ctypes.byref(point)):
            return int(point.x), int(point.y)
        return (-1, -1)


# ---------------------------------------------------------------------------
# 配置：命令行 > 环境变量 > agent/agent.env
# ---------------------------------------------------------------------------
class ConfigError(Exception):
    pass


@dataclass
class Config:
    server: str
    token: str
    max_width: int = DEFAULT_MAX_WIDTH
    quality: int = DEFAULT_JPEG_QUALITY
    env_file: str = DEFAULT_ENV_FILE

    def redacted(self) -> dict:
        token = self.token or ""
        shown = (token[:4] + "***") if len(token) > 4 else ("***" if token else "")
        return {
            "server": self.server,
            "token": shown,
            "max_width": self.max_width,
            "quality": self.quality,
            "env_file": self.env_file,
        }


def read_env_file(path: str) -> dict[str, str]:
    """读取 KEY=VALUE 形式的 agent.env（支持 # 注释、export 前缀、引号）。"""
    values: dict[str, str] = {}
    if not path or not os.path.isfile(path):
        return values
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                if key:
                    values[key] = value
    except OSError as exc:
        LOG.warning("cannot read env file %s: %s", path, exc)
    return values


def build_uri(server: str, token: str) -> str:
    """把 token 拼进 query（覆盖已有的 token 参数）。"""
    parts = urlsplit(server)
    if parts.scheme not in ("ws", "wss"):
        raise ConfigError(f"invalid server URL (need ws:// or wss://): {server!r}")
    if not parts.netloc:
        raise ConfigError(f"invalid server URL (no host): {server!r}")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "token"]
    query.append(("token", token))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.py",
        description="PC2MB Windows agent: capture desktop and inject input over WSS.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--server", help="agent WebSocket URL, e.g. ws://127.0.0.1:18091/ws/agent")
    parser.add_argument("--token", help="agent token (query parameter token)")
    parser.add_argument("--env-file", help="path to KEY=VALUE config file")
    parser.add_argument(
        "--max-width",
        type=int,
        help="downscale frames wider than this (0 disables downscaling)",
    )
    parser.add_argument("--quality", type=int, help="JPEG quality (1-100)")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="stdout log level",
    )
    return parser


def resolve_config(
    argv: Optional[list[str]] = None,
    environ: Optional[dict[str, str]] = None,
) -> tuple[Config, argparse.Namespace]:
    """优先级：命令行参数 > 环境变量 > agent.env > 内置默认值。"""
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    env = dict(os.environ if environ is None else environ)

    env_file = args.env_file or env.get("PC2MB_ENV_FILE") or DEFAULT_ENV_FILE
    file_values = read_env_file(env_file)

    def pick(cli_value: Optional[str], env_key: str) -> Optional[str]:
        if cli_value is not None and cli_value != "":
            return cli_value
        if env.get(env_key):
            return env[env_key]
        if file_values.get(env_key):
            return file_values[env_key]
        return None

    server = pick(args.server, "PC2MB_SERVER") or DEFAULT_SERVER
    token = pick(args.token, "PC2MB_TOKEN") or ""
    if not token:
        raise ConfigError(
            "missing token: pass --token, set PC2MB_TOKEN, or put PC2MB_TOKEN=... in agent/agent.env"
        )

    max_width_raw = args.max_width
    if max_width_raw is None:
        raw = env.get("PC2MB_MAX_WIDTH") or file_values.get("PC2MB_MAX_WIDTH")
        max_width_raw = int(raw) if raw else DEFAULT_MAX_WIDTH
    quality_raw = args.quality
    if quality_raw is None:
        raw = env.get("PC2MB_QUALITY") or file_values.get("PC2MB_QUALITY")
        quality_raw = int(raw) if raw else DEFAULT_JPEG_QUALITY

    if not (0 <= max_width_raw <= 16384):
        raise ConfigError(f"--max-width out of range: {max_width_raw}")
    if not (1 <= quality_raw <= 100):
        raise ConfigError(f"--quality out of range: {quality_raw}")

    cfg = Config(
        server=server,
        token=token,
        max_width=int(max_width_raw),
        quality=int(quality_raw),
        env_file=env_file,
    )
    # 提前校验 URL，配置错误直接退出而不是无限重连。
    build_uri(cfg.server, cfg.token)
    return cfg, args


# ---------------------------------------------------------------------------
# Agent 主体
# ---------------------------------------------------------------------------
class TokenError(Exception):
    """服务端以 4401 关闭：token 错误，不再重连。"""


class Agent:
    def __init__(self, cfg: Config, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self.cfg = cfg
        self.loop = loop
        self.capture = ScreenCapture(max_width=cfg.max_width, quality=cfg.quality)
        self.input = InputInjector()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pc2mb-capture")
        self._last_meta: Optional[tuple[int, int]] = None
        self._frames_sent = 0
        self._last_frame: Optional[Frame] = None
        self._stop = False
        self._fail_count = 0  # 连续失败次数，决定退避档位（成功连上后清零）

    # -- 生命周期 ---------------------------------------------------------
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self.loop = loop
        await self._prepare_screen()
        uri = build_uri(self.cfg.server, self.cfg.token)
        LOG.info("agent starting: %s", self._describe(uri))
        while not self._stop:
            try:
                await self._session(uri)
                # 服务端主动关闭（例如被新 agent 顶掉 4409 / 正常下线）：也重连。
                LOG.warning("connection closed by server")
            except TokenError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 任何异常都重连
                LOG.warning("connection failed: %s: %s", type(exc).__name__, exc)
            if self._stop:
                break
            # 指数退避：1,2,4,8,15,15...（_session 成功握手后会清零）
            delay = BACKOFF_SECONDS[min(self._fail_count, len(BACKOFF_SECONDS) - 1)]
            self._fail_count += 1
            LOG.info("reconnecting in %ss (consecutive failures: %d)", delay, self._fail_count)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
        LOG.info("agent stopped")

    async def _prepare_screen(self) -> None:
        """在采集线程里初始化 mss，拿到真实分辨率（线程归属很重要）。"""
        if self.loop is None:
            raise RuntimeError("loop not bound")
        try:
            src = await self.loop.run_in_executor(self._pool, self.capture.source_size)
        except Exception as exc:  # noqa: BLE001 - 无显示器等极端情况
            LOG.error("screen capture init failed: %s: %s", type(exc).__name__, exc)
            raise
        self.input.screen_w, self.input.screen_h = src
        out_w, out_h = self.capture.output_size()
        LOG.info(
            "screen: %dx%d (frame %dx%d, jpeg q=%d, max_width=%d)",
            src[0],
            src[1],
            out_w,
            out_h,
            self.cfg.quality,
            self.cfg.max_width,
        )

    def _describe(self, uri: str) -> str:
        return uri.replace(self.cfg.token, "***") if self.cfg.token else uri

    # -- 单次连接 ---------------------------------------------------------
    async def _session(self, uri: str) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import (
            ConnectionClosed,
            InvalidStatus,
            InvalidURI,
        )

        LOG.info("connecting to %s", self._describe(uri))
        try:
            async with connect(
                uri,
                open_timeout=10,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=None,
                compression=None,  # JPEG 不需要 permessage-deflate，省 CPU
            ) as ws:
                LOG.info("connected")
                await self._send_hello(ws)
                self._fail_count = 0  # 握手成功，退避档位清零
                try:
                    async for raw in ws:
                        if isinstance(raw, (bytes, bytearray)):
                            LOG.warning("unexpected binary message from server, ignored")
                            continue
                        await self._handle_message(ws, raw)
                except TokenError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    from websockets.exceptions import ConnectionClosed

                    if isinstance(exc, ConnectionClosed):
                        # websockets >=15 提供 .code，旧版本只有 .rcvd/.sent
                        code = getattr(exc, "code", None)
                        if code is None:
                            code = getattr(getattr(exc, "rcvd", None), "code", None)
                        if code == 4401:
                            raise TokenError("token rejected (close code 4401)") from exc
                    raise
                # async for 正常结束（服务端 close）
                code = getattr(ws, "close_code", None)
                if code == 4401:
                    raise TokenError("token rejected (close code 4401)")
                LOG.info(
                    "websocket closed by peer: code=%s reason=%r",
                    code,
                    getattr(ws, "close_reason", ""),
                )
        except InvalidURI as exc:
            raise ConfigError(f"invalid server URL: {exc}") from exc
        except InvalidStatus as exc:
            # 服务端没有走 WS close code 而是拒绝 HTTP 握手时的兜底判断。
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403):
                raise TokenError(f"token rejected by HTTP handshake (status {status})") from exc
            if status == 404:
                raise ConfigError(
                    "handshake returned 404: check --server path (should end with /ws/agent)"
                ) from exc
            raise  # 502/503（网关后面服务端挂了）等按可重试处理
        finally:
            # 每次断线都释放按下的键，避免服务端断开时 Ctrl 卡住。
            self.input.release_all()

    async def _send_hello(self, ws: Any) -> None:
        out_w, out_h = self.capture.output_size()
        self._last_meta = (out_w, out_h)
        payload = {
            "t": "hello",
            "screen": {"w": out_w, "h": out_h},
            "host": socket.gethostname(),
        }
        await ws.send(json.dumps(payload, ensure_ascii=False))
        LOG.info("hello sent: %s", payload)

    async def _handle_message(self, ws: Any, raw: Any) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            LOG.warning("invalid JSON from server, ignored: %.120r", raw)
            return
        if not isinstance(msg, dict):
            LOG.warning("non-object JSON from server, ignored: %.120r", raw)
            return
        kind = msg.get("t")
        if kind == "req":
            await self._handle_req(ws)
        elif kind == "input":
            ev = msg.get("ev")
            if isinstance(ev, dict):
                self.input.handle_event(ev)
            else:
                LOG.debug("input message without ev: %.120r", raw)
        elif kind == "viewer":
            LOG.info("viewer watching: %s", msg.get("on"))
        elif kind == "ping":
            await ws.send(json.dumps({"t": "pong"}))
        else:
            LOG.debug("unhandled server message: %.120r", raw)

    async def _handle_req(self, ws: Any) -> None:
        """处理一次抓帧请求。采集/编码失败只记录，不退出。"""
        if self.loop is None:
            return
        try:
            frame = await self.loop.run_in_executor(self._pool, self.capture.grab_jpeg)
        except Exception as exc:  # noqa: BLE001 - 采集失败不退出
            LOG.error("capture failed: %s: %s", type(exc).__name__, exc)
            await self._send_error(ws, "capture", str(exc))
            return
        self._last_frame = frame
        meta = (frame.width, frame.height)
        if meta != self._last_meta:
            self._last_meta = meta
            await ws.send(json.dumps({"t": "meta", "w": meta[0], "h": meta[1]}))
            LOG.info("frame size changed -> meta %dx%d", meta[0], meta[1])
        await ws.send(frame.jpeg)
        self._frames_sent += 1
        if self._frames_sent == 1 or self._frames_sent % 120 == 0:
            LOG.info(
                "frame #%d: %dx%d %d bytes %.0f KB, grab %.1f ms, encode %.1f ms",
                self._frames_sent,
                frame.width,
                frame.height,
                len(frame.jpeg),
                frame.size_kb,
                frame.grab_ms,
                frame.encode_ms,
            )
        else:
            LOG.debug(
                "frame #%d: %dx%d %d bytes, grab %.1f ms, encode %.1f ms",
                self._frames_sent,
                frame.width,
                frame.height,
                len(frame.jpeg),
                frame.grab_ms,
                frame.encode_ms,
            )

    async def _send_error(self, ws: Any, code: str, message: str) -> None:
        """协议没定义 agent->server 的 error；给 server 的日志留个可读提示即可。"""
        payload = {"t": "error", "code": code, "msg": message[:200]}
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - 连接已坏时忽略
            pass

    # -- 关闭 -------------------------------------------------------------
    def shutdown(self) -> None:
        self._stop = True
        try:
            self.input.release_all()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.capture.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def configure_logging(level: str = "INFO") -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    dpi_mode = enable_dpi_awareness()
    LOG.info("PC2MB agent (DPI: %s, python %s)", dpi_mode, sys.version.split()[0])
    try:
        cfg, _args = resolve_config(argv)
    except ConfigError as exc:
        LOG.error("config error: %s", exc)
        return EXIT_CONFIG
    except ValueError as exc:
        LOG.error("config error: %s", exc)
        return EXIT_CONFIG

    LOG.info("config: %s", cfg.redacted())
    agent = Agent(cfg)
    try:
        asyncio.run(agent.run())
    except TokenError as exc:
        LOG.error("fatal: %s - check PC2MB_TOKEN", exc)
        return EXIT_CONFIG
    except ConfigError as exc:
        LOG.error("fatal config error: %s", exc)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        LOG.info("interrupted (Ctrl+C), exiting")
    except Exception:  # noqa: BLE001
        LOG.exception("fatal error")
        return EXIT_ERROR
    finally:
        agent.shutdown()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
