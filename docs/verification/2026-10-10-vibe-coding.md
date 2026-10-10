# Vibe coding 检查点与仓库界面验收

日期：2026-10-10（Asia/Shanghai）。版本：0.8.0。功能提交：`e26133e`，最初位于 `feature/github-conversations`。随后按用户要求归并到唯一开发目录 `/ssd1/code/yqq/Git_for_ai`，当前分支为 `feature/vibe-coding`；该目录的公网服务已更新。没有合并 main 或推送本轮提交。

## 开发目录归并与公网更新

用户要求后续只在 `/ssd1/code/yqq/Git_for_ai` 开发，已写入根目录 `AGENTS.md`。此前工作树不再用于开发，验收材料复制到本目录 `.artifacts/v08-development-verification/`。

2026-10-10 14:42（Asia/Shanghai）停止应用，完整备份原 `.context-data` 到 `.artifacts/backups/20261010-144203-before-v08-directory-sync/`，切换到已验收提交后启动应用。保留数据目录与服务/隧道配置。应用和隧道均为 active，应用 WorkingDirectory 为指定目录。

公网 `/api/config` 返回 0.8.0；主页、`repository.js`、`repository.css`、CLI 校验文件均为 200，缓存状态 DYNAMIC、Cache-Control 为 no-store；匿名下载为 401。没有读取真实对话正文或写入验收账号。本目录下另外执行检查点 API、Hub 同步恢复、CLI 分发测试，共 6 项通过。证据：`.artifacts/v08-public-sync.json` 与 `.artifacts/v08-original-directory-tests.log`。

## 交付行为

- 对话包可选 `library.code_checkpoint` 绑定规范对话摘要与完整 Git commit/tree。旧版不含关联的包字节保持兼容；关联随 Hub 上传、历史版本、下载、Fork、草稿、Git 导出/导入保留。
- 对话编辑使关联过期；检查点按完整记录合并，不会拼接两个分支的 commit/tree。
- `ctx checkpoint --code PATH -m MSG` 要求已提交且干净的对话/代码。`ctx restore REF --code PATH --to NEWDIR` 在新目录原子发布 `code/`、`conversations/`、`receipt.json`，保留原项目、索引、分支和未提交编辑。
- `--codex` 可在恢复代码目录建立新原生文本会话；`--resume` 核验原恢复，禁止在原生导入结果不确定时重复创建会话。未解析原文存档可以成对保存，但不能冒充已解析的原生会话导入。
- 网页改为 GitHub 风格的仓库列表、顶部导航、owner/name、功能导航、对话目录及 About/代码关联栏。公开库匿名可读，下载与写操作登录要求、私有库仅所有者权限保持。

## 可重复验证

```sh
CONTEXTHUB_TEST_POWERSHELL=/tmp/contexthub-powershell-7.5.3/pwsh python3 scripts/verify_release.py
python3 -m unittest tests.test_code_sync tests.test_code_sync_review tests.test_code_sync_hub tests.test_distribution -q
python3 scripts/verify_code_sync.py --output .artifacts/code-sync-native.json
node scripts/verify_draft_races.js
```

全量运行 420 项：400 通过，20 项原生 Windows 检查因 Linux 环境跳过，零失败。之后补充原文存档不能直接恢复为原生会话的检查，恢复/独立审查/Hub/分发专项 25 项通过。原有包、版本、贡献、权限、存储容量和多平台导入均包含在全量回归中。

首次并行开发时的全量运行有一项打包字节确定性检查失败，原因为测试期间源文件继续变更；源代码稳定后重跑全量，检查通过。此记录只采用后一次完整结果。

独立恢复审查覆盖真实 Git 对象损坏与并发变化：commit/tree/blob 重新计算 SHA-1/SHA-256，损坏对象被拒绝；打包完成后源对象变化不会改变恢复文件，恢复从验证后的独立对象库读取。另覆盖 CRLF 差异、目录数量上限、路径链接、传输失败不发布、原生中断及禁止盲目重试。

真实 Codex 0.153.4 验证在 bwrap 空数据目录、无网络环境中执行：对话快照精确恢复、代码回到固定提交、用户/助手正文可再导出、新进程 resume 使用恢复后的代码目录、重复核验不增加会话、源编辑保留。模型调用次数为 0；未测试原生 GUI 渲染和实际模型续聊。

8 组 Chromium 验收通过：`verify_web.py`、`verify_versions_web.py`、`verify_repository_web.py`、`verify_contributions_web.py`、`verify_drafts_web.py`、`verify_public_access_web.py`、`verify_multiplatform_web.py`、`verify_cli_web.py`。运行时使用 `PYTHONPATH=/tmp/contexthub-browser-libs`。新页面额外检查了 320/390/768px 无横向溢出，历史匹配/过期状态、候选关联替换/清除、服务器草稿恢复、固定版本命令和提交链接；无 JavaScript 错误。草稿并发检查 5 项通过。

结果和截图存放在不入库的 `.artifacts/`：`release-platform-verification.json`、`code-sync-native.json`、各 `*-web-verification.json`、`repository-desktop.png`、`repository-mobile.png`、`repository-list.png`。

## 首版边界

一次检查点关联一个代码仓库。恢复代码为固定提交的独立浅仓库，不复制远端设置或完整历史。代码已需在本机取得，不自动从 GitHub 拉取或改变托管权限。代码 Diff 通过 GitHub 提交链接查看，未实现平台内双栏代码审阅或 GitHub OAuth 自动同步。

普通文件代码库上限为 50,000 项、32 MiB/文件、256 MiB 总内容和传输包。不支持子模块、符号链接、LFS 指针、冲突/保留路径名及经过换行或过滤器转换的工作文件。捕获需与 Git 对象字节一致，遇到转换时停止而非自动改写。

Windows 仍为预览版；本轮未取得原生 Windows 验收结果。Codex timeline 模式沿用 0.153.4 版本限制，附件、工具状态与原运行环境不随恢复迁移。关联元数据属于来源声明，摘要匹配不是真实性认证。
