#!/usr/bin/env python3
"""pc2mb 本地集成测试：不需要真实 Agent/浏览器，验证服务端中继链路。

用法（先启动服务端）：
  python tests/e2e_local.py                            # 默认 http://127.0.0.1:18091
  python tests/e2e_local.py https://wkeeli.cn/pc2mb/   # 线上验证

环境变量：PC2MB_TEST_USER / PC2MB_TEST_PASSWORD / PC2MB_TEST_TOKEN
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time

import requests
import websockets
from PIL import Image

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18091").rstrip("/")
WS_BASE = BASE.replace("https://", "wss://").replace("http://", "ws://")
USER = os.environ.get("PC2MB_TEST_USER", "admin")
PASSWORD = os.environ.get("PC2MB_TEST_PASSWORD", "test1234")
TOKEN = os.environ.get("PC2MB_TEST_TOKEN", "localdevtoken123")

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  <- ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(name)


def make_jpeg(width: int = 320, height: int = 200) -> bytes:
    image = Image.new("RGB", (width, height), (30, 90, 160))
    for x in range(0, width, 20):
        for y in range(0, height, 20):
            if (x // 20 + y // 20) % 2 == 0:
                image.paste((240, 240, 240), (x, y, x + 20, y + 20))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=70)
    return buffer.getvalue()


async def recv_until(ws, predicate, timeout: float = 5.0, name: str = ""):
    """跳过无关消息，直到 predicate(msg) 为真；超时抛 TimeoutError。"""
    deadline = time.time() + timeout
    seen: list[str] = []
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError(f"等待 {name or predicate} 超时，已收到: {seen[-6:]}")
        try:
            msg = await asyncio.wait_for(ws.recv(), remaining)
        except asyncio.TimeoutError:
            raise TimeoutError(f"等待 {name or predicate} 超时，已收到: {seen[-6:]}")
        if predicate(msg):
            return msg
        seen.append(msg[:80] if isinstance(msg, bytes) else msg)


def http_checks() -> str:
    session = requests.Session()

    health = session.get(f"{BASE}/api/health", timeout=10)
    check("GET /api/health", health.status_code == 200 and health.json().get("ok") is True)

    state = session.get(f"{BASE}/api/state", timeout=10)
    check("GET /api/state 未登录=401", state.status_code == 401)

    bad = session.post(f"{BASE}/api/login", json={"u": USER, "p": "wrong-password"}, timeout=10)
    check("POST /api/login 错误密码=401", bad.status_code == 401)

    good = session.post(f"{BASE}/api/login", json={"u": USER, "p": PASSWORD}, timeout=10)
    check("POST /api/login 正确密码=200", good.status_code == 200 and good.json().get("ok") is True)
    cookie = session.cookies.get("pc2mb_session")
    check("登录后下发 pc2mb_session cookie", bool(cookie))

    state2 = session.get(f"{BASE}/api/state", timeout=10)
    check("GET /api/state 登录后=200", state2.status_code == 200 and state2.json().get("ok") is True)
    return cookie or ""


async def ws_checks(cookie: str) -> None:
    # 1) 无 cookie 的 viewer 应被 4401 拒绝
    try:
        async with websockets.connect(f"{WS_BASE}/ws/viewer") as viewer:
            await viewer.recv()
        check("viewer 无 cookie 被拒", False)
    except websockets.exceptions.ConnectionClosed as exc:
        check("viewer 无 cookie 被拒(4401)", exc.code == 4401, f"code={exc.code}")

    # 2) 错误 agent token 应被 4401 拒绝
    try:
        async with websockets.connect(f"{WS_BASE}/ws/agent?token=wrong") as agent:
            await agent.recv()
        check("agent 错误 token 被拒", False)
    except websockets.exceptions.ConnectionClosed as exc:
        check("agent 错误 token 被拒(4401)", exc.code == 4401, f"code={exc.code}")

    # 3) 正常链路：agent 上线 -> viewer 要帧 -> 收到 JPEG；输入事件转发给 agent
    headers = {"Cookie": f"pc2mb_session={cookie}"}
    jpeg = make_jpeg()
    async with websockets.connect(
        f"{WS_BASE}/ws/agent?token={TOKEN}", max_size=None
    ) as agent, websockets.connect(f"{WS_BASE}/ws/viewer", additional_headers=headers, max_size=None) as viewer:
        await agent.send(json.dumps({"t": "hello", "screen": {"w": 1440, "h": 900}, "host": "TEST-PC"}))

        def is_ready_state(msg) -> bool:
            if not isinstance(msg, str):
                return False
            try:
                data = json.loads(msg)
            except Exception:
                return False
            return data.get("t") == "state" and data.get("agent") is True and data.get("screen") == {"w": 1440, "h": 900}

        state = json.loads(await recv_until(viewer, is_ready_state, name="state(1440x900)"))
        check("viewer 收到 state(agent在线,1440x900)", state.get("host") == "TEST-PC", json.dumps(state)[:200])

        await viewer.send(json.dumps({"t": "req"}))
        request = json.loads(await recv_until(agent, lambda m: isinstance(m, str) and json.loads(m).get("t") == "req"))
        check("agent 收到 req", request.get("t") == "req")

        await agent.send(jpeg)
        frame = await recv_until(viewer, lambda m: isinstance(m, bytes))
        check("viewer 收到 binary JPEG 帧", frame[:2] == b"\xff\xd8" and len(frame) > 500, f"len={len(frame)}")

        await viewer.send(json.dumps({"t": "m", "x": 0.25, "y": 0.75}))

        def is_mouse(msg) -> bool:
            if not isinstance(msg, str):
                return False
            data = json.loads(msg)
            return data.get("t") == "input" and data.get("ev", {}).get("t") == "m"

        forwarded = json.loads(await recv_until(agent, is_mouse))
        check(
            "鼠标事件转发到 agent",
            forwarded["ev"].get("x") == 0.25 and forwarded["ev"].get("y") == 0.75,
            json.dumps(forwarded)[:200],
        )

        await viewer.send(json.dumps({"t": "kd", "k": "Enter"}))

        def is_key(msg) -> bool:
            if not isinstance(msg, str):
                return False
            data = json.loads(msg)
            return data.get("t") == "input" and data.get("ev", {}).get("k") == "Enter"

        key_event = json.loads(await recv_until(agent, is_key))
        check("键盘事件转发到 agent", key_event["ev"].get("t") == "kd")

        await viewer.send(json.dumps({"t": "ping"}))
        pong = json.loads(
            await recv_until(viewer, lambda m: isinstance(m, str) and json.loads(m).get("t") == "pong")
        )
        check("ping -> pong", pong.get("t") == "pong")

        await agent.send(json.dumps({"t": "meta", "w": 1280, "h": 800}))
        meta = json.loads(
            await recv_until(
                viewer,
                lambda m: isinstance(m, str) and json.loads(m).get("t") == "meta" and json.loads(m).get("w") == 1280,
            )
        )
        check("meta 帧尺寸广播", meta.get("w") == 1280 and meta.get("h") == 800)


def main() -> int:
    print(f"== pc2mb 集成测试 {BASE} ==")
    cookie = http_checks()
    if cookie:
        asyncio.run(ws_checks(cookie))
    print()
    if failures:
        print(f"{len(failures)} 项失败: {failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
