# PC2MB 生产部署指南

本文以实际在用的拓扑为例（域名为占位符）：

```
浏览器 ──HTTPS──> 反向代理机（公网, Caddy 自动 TLS）
                     └─ 127.0.0.1:18091 ── SSH 反向隧道 ──> 应用机（内网/云主机）
                                                              └─ FastAPI 127.0.0.1:18091
Windows 被控机 ──WSS 出站──> 反向代理机 /pc2mb/ws/agent
```

要点：应用只监听 `127.0.0.1`，不直接暴露公网；反向代理机用 SSH 反向隧道把 18091 映射到本机回环地址；
子路径 `/pc2mb/` 由 Caddy `handle_path` 去掉前缀后转发。

## 1. 应用机（Linux）

```bash
# 1.1 创建专用系统用户和目录
useradd --system --home /opt/pc2mb --shell /usr/sbin/nologin pc2mb
mkdir -p /opt/pc2mb

# 1.2 上传代码（在本地仓库根目录执行）
tar -czf /tmp/pc2mb.tar.gz server web docs tests deploy agent
scp /tmp/pc2mb.tar.gz root@APP_HOST:/tmp/
ssh root@APP_HOST "tar -xzf /tmp/pc2mb.tar.gz -C /opt/pc2mb"

# 1.3 虚拟环境 + 依赖
python3 -m venv /opt/pc2mb/.venv
/opt/pc2mb/.venv/bin/pip install -r /opt/pc2mb/server/requirements.txt

# 1.4 生成生产配置（把输出重定向到 /opt/pc2mb/.env）
/opt/pc2mb/.venv/bin/python /opt/pc2mb/server/app.py --gen-secrets > /opt/pc2mb/.env
# 记下生成的随机密码；也可以自己指定：
#   PC2MB_PASSWORD='xxx' python server/app.py --gen-secrets
chown -R pc2mb:pc2mb /opt/pc2mb
chmod 600 /opt/pc2mb/.env
```

`.env` 内容示例：

```ini
PC2MB_USER=admin
PC2MB_PASSWORD_HASH=pbkdf2_sha256$200000$<salt>$<hash>
PC2MB_AGENT_TOKEN=<随机长字符串>
PC2MB_COOKIE_SECRET=<随机长字符串>
PC2MB_HOST=127.0.0.1
PC2MB_PORT=18091
```

## 2. systemd 服务

```bash
cp /opt/pc2mb/deploy/pc2mb.service /etc/systemd/system/pc2mb.service
systemctl daemon-reload
systemctl enable --now pc2mb
systemctl status pc2mb --no-pager
curl -s http://127.0.0.1:18091/api/health
```

## 3. SSH 反向隧道（应用机 → 反向代理机）

应用机生成专用密钥，并把公钥加入反向代理机的 `authorized_keys`：

```bash
# 应用机
ssh-keygen -q -t ed25519 -f /root/.ssh/id_ed25519_pc2mb -N '' -C pc2mb-tunnel
cat /root/.ssh/id_ed25519_pc2mb.pub
# 把上面的公钥追加到 反向代理机 的 /root/.ssh/authorized_keys
```

反向代理机 sshd 需允许转发（默认允许）；建议 `GatewayPorts clientspecified`，这样
`-R 127.0.0.1:18091:127.0.0.1:18091` 只会绑定回环地址，公网无法直连端口。

```bash
# 应用机：/etc/systemd/system/pc2mb-tunnel.service（见 deploy/pc2mb-tunnel.service）
systemctl daemon-reload
systemctl enable --now pc2mb-tunnel
systemctl status pc2mb-tunnel --no-pager
```

验证（反向代理机上）：

```bash
ss -tlnp | grep 18091          # 应看到 127.0.0.1:18091 (sshd)
curl -s http://127.0.0.1:18091/api/health
```

## 4. Caddy 反向代理

在反向代理机的 `/etc/caddy/Caddyfile` 里，把 `deploy/caddy-pc2mb.conf` 的内容插入到站点块内
（必须在兜底 `reverse_proxy` 之前）：

```caddyfile
	# ===== pc2mb 云远控 =====
	redir /pc2mb /pc2mb/ 308
	handle_path /pc2mb/* {
		reverse_proxy http://127.0.0.1:18091 {
			header_up X-Forwarded-Prefix /pc2mb
		}
	}
```

```bash
cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M%S)
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload caddy
curl -s https://YOUR_DOMAIN/pc2mb/api/health     # {"ok":true,...}
```

Caddy 会自动处理 WebSocket Upgrade，无需额外配置；站点级 CSP 的 `connect-src 'self'`
已允许同源 `wss://`。

## 5. Windows 被控端 Agent

```powershell
# 5.1 依赖
pip install -r agent\requirements.txt

# 5.2 配置（不要提交到 Git）
@"
PC2MB_SERVER=wss://YOUR_DOMAIN/pc2mb/ws/agent
PC2MB_TOKEN=<与服务器 .env 的 PC2MB_AGENT_TOKEN 相同>
"@ | Set-Content agent\agent.env -Encoding utf8

# 5.3 前台试跑
python agent\agent.py
# 另开浏览器打开 https://YOUR_DOMAIN/pc2mb/ 登录，看到桌面即成功

# 5.4 注册开机自启（登录时隐藏窗口运行，日志写 agent\agent.log）
powershell -ExecutionPolicy Bypass -File deploy\install-agent-autostart.ps1
```

卸载自启：`powershell -ExecutionPolicy Bypass -File deploy\install-agent-autostart.ps1 -Uninstall`

## 6. 验证清单

```bash
curl -s https://YOUR_DOMAIN/pc2mb/api/health                 # {"ok":true,"agent":true}
curl -s -o /dev/null -w '%{http_code}\n' https://YOUR_DOMAIN/pc2mb/     # 200
python tests/e2e_local.py https://YOUR_DOMAIN/pc2mb          # 服务端中继测试
python tests/browser_smoke.py https://YOUR_DOMAIN/pc2mb      # 浏览器端到端（需本机 Chrome）
```

## 7. 常见问题

| 现象 | 排查 |
| --- | --- |
| `/pc2mb/api/health` 返回 502 | 隧道没起来：`systemctl status pc2mb-tunnel`、`ss -tlnp \| grep 18091`（反向代理机上） |
| 登录一直 401 | `.env` 的 `PC2MB_PASSWORD_HASH` 与账号不匹配，重新 `--hash-password` 并 `systemctl restart pc2mb` |
| 登录 429 | 失败 5 次被锁 60 秒，等一分钟或重启服务清空内存限流 |
| 画面显示"桌面离线" | Agent 没连上：检查 `agent\agent.env` 的 URL/Token、`agent\agent.log`；服务端 4401 = Token 错 |
| 打开页面空白/样式错 | 确认是 `https://域名/pc2mb/`（带结尾斜杠），静态资源走同目录相对路径 |
| 手机触摸无反应 | 确认用手机浏览器直接打开（非微信内置浏览器旧内核），页面需 HTTPS |
| 想换账号密码 | 改 `.env` 的 `PC2MB_USER` / `PC2MB_PASSWORD_HASH`，`systemctl restart pc2mb`，浏览器重新登录 |
