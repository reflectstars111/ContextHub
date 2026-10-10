# 参与 ContextHub

当前开发版本 0.7.0 提供多平台对话导入、Codex 文本时间线迁移、固定版本、Fork、网页编辑、服务器草稿、可见性设置和永久清理，以及贡献审阅、ctx 分支合并与普通 Git 仓库互通。Windows 保留预览标记，支持范围以对应版本的原生验收为准。

## 本地开发

需要 Python 3.12+ 和 Git。运行产品与单元测试不需要额外 Python 依赖。

```bash
python3 -m unittest discover -v
python3 -m contexthub serve --data .context-data --port 8765
python3 -m contexthub connect --port 8766
```

请使用 `python3 -m contexthub demo` 的合成数据测试，不将私人对话、凭证、本机 Codex 数据库或迁移包提交进源码库。

## 模块边界

- `package.py`：迁移格式、校验、阅读版、辅助脱敏。
- `codex.py`、`codex_timeline.py`：本地 JSON-RPC 协议、上下文注入和版本限定的文本时间线重建；导入不发起模型生成。
- `store.py`：账号、会话、不可变版本、Fork、访问控制与公开 Git 快照。
- `library_state.py`：拥有者专属草稿、可见性切换与可重试的永久清理。
- `workspace.py`、`git_bridge.py`：本地分支、提交与合并，以及普通 Git 仓库的受管内容导入导出。
- `ctx.py`、`hub_client.py`：终端命令和限定站点来源的 Hub 客户端。
- `revisions.py`：服务端人工编辑记录和有界差异展示。
- `merge.py`：按稳定 ID、字段与顺序确定性地合并共同起点、目标和源快照；返回候选包或有界冲突报告。
- `contributions.py`：固定源版本的贡献请求、评论与状态，依赖访问校验、候选持久化及事务内发布。
- `server.py`：Hub 和本地连接器两种隔离服务模式。
- `__main__.py`：终端命令。
- `static/`：不依赖构建工具的浏览器界面；`versions.js` 管理编辑与历史，`drafts.js` 管理自动保存，`lifecycle.js` 管理库设置，`contributions.js` 管理贡献审阅流程。

修改版本功能需验证旧数据库升级、历史下载字节不变、跨账号权限、并发基准检查与成功重试。`scripts/verify_versions_web.py` 用合成内容和两个浏览器账号检验 Fork、编辑、导入新版、差异、固定链接、并发冲突以及异步加载时的草稿保护。运行该脚本需要可选 Playwright 开发依赖。

贡献请求只允许公开的直接 Fork 回流到公开上游。提交者固定源版本，维护者基于当前上游检查三方合并，再审阅并发布同一候选；源库后续发布其他版本不能悄悄更新提案。源库拥有者与上游拥有者可评论、关闭和重开，只有上游拥有者可检查或合并。私有内容、已删除依赖、冲突和过期候选必须在服务端阻断。

修改贡献功能需验证：固定版本与共同祖先访问权限、确定性合并和审计记录、候选原始字节与准确 Git OID、两个父提交、上游并发推进、明确更换源版本后检查失效、规则版本变化、成功重试、SQL/Git 失败不发布，以及纯 ZIP 编码变化不产生空贡献。展示差异可截断，实际候选与冲突计数不可静默裁剪；不得用模型猜测解决冲突。

`scripts/verify_contributions_web.py` 检验双用户从公开 Fork 提交贡献、审阅候选到合并的浏览器流程。运行浏览器验收前安装可选开发依赖：

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
python3 scripts/verify_web.py
python3 scripts/verify_versions_web.py
python3 scripts/verify_contributions_web.py
```

修改草稿和库设置还需运行 `node scripts/verify_draft_races.js` 与 `python3 scripts/verify_drafts_web.py`，检查跨标签和账号切换、过期请求、私有编辑期间可见性变化，以及共享内容的清理保护。发布验收可用 `python3 scripts/verify_release.py` 保存平台与跳过项目记录。

当前尚无图形冲突解决或站内通知。具体交付范围与验证证据见[0.7.0 本地验收](docs/verification/2026-10-10-platform-maturity.md)。

改动迁移格式时增加往返和损坏输入测试；改动 Codex 适配时同时报告版本、历史模式、API 回读、持久化、原生 UI 和实际续聊的验证范围。不要把请求成功等同于全部兼容。

提交说明应包含具体问题、结果、测试命令及已知限制。平台软件采用 MIT 许可；用户上传的对话内容不因此自动获得 MIT 许可。
