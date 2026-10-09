# 使用 Cloudflare Tunnel 部署 ContextHub

此配置适用于独立 HTTPS 子域名。Hub 仅监听 `127.0.0.1:8765`，cloudflared 主动建立出站加密连接，不需要开放服务器的 8765 入站端口。本地 Codex 连接器不参与隧道。

## 权限与信任边界

- 公开库允许匿名浏览、搜索、阅读、查看版本和贡献讨论。
- 下载任何完整包、历史版本和合并候选均须登录；网页 JSON/Markdown 导出与正文复制也先登录。公开正文已经允许阅读，因此登录要求不能阻止访问者自行保存公开内容。
- 上传、Fork、编辑、发布、删除及贡献操作须登录，并执行所有权检查与 CSRF 校验。
- 私有库只有所有者可见。新上传默认私有。
- Cloudflare 终止浏览器 TLS，能够处理 HTTP 内容；Cloudflare 到 cloudflared 使用加密隧道，cloudflared 到 Hub 为本机回环 HTTP。这不是对 Cloudflare 隐藏内容的端到端加密。
- `--trust-cloudflare` 仅与公网 HTTPS 来源、回环监听同时使用。应用校验 Host、来源和转发协议，并从 `CF-Connecting-IP` 取得限流地址。同机进程仍处于信任边界内；不要把此端口再转发给不可信代理，也不要添加可改写来源标头的同域 Workers。

应用设置 `Secure; HttpOnly; SameSite=Strict` 的 `__Host-` 会话 Cookie，不设置跨子域 Domain；HTTPS 响应带 HSTS，所有应用响应声明 `Cache-Control: no-store`。HTTP 读取重定向到固定 HTTPS 来源，HTTP 写入被拒绝。公网模式开启后，原 `localhost` 地址会因 Host 不匹配被拒绝，请使用子域名。

## 初次配置

先安装并校验官方 cloudflared 发布文件，放在 `~/.local/bin/cloudflared`。执行 `cloudflared tunnel login`，在浏览器选择正确域名，再执行 `cloudflared tunnel create contexthub`。管理证书 `cert.pem` 的权限大于单隧道凭据，运行服务只需要新生成的 UUID JSON 文件。[官方权限说明](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/tunnel-permissions/)

1. 建立权限为 700 的 `~/.config/contexthub/`，将对应隧道 JSON 放入 `tunnel.json`，权限 600。
2. 复制 [隧道配置模板](../../deploy/cloudflared/config.example.yml) 为该目录的 `cloudflared.yml`，替换 UUID、凭据绝对路径和两处域名，权限 600。保留最后的 404 规则，不使用通配域名。
3. 安装 [Hub 基础用户服务](../../deploy/systemd/contexthub.service)，修改实际工作目录；安装 [公网覆盖配置](../../deploy/systemd/contexthub-public.conf) 到 `~/.config/systemd/user/contexthub.service.d/public.conf`，修改域名。
4. 安装 [隧道用户服务](../../deploy/systemd/contexthub-tunnel.service) 到 `~/.config/systemd/user/`。
5. 在 Cloudflare 的域名「缓存 → 缓存规则」创建表达式 `(http.host eq "你的子域名")`，缓存资格选择「绕过缓存」，放在最后，避免继承全站强制缓存规则。保存后实际核验响应缓存状态。[官方缓存设置](https://developers.cloudflare.com/cache/how-to/cache-rules/settings/)
6. 先完成应用测试与备份，再启动服务及建立该子域名 DNS 路由。

```bash
cloudflared --config ~/.config/contexthub/cloudflared.yml tunnel ingress validate
cloudflared --config ~/.config/contexthub/cloudflared.yml tunnel ingress rule https://你的子域名/
systemd-analyze --user verify ~/.config/systemd/user/contexthub.service ~/.config/systemd/user/contexthub-tunnel.service
systemctl --user daemon-reload
systemctl --user enable contexthub.service
systemctl --user restart contexthub.service
systemctl --user enable --now contexthub-tunnel.service
cloudflared tunnel route dns YOUR_TUNNEL_UUID 你的子域名
```

确认服务为 enabled/active 且 `loginctl show-user "$USER" -p Linger` 为 yes。管理证书在创建路由后可移到离线管理环境或移除；以后管理时重新授权，运行隧道无需保留它。不要提交任何证书或凭据文件到 Git。

配置固定 `http2`，使用出站 TCP 7844；网络必须允许 Cloudflare Tunnel 的目标地址与端口。不得为绕过连接失败而关闭 TLS 验证。[官方网络要求](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)

保留 `disableChunkedEncoding: true`：应用拒绝分块或歧义请求标头，此选项避免隧道把空 POST 转成分块请求。非空上传须有 Content-Length，浏览器和 ContextHub CLI 会自动设置。[官方来源连接参数](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/)

CLI 通过真实产品标识 `User-Agent: ContextHub/版本号` 发起请求。若用自写 Python 工具访问，默认 `Python-urllib` 标识可能被已有的 Cloudflare 浏览器完整性规则拒绝；请设置准确的客户端标识，不必关闭该域名的保护规则。

## 本配置的资源限制

| 项目 | 限制 |
| --- | --- |
| 同时处理的 HTTP 连接 | 4，超出返回 503 |
| 请求体 | 16 MiB，超出返回 413 |
| 连接空闲 / 总期限 | 30 秒 / 120 秒 |
| 注册、登录 | 每来源地址每分钟 20 次 |
| 登录后的写操作 | 每账号每分钟 60 次，超出返回 429 |
| 数据目录预算 / 磁盘保留空间 | 5 GiB / 5 GiB |
| Hub CPU / 内存 | 最多 1 个 CPU 核心；512 MiB 软阈值、1 GiB 硬上限 |
| 隧道 CPU / 内存 | 最多半个 CPU 核心；256 MiB 硬上限 |

CPU 配额表示使用上限，不预留 CPU；内存上限也不预分配。连接总期限中断网络读写，不终止已经运行的 Python/Git 计算。压缩包的 16 MiB 上传限制与格式的 100 MiB 解压上限不同，解析会产生额外内存开销；极端负载可能触发内存上限并重启服务。这些保护降低对同机任务的影响，并不保证任意负载下均可用。

存储限制是带跨进程锁的保守写入预算，包含数据库、包和 Git 文件；会预留事务与写入开销，可能提前拒绝并返回 507。它不是文件系统硬配额，其他程序仍可消耗磁盘。删除库不会立即删除历史文件或释放预算。当前没有每用户容量配额、验证码、邮箱验证与自动备份；面对持续滥用需监控并进一步配置边缘限制。

## 运维与验收

```bash
systemctl --user status contexthub.service contexthub-tunnel.service
journalctl --user -u contexthub-tunnel.service -n 30 --no-pager
# 更新代码后只重启应用
systemctl --user restart contexthub.service
# 暂停公网访问；Hub 数据保留
systemctl --user stop contexthub-tunnel.service
```

检查公网主页、静态资源、公开列表与 HTTPS 证书；匿名下载应为 401，匿名上传应为 401，私有库不能出现在匿名列表。验证缓存状态为 DYNAMIC/BYPASS，没有 HIT/缓存 Age。注册、登录、不同账号的私有访问和导出在临时测试实例验证，避免往生产数据加入验收账号。

本机健康检查必须携带受信代理标头：

```bash
curl --fail http://127.0.0.1:8765/api/config \
  -H 'Host: 你的子域名' \
  -H 'CF-Connecting-IP: 127.0.0.1' \
  -H 'X-Forwarded-Proto: https'
```

定期备份整个数据目录：停止 Hub，复制目录到受保护位置，再启动 Hub。只复制 SQLite 不能替代包文件与 Git 历史的完整备份。恢复时停服、还原原目录后启动。保留目录权限 700、文件 600；对话正文与哈希口令均属于私人数据。
