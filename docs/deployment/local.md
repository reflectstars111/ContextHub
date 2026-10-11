# 本地运行与 Linux 常驻

适合自己使用、在远程服务器上运行，或准备自部署。公网访问另见 [Cloudflare Tunnel 部署指南](public-cloudflare.md)。

## 从源码启动

需要 Python 3.12+ 和 Git；Hub 运行不需要第三方 Python 包，也不运行模型或使用 GPU。

在项目源码目录执行：

```bash
python3 -m contexthub serve --port 8765
```

打开 <http://127.0.0.1:8765> 并注册自己的账号。默认仅监听本机，数据保存在当前目录的 `.context-data/`。要单独存放数据，可添加 `--data /绝对路径/contexthub-data`。

用户名支持 3–32 位字母、数字、下划线或连字符，密码为 10–128 个字符。

使用远程 VS Code 时，在“端口”面板转发 `8765`，用面板提供的本地地址访问。服务常驻并不等于浏览器能直接访问，重新连接 SSH 后可能需要恢复端口转发。已配置公网模式的实例应使用 HTTPS 子域名，原 localhost 地址会受 Host 校验限制。

## 可选：本地 Codex 连接器

日常使用可以通过 [ctx](../cli.md) 采集和导入。若希望在浏览器中选择本机 Codex 会话，可在**安装了 Codex CLI 的机器上**另开终端：

```bash
python3 -m contexthub connect --port 8766 --open
```

使用终端输出的完整地址，保留其中本次启动的令牌。可勾选会话、预览并下载对话包，也可导入下载的包；Codex 导入方式和兼容范围见 [Codex 导出与导入](../cli.md#codex-导出与导入)。

连接器与 Hub 是两个独立进程。连接器只监听本机，Hub 不会读取访问者电脑上的 Codex 会话。不要将连接器代理到公网。

## 让 Linux 服务持续运行

使用仓库提供的 [systemd 用户服务模板](../../deploy/systemd/contexthub.service)。在源码目录执行：

```bash
install -Dm644 deploy/systemd/contexthub.service ~/.config/systemd/user/contexthub.service
```

编辑复制后的文件，将 `WorkingDirectory` 改为源码的绝对路径，并确认 `ExecStart` 使用的 Python 是 3.12+。模板中的 `.context-data` 相对于工作目录；接管已有实例时，必须保持原数据路径。

```bash
systemd-analyze --user verify ~/.config/systemd/user/contexthub.service
systemctl --user daemon-reload
# 首次接管前，先停止占用同一端口的手动 Hub 进程
systemctl --user enable --now contexthub.service
```

检查退出登录后是否仍能运行：

```bash
loginctl show-user "$USER" -p Linger
```

结果应为 `Linger=yes`。若尚未启用，按服务器管理权限执行 `loginctl enable-linger "$USER"`；这让用户服务在退出登录后保持运行，并在开机时启动。

常用管理命令：

```bash
systemctl --user status contexthub.service
systemctl --user restart contexthub.service
systemctl --user stop contexthub.service
journalctl --user -u contexthub.service -n 50 --no-pager
```

模板会在异常退出后等待 3 秒重启，手动停止不会自动重启。空闲时主要是 Python 进程的内存开销；上传、比较和合并会短时增加 CPU 和内存用量，版本越多，磁盘占用越大。

自动化终端若报 `Failed to connect to bus`，可在当前 shell 设置用户总线地址后重试：

```bash
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
systemctl --user status contexthub.service
```

## 更新、备份与恢复

更新前先停止 Hub，并备份**整个数据目录**。这里包含数据库、上传包和 Git 历史，只备份 SQLite 文件不够。备份应放在受保护的位置，数据目录也不要作为静态网站公开。

更新源码后，继续使用原来的 `--data` 路径启动服务，再检查页面和版本是否正常。恢复备份时同样先停服，还原完整目录后启动；保留目录和文件的访问权限。

旧 0.1/0.2 实例会在启动时自动升级数据结构。原始上传记录会迁移为初始版本，保留库 ID、原包字节、创建时间和 Git 提交；后续升级增加贡献、草稿等所需记录。重复启动不会重复创建初始版本。升级前仍需完整备份，回退代码时应同时使用对应版本的备份数据。

常驻服务不会自动备份。网站里的“永久清理”也不会删除独立备份；需要按自己的保留规则管理这些副本。
