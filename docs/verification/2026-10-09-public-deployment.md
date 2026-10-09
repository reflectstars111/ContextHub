# ContextHub 公网部署与权限验收

日期：2026-10-09（Asia/Shanghai）。应用版本：0.3.1。公网地址：<https://contexthub.reflectstars.dev/>。

## 已部署配置

- Hub 与 cloudflared 均为 enabled、active/running 的 systemd 用户服务，用户已有 `Linger=yes`，已配置开机及退出登录后常驻；本次没有重启整台服务器。
- Hub 继续使用原 `.context-data`，仅监听 `127.0.0.1:8765`。隧道指标仅监听 `127.0.0.1:20242`；默认拟用的 20241 已被其他服务占用，因此选用空闲端口，没有修改既有服务。
- 名为 `contexthub` 的独立命名隧道只转发 `contexthub.reflectstars.dev`，其余域名命中 404；新建该子域名 DNS 路由，没有修改主域名或其他应用路由。
- cloudflared 2026.10.0 官方二进制 SHA-256 校验为 `d33ff2d14475178d2012c2c56beba87389ac5ded27649519f198a7d3134a99db`，传输使用 HTTP/2。来源连接关闭 chunked encoding，以兼容严格请求帧检查和空 POST。
- 运行凭据在 `~/.config/contexthub/tunnel.json`，文件 600、目录 700；安装使用的管理证书及重复凭据副本已移除，随后重启隧道并得到 readiness 200、2 个有效连接。
- 已启用请求并发、体积、总期限、认证/写入限流、数据预算和 systemd CPU/内存上限，具体参数与运维步骤见[部署指南](../deployment/public-cloudflare.md)。

切换前已停服完整复制原数据目录到受保护的 `.artifacts/backups/20261009-162308-before-public/`，随后启动公网配置。备份未提交 Git；尚未配置自动定期备份。

## 应用回归

最终 `python3 -m unittest discover -s tests -q`：**163 项通过，59.799 秒**。覆盖原导入导出、版本和贡献流程，以及公网 Host/Origin、可信代理、Cookie、HTTP/HEAD、限流、容量、下载登录和注册原子性。

独立审查曾复现两项问题，修复后再次验证：

1. 空间不足发生在注册首次会话创建阶段，会同时回滚用户与会话，同名可重新注册。
2. 每 50 毫秒滴入字节的请求头和请求体仍会在测试配置的 350 毫秒总期限关闭并释放连接；总期限不终止已经运行的 Python/Git 计算。

四套浏览器脚本均通过，无 JavaScript 错误：`verify_web.py`、`verify_versions_web.py`、`verify_contributions_web.py`、`verify_public_access_web.py`。临时合成实例覆盖公开阅读、登录后固定历史版本下载、JSON/Markdown/正文复制、候选下载、权限隔离、登录弹窗竞争和本地连接器。没有读取真实 Codex 历史或调用模型。

域名既有规则拒绝 `Python-urllib` 默认 User-Agent（Cloudflare 1010）。CLI 已统一发送真实产品标识 `ContextHub/0.3.1`，实测该标识访问公网配置成功；临时真实 HTTP 实例验证 CLI 登录、带 Cookie/CSRF 上传、重新登录下载及匿名拒绝。没有关闭全域安全规则。

## 真实域名验收

21 项公网及来源检查全部通过：

- HTTPS 主页、样式、三份 JS、配置及公开列表为 200，配置版本 0.3.1、上传限制 16 MiB。
- 匿名包下载 GET/HEAD、候选下载及上传/预览 POST 均为 401。
- 本地 Codex API 为 404；不匹配的 Origin 为 403。
- HTTP GET 为 308，目标为固定 HTTPS 地址；HTTP POST 为 426。
- 直接本机访问的错误 Host 为 421；缺失可信代理标头为 403；完整健康检查标头为 200。
- HTTPS 响应均含 `Cache-Control: no-store` 与 HSTS；缓存状态为 DYNAMIC，没有 HIT 或 Age。用户已保存子域名缓存绕过规则，验收验证了实际响应，没有读取 Cloudflare 缓存规则管理 API。

HTTPS 校验保持开启。公网浏览器桌面与移动视口均成功加载，无页面异常或失败响应。真实域名提交缺字段注册/登录请求分别得到 422/403，没有创建账号或发放会话。验收时公开列表为 0 条，真实私有内容未读取；跨账号私有 ACL 与成功注册/下载在临时实例验收。

服务器出站 Python HTTPS 采样出现过 TLS 握手 EOF；最终 21 项检查中有 1 次传输重试，随后成功。浏览器加载未见同类失败。此记录不代表长期可用性或高负载承诺。

## 资源采样

2026-10-09 16:27 的 15 秒轻载采样：

| 服务 | RSS | CPU（单核百分比） | 线程 |
| --- | --- | --- | --- |
| Hub | 26.05 MiB | 0.008% | 1 |
| cloudflared | 39.25 MiB | 0.007% | 6 |
| 合计 | 65.30 MiB | 0.015% | 7 |

这是小数据、少量请求下的实测，上传和合并峰值会增加。Hub 无模型推理和 GPU 使用；硬资源上限及保守存储预算用于限制对同机任务的影响。

机器摘要和截图保存在忽略目录 `.artifacts/`：`public-deployment-verification.json`、`public-resource-verification.json`、`public-live-browser.json`、`public-auth-validation.json`、四套浏览器验收 JSON，以及 `public-live-desktop.png`/`public-live-mobile.png`。
