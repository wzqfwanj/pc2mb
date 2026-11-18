#!/usr/bin/env python3
"""pc2mb server — 极简云远控中继服务。

职责：
  1) 账号密码登录（PBKDF2 哈希 + HMAC 签名 cookie + 失败限流）
  2) 浏览器 (viewer) 与 Windows Agent 之间的 WebSocket 中继
     - 画面：viewer 发 req → agent 回一帧 JPEG → 转给 viewer（自适应帧率）
     - 输入：viewer 的鼠标/键盘事件原样转发给 agent
  3) 托管 web/ 目录的静态前端

协议见 docs/PROTOCOL.md。默认只监听 127.0.0.1，由 Caddy 反代 /pc2mb/*。

用法：
  python server/app.py --hash-password '你的密码'   # 生成密码哈希
  python server/app.py --gen-secrets                # 生成 .env 配置块
  python server/app.py                              # 启动服务（读取环境变量 / .env）
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
ENV_FILE = ROOT / ".env"

APP_NAME = "pc2mb"
VERSION = "1.0.0"
SESSION_COOKIE = "pc2mb_session"
SESSION_TTL = 7 * 24 * 3600
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 60
FRAME_TIMEOUT = 5.0
MAX_TEXT_LEN = 2000

log = logging.getLogger(APP_NAME)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_env_file(path: Path = ENV_FILE) -> None:
    """极简 .env 解析（KEY=VALUE，# 注释），不覆盖已有环境变量。"""
    if not path.exists():
        return
    # utf-8-sig：兼容 Windows 记事本/PowerShell 写入的 BOM
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    user: str
    password_hash: str
    agent_token: str
    cookie_secret: str
    host: str
    port: int


_cfg: Optional[Config] = None


def config() -> Config:
    global _cfg
    if _cfg is None:
        load_env_file()
        missing = [
            name
            for name in ("PC2MB_USER", "PC2MB_PASSWORD_HASH", "PC2MB_AGENT_TOKEN", "PC2MB_COOKIE_SECRET")
            if not os.environ.get(name)
        ]
        if missing:
            raise SystemExit(
                "缺少配置: " + ", ".join(missing) + "\n"
                "先运行 `python server/app.py --gen-secrets` 生成 .env 示例。"
            )
        _cfg = Config(
            user=os.environ["PC2MB_USER"],
            password_hash=os.environ["PC2MB_PASSWORD_HASH"],
            agent_token=os.environ["PC2MB_AGENT_TOKEN"],
            cookie_secret=os.environ["PC2MB_COOKIE_SECRET"],
            host=os.environ.get("PC2MB_HOST", "127.0.0.1"),
            port=int(os.environ.get("PC2MB_PORT", "18091")),
        )
    return _cfg


# --------------------------------------------------------------------------- #
# 密码 / 会话
# --------------------------------------------------------------------------- #
def hash_password(password: str, iterations: int = 200_000) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, expected = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except Exception:
        return False


def _sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session(secret: str, ttl: int = SESSION_TTL) -> str:
    expires = int(time.time()) + ttl
    message = f"v1.{expires}"
    return f"{message}.{_sign(secret, message)}"


def verify_session(secret: str, token: Optional[str]) -> bool:
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return False
    expires = parts[1]
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(_sign(secret, f"v1.{expires}"), parts[2])


class LoginLimiter:
    """内存级登录失败限流，够用且无依赖。"""

    def __init__(self, max_fails: int = LOGIN_MAX_FAILS, lock_seconds: int = LOGIN_LOCK_SECONDS) -> None:
        self.max_fails = max_fails
        self.lock_seconds = lock_seconds
        self._state: dict[str, tuple[int, float]] = {}

    def locked(self, ip: str) -> bool:
        fails, until = self._state.get(ip, (0, 0.0))
        if until and until > time.time():
            return True
        if until and until <= time.time():
            self._state.pop(ip, None)
        return False

    def fail(self, ip: str) -> None:
        fails, _ = self._state.get(ip, (0, 0.0))
        fails += 1
        until = time.time() + self.lock_seconds if fails >= self.max_fails else 0.0
        self._state[ip] = (fails, until)
        if len(self._state) > 2048:  # 防止内存膨胀
            self._state.clear()

    def ok(self, ip: str) -> None:
        self._state.pop(ip, None)


# --------------------------------------------------------------------------- #
# 中继核心
# --------------------------------------------------------------------------- #
class Hub:
    """单 agent + 单 viewer 的极简中继。"""

    def __init__(self) -> None:
        self.agent: Optional[WebSocket] = None
        self.agent_host: str = ""
        self.screen: Optional[dict[str, int]] = None  # 逻辑屏幕尺寸
        self.frame: Optional[dict[str, int]] = None  # 帧像素尺寸
        self.viewer: Optional[WebSocket] = None
        self.pending_req = False
        self.req_seq = 0

    # -- 状态 ------------------------------------------------------------- #
    def state_payload(self) -> dict[str, Any]:
        return {
            "t": "state",
            "agent": self.agent is not None,
            "screen": self.screen,
            "host": self.agent_host,
            "viewer": self.viewer is not None,
        }

    async def notify_state(self) -> None:
        viewer = self.viewer
        if viewer is None:
            return
        try:
            await viewer.send_json(self.state_payload())
        except Exception:
            pass

    async def notify_agent_viewer(self) -> None:
        agent = self.agent
        if agent is None:
            return
        try:
            await agent.send_json({"t": "viewer", "on": self.viewer is not None})
        except Exception:
            pass

    # -- 帧流控 ----------------------------------------------------------- #
    async def request_frame(self, viewer: WebSocket) -> None:
        agent = self.agent
        if agent is None or self.viewer is not viewer or self.pending_req:
            return
        self.pending_req = True
        self.req_seq += 1
        seq = self.req_seq
        try:
            await agent.send_json({"t": "req"})
        except Exception:
            self.pending_req = False
            return
        asyncio.create_task(self._frame_timeout(seq))

    async def _frame_timeout(self, seq: int) -> None:
        await asyncio.sleep(FRAME_TIMEOUT)
        if self.req_seq == seq and self.pending_req:
            self.pending_req = False

    async def relay_frame(self, data: bytes) -> None:
        self.pending_req = False
        viewer = self.viewer
        if viewer is None or not data:
            return
        try:
            await viewer.send_bytes(data)
        except Exception:
            pass

    async def relay_input(self, event: dict[str, Any]) -> None:
        agent = self.agent
        if agent is None:
            return
        try:
            await agent.send_json({"t": "input", "ev": event})
        except Exception:
            pass


hub = Hub()
limiter = LoginLimiter()


# --------------------------------------------------------------------------- #
# HTTP 应用
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动即校验配置，避免运行中才发现 .env 缺失
    config()
    log.info("pc2mb server ready")
    yield


app = FastAPI(
    title=APP_NAME,
    version=VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def is_https(request: Request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").split(",")[0].strip()
    return proto == "https"


def unauthorized() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "auth"}, status_code=401)


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    return {"ok": True, "app": APP_NAME, "version": VERSION, "agent": hub.agent is not None}


@app.post("/api/login")
async def api_login(request: Request) -> JSONResponse:
    cfg = config()
    ip = client_ip(request)
    if limiter.locked(ip):
        return JSONResponse({"ok": False, "error": "locked"}, status_code=429)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)

    username = str(data.get("u") or "")[:128]
    password = str(data.get("p") or "")[:256]
    user_ok = secrets.compare_digest(username.encode("utf-8"), cfg.user.encode("utf-8"))
    pass_ok = verify_password(password, cfg.password_hash)
    if user_ok and pass_ok:
        limiter.ok(ip)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            SESSION_COOKIE,
            make_session(cfg.cookie_secret),
            max_age=SESSION_TTL,
            httponly=True,
            samesite="lax",
            secure=is_https(request),
            path="/",
        )
        log.info("login ok ip=%s", ip)
        return response

    limiter.fail(ip)
    log.warning("login failed ip=%s user=%r", ip, username)
    return JSONResponse({"ok": False, "error": "invalid"}, status_code=401)


@app.post("/api/logout")
async def api_logout(request: Request) -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/state")
async def api_state(request: Request) -> JSONResponse:
    cfg = config()
    if not verify_session(cfg.cookie_secret, request.cookies.get(SESSION_COOKIE)):
        return unauthorized()
    return JSONResponse({"ok": True, **hub.state_payload()})


# --------------------------------------------------------------------------- #
# WebSocket: viewer
# --------------------------------------------------------------------------- #
async def _recv_ws(ws: WebSocket) -> Optional[dict[str, Any]]:
    """读取一条原始 WebSocket 消息；连接断开时返回 None。"""
    try:
        message = await ws.receive()
    except (WebSocketDisconnect, RuntimeError):
        return None
    if message.get("type") == "websocket.disconnect":
        return None
    return message


VALID_INPUT = {"m", "d", "u", "w", "kd", "ku", "txt"}
MAX_EVENT_BYTES = 8192


async def _handle_viewer_message(viewer: WebSocket, text: str) -> None:
    if len(text) > MAX_EVENT_BYTES:
        return
    try:
        msg = json.loads(text)
    except Exception:
        return
    if not isinstance(msg, dict):
        return

    kind = msg.get("t")
    if kind == "req":
        await hub.request_frame(viewer)
    elif kind == "ping":
        try:
            await viewer.send_json({"t": "pong"})
        except Exception:
            pass
    elif kind in VALID_INPUT:
        if kind == "txt":
            msg["s"] = str(msg.get("s") or "")[:MAX_TEXT_LEN]
        await hub.relay_input(msg)
    # 其它消息忽略


async def _retire(ws: WebSocket, message: Optional[dict[str, Any]], code: int, reason: str) -> None:
    """尽力关闭被顶替的旧连接：最多等 2 秒，绝不阻塞新连接握手。"""
    try:
        if message is not None:
            await asyncio.wait_for(ws.send_json(message), 2.0)
    except Exception:
        pass
    try:
        await asyncio.wait_for(ws.close(code=code, reason=reason), 2.0)
    except Exception:
        pass


@app.websocket("/ws/viewer")
async def ws_viewer(viewer: WebSocket) -> None:
    cfg = config()
    if not verify_session(cfg.cookie_secret, viewer.cookies.get(SESSION_COOKIE)):
        # 先 accept 才能带自定义 close code（协议要求 4401）
        await viewer.accept()
        await viewer.close(code=4401, reason="unauthorized")
        return

    await viewer.accept()

    previous = hub.viewer
    # 先接管，再后台清理旧连接：避免旧 ws 卡住时新 viewer 迟迟收不到 state
    hub.viewer = viewer
    hub.pending_req = False
    if previous is not None and previous is not viewer:
        asyncio.create_task(
            _retire(previous, {"t": "error", "code": "replaced", "msg": "其他设备已接管"}, 4410, "replaced")
        )
    log.info("viewer connected")

    try:
        await viewer.send_json(hub.state_payload())
        if hub.frame:
            await viewer.send_json({"t": "meta", **hub.frame})
        await hub.notify_agent_viewer()
        while True:
            message = await _recv_ws(viewer)
            if message is None:
                break
            if message.get("text") is not None:
                await _handle_viewer_message(viewer, message["text"])
    finally:
        if hub.viewer is viewer:
            hub.viewer = None
            hub.pending_req = False
            hub.req_seq += 1
        await hub.notify_agent_viewer()
        log.info("viewer disconnected")


# --------------------------------------------------------------------------- #
# WebSocket: agent
# --------------------------------------------------------------------------- #
def _screen_from(value: Any) -> Optional[dict[str, int]]:
    if not isinstance(value, dict):
        return None
    try:
        width, height = int(value.get("w", 0)), int(value.get("h", 0))
    except Exception:
        return None
    if width <= 0 or height <= 0 or width > 20000 or height > 20000:
        return None
    return {"w": width, "h": height}


async def _handle_agent_message(agent: WebSocket, text: str) -> None:
    try:
        msg = json.loads(text)
    except Exception:
        return
    if not isinstance(msg, dict):
        return
    kind = msg.get("t")
    if kind == "hello":
        screen = _screen_from(msg.get("screen"))
        if screen:
            hub.screen = screen
        host = str(msg.get("host") or "")[:64]
        if host:
            hub.agent_host = host
        await hub.notify_state()
        log.info("agent hello host=%s screen=%s", hub.agent_host, hub.screen)
    elif kind == "meta":
        size = _screen_from({"w": msg.get("w"), "h": msg.get("h")})
        if size:
            hub.frame = size
            viewer = hub.viewer
            if viewer is not None:
                try:
                    await viewer.send_json({"t": "meta", **size})
                except Exception:
                    pass
    elif kind == "pong":
        pass


@app.websocket("/ws/agent")
async def ws_agent(agent: WebSocket) -> None:
    cfg = config()
    token = agent.query_params.get("token") or ""
    if not hmac.compare_digest(token, cfg.agent_token):
        await agent.accept()
        await agent.close(code=4401, reason="bad token")
        log.warning("agent rejected: bad token")
        return

    await agent.accept()
    previous = hub.agent
    hub.agent = agent
    hub.screen = None
    hub.frame = None
    hub.pending_req = False
    if previous is not None and previous is not agent:
        asyncio.create_task(_retire(previous, None, 4409, "replaced"))
    await hub.notify_state()
    log.info("agent connected")

    try:
        while True:
            message = await _recv_ws(agent)
            if message is None:
                break
            if message.get("text") is not None:
                await _handle_agent_message(agent, message["text"])
            else:
                await hub.relay_frame(message.get("bytes") or b"")
    finally:
        if hub.agent is agent:
            hub.agent = None
            hub.screen = None
            hub.frame = None
            hub.pending_req = False
        await hub.notify_state()
        log.info("agent disconnected")


# --------------------------------------------------------------------------- #
# 静态前端（必须最后挂载，避免抢占 /api 与 /ws）
# --------------------------------------------------------------------------- #
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_secrets() -> None:
    username = os.environ.get("PC2MB_USER", "admin")
    password = os.environ.get("PC2MB_PASSWORD", "")
    if not password:
        password = secrets.token_urlsafe(9)
        print(f"# 随机初始密码: {password}", file=sys.stderr)
    print("# 把下面内容保存为项目根目录 .env（不要提交仓库）")
    print(f"PC2MB_USER={username}")
    print(f"PC2MB_PASSWORD_HASH={hash_password(password)}")
    print(f"PC2MB_AGENT_TOKEN={secrets.token_urlsafe(32)}")
    print(f"PC2MB_COOKIE_SECRET={secrets.token_urlsafe(32)}")
    print("PC2MB_HOST=127.0.0.1")
    print("PC2MB_PORT=18091")


def main() -> None:
    args = sys.argv[1:]
    if "--hash-password" in args:
        index = args.index("--hash-password")
        password = args[index + 1] if len(args) > index + 1 else ""
        if not password:
            print("用法: python server/app.py --hash-password '密码'", file=sys.stderr)
            raise SystemExit(2)
        print(hash_password(password))
        return
    if "--gen-secrets" in args:
        _print_secrets()
        return

    import uvicorn

    cfg = config()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("pc2mb server starting on http://%s:%d (web=%s)", cfg.host, cfg.port, WEB_DIR)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
