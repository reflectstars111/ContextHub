# 网页到本机 Codex 导入验收

## 完成的流程

网页新增“在本机 Codex 中继续”：自动固定正在查看的 Hub 版本与消息或代码快照，选择目标目录、Bash / PowerShell 及导入方式后，复制单条 `ctx open` 命令。多段对话可选择；从消息进入时固定对应对话并只导入截至该消息的正文。完整原始对话包另外保留。

`ctx open` 无需先初始化、clone 或切换工作目录。它要求明确版本，使用已有认证与传输校验，下载到临时工作区后复用代码恢复流程，生成 `code/`、`conversations/` 和 `receipt.json`。显式 `--codex` 创建会话，成功输出正确工作目录下的 `codex resume` 命令，不自动调用模型。临时传输工作区在成功及失败时均清理。

恢复来源采用包含 Hub、仓库、固定版本及包内容的稳定提交标识，让 `--resume` 能跨临时下载核验同一次恢复。既有目录不会覆盖；原生导入不确定时保留回执并拒绝自动重试。无内嵌代码的包继续使用现有 `ctx codex import` 流程。

网页默认明确选择 context 上下文注入，不保证完整原生时间线呈现或后续重新导出。timeline 仍仅支持已验证的 Codex 0.153.4；CLI 默认值保持 timeline。未关联代码的轮次、未发布草稿及只有原文存档的内容不生成项目导入命令。

## 实际验证

`python3 -m unittest tests.test_ctx_open tests.test_ctx tests.test_embedded_restore tests.test_distribution -q`：最终 **37 项全部通过**。新测试使用合成 Hub，验证认证、固定历史版本、实际代码字节、完整包保留、消息截断、重复恢复、原生失败回执、目标保护、内容校验及真实 zipapp 脱离源码目录运行。

推送前按 CI 相同入口运行 `python3 scripts/verify_release.py`：**525 项，496 项通过，29 项因 Windows / PowerShell 环境条件跳过，0 失败、0 错误**。全部八个网页脚本的语法检查及五项草稿并发检查通过；重新构建 CLI 并在源码目录外执行 `--version`、`open --help` 通过。

Codex 原生创建接口采用 mock 验证参数与重试边界，本轮没有读写真实用户会话或发起模型调用。Bash 生成命令实际通过临时测试程序执行，含空格、单引号及命令替换字符的目录仍为字面量。PowerShell 转义经浏览器断言与审查，尚未执行 Windows / PowerShell 原生运行验证。

`scripts/verify_codex_open_web.py` 和 `scripts/verify_reader_navigation_web.py` 均通过，覆盖新导入弹窗、剪贴板、版本与代码选择、草稿、匿名阅读导入说明、导航历史、来源返回、真实旧代码恢复及 320 / 390 / 768 像素布局，无 JavaScript 错误。另通过 JavaScript 语法检查、五项草稿并发检查和 `git diff --check`。

独立审查发现并修复：离开阅读页或切换账户后，旧导入弹窗残留。现在会关闭弹窗，复制前还会重新核验阅读版本与选择。新增第四个主操作按钮导致的 320 像素页面横向溢出也已修正。

## 运行更新

已重启现有 `contexthub.service`，工作目录仍为 `/ssd1/code/yqq/Git_for_ai`，未修改数据库或隧道配置。重启用于加载新增静态资源路由及清除 CLI 分发包的进程缓存。

通过带现有代理 Host 头的本机源站请求核验六个页面 / 脚本 / 样式资源与磁盘一致，下载的 `ctx.pyz` 与当前源码构建一致，SHA-256 校验通过，实际运行下载包的 `open --help` 成功。检查范围为源站，不代表公网 Cloudflare 路径的端到端验收。

下载包大小 117,559 字节；SHA-256：`dadcbb22f14ec661526bc5bf6dcb4c1ba038aba655a22051362df999480e72b9`。已安装的旧 CLI 需重新下载更新，可用 `ctx open --help` 确认新命令。

证据保存在 `.artifacts/codex-open-web-verification.json`、`.artifacts/codex-open-origin-verification.json`、`.artifacts/codex-open-desktop.png`、`.artifacts/codex-open-mobile.png` 和 `.artifacts/codex-open-mobile-command.png`。
