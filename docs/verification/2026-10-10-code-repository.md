# 0.10.0：代码与对话仓库验收

## 实现范围

- 对话按会话、日期、轮次组织，默认仅展开最新轮次；长轮次每页最多 20 条消息。对话、代码、版本历史、贡献请求、设置采用互斥标签页。
- `ctx add` 在 Git 项目中默认采集工作树代码，包括未提交修改及未被忽略的未跟踪文件，不修改源 Git HEAD/index。显式 `--no-code` 保留当前代码快照；`--code` 可指定另一代码目录。
- `.contextpack` 以 `code/index.json` 和按 SHA-256 寻址的原始 blobs 携带代码快照、父关系和消息关联。上传、下载、Fork、版本发布、草稿候选、分支合并和 Git 格式互通均保留代码。
- 网页提供固定版本、固定快照的文件树、文本预览、二进制提示和真实文件 diff。公共代码只读访问遵守公共仓库规则；私有代码仅所有者可读，完整包及其他导出仍需登录。
- `ctx restore HEAD --to 新目录` 无需原代码仓库，支持 `--snapshot` 或 `--message`，原子创建 `code/`、完整 `conversations/` 及回执。原生 Codex 导入可选，沿用版本与重试限制。

## 关联语义

代码关联记录的是采集时的工作树，不能证明历史消息生成时使用的文件。采集只给所选对话最新一轮添加保存时关联，旧消息没有记录则显示“未记录当时代码”。导入转换标记改称“导入与处理记录”，不再冒充代码变更。

Codex RPC 实测提供 `startedAt`。新采集保存真实轮次开始时间，并明确它不是逐消息发送时间；缺失时间不推测。旧版本中的来源记录保持不可改写，新导出包可包含完整历史轮次日期。

## 测试

最终执行 `python3 -m unittest discover -s tests -q`：**512 项，485 项通过，27 项环境条件跳过**，耗时约 142 秒。跳过项涉及 Windows/PowerShell，0.10.0 保留 Windows 预览标记。

实际浏览器验证通过：`verify_web.py`、`verify_code_web.py`、`verify_versions_web.py`、`verify_repository_web.py`、`verify_contributions_web.py`、`verify_drafts_web.py`、`verify_public_access_web.py`、`verify_cli_web.py`。覆盖互斥标签、日期目录、原索引编辑、候选代码自动保存/恢复/发布、固定历史版本、真实 diff、文本注入防护、移动端和下载登录边界。

审查与实际运行发现并修复的回归：

- 工作区非快进合并的 revision 重建必须携带合并后的代码，防止丢失另一分支文件。
- 导入内嵌代码包时遵守显式 `--no-code` / `--code` 优先级。
- 每个快照按展开文件总量计入 64 MiB 限制，相同 blob 被多个路径引用不能绕过恢复体积上限。
- 从源仓库内部子目录执行命令时，Git 枚举显式定位仓库根目录，防止漏采文件或漏查未跟踪文件。

代码内容、哈希、大小、父关系和路径均经过验证；拒绝链接、子模块、LFS 指针及不安全路径。上限为 128 个代码快照、10,000 个文件或 blob、单 blob 16 MiB、唯一 blob 总量和单快照展开总量各 64 MiB，整个包解压总量仍为 100 MiB。公共服务的请求上限以 `/api/config` 为准。

## 本项目真实往返

使用构建后的 `ctx-0.10.0.pyz`，明确选择本项目 Codex 会话和本代码目录，执行 `init → add → commit → export → restore`，自动排除当前执行中的轮次。

- 本地上传包：`.artifacts/contextpacks/ContextHub-development-20261010-v010.contextpack`
- 217 条可见消息，217 条均有真实轮次时间；143 个代码文件，代码原始字节共 1,663,346。
- 包大小 621,294 字节；SHA-256：`af260268d0999a012ef456f02786345c0e504f33cda9fb2dc5d15b5bef6a5c71`。
- 单个初始代码快照，最新一轮 10 条消息关联此采集时快照，未给全部历史消息补造代码。
- 恢复后的每个文件均与包内 blob 逐字节一致，`pair_status=complete`。此次未请求原生 Codex 导入，未调用模型。
- 真实用户内容仅用于本地验证，未上传到公开仓库；原始项目工作区与 Git 状态保留。

CLI 构建包 `.artifacts/ctx-0.10.0.pyz` 大小 115,741 字节，SHA-256 为 `b908a7ece07e38b9ed305774d220fd0c9c5e1662bcba0e996304d9824d564b04`。

机器可读记录：`.artifacts/code-repository-tests-final.log`、`.artifacts/code-web-verification.json`、`.artifacts/code-v010-project/verification.json`、`.artifacts/code-v010-public-verification.json`。截图在 `.artifacts/code-conversation-desktop.png`、`.artifacts/code-browser-desktop.png` 和 `.artifacts/code-conversation-mobile.png`。

## 运行更新

更新前备份：`.artifacts/backups/20261010-161109-before-code-v010/data/`，SQLite 使用备份 API 保留一致快照。重启现有 `contexthub.service` 加载 0.10.0，隧道、数据目录和权限配置保持原有部署。上线检查核对版本能力、静态脚本、CLI 实际下载哈希、`no-store` 及匿名下载 401。

此版本提供代码快照托管、文件级三方合并和 Git 格式交换；同文件冲突需要人工解决，尚未提供 Git wire protocol 服务或 GitHub 全部功能。
