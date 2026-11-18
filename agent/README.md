# PC2MB Windows Agent（被控端）

浏览器 / 手机通过 `https://wkeeli.cn/pc2mb/` 远程控制本机 Windows 桌面的**被控端**。
本目录只负责两件事：

1. **采集**：mss 抓主显示器 → GDI+ 编码 JPEG(q=60) → 收到 `{"t":"req"}` 就回一帧 binary；
2. **注入**：收到 `{"t":"input","ev":{...}}` 用 `ctypes` + `user32.SendInput` 还原成真实的鼠标 / 键盘事件。

接口契约见 [`../docs/PROTOCOL.md`](../docs/PROTOCOL.md)（协议 v1），本实现与其对齐。
依赖只有 `mss` + `websockets`；JPEG 编码走 Windows 自带 GDI+（`gdiplus.dll`），**不需要 Pillow**。

```
浏览器 ──HTTPS──> Caddy(wkeeli.cn) ──> Server(FastAPI :18091)
桌面 Agent ──WSS(出站)──────────────> Caddy/Server
```

---

## 1. 文件说明

| 文件 | 作用 |
| --- | --- |
| `agent.py` | 主程序，单文件可运行（采集 + 注入 + WSS 客户端 + 重连 + 配置） |
| `selftest.py` | 离线自检：截一帧存 `_selftest.jpg`、解码校验、安全鼠标/键盘注入、配置优先级 |
| `run.bat` | Windows 本机运行入口（自动找 Python、缺依赖时自动 pip install） |
| `requirements.txt` | 依赖：`mss>=9.0`、`websockets>=14.0` |
| `agent.env` | 本机配置（已被 `.gitignore` 忽略，不要提交）：`PC2MB_SERVER` / `PC2MB_TOKEN` |
| `_selftest.jpg` | **自检产物**（可随时删除），`python selftest.py` 会覆盖生成 |

环境：Windows 11 + Python 3.12（本机 `C:\Users\26876\AppData\Local\Programs\Python\Python312\python.exe`）。

## 2. 安装依赖

```powershell
& C:\Users\26876\AppData\Local\Programs\Python\Python312\python.exe -m pip install -r agent\requirements.txt
# 网络慢时：
& C:\Users\26876\AppData\Local\Programs\Python\Python312\python.exe -m pip install -r agent\requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 3. 快速开始

### 3.1 本机联调（直连 server，不走 Caddy）

```powershell
cd C:\Users\26876\Desktop\pc2mb
.\agent\run.bat --server ws://127.0.0.1:18091/ws/agent --token localdevtoken123
```

### 3.2 线上（经 Caddy）

```powershell
.\agent\run.bat --server wss://wkeeli.cn/pc2mb/ws/agent --token <PC2MB_AGENT_TOKEN>
```

Caddy 的 `handle_path /pc2mb/*` 会去掉 `/pc2mb` 前缀，所以对外是
`wss://wkeeli.cn/pc2mb/ws/agent`，服务端看到的是 `/ws/agent?token=...`。

### 3.3 写进 agent.env（免参数，推荐日常使用）

`agent/agent.env`：

```ini
PC2MB_SERVER=ws://127.0.0.1:18091/ws/agent
PC2MB_TOKEN=localdevtoken123
```

然后直接 `.\agent\run.bat` 或 `python agent\agent.py`。

## 4. 配置

优先级：**命令行参数 > 环境变量 > `agent/agent.env` > 内置默认值**。

| 命令行 | 环境变量 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `--server` | `PC2MB_SERVER` | `ws://127.0.0.1:18091/ws/agent` | Agent WS 地址，必须 `ws://` 或 `wss://` |
| `--token` | `PC2MB_TOKEN` | **必填** | 拼到 `?token=`（会覆盖 URL 里已有的 token，其余 query 保留） |
| `--max-width` | `PC2MB_MAX_WIDTH` | `1600` | 帧宽超过该值时等比缩小；`0` 表示不缩放 |
| `--quality` | `PC2MB_QUALITY` | `60` | JPEG 质量 1~100 |
| `--env-file` | `PC2MB_ENV_FILE` | `agent/agent.env` | 配置文件路径 |
| `--log-level` | - | `INFO` | `DEBUG/INFO/WARNING/ERROR`，日志到 stdout 带时间戳 |

`run.bat` 额外识别 `PC2MB_PYTHON`（指定 Python 解释器，默认本机 3.12 路径）。

配置错误 / 缺 token / token 被服务端拒绝（close 4401）时：打印错误并以**退出码 2** 退出，
不会无限重连。退出码：`0` 正常（Ctrl+C）、`1` 未预期异常、`2` 配置或 token 错误。

## 5. 运行时行为

1. 启动即声明 **Per-Monitor DPI aware**（`SetProcessDpiAwareness(2)`），保证 mss 抓到的
   分辨率和 `SetCursorPos` 的坐标系一致（否则缩放屏上画面与鼠标会错位）。
2. 连接成功后立刻发 `{"t":"hello","screen":{"w","h"},"host":"<主机名>"}`。
3. 每收到一次 `{"t":"req"}` → 抓一帧 → 回一帧 binary JPEG。**不主动推流**，由 viewer 的
   req 驱动（自适应帧率）。抓帧/编码失败只记录日志，不退出（会给 server 发一条
   `{"t":"error","code":"capture","msg":...}` 便于排查）。
4. 帧尺寸与上次不同时，先发 `{"t":"meta","w","h"}` 再发帧（启动时为 hello 里的尺寸，
   运行中显示器分辨率变化才会触发）。
5. `{"t":"viewer","on":...}` 只记日志；未知 JSON 类型 / 未知 `t` 忽略不报错。
6. 断线重连：指数退避 **1,2,4,8,15,15...s**；成功握手后档位清零。
   服务端 close **4401**（token 错误）→ 打印错误退出 2，不重连；HTTP 握手 401/403 同上；
   404 视为 server 路径配置错误退出 2；502/503 等网关错误按可重试处理。
7. 每次断线 / 退出都调用 `release_all()` 释放所有按下的键，避免 Ctrl/Shift 卡在按下状态。
8. Ctrl+C 干净退出（关闭 mss、GDI+、线程池）。

## 6. 输入注入细节（纯 ctypes user32）

| 协议事件 | 实现 |
| --- | --- |
| `{"t":"m","x","y"}` | `SetCursorPos(round(x*屏幕宽), round(y*屏幕高))`，坐标 clamp 到屏幕内；归一化坐标以**主显示器物理像素**为基准 |
| `{"t":"d","b"}` / `{"t":"u","b"}` | `SendInput` + `MOUSEEVENTF_LEFT/MIDDLE/RIGHTDOWN/UP` |
| `{"t":"w","dy"}` | `SendInput` + `MOUSEEVENTF_WHEEL`，`mouseData = dy`（原样，负数按 DWORD 补码） |
| `{"t":"kd","k"}` / `{"t":"ku","k"}` | 见下 |
| `{"t":"txt","s"}` | 逐字符 `KEYEVENTF_UNICODE`（按下+抬起一对），中文/emoji 以外任意 BMP 字符都能输入 |

`kd` / `ku` 规则：

- 修饰键：`Control/Shift/Alt/Meta` → `VK_CONTROL(0x11)/VK_SHIFT(0x10)/VK_MENU(0x12)/VK_LWIN(0x5B)`；
- 单字符且**没有**按下 Ctrl/Alt/Meta → `KEYEVENTF_UNICODE` 直接下发“按下+抬起”一对，
  随后的 `ku` 被吞掉（保证一次按键只出一个字符，IME/中文标点正确）；
- 按着 Ctrl/Alt/Meta 时字母数字 → 用 `VkKeyScanW` 换算 VK 走快捷键路径（Ctrl+C 等可用）；
- 特殊键：`Enter Backspace Tab Escape Delete Insert Arrow* Home End PageUp PageDown F1~F12 Space CapsLock` 等 → VK 表；
  方向键/Insert/Delete/Home/End/PageUp/PageDown/Win 等自动带 `KEYEVENTF_EXTENDEDKEY`；
- 未知键（如 `MediaPlayPause`）忽略，仅 DEBUG 日志，不影响连接；
- 维护“当前按下集合”，`ku` 用 kd 时记录的 VK 抬起，不会发错键。

## 7. 实测性能（本机：2880×1800 物理分辨率 @200% 缩放，帧 1600×1000 q=60）

| 指标 | 数值 |
| --- | --- |
| 抓帧（mss，20MB BGRA） | 40~50 ms |
| 编码（GDI+ JPEG q60，含 0.55× 缩小） | 25~30 ms |
| 单帧总耗时（稳态，selftest） | **67~73 ms** |
| 单帧总耗时（mock 联调，含网络） | 72~86 ms（首帧冷启动 ~128 ms） |
| 帧大小 | ~99~102 KB |
| 理论帧率 | ~10~15 fps（受 viewer req 流控） |

优化备注：缩小插值用 `HighQualityBilinear`（约 23 ms），实测比 `HighQualityBicubic`
（约 91 ms）快 4 倍而体积相近；JPEG 由 GDI+ 编码（2880×1800 不缩放时仅 ~18 ms）。
若机器性能不足，可下调 `--max-width 1280` / `--quality 55`。

## 8. 自检

```powershell
python agent\selftest.py
```

覆盖 26 项检查（全部 PASS），输出示例见下节：
DPI 生效模式、抓帧 + JPEG 存盘 `agent/_selftest.jpg`、GDI+ 解码回来校验尺寸/非全黑/耗时、
鼠标归一化坐标移动 + `(100,100)` + 复位校验、协议全部 key 的映射表（纯函数，不真按键）、
`SendInput` 结构体大小（x64 下 `INPUT=40`）、安全键盘注入（只按/放 Ctrl，用
`GetAsyncKeyState` 校验；再验证 `release_all()` 能释放卡住的 Shift）、配置优先级（临时 env 文件）。

**注意**：自检只移动鼠标（并复位）和单独按放 Ctrl/Shift，不点击、不输入文字，
不会对真实桌面产生破坏性操作。

## 9. 已知限制与风险

1. **只采集主显示器**（协议范围）；多显示器拼接/副屏不可见，鼠标坐标也只按主屏归一化。
2. **仅 Windows**：依赖 mss + GDI+ + user32.SendInput；非 Windows 下 `--help` 可用，运行会失败。
3. **帧率上限约 10~15 fps**：每次 req 同步抓帧+编码，无增量/差量编码、无硬件编码；
   高清大屏或低配机会更慢。
4. **不做破坏性注入自测**：`d/u`（会真实点击当前光标位置）、`w`（会滚动焦点窗口）、
   `kd/ku/txt`（会真实打字）没有做端到端真实注入测试，避免影响真实桌面；
   键盘路径只验证了安全子集（Ctrl/Shift 修饰键状态）。已在 mock 测试中验证事件分发与
   鼠标移动链路。**首次真机联调时请把光标放在安全位置。**
5. **UAC 安全桌面 / 锁屏界面无法注入**：Windows 限制（需要 UIAccess 签名进程 / 驱动），
   对以管理员权限运行的窗口，普通权限 agent 的 SendInput 会被丢弃（UIPI）。
6. 部分反作弊游戏 / 受保护窗口会忽略 `SendInput`；不经过驱动层，不是游戏级远控。
7. **无剪贴板同步、文件传输、音频、多屏切换、画面质量动态调节**（协议未定义，后续可加）。
8. 非 BMP 字符（emoji 等代理对）跳过不注入；`txt` 逐字符输入，极长文本会较慢。
9. **分辨率热切换**（改缩放/改分辨率）时应发 `meta`，但本机未实测热切换场景；
   `max-width` 缩放比例只在启动时根据当前分辨率决定。
10. 需在**交互式桌面会话（session 1）**运行；作为 Windows 服务跑在 session 0 时抓不到桌面、
    也注入不了输入（可用“登录时启动”的计划任务）。
11. token 按协议明文放在 query string，可能出现在本机/网关访问日志里；务必用 HTTPS/WSS
    且只连可信服务器，`agent.env` 已被 `.gitignore` 忽略。
12. Agent 是**出站连接**，本机不需要开入站端口、不需要防火墙入站规则。
13. 单 agent 单 viewer 的接管逻辑在服务端；被新 agent 顶掉（4409）后本 agent 按普通断线重连，
    最终以最新连接为准。
14. 日志默认 UTF-8 输出到 stdout；重定向到文件时建议 `chcp 65001`，否则中文注释类日志可能显示异常
    （运行日志本身为英文，不受影响）。

## 10. 故障排查

| 现象 | 处理 |
| --- | --- |
| 退出码 2，`missing token` | 配 `--token` / `PC2MB_TOKEN` / `agent/agent.env` |
| 退出码 2，`token rejected (close code 4401)` | token 与服务端 `PC2MB_AGENT_TOKEN` 不一致 |
| 退出码 2，`invalid server URL` | `--server` 必须 `ws://` 或 `wss://`，且路径以 `/ws/agent` 结尾 |
| 一直 `reconnecting in 1s/2s/...` | 服务端没起 / 端口不通 / 防火墙拦截出站；先确认 `curl http://127.0.0.1:18091/api/health` |
| 画面比例/鼠标位置错位 | 确认没有别的 DPI 虚拟化（agent 启动日志会打印 `DPI: per-monitor` 和真实分辨率） |
| 卡顿、帧率低 | 降 `--max-width 1280` / `--quality 50`；关闭其他占 CPU 的程序 |
| Ctrl+C 停不下来 | 再按一次；或用任务管理器结束 `python.exe`（agent 退出时会释放按下的键） |
| `--help` 报 `No module named mss` | 依赖没装：`pip install -r agent\requirements.txt`，或直接跑 `run.bat` 让它自动装 |

## 11. 测试证据（2026-11-18 本机）

- `python agent\selftest.py` → **26/26 PASS**
  - `agent/_selftest.jpg`：1600×1000、101,066 bytes、单帧 67.0 ms；GDI+ 解码回读 1600×1000，
    平均亮度 34.1（非全黑，深色主题桌面）
  - 鼠标：原坐标 `(1916,911)` → 归一化 (0.5,0.5) → `(1440,900)` → `(100,100)` → 复位 `(1916,911)` 全部校验通过
- 临时 mock WS 服务（`%TEMP%\pc2mb_mock_test.py`，端口 18099，websockets，不随仓库提交）→ **18/18 PASS**
  - 连接 + `hello`（`screen 1600×1000`、`host DESKTOP-P36AAQ5`、保留 URL 里已有的 `?x=1`）
  - `req` → binary JPEG 帧 3 次（101.6/101.2/101.8 KB，72/86/78 ms，SOI/EOI 与尺寸正确）
  - `input m(0.5,0.5)` → 光标 `(1440,900)`；`m(0.25,0.25)` → `(720,450)`（与物理分辨率一致）
  - 服务端异常关闭 1011 → **1.0 s** 后自动重连，重连后继续出帧
  - token 错误 → 服务端 4401 → agent 打印错误并**退出码 2**（无无限重连）
  - 坏 URL / 缺 token → 退出码 2
  - 全程只移动鼠标，结束后恢复原坐标
