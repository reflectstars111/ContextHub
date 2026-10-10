# 0.9.0：直接采集与 Git 式暂存

用户在实际打包本项目开发对话后指出，查询会话 ID、手工导出 `.contextpack`、再上传的流程不如 Git 方便，并选择保留 `add → commit → push` 的操作习惯。

## 已实现

- `ctx add --codex` 从本机 Codex API 直接采集、辅助脱敏并暂存。优先显式 ID、当前会话，再选择工作区目录唯一匹配；不自动选最近的其他项目。
- `ctx add .` 刷新当前分支的已跟踪会话；无参数 `ctx add` 刷新绑定或首次识别当前会话。当前会话内部执行时自动排除正在执行的最后一轮，避免采集操作把自身不断加入快照。
- `commit` 仅提交暂存区，`status / commit / push` 不隐式调用 Codex。重复采集不会因导出时间产生空版本。
- 本地绑定与工作树共用原子发布点，按分支保存。人工修改或删除阻止自动覆盖；`add . --no-refresh` 允许离线暂存人工版本，显式 `--replace` 才恢复原生内容。
- 绑定不进入上传包、Git 历史交换或 clone。来源核验会逐次检查 Codex 返回的会话 ID；采集只允许读取类 RPC。
- 源码目录新增可直接执行的 `./ctx`，单文件客户端包含新采集模块，网站教程及下载包更新为 0.9.0。

上一轮真实打包还发现了首个 interrupted 轮次使导出变成空包的问题。本轮保留该修复：导出已结束的 completed/interrupted/failed 轮次，中断/失败保留部分正文提示；运行中或未知状态仍停止，二次读取校验稳定快照。空正文导出失败；`--before-turn` 可显式选择截断边界。

## 验证证据

- 全套 `python3 -m unittest discover -q`：467 项，440 通过、27 因平台环境跳过。跳过包含 20 项 Windows 原生测试与 7 项可选 PowerShell 测试。
- 随后新增会话 ID 错配校验；采集和 CLI 集成定向 21 项通过。版本及下载更新后，`tests.test_distribution tests.test_capture tests.test_ctx_capture` 共 23 项通过。
- 浏览器 `scripts/verify_cli_web.py`：桌面/移动端教程、实际下载、SHA-256 和下载文件执行通过，无 JavaScript 错误。
- 当前真实开发会话：自动识别，无显式线程 ID，采集 207 条可见消息。通过源码入口执行 init/add/commit；通过生成的单文件客户端重复 add，无额外暂存差异；随后登录临时 Hub、私有 push、clone，包字节完全一致，克隆工作区没有本机来源绑定。测试未生成中间导出文件、未修改原生会话、未向公网上传私人对话。证据位于 `.artifacts/ctx-capture-verification.json`。

## 当前项目与部署

本项目根目录已初始化本地对话工作区并保存首个 ctx 提交，含上述 207 条消息，尚未绑定公网仓库。Git 忽略规则已排除本机 `.contexthub/`、`library.json`、`conversations/`，这些真实对话未加入代码 Git。

服务在原项目目录运行，数据备份后重启。公网 `/api/config` 已核验为 0.9.0，`/cli` 展示新流程，`/ctx.pyz` 与本地通过验证的归档完全一致：

```text
size:   99980 bytes
sha256: c066504325a27ad9203442a7d555665649673454ed64f171e8f19a0d553dab4f
```

下载返回 `Cache-Control: no-store`，匿名对话下载仍为 401，应用与隧道服务均 active。部署证据位于 `.artifacts/ctx-v09-public-verification.json`。

## 验证边界

直接采集目前接入 Codex；Claude、DeepSeek 等仍通过已有文件导入。采集保存可见正文，不重放工具、不恢复原生运行状态，也不调用模型。原生恢复沿用已有版本兼容限制。0.9.0 的新增功能未在原生 Windows 环境重新验收，保留预览标记。

来源记录仍保留首次导入的声明与后续编辑审计；每次采集的最新截断提示在命令输出和 `--json` 回执中提供。拉取、合并或人工修改之后，若本地跟踪摘要不再匹配，需要明确处理编辑后再恢复自动刷新。新工作区状态使用 0.9.0 或更新 CLI。
