# PC2MB 通信协议 v1

三个角色：**浏览器 Viewer** / **服务器 Server** / **桌面 Agent**。

```
浏览器 ──HTTPS──> Caddy(wkeeli.cn) ──HTTP──> Server(FastAPI, dev 机 127.0.0.1:18091)
桌面 Agent ──WSS(出站)──> Caddy ──> Server
```

- 公网入口：`https://wkeeli.cn/pc2mb/`（Caddy `handle_path /pc2mb/*` 会去掉 `/pc2mb` 前缀，服务端看到的路径是 `/`、`/api/...`、`/ws/...`）。
- 前端必须使用**相对路径**（`./style.css`、`./app.js`），WebSocket 用
  `(location.protocol === 'https:' ? 'wss' : 'ws') + '://' + location.host + base + 'ws/viewer'`，
  其中 `base` 是当前页面目录（`location.pathname` 去掉末尾 `index.html`/`/` 前的最后一段）。
- 二进制帧一律是 **JPEG 原始字节**（WebSocket binary）；控制消息一律是 **JSON 文本帧**（UTF-8）。

## 1. HTTP 接口

### POST /api/login
请求：`{"u": "admin", "p": "明文密码"}`
成功：`200 {"ok": true}`，并 `Set-Cookie: pc2mb_session=<签名>; Path=/; HttpOnly; SameSite=Lax[; Secure]`
失败：`401 {"ok": false, "error": "invalid"}`；被限流：`429 {"ok": false, "error": "locked"}`

### POST /api/logout
`200 {"ok": true}` 并清 cookie。

### GET /api/state（需登录）
`200 {"agent": true|false, "screen": {"w":1440,"h":900} | null, "viewer": true|false}`
未登录：`401 {"ok": false, "error": "auth"}`

### 静态资源（无需登录）
`GET /` → `web/index.html`（登录页或控制台，由前端自己判断，服务端统一返回同一个页面）
`GET /style.css`、`GET /app.js` 等 → `web/` 目录下的文件。

## 2. WebSocket /ws/viewer（需登录 cookie）

服务端在握手时校验 `pc2mb_session`，失败则以 close code **4401** 关闭。

### Server → Viewer

```jsonc
{"t":"state","agent":true,"screen":{"w":1440,"h":900}}   // 连接时、agent 上下线时推送
{"t":"meta","w":1280,"h":800}                            // 帧像素尺寸（画面分辨率变化时推送）
{"t":"pong"}                                             // 对 ping 的回应
{"t":"error","code":"replaced","msg":"其他设备已接管"}     // 然后关闭；code 还可能是 "agent_offline"
```
以及 **binary JPEG 帧**（只在收到 viewer 的 `req` 后由 server 转发，一帧一次）。

### Viewer → Server

```jsonc
{"t":"req"}                       // 请求下一帧：上一帧画完后立刻发；server 只在无未完成请求时转发给 agent
{"t":"ping"}                      // 心跳，server 回 {"t":"pong"}
{"t":"m","x":0.51,"y":0.33}       // 鼠标移动到归一化坐标（0..1，相对主显示器）
{"t":"d","b":0}                   // 鼠标按下：b=0 左 / 1 中 / 2 右
{"t":"u","b":0}                   // 鼠标抬起
{"t":"w","dy":120}                // 滚轮，dy>0 向下滚（一格 120）
{"t":"kd","k":"a"}                // 键盘按下，k 取 KeyboardEvent.key（"a" / "Enter" / "Control" / "ArrowUp" ...）
{"t":"ku","k":"a"}                // 键盘抬起
{"t":"txt","s":"你好"}            // 直接输入一段文本（手机软键盘/IME 用），走 unicode 注入
```

规则：
- 触摸：单击 = `m` + `d` + `u`（左键）；拖动 = `d` 后连续 `m`，结束 `u`；长按 = 右键 `d(b=2)/u`；
  双指上下滑 = `w`。前端必须阻止默认滚动/缩放。
- 桌面键盘：页面获得焦点时转发 keydown/keyup；`k` 用 `e.key`，单字符统一小写。
- 帧流控：viewer 画完一帧才发下一个 `req`（自适应帧率）；server 5 秒收不到帧则丢弃该次请求。

## 3. WebSocket /ws/agent?token=XX（需 agent token）

握手 query 必须带 `token`，与服务端 `PC2MB_AGENT_TOKEN` 常量时间比较，失败 close **4401**。
新 agent 连接会顶掉旧 agent（旧连接 close 4409）。

被顶替的旧 viewer 会先收到 `{"t":"error","code":"replaced","msg":"其他设备已接管"}` 再被 close **4410**；
前端不依赖 4410，只按 `error.replaced` 停止重连。

### Agent → Server

```jsonc
{"t":"hello","screen":{"w":1440,"h":900},"host":"DESKTOP-XXX"}  // 连接后立刻发
{"t":"meta","w":1280,"h":800}                                     // 帧分辨率变化时发（可选）
```
以及 **binary JPEG 帧**：每收到一次 `req` 回一帧，不主动推流。

### Server → Agent

```jsonc
{"t":"req"}                                    // 请求捕获并回一帧
{"t":"input","ev":{ ... }}                     // 浏览器输入事件，ev 就是第 2 节的 JSON 原文
{"t":"viewer","on":true}                       // 是否有浏览器在看（可选，仅提示）
```

## 4. 认证与安全

- 账号密码只在 HTTPS 下提交；密码服务端存 PBKDF2-HMAC-SHA256 哈希（`pbkdf2_sha256$迭代$salt_hex$hash_hex`）。
- 会话 cookie：`exp_ts.hmac_sha256(secret, exp_ts)`，默认 7 天。
- 登录失败限流：同一 IP 连续 5 次失败锁定 60 秒（内存即可）。
- Agent token、cookie secret 只放 `.env`，**绝不提交仓库**。
- 服务端默认只监听 `127.0.0.1`（dev 机由 Caddy 反代），本机测试可监听 127.0.0.1。

## 5. 阶段目标（最小可用）

1. 登录 → 看到实时画面（JPEG 自适应帧率）
2. 鼠标移动/点击/滚轮、键盘输入、手机触摸 + 软键盘输入
3. 明/灰/暗三主题切换并记忆
4. Agent 断线重连（指数退避）、画面暂停/恢复、全屏
