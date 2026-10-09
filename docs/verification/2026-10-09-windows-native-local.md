# Windows 本机 CLI 验证

日期：2026-10-09（Asia/Shanghai）。源码版本：0.6.0。

本文件记录修复前的初测。后续源码重构与通过结果见 [Windows CLI 重构验收](2026-10-09-windows-cli-refactor.md)。

## 环境与执行

- 原生 Windows，PowerShell 5.1.26100.2161（Desktop）。
- Python 3.13.7，Git 2.53.0.windows.2。
- 工作目录：`F:\Git_for_ai`。
- 设置 `PYTHONUTF8=1`，执行 `python -m unittest discover -v`。
- 完整日志：`.artifacts/windows-native-unittest.log`（已转为 UTF-8）。

结果：320 项，11 failures、59 errors、8 skipped，耗时 222.597 秒；本次验收未通过。跳过项包括一个 POSIX 权限测试和七个仅用于非 Windows PowerShell 的安装器测试；原生 Windows 安装器测试实际执行。

## 已验证通过的能力

- 构建真实源码 `ctx.pyz`，在源码目录之外运行 `--version`，返回 `ContextHub 0.6.0`。
- 使用真实 PowerShell 5.1 安装器离线安装真实源码包，生成并运行原生 `ctx.exe`。
- 上述实际安装路径含中文、空格、`&` 和 `!`；使用 `-NoPathUpdate`，未更改用户 PATH。
- 安装器测试中，中文空格路径安装与升级、复杂参数保留、校验/版本失败保留旧安装、junction 拒绝、中文 Python 路径通过。
- 原生启动器与 Codex 定位相关测试已执行，失败清单中没有这两个模块的测试。

## 阻断与其他失败

### 1. 安全文件层原子替换共享冲突

真实 EXE 登录临时本机 Hub 时，保存凭据失败。继续最小化后发现，无需 HTTP 或 DPAPI，只需持有 `safe_fs.directory` 的目录句柄，创建并关闭常规源文件，再调用 `safe_fs.replace`，即返回共享冲突 `PermissionError`。

该现象在 C 盘临时目录和 F 盘项目临时目录均复现。凭据保存的异常链到达 `windows_credentials.py:152` → `safe_fs.py:281`。工作区的 `state.json` 写入也出现相同错误，导致 init、CLI 同步及大部分工作区测试无法完成。

最小复现（在项目根目录运行）：

```python
import os
import tempfile
from contexthub import safe_fs

with tempfile.TemporaryDirectory() as path:
    directory = safe_fs.directory(path)
    try:
        fd = safe_fs.open_file(directory, 'source', os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        os.write(fd, b'synthetic')
        os.close(fd)
        safe_fs.replace(directory, 'source', 'target')
    finally:
        safe_fs.close(directory)
```

结果为“另一个程序正在使用此文件，进程无法访问”。目前确认失败发生在原子替换层；具体句柄共享机制仍需进一步定位，不能将提示中的链接/junction 当作已证实原因。

### 2. 安装器回滚失败

`test_locked_existing_launcher_rolls_back_archive` 失败：升级失败后预期仍为 `0.6.0`，实际启动返回 `0.6.1`。旧归档没有按测试要求恢复，需修复并重新验证。

### 3. Windows ZIP 路径拒绝测试失败

`test_zip_traversal_symlink_duplicate_and_ambiguous_payloads_rejected` 中条目 `a\\b` 未抛出预期 `PackageError`。需检查 Windows 路径规范化与 ZIP 校验规则。

### 4. 服务端/测试平台限制

- 14 个错误包含 `Storage budgets require POSIX file locks on this platform`，表明当前服务端预算层依赖 POSIX 锁；这是全量测试包含服务端能力带来的 Windows 限制，不能全部算作 CLI 失败。
- 并发连接限流测试出现 `WinError 10053`。
- 旧版本迁移测试清理数据库时出现文件占用错误。

## 验证边界

真实 EXE 的端到端脚本保存于 `.artifacts/windows_native_smoke.py`，在登录凭据保存步骤被阻断；后续 push、clone、pull、export 不应视为通过。本次使用临时本机 Hub、合成账号与对话，没有访问生产站点、真实 Codex 会话或模型服务。

本次仅测试与记录结果，没有修改产品源码。先前 Wine 验证不能替代本次原生 Windows 结果，Windows CLI 尚不具备本机验收通过的证据。
