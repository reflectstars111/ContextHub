# Windows CLI 重构与本机验收

日期：2026-10-09（Asia/Shanghai）。源码版本：0.6.0。

## 结论

本机 Windows 11 上全部 `ctx` 命令的原生 EXE 验收通过。Python 3.12、3.13 全量回归通过，Python 3.14 的 CLI 与 Windows 专用测试通过。初次测试中的文件写入、凭据保存、升级回滚及 HTTP 503 响应问题已解决。

初测记录保留在 [Windows 本机初测](2026-10-09-windows-native-local.md)，其中的失败状态描述修复前的源码。

## 重构内容

- `safe_fs.py`：将 Windows 全路径 `MoveFileExW` 替换为针对已检查文件句柄的同目录 `NtSetInformationFile(FileRenameInformation)`。使用简单目标文件名和空 RootDirectory，避免重新打开目标目录导致共享冲突；保持所有祖先目录的防重命名、防重解析点修改保护。已打开目标文件、硬链接、junction 仍被拒绝。参考 [Microsoft FILE_RENAME_INFORMATION](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information)。
- `cli_runtime.py`：统一 `ctx` 和迁移命令入口的 Windows UTF-8 输出，以及适用于 PowerShell 的示例参数引用；源码、zipapp 和 EXE 复用相同命令实现。
- `windows_credentials.py`：恢复 DPAPI 凭据的原子保存，并让异常保留真实文件占用/权限信息。
- `storage_budget.py`：复用文件适配层的原生阻塞锁，Windows 使用 LockFileEx，POSIX 使用 flock；同一 Windows 目录的大小写别名共享重入锁，防止嵌套操作死锁。
- `install-ctx.ps1`：回滚使用有效的暂存备份路径，修复 PowerShell 5.1 把 File.Replace 的 null 备份参数转换为空字符串导致的恢复失败。
- `codex.py`：Windows 后台协议进程不新建控制台窗口，保持直接调用原生可执行文件。
- `server.py`：503 拒绝响应后半关闭连接并限时读取剩余请求字节，防止 Windows TCP reset 丢弃响应；最多读取 64 KiB、耗时最多 50 ms。
- 测试：修正 Windows ZIP 测试夹具自动规范化反斜杠、SQLite 连接未显式关闭、Windows 无 st_blocks 等测试兼容问题；安装器 PATH 测试增加跨解释器锁并验证恢复原用户 PATH。

## 环境与结果

- Windows 11，版本 10.0.26100，AMD64。
- Windows PowerShell 5.1.26100.2161。
- Git 2.53.0.windows.2。
- 本机安装的 CPython 3.12、3.13.7、3.14。

| 解释器 | 验证范围 | 结果 | 日志 |
|---|---|---|---|
| Python 3.12 | `python -m unittest discover -v` | 324 项，316 通过、8 跳过，252.199 秒 | `.artifacts/windows-native-python312-refactor.log` |
| Python 3.13.7 | `python -m unittest discover -v` | 324 项，316 通过、8 跳过，255.895 秒 | `.artifacts/windows-native-unittest-refactor.log` |
| Python 3.14 | Windows 命令、Codex 定位、凭据、启动器、安装器、文件安全、ctx 与打包 | 59 项全部通过，120.023 秒 | `.artifacts/windows-native-python314-cli-refactor.log` |

八项跳过为一项 POSIX 权限测试和七项专用于非 Windows PowerShell 的安装器测试。真实 Windows 的 junction、DPAPI、PE 启动器和 PowerShell 5.1 安装器测试实际执行。

全量回归之后补充的大小写目录别名重入锁测试在 3.12、3.13、3.14 均通过。最终 Python 3.12/3.13 并行执行用户 PATH 安装测试和别名测试，确认原用户 PATH 精确恢复；日志 `.artifacts/windows-native-final-followups.log`。当前测试集合共 325 项；上表的全量运行在增加别名测试之前执行，该新增用例已单独验证。

## 原生命令覆盖

`tests/test_windows_cli.py` 使用真实源码包和本机 Python 的 PE 启动器，在源码目录之外执行：

| 类别 | 已验证命令 |
|---|---|
| 登录 | `auth login/status/logout`，DPAPI 持久化，退出后匿名 clone 拒绝 |
| 本地内容 | `init`、`inspect`、`add 文件`、`add .`、`status`、`diff`、`diff --staged` |
| 本地历史 | `commit`、`log`、`export` |
| Hub 同步 | `push`、`clone`、`pull`、`fork`、`remote` |
| Codex 桥接 | `codex list/export/import`、导入 dry-run；真实原生 EXE 合成 JSON-RPC 服务 |
| 帮助与终端 | 全部命令帮助、中文和 shell 特殊字符、PowerShell 引用、非 UTF-8 默认环境中的 UTF-8 输出 |

既有 `test_ctx.py` 另覆盖私有/公开权限、历史版本 clone、陈旧 push 冲突、脏工作区保护、网络响应丢失后的幂等重试及 Fork 独立性。

实际 PowerShell 离线安装真实源码包之后，原生 EXE 完成私有 push → clone → 编辑 → push → pull → export，导出字节与远端完整包一致。证据：`.artifacts/windows-native-smoke.json`，可重现脚本 `.artifacts/windows_native_smoke.py`。

## 本机产物

- `.artifacts/windows-cli-refactor/ctx.pyz`：65,080 字节。
- SHA-256：`ffe998ea50ea9bf6833d95fec085e210de3a5cc2a232c725198f9378d9a72717`。
- 同目录提供 `ctx.exe` 和 `ctx.pyz.sha256`；EXE 使用本机 Python 3.13，输出 `ContextHub 0.6.0`。
- 产物由真实离线安装器安装，使用 `-NoPathUpdate`。

## 范围与后续验证

本次使用本机临时 Hub、合成账号和对话，没有访问生产服务、真实 Codex 历史或模型。Codex 桥接的原生进程与协议经过合成服务验证，不表示真实 Codex 的 UI 时间线恢复或模型续聊已验证。

本机验证为 Windows 11 AMD64；Windows 10、ARM64 和远端 CI 没有在本轮执行。CI 已扩展为 Ubuntu/Windows × Python 3.12/3.13/3.14 全量回归，本次不宣称远端 CI 或 Linux 验证已经通过。产品继续保留 Windows 预览标记。
