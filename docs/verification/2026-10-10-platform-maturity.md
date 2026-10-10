# ContextHub 0.7.0 本地验收

日期：2026-10-10（Asia/Shanghai）。开发分支：`feature/platform-maturity`。

本轮先将原工作目录同步至 `origin/main` 的 `ceda7f5`，再在独立工作区完成开发。首次本地验收时，按用户选择先完成代码，未推送、运行远端 CI 或部署生产服务；本文的测试结果均为该次本地验收证据。后续按用户要求从 Fork 整理 `pr/platform-maturity` 分支提交 PR，远端 CI 状态以 PR 检查为准。公网安装链接仍以已部署版本为准。

## 已实现与验收范围

| 原有问题 | 当前行为 | 边界 |
|---|---|---|
| 单分支快照，无合并 | `ctx branch/switch/merge`、双父提交、独立修改三方合并、pull 分歧合并与冲突保留 | Hub 仍接收线性发布快照；本地和 Git 保留分支 DAG |
| 无普通 Git 互通 | `ctx git export/import` 保留受管 JSON、分支、父子关系和原样快照；接受普通 Git 提交后的受管正文修改 | 需要 ctx 导出格式，不解释任意代码仓库；不是 Hub Git HTTPS 服务 |
| Codex 只有上下文注入 | 默认 timeline 在 Codex 0.153.4 重建新的用户/助手文本会话，独立进程读回和 resume；可显式选择 context | 不是完整原生状态克隆；GUI 渲染、模型续聊、附件、工具状态和原始回合边界未验证或不迁移 |
| 草稿仅当前页面保存 | 正文输入自动保存到拥有者专属服务器草稿；刷新恢复、下载备份、单独丢弃 | 多标签冲突保留本页，要求明确选择；草稿不产生发布版本 |
| 创建后可见性固定 | 拥有者可切换公开/私有；全部 HTTP 历史一起变更；旧页面/CLI 发布校验预期可见性 | 不撤回已有下载或独立 Fork；私有来源不能被 Fork 后改公开 |
| 删除后文件一直保留 | 保留撤销访问，新增输入库 ID 确认的永久清理；内容和 Git 对象按剩余引用清理，失败可重试 | 保留其他库/Fork 所需内容；独立运维备份不在在线清理范围 |
| Windows 仍预览 | 同步远端 Windows 11 原生修复；跨平台工作流输出结构化结果并运行草稿竞态验证 | **0.7.0 未经原生 Windows 验收，继续保留预览标记** |

## 关键回归

- 分支创建不继承原分支的 Hub 绑定。持久化 `new_library_key` 让相同内容的新分支创建独立库，响应丢失重试仍定位同一初始版本。旧 Hub 不声明该能力时，CLI 在发送正文前拒绝首次上传，提示升级。
- Git 导入只读取已提交本地分支，在隔离配置下读取对象，不执行来源仓库的 hooks、filters 或 fsmonitor。未提交文件、凭据和 Hub 绑定不进入导入结果。新目录通过同父目录、不覆盖的原子重命名发布。
- 草稿 revision 在删除和发布后仍单调递增，避免旧请求覆盖新草稿。发布只消费匹配 revision，刷新可恢复历史基准草稿；新版本冲突不清除草稿。
- 私有库被另一页面改公开后，旧页面和 CLI 发布均返回冲突，不产生新版本，草稿保留。重新读取当前可见性后再决定发布。
- 并发保存串行处理；退出或切换账号停止自动写入；迟到的丢弃/恢复响应不替换另一库的内容。
- 永久清理与上传、版本发布、候选生成共用进程间锁，即使未启用存储配额也不绕过锁。清理保留其他版本/候选需要的 Git 对象及共享包，创建请求只留下无正文的 owner/key 哈希墓碑，防止迟到重试重建已清理库。

## 自动测试与浏览器

最终完整回归：**381 项，361 通过、20 项原生 Windows 专用测试跳过，0 失败、0 错误，119.756 秒**。环境为 Linux x86_64、Python 3.12.3；PowerShell 安装器核心测试使用 Linux PowerShell 7.5.3 实际执行。

```sh
CONTEXTHUB_TEST_POWERSHELL=/tmp/contexthub-powershell-7.5.3/pwsh python3 scripts/verify_release.py
node scripts/verify_draft_races.js
```

日志为 `.artifacts/maturity-release-final.log`，结构化结果为 `.artifacts/release-platform-verification.json`。测试只使用临时 Hub、合成账户和对话，没有向生产添加测试内容。

浏览器验收使用 Chromium，全部通过，未捕获页面 JavaScript 错误：

| 脚本 | 覆盖 |
|---|---|
| `scripts/verify_web.py` | 上传下载字节一致、两账号私有隔离、搜索、XSS 惰性文本、连接器夹具、移动端 |
| `scripts/verify_versions_web.py` | Fork、编辑、预览、发布确认、历史链接、整包替换、过期保存保留草稿 |
| `scripts/verify_contributions_web.py` | 提案、候选审阅、精确包与 Git OID 合并、陈旧检查、评论与导航竞态 |
| `scripts/verify_public_access_web.py` | 匿名公开阅读、下载登录、私有拥有者隔离、历史和候选权限 |
| `scripts/verify_multiplatform_web.py` | Claude、DeepSeek、ChatGPT 分类、兼容提示、原文存档、历史来源权限 |
| `scripts/verify_cli_web.py` | 桌面/移动指南、0.7.0 单文件下载、校验及包外执行 |
| `scripts/verify_drafts_web.py` | 输入级自动保存、刷新恢复、跨标签冲突与备份、发布消费、可见性竞态、永久清理保护 Fork |

`node scripts/verify_draft_races.js` 的 5 项确定性竞态回归通过，覆盖并发 flush、身份变化以及等待中的丢弃/恢复。

所有浏览器 JavaScript 的 `node --check` 和 `git diff --check` 通过。上述浏览器报告保存为 `.artifacts/*-web-verification.json`（基础报告为 `.artifacts/web-verification.json`），原始日志和图片不进入版本库。

## 本地产物

产物位于 `.artifacts/maturity-dist/`：

| 文件 | SHA-256 |
|---|---|
| `ctx.pyz`（84,467 字节） | `ca1f384406aff95c6b5ee43d44e00af146d16abe98bed254e4007677904c0550` |
| `contexthub_conversations-0.7.0-py3-none-any.whl` | `a7f3fc921b2b816755c944e8936ea199c09a0080eceda5c196a3bec6ff368237` |

单文件在源码目录之外执行 init → add → commit → 两分支独立编辑 → merge → 普通 Git export/import → contextpack export，恢复后的历史和包字节完全一致。wheel 在临时目录安装后，CLI 版本、服务端及新增模块导入通过，新增网页静态文件完整。证据：`.artifacts/maturity-distribution-verification.json`。下载入口的浏览器验收也重新确认了最终 0.7.0 单文件校验与执行。

## 真实 Codex 隔离验证

运行 `python3 scripts/verify_codex_timeline.py --output .artifacts/maturity-codex-timeline.json`，使用 Codex **0.153.4**、空存储和禁网隔离环境。用户/助手合成正文包含中文、代码、连续助手消息和末尾用户消息。

新进程磁盘读取、resume、会话列表发现、再导出正文一致、接收端权限保留和已有状态保护均通过；模型调用为 **0 次**。产物中的 `/home/user/.codex` 是隔离环境的挂载路径，并非读取真实用户历史。支持范围详见 [Codex 时间线研究记录](../research/2026-10-10-codex-native-timeline.md)。

## Windows 状态与后续

远端 `ceda7f5` 的 Windows 11 AMD64 / Python 3.12、3.13 全量，以及 3.14 CLI 验收证据属于 **0.6.0**，详见 [Windows 重构验证](2026-10-09-windows-cli-refactor.md)，不作为 0.7.0 原生验收。

本轮 Linux 上运行 PowerShell 7.5.3 安装器核心测试；DPAPI、NT 句柄、原生 EXE、junction 和 PowerShell 5.1 等 Windows 专用项目跳过。Wine 9 对基线及新增 NT 简单名称重命名均报 WinError 161，未将 Wine 结果计作 Windows 通过，也未降低文件保护策略绕过它。

既有 `.github/workflows/cli-cross-platform.yml` 运行 Ubuntu/Windows × Python 3.12/3.13/3.14 全量回归，包含本轮新增的 Python 测试。PR 保持该工作流与上游一致：当前 OAuth 登录缺少修改工作流的 `workflow` scope，原本接入结构化结果与 Node 竞态检查的工作流增强另存本地补丁，新增验收脚本仍包含在 PR 中并已在本地执行。无需直接获得上游写权限即可通过 Fork 提交 PR；远端运行或首次贡献审批由仓库 Actions 设置决定。原生通过前保持预览；Windows 10、ARM64 仍需单独界定支持范围。

部署时需同时升级 Hub 和 CLI，停服务备份整个数据目录，再验证迁移和公开入口；本轮未执行部署。
