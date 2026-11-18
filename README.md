# pc2mb

**浏览器 / 手机远程控制本机 Windows 桌面。** 只需要一台能被浏览器访问的服务器；被控的 Windows 电脑装一个 Agent 主动出站连接，无需公网 IP、无需端口映射。

- 前端：原生 HTML / CSS / JS，无第三方库、无 CDN、极简风格，明 / 灰 / 暗三主题
- 后端：Python 3.12 + FastAPI + uvicorn，单文件服务端
- 被控端：Python 3.12 + mss + websockets，纯 ctypes 注入输入，无重量级依赖
- 传输：WebSocket（画面走 JPEG binary，输入走 JSON），自适应帧率（画完一帧才要下一帧）
- 鉴权：账号密码登录（PBKDF2 哈希 + HMAC 签名 Cookie）、登录失败限流、Agent Token

## 架构

```
 手机 / 电脑浏览器                      云端服务器                     Windows 被控机
 ┌───────────────┐    HTTPS/WSS     ┌──────────────────┐   WSS 出站   ┌────────────────┐
 │ web/ 前端      │ ───────────────> │ Caddy (TLS 反代)  │ <─────────── │ agent/agent.py │
 │ canvas 渲染画面 │                 │   ↓              │             │ mss 抓屏        │
 │ 鼠标/键盘/触屏  │ <─────────────── │ FastAPI (中继)    │             │ SendInput 注入  │
 └───────────────┘                  └──────────────────┘             └────────────────┘
```

浏览器和 Agent 都只和服务器通信；Agent 主动出站建连，所以被控机在 NAT/内网里也能用。

## 目录结构

```
server/app.py        服务端：登录鉴权 + WebSocket 中继 + 静态托管（单文件）
web/index.html       前端：登录页 + 控制台（单页，原生 JS 切换视图）
web/style.css        前端：明/灰/暗三主题、响应式布局
web/app.js           前端：canvas 渲染、帧流控、鼠标/键盘/触摸/IME
agent/agent.py       Windows Agent：抓屏 JPEG + SendInput 输入注入 + 断线重连
agent/selftest.py    Agent 离线自检（截图 + 光标移动校验）
deploy/              部署脚本与 systemd / Caddy / 隧道配置
docs/PROTOCOL.md     浏览器 / 服务端 / Agent 三方通信协议（v1）
docs/DEPLOY.md       生产部署步骤（含反向代理与隧道）
tests/               服务端集成测试 + 浏览器端到端冒烟测试
```

## 本地快速开始

环境：Python 3.12（Windows 被控端需要 Windows；服务端跨平台）。

```bash
# 1. 生成配置（项目根目录 .env）
python server/app.py --gen-secrets > .env      # 会打印随机初始密码
# 或者指定账号密码：
python server/app.py --hash-password '你的密码'  # 把输出贴进 .env 的 PC2MB_PASSWORD_HASH

# 2. 安装依赖并启动服务端
pip install -r server/requirements.txt
python -m uvicorn server.app:app --host 127.0.0.1 --port 18091
# 打开 http://127.0.0.1:18091/

# 3. 在被控 Windows 机器上安装依赖并启动 Agent
pip install -r agent/requirements.txt
python agent/agent.py --server ws://127.0.0.1:18091/ws/agent --token <PC2MB_AGENT_TOKEN>
```

> Windows PowerShell 下请不要用 `>` 重定向（会写成 UTF-16）：
> `python server/app.py --gen-secrets | Set-Content .env -Encoding utf8`
> （服务端已兼容 BOM/UTF-16，但统一 UTF-8 更省心。）

Agent 配置优先级：命令行参数 > 环境变量 > `agent/agent.env`：

```ini
PC2MB_SERVER=wss://example.com/pc2mb/ws/agent
PC2MB_TOKEN=与服务器 .env 中 PC2MB_AGENT_TOKEN 相同
```

## 功能

- 登录后实时查看被控桌面（JPEG 自适应帧率，实测局域网约 10~15 fps）
- 鼠标移动 / 左中右键 / 滚轮；键盘输入（含 Ctrl/Alt 组合键）
- 手机：单指移动、单击、拖动、长按右键、双指上下滚动、软键盘中文输入（IME）、特殊键条
- 明 / 灰 / 暗三主题，记忆在 localStorage
- 断线自动重连、Agent 离线提示、全屏、分辨率显示、FPS 显示
- 单浏览器会话接管：新设备登录会顶掉旧画面，防止两台设备抢控制权

## 安全说明

- 密码只在 HTTPS 下提交；服务端只存 PBKDF2-HMAC-SHA256 哈希
- 会话 Cookie 为 HMAC-SHA256 签名、HttpOnly、SameSite=Lax、默认 7 天
- 登录失败 5 次锁定 60 秒（按 IP，内存级）
- Agent 使用独立长随机 Token（`?token=`），不通过登录密码
- 服务端默认只监听 `127.0.0.1`，由反向代理对外提供 HTTPS
- `.env`、`agent/agent.env` 已被 `.gitignore` 忽略，**不要提交密钥**

## 手机使用

1. 手机浏览器打开部署地址（例如 `https://wkeeli.cn/pc2mb/`），输入账号密码
2. 单指点按 = 鼠标左键单击；拖动 = 按住拖动；长按 0.5 秒 = 右键
3. 双指上下滑动 = 滚轮；工具栏键盘按钮 = 展开特殊键条 + 中文输入
4. 建议加到主屏幕，全屏后体验更好

## 已知限制

- 只控制主显示器；仅 Windows 被控端；Agent 必须在已登录的交互式会话里运行
- 约 10~15 fps（JPEG 全帧，无增量/硬件编码），可调低 `--max-width` / `--quality` 提速
- UAC 安全桌面、锁屏界面、部分管理员权限窗口无法注入输入（UIPI）
- 无剪贴板、文件传输、音频、多屏切换（最小功能原则）
- 手机端 iOS Safari 的软键盘弹起会有视口滚动，暂无专门适配

## 部署

见 [docs/DEPLOY.md](docs/DEPLOY.md)。通信协议见 [docs/PROTOCOL.md](docs/PROTOCOL.md)。

## License

MIT © 2026 wzqfwanj
