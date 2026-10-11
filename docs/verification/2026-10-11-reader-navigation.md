# 阅读位置与所选代码恢复验收

## 实现范围

- “复制当前位置”固定仓库版本，并携带当前对话、轮次、分页、仓库标签、代码快照和文件路径。刷新、打开分享链接、浏览器前进与后退可恢复对应位置。“复制版本链接”仍只固定整版内容。
- 消息的“采集时的代码”入口保留来源，在代码页可“返回来源消息”。来源说明明确：关联记录采集时状态，快照 diff 不能证明单条消息产生了哪些修改。
- “恢复所选代码”生成固定版本的 `ctx clone` 和指定消息或快照的 `ctx restore` 命令。一个轮次涉及多个快照时，需先选择具体消息；未关联代码的轮次不自动恢复最新版。草稿、本地未发布内容和失效 Git 检查点不生成可执行恢复命令。
- Bash 命令用 `&&` 连接，克隆或切换目录失败即停止。消息和对话参数使用 `--参数='值'` 形式，支持以 `-` 开头的合法 ID；参数中的单引号按 shell 规则转义。
- URL 参数只在当前版本中的对话、快照和文件范围内解析。错误位置提示并规范化，未匹配的文件路径不触发文件请求。私有库读取及下载登录规则继续由服务端控制。

## 验证方式

新增 `scripts/verify_reader_navigation_web.py`：启动隔离的临时 Hub，构建两个代码快照、多个轮次、长轮次和中文文件名，通过 Chromium 操作真实页面。测试使用合成账户与合成内容，不读取真实 Codex 会话或操作线上数据。

上述新增浏览器验收通过，覆盖位置复制、重新打开、刷新、单次导航对应单条历史记录、来源返回、历史版本固定、错误参数、延迟文件响应、草稿与行内编辑保护，以及 320 / 390 / 768 像素宽度。复制的恢复命令还通过独立 CLI 配置实际执行，逐字节核验恢复的是较早快照。

额外验证通过：使用以 `-` 开头的对话和消息 ID 完成真实 CLI 恢复；以临时 `ctx` 测试程序模拟克隆失败，实际运行复制的 Bash 命令，确认即使同名工作目录已经存在也不会执行恢复。

既有浏览器验收 `verify_code_web.py`、`verify_repository_web.py`、`verify_versions_web.py`、`verify_drafts_web.py`、`verify_contributions_web.py` 均通过；四个修改后的 JavaScript 文件语法检查及 `verify_draft_races.js` 五项草稿并发检查通过。

## 运行服务

本轮为静态资源修改，无需重启服务。通过带现有代理 Host 头的本机源站请求核验八个页面、脚本和样式资源，均与磁盘内容逐字节一致，响应带 `Cache-Control: no-store`。该检查覆盖源站，不代表公网 Cloudflare 路径的端到端验收。

机器记录与截图位于 `.artifacts/reader-navigation-web-verification.json`、`.artifacts/reader-navigation-origin-verification.json`、`.artifacts/reader-navigation-desktop.png` 和 `.artifacts/reader-navigation-mobile.png`。
