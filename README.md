# ContextHub：GitHub for AI Conversations

> 一个面向 AI 对话的开源版本控制平台，让任何人的高质量 AI 对话都可以被保存、共享、复用、Fork 和持续演化。

当前版本 **0.7.0**：ctx 支持分支、三方合并和普通 Git 仓库导入导出；Codex 可重建原生文本时间线；网页草稿自动保存，拥有者可修改可见性和永久清理对话库。

## 在终端管理对话：ctx

面向 Linux / 远程服务器 / VS Code 终端，同时提供 Windows 10/11 原生预览版（无需 WSL）。网站提供[单文件与 PowerShell 安装教程](https://contexthub.reflectstars.dev/cli)，只需 Python 3.12+，无需第三方 Python 包。0.6.0 已完成 Windows 11 本机的原生命令、DPAPI、文件锁和安装器验证，详见 [Windows 重构验证记录](docs/verification/2026-10-09-windows-cli-refactor.md)。0.7.0 新增功能的原生 Windows 验收仍待完成，继续保留预览标记；Windows 10/ARM64 不借用 Windows 11 AMD64 的验收结论。

```bash
ctx auth login --username alice
ctx init my-dialogues --title '我的对话库'
cd my-dialogues
ctx add ../conversation.contextpack
ctx diff --staged
ctx commit -m '整理第一批对话'
ctx push
```

首次 push 默认私有。之后编辑受管 JSON，再 `ctx add .`、`ctx commit`、`ctx push`；另一台机器登录后可 `ctx clone '网站分享链接'`、`ctx pull`。复用别人的库用 `ctx fork`，Codex 本地桥接用 `ctx codex list/export/import`。工作区保留分支和不可变提交 DAG；独立修改自动合并，冲突保留双方快照。

安装、登录、分支与 Git 互通示例见 [ctx 使用指南](docs/cli.md)，本轮证据见 [0.7.0 验证记录](docs/verification/2026-10-10-platform-maturity.md)。普通 Git 互通使用显式受管格式，Git 网络传输由 Git 自身提供；Hub 的 clone 下载所选快照。

## 快速启动

需要 Python 3.12+、Git；只有本地 Codex 连接器需要安装 Codex CLI。产品运行不依赖第三方 Python 包。

```bash
cd /ssd1/code/yqq/Git_for_ai
python3 -m contexthub serve --port 8765
```

打开 <http://127.0.0.1:8765>，注册自己的账号即可使用。默认数据存放在 `.context-data/`。用户名为 3–32 位字母、数字、下划线或连字符；密码为 10–128 个字符。

另开一个终端启动本地连接器：

```bash
python3 -m contexthub connect --port 8766 --open
```

使用终端输出的完整地址打开连接器，地址中包含本次启动的令牌。Hub 和连接器是两个独立进程；连接器只监听本机，Hub 不读取服务器上的 Codex 会话。

## Claude、DeepSeek/dsh 与其他平台

Hub 的「上传对话包」现在也可选择导出 JSON/JSONL 或受支持的导出 ZIP，先检查内容再保存。支持 Claude 网页对话、Claude Code 文本日志、ChatGPT 当前分支、DeepSeek 通用消息/文本片段，以及 DeepSeek Harness（dsh）的受支持会话事件。平台分类为 **Codex、ChatGPT、Claude、DeepSeek、其他**；混合来源库可出现在多个分类中，版本与 Fork 保留来源记录。

默认自动识别格式。通用 `messages` 结构无法确定来源时可手选平台；不能仅凭文件名判断。遇到图片、附件、工具、思考或分支等不能转换的部分，会提示遗漏并要求确认后保存。未知 UTF-8 JSON、JSONL、文本或 Markdown 可手动切换为「原文存档」，仍可分类、阅读和下载；不会把未解析原文当作已恢复的会话。

新上传默认私有，所有下载和写操作仍须登录。原生转换保存受支持正文，原文存档则保留完整文本；二者均不承诺能回导原平台继续聊天。文件结构、导出入口、大小限制和命令行示例见[多平台导入指南](docs/import-formats.md)，测试与上线证据见[多平台验证记录](docs/verification/2026-10-09-multiplatform.md)。

## 把对话交给另一个人

1. 在自己的本地连接器中勾选 Codex 会话，预览正文、编辑或移除不想分享的消息，再下载 `.contextpack`。
2. 在 Hub 登录，选择迁移包并查看预览，再决定“仅自己”或“公开分享”。新上传默认私有。
3. 对方可匿名阅读公开库，注册或登录后下载完整对话包。私有库仅所有者登录后可见，其他账号不能访问。
4. 对方在自己的电脑上启动连接器，选择下载的包、检查内容，填写已存在的项目目录，确认导入。
5. 按导入回执中的新会话 ID 使用 `codex resume <会话ID>`。继续对话时由对方自己的 Codex 配置和模型服务处理。

**Codex 恢复方式：**默认 `--mode timeline` 在已验证的 Codex **0.153.4** 中创建新会话，重建用户/助手正文时间线，并以独立进程检查磁盘读回和 `resume`。合成正文也已通过列表与再导出验证，不调用模型。其他版本会在写入前拒绝；可显式选择 `--mode context` 使用原有上下文注入。时间线重建不复制原始 ID、回合边界、时间、附件、工具状态或源机器权限，也不宣称 GUI 或模型续聊已验证，详见[原生文本时间线验证](docs/research/2026-10-10-codex-native-timeline.md)。

当前导出公开时间线中已完成的用户/助手文本和代码块，遇到未完成或失败的轮次时停止并提示；保留可用的来源标记。时间线模式可重新导出已重建的正文；context 模式的注入内容不保证出现在后续时间线导出中，请保留原始包。图片、附件、工具执行状态和其他不支持的事件会生成兼容警告。迁移包不携带账号凭据、源机器权限、系统指令或项目文件；继续代码任务前需自行取得对应项目文件。自动脱敏只是辅助，公开前应检查正文。

## Fork、编辑与版本管理

1. 打开对话库，用“版本历史”选择要复用的版本，点击“Fork 到我的对话库”。默认创建私有副本，也可明确选择公开。
2. 在自己的最新版本点击“编辑副本”，修改标题、背景和消息，或新增、移除、移动消息。
3. 如果已经在 Codex 中继续对话并导出新的 `.contextpack`，选择“导入对话包作为下一版”，检查内容后用它替换整份草稿。平台不会自动推测不同线程之间的消息对应关系。
4. 填写保存说明，点击“预览变化”，检查后保存。公开库的按钮为“发布新版本”，保存后该版本即可被访问者阅读和下载。
5. 通过“比较版本”查看增删修改及顺序变化；选择历史版本可阅读、下载或复制固定版本链接。新版本不会覆盖原始文件，Fork 的修改不会改变上游。

编辑中的正文与库信息会自动保存为仅拥有者可见的服务器草稿，刷新后可恢复，尚未填写发布说明也可保存。草稿不是正式版本；多标签页通过递增 revision 检查防止互相覆盖，冲突时保留本页内容并可下载备份。发布只清理匹配的那份草稿；离开前若尚有未同步内容会提示。

拥有者可在“对话库设置”中切换公开/私有，变更适用于此库的全部历史版本。公开变为私有不能撤回已下载内容或独立 Fork。发布会检查编辑时确认的可见性，防止跨页面权限变更让私有编辑被意外公开。私有来源只能由所有者 Fork 为私有副本。Fork 复制所选快照并记录来源版本，不复制全部历史；上游删除后，该独立副本仍保留，来源信息会隐藏。

差异摘要统计全部变化；正文详情最多显示 200 条消息，每段最多 4,000 字符，超出时明确提示下载完整版本。服务端保留已有来源并添加人工编辑记录，但来源声明不等于模型真实性认证。

## 把改进贡献回上游

1. 从公开库的固定版本创建公开 Fork，在自己的 Fork 中编辑并保存新版本。
2. 选择要贡献的版本，点击“贡献给上游”，填写标题和说明。提案固定这个源版本及原始 Fork 起点；之后继续编辑 Fork 不会自动改变已提交的内容。
3. 上游拥有者打开贡献请求，查看相对 Fork 起点的变化，然后点击“检查合并”。平台对共同起点、上游当前版本和固定源版本进行三方比较，生成候选或列出冲突。
4. 若可合并，点击“查看本次合并变化”，核对将发布的准确内容；需要完整内容时下载候选包。确认后点击“合并并发布新版本”，上游得到一个新版本，原有历史与 Fork 保持可访问。

当前只支持**公开的直接 Fork 向它的公开上游**贡献，私有库不参与。源库拥有者可提交或“更新提案版本”；上游拥有者才可检查和合并。双方均可评论、关闭或重新打开请求，其他人可匿名阅读提案和讨论。每条请求最多 200 条评论，每条最多 4,000 字符；已合并的请求不可重开。

更换提案版本或关闭后重开都会清除旧检查。上游在检查后发布了新版，则候选失效，必须重新检查和审阅；源库发布未纳入提案的其他版本不会使检查失效。合并直接采用已检查候选的包字节与 Git 提交，保留上游和源版本两个父提交；重复确认不会重复创建版本。

合并按稳定 ID 和字段进行：独立修改可组合，双方对同一字段的不同修改、删除与编辑或无法确定的顺序变化会阻止合并。当前没有图形冲突解决器；双方需讨论后在 Fork 中编辑、保存新版本，再显式更新提案并重新检查。必要时需基于最新上游重新 Fork 并提交新提案。任何依赖库被删除后，相关贡献、候选下载和合并操作都会被阻断。

## 命令行使用

```bash
# 列出本机 Codex 会话；可另加 --archived、--subagents
python3 -m contexthub list

# 一次导出多段对话，默认辅助脱敏
python3 -m contexthub export \
  --thread THREAD_ID_1 --thread THREAD_ID_2 \
  --title '我的对话库' -o my-library.contextpack

# 只检查，不写入 Codex
python3 -m contexthub inspect my-library.contextpack
python3 -m contexthub import my-library.contextpack --dry-run

# 导入为多个新会话，目标目录必须已存在
python3 -m contexthub import my-library.contextpack --cwd /absolute/path/to/project

# 上传；密码交互输入，省略 visibility 时为私有
python3 -m contexthub upload my-library.contextpack \
  --url http://127.0.0.1:8765 --username alice --visibility public

# 下载公开或自己的私有包，均须登录；密码交互输入
python3 -m contexthub download \
  http://127.0.0.1:8765/api/packages/PACKAGE_ID/download \
  --username alice -o downloaded.contextpack

# 生成两段合成对话，可用来体验上传下载
python3 -m contexthub demo -o demo.contextpack
```

现有文件默认不覆盖。批量导入逐条返回回执，部分失败时命令返回非零状态；已成功创建的会话不会回滚，重试前请查看回执。

## 开放格式与存储

`.contextpack` 是可直接解压的 ZIP：包含清单、对话库元数据、规范化 JSON、可阅读 Markdown 和兼容报告。每个文件都有 SHA-256 校验，JSON 是内容主记录；导入时验证 Markdown 与 JSON 一致。格式版本为 `0.1`。

单包最多 200 段对话、每段最多 2,000 条消息、每段 JSON 不超过 5 MiB，总解压内容不超过 100 MiB。未知格式版本、路径穿越、重复条目、哈希不符和不支持的消息角色会被拒绝。

Hub 使用 SQLite 保存账号、索引、不可变版本、来源关系、贡献请求及评论，按内容哈希保存原始上传包与合并候选。下载选定版本保留该版的字节内容；网页重复上传相同内容、相同可见性的现有包会复用记录。ctx 为不同本地分支使用独立新建标识，相同内容也能创建独立库，并保证重试幂等；这需要同时升级到支持该能力的 Hub。

公开版本建立带父提交的内部 Git 历史，公开 Fork 连接来源的公开提交，贡献合并保留两个父提交。私有内容不新建 Git 提交；公开转私有会撤销公开引用和访问，曾公开的对象可能保留到永久清理，或因其他 Fork 引用而继续保留。私有转公开仅为当前快照建立新的 Git 根提交，HTTP 历史则按设置页明确提示全部公开。数据库事务决定新版本是否发布，Git refs 可由数据库恢复。

升级现有 0.1 或 0.2 实例前先停止服务并备份数据目录，然后使用原来的 `--data` 路径启动。0.1 的已有上传记录会自动迁移为 v1，保留库 ID、原包字节、创建时间和 Git 提交；0.3 会增加贡献与评论表。迁移可重复运行，不会再次生成初始版本。

“删除”仍撤销站点访问并保留存储。拥有者可在设置中输入库 ID 确认“永久清理”，删除库记录、版本、私有草稿、相关贡献和独占包文件，并清理不再被引用的 Git 对象。其他库或 Fork 所需的共享内容保留，清理失败可通过同一 API 重试；独立运维备份不在在线清理范围。数据目录仍应按私人应用数据管理，不要公开为静态目录。

## 自部署与当前范围

```bash
python3 -m contexthub serve --host 0.0.0.0 --port 8765 --data /srv/contexthub-data
```

公开库允许匿名浏览、搜索、阅读；上传、下载（包括历史版本和合并候选）、Fork、编辑、发布、评论等操作均须注册或登录，并执行对应的所有权检查。网页 JSON/Markdown 导出与正文复制也会提示登录，复制分享链接不需要登录。公开内容已允许阅读，无法阻止访问者自行保存正文。

公网部署请按 [Cloudflare Tunnel 部署指南](docs/deployment/public-cloudflare.md) 配置独立 HTTPS 子域名、可信代理、缓存绕过、资源限制及备份。模板默认只监听本机，16 MiB 请求体、4 个并发连接、5 GiB 保守存储预算及 5 GiB 磁盘保留空间；这些参数可调整。备份时停止服务，保存整个数据目录，再以同一 `--data` 路径启动即可恢复。不要把本地连接器代理到公网。

### 在 Linux 服务器常驻

提供 [systemd 用户服务模板](deploy/systemd/contexthub.service)。它保留本机监听，异常退出后等待 3 秒重启，日志交由 journal 管理，CPU 调度优先级设为较低的 `Nice=10`。Hub 自身不运行模型，也不需要 GPU；没有请求时主要占用少量 Python 进程内存。上传大包、比较与合并会临时增加内存和 CPU，磁盘用量随包及版本增长。

安装时先复制模板，并将其中 `WorkingDirectory` 改为实际项目绝对路径；`.context-data` 相对于此目录，迁移现有实例时必须指向原目录：

```bash
install -Dm644 deploy/systemd/contexthub.service ~/.config/systemd/user/contexthub.service
# 编辑 ~/.config/systemd/user/contexthub.service 中的 WorkingDirectory
systemd-analyze --user verify ~/.config/systemd/user/contexthub.service
systemctl --user daemon-reload
# 首次接管前停止原先手动启动的同端口 Hub
systemctl --user enable --now contexthub.service
```

用 `loginctl show-user "$USER" -p Linger` 检查是否为 `Linger=yes`。该选项使用户服务在退出登录后持续运行，并在开机时启动；若未启用，请按服务器的管理权限执行 `loginctl enable-linger "$USER"`。本服务器已启用。

```bash
systemctl --user status contexthub.service      # 状态与资源
systemctl --user restart contexthub.service     # 更新代码后重启
systemctl --user stop contexthub.service        # 停止；手动停止不会自动重启
journalctl --user -u contexthub.service -n 50 --no-pager
```

在不带完整登录环境的自动化终端里，若出现 `Failed to connect to bus`，可为命令设置 `XDG_RUNTIME_DIR=/run/user/$(id -u)` 和 `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus`。

仅本地模式下，远程 VS Code 访问仍需 SSH 端口转发：在「端口」面板转发 `8765`，使用面板给出的本地地址打开。服务器服务常驻与浏览器侧转发是两件事；重新连接 SSH 后需恢复转发。[VS Code 官方说明](https://code.visualstudio.com/docs/remote/ssh#_forwarding-a-port-creating-ssh-tunnel) 已配置公网模式的实例直接使用 HTTPS 子域名，原 localhost 地址因 Host 校验不再可用。

已实现：账号、公开/私有库、正文搜索、批量 Codex 迁移、上传预览、阅读、JSON/Markdown 导出、完整包下载、校验、去重、不可变版本历史、固定版本链接、Fork、网页编辑、整包更新、消息差异比较，以及公开贡献请求、双方评论、三方检查与候选审阅合并。

后续平台工作：图形冲突解决、许可与发布管理、粘贴整理、更多原生适配器、站内通知与发现。当前还没有原生 Git push/clone 服务。

## 开发与验证

```bash
python3 -m unittest discover -v
python3 scripts/verify_codex_transfer.py
```

第二条使用隔离临时存储与合成内容，需要 Linux、`bwrap` 和 Codex CLI。不会批量读取真实用户对话，也不会向真实模型发送请求。

浏览器验收脚本使用可选的 Playwright 开发依赖，产品运行不需要它：

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
python3 scripts/verify_web.py
python3 scripts/verify_versions_web.py
python3 scripts/verify_contributions_web.py
python3 scripts/verify_public_access_web.py
python3 scripts/verify_multiplatform_web.py
```

更多细节：[开发贡献指南](CONTRIBUTING.md)、[公网部署验证记录](docs/verification/2026-10-09-public-deployment.md)、[贡献回流实现计划](docs/superpowers/plans/2026-10-09-contributions.md)、[贡献回流验证记录](docs/verification/2026-10-09-contributions.md)、[版本管理验证记录](docs/verification/2026-10-09-versions.md)、[Codex 迁移证据](docs/verification/2026-10-09-transfer.md)。平台源码采用 [MIT](LICENSE)；该许可不自动适用于用户上传的对话内容。

## 产品文档

- [0.7.0 实施范围与验收要求](docs/superpowers/specs/2026-10-10-platform-maturity.md)
- [对话库早期产品与架构设计](docs/superpowers/specs/2026-10-08-contexthub-conversations-design.md)：历史设计，部分限制已在后续版本解决。
- [Codex 导入导出核查](docs/research/2026-10-08-codex-portability.md)
- [早期 Agent 构想](ContextHub_Git_for_AI_Agents_Idea.md)：历史记录，当前定位以对话库设计为准。
