# ContextHub：GitHub for AI Conversations

> 一个面向 AI 对话的开源版本控制平台，让高质量 AI 对话可以被保存、共享、复用、Fork 和持续演化。

面向 **vibe coding**，ContextHub 把开发对话和对应代码保存在同一个版本中。用配套终端工具 **`ctx`** 采集、提交和同步，网页负责阅读、代码浏览、版本比较与协作；也可以只管理对话。

日常保存只需 **`ctx add .` → `ctx commit -m "说明"` → `ctx push`**，代码的未提交修改也能一起保存，无需先手动导出对话包。

[访问 ContextHub](https://contexthub.reflectstars.dev/) · [完整终端指南](docs/cli.md) · [多平台导入](docs/import-formats.md) · [自部署](#快速启动)

当前版本 **0.10.0**。网页按日期和轮次整理对话，提供独立的对话、代码、版本历史、贡献请求和设置页面；部署版本以 `/api/config` 为准。[0.10.0 验证记录](docs/verification/2026-10-10-code-repository.md)

## 终端快速上手

### 1. 安装 ctx

需要 **Python 3.12+**；采集项目代码需要 **Git**，直接读取本机 Codex 对话还需要安装 **Codex CLI**。`ctx` 本身无需第三方 Python 包，适合 Linux、远程服务器和 VS Code 终端。

**Linux：下载、校验并安装单文件工具。**

```bash
mkdir -p ~/.local/bin
curl -fS https://contexthub.reflectstars.dev/ctx.pyz -o ctx.pyz
curl -fS https://contexthub.reflectstars.dev/ctx.pyz.sha256 -o ctx.pyz.sha256
sha256sum -c ctx.pyz.sha256 && install -m 755 ctx.pyz ~/.local/bin/ctx
export PATH="$HOME/.local/bin:$PATH"
ctx --version
```

把 PATH 设置加入 `~/.bashrc` 后，新终端也能直接使用 `ctx`；升级时重复上述下载和校验步骤。在本项目源码目录可直接用 `./ctx`，或运行 `python3 -m contexthub.ctx`。

<details>
<summary>Windows：PowerShell 安装（原生预览版，无需 WSL）</summary>

使用带 pip 或 ensurepip 的完整 Python 3.12+。下载安装脚本，查看内容后运行：

```powershell
Invoke-WebRequest https://contexthub.reflectstars.dev/install-ctx.ps1 -OutFile .\install-ctx.ps1
Get-Content .\install-ctx.ps1
.\install-ctx.ps1
ctx --version
```

安装到当前用户目录，无需管理员权限。脚本不修改 PowerShell 执行策略；无法运行脚本时，可按[Windows 安装指南](docs/cli.md#windowspowershell)下载并校验 `ctx.pyz`，用 `py -3 .\ctx.pyz` 运行。0.10.0 新增功能尚未完成原生 Windows 验收，继续保留预览标记。

</details>

### 2. 第一次保存项目

先在 [Hub](https://contexthub.reflectstars.dev/) 注册账号。进入要保存的 Git 项目根目录，将 `YOUR_USERNAME` 换成自己的用户名：

```bash
cd /path/to/your-project
ctx auth login --username YOUR_USERNAME   # 交互输入密码，登录状态会保存
ctx init --title '项目开发记录'            # 每个项目只需初始化一次
ctx add --codex                          # 采集本机会话及当前 Git 工作区代码
ctx diff --staged                        # 检查即将保存的对话和代码差异
ctx commit -m '保存第一版开发进度'          # 保存为本地版本
ctx push                                 # 首次创建私有库，后续推送新版本
ctx remote                               # 查看对应的网页链接
```

`add` 采集并暂存，`commit` 保存本地版本，`push` 上传已提交版本。采集使用当前工作树的文件内容，包含尚未执行 `git commit` 的修改；未跟踪文件遵循 Git 忽略规则。没有 Git 仓库时只保存对话，并给出提示。

`ctx init` 会创建 `.contexthub/`、`library.json` 和 `conversations/`，保存代码后还会生成 `code.json`。若项目同时使用 Git，请把这些根目录路径加入 `.gitignore`；它们由 ctx 管理，本项目已配置对应忽略规则。

**有多段 Codex 对话时，先选择要跟踪的会话：**

```bash
ctx codex list
ctx add --codex --thread THREAD_ID        # 将 THREAD_ID 换成列表中的会话 ID
```

在 Codex 内部执行时，会优先识别当前会话；普通终端只自动选择项目目录内唯一匹配的会话。首次用 `add --codex` 绑定来源，后续 `add .` 刷新已绑定的对话。在当前会话内部采集时，会排除正在执行的这一轮，下一次再收录。

### 3. 日常保存：add → commit → push

继续开发后，在同一项目中执行：

```bash
ctx add .
ctx diff --staged
ctx commit -m '完成登录功能'
ctx push
```

初始化和登录无需每次重复；登录会话过期后再登录。本地采集和提交不需要 Hub 网络连接，联网后再 push。`commit` 和 `push` 不会自动收录 `add` 之后的新改动。

按需要调整采集方式：

| 需要做什么 | 命令 |
| --- | --- |
| 更新已跟踪对话，同时保存代码 | `ctx add .` |
| 指定另一个 Git 代码目录 | `ctx add . --code ../my-code` |
| 仅更新对话，保留已保存的代码快照 | `ctx add . --no-code` |
| 暂存本地对话编辑并采集代码，跳过 Codex 刷新 | `ctx add . --no-refresh` |
| 只暂存本地对话编辑 | `ctx add . --no-refresh --no-code` |

在 VS Code 中手动修改受管对话后，用 `--no-refresh` 跳过原生会话刷新，保留并暂存自己的编辑。需要新建公开库时，在**首次**推送使用 `ctx push --visibility public`；已有库的公开性在网页设置中修改。上传前用 `ctx diff --staged` 检查内容，自动脱敏和凭据文件排除只是辅助。

### 4. 换电脑或复用他人的库

在另一台机器登录，复制网页分享链接，将下面链接中的 `PACKAGE_ID` 换成实际库 ID：

```bash
ctx auth login --username YOUR_USERNAME
ctx clone 'https://contexthub.reflectstars.dev/?package=PACKAGE_ID' my-copy
cd my-copy
ctx log
ctx pull                                  # 同步这个库的最新版本
```

`clone` 下载所选版本作为本地起点，不复制全部远端提交历史。它可以包含已记录的代码快照；**包内有代码时**，用下面的命令还原为可直接查看和使用的文件：

```bash
ctx restore HEAD --to ../restored-project
```

目标目录必须不存在；内嵌代码无需原 Git 仓库即可恢复：

```text
restored-project/
  code/             # 采集时的代码文件，包含保存下来的未提交修改
  conversations/    # 保留完整原始包的 ctx 对话工作区
  receipt.json      # 恢复回执
```

要从某个本地版本或消息关联恢复，可替换 `HEAD`，或选择包内快照：

```bash
ctx --json log                           # 查看完整版本 ID
ctx restore COMMIT_ID --to ../restored-version
ctx restore HEAD --snapshot SNAPSHOT_ID --to ../restored-snapshot
ctx restore HEAD --message MESSAGE_ID --conversation CONVERSATION_ID --to ../restored-message
```

`COMMIT_ID` 使用 `ctx --json log` 中的完整 `id`，不能使用普通 `ctx log` 显示的缩写；快照和消息 ID 可从已导出的包中查看。代码关联表示**采集时**的状态，旧消息没有记录的历史代码不会被推测补齐；`--snapshot` 和 `--message` 二选一。恢复生成源文件，不复制原项目的完整 Git 历史。

**想基于他人的库继续保存和上传，使用 Fork：**

```bash
ctx fork 'https://contexthub.reflectstars.dev/?package=PACKAGE_ID' my-fork
```

Fork 默认创建自己的私有副本；只有库的所有者可以向它 push。公开库可匿名阅读，但 clone、Fork、上传、下载均需登录。

本机 Codex 跟踪关系不会随 clone/Fork 传播。继续开发时，在克隆或 Fork 的 ctx 工作区用 `ctx add --codex --thread THREAD_ID --code 代码目录` 绑定自己的会话，并采集指定代码目录。指定的代码目录须是 Git 工作区；恢复出的 `code/` 仅包含源文件，如需继续采集，先在该目录执行 `git init`，或改用已有的 Git 项目目录。

`--code` 目录不会被记住：以后在此工作区保存外部代码时，每次都需指定，例如 `ctx add . --code ../restored-project/code`。

<details>
<summary>恢复代码后，还想把对话导入自己的 Codex？</summary>

在安装了 Codex CLI 的机器上，恢复时显式添加 `--codex`。例如使用上下文注入方式：

```bash
ctx restore HEAD --to ../restored-codex --codex --mode context
```

多段对话时加 `--conversation CONVERSATION_ID`，按回执中的新会话 ID 使用 `codex resume 会话ID` 继续。`context` 注入已有正文，不保证恢复原生聊天时间线；`--mode timeline` 重建文本时间线，目前仅支持已验证的 Codex **0.153.4**。两种方式均不还原附件、工具执行状态或源机器权限。详见[代码与对话一起恢复](docs/cli.md#代码与对话一起恢复)。

</details>

### 5. 导入 Claude、DeepSeek，或导出一个上传包

在已初始化的 ctx 工作区内，直接添加已有的对话导出文件：

```bash
ctx inspect ../exports/claude.json --platform claude
ctx add ../exports/claude.json --platform claude
ctx add ../exports/deepseek.json --platform deepseek
ctx diff --staged
ctx commit -m '整理不同平台的开发对话'
ctx push

# 导出最近一次已提交的完整版本，供网页上传或离线传递
ctx export ../project.contextpack
```

也可 `ctx add ../downloaded.contextpack` 导入已下载的包，再 `commit` 保存。含代码的包需逐个导入，默认保留包内代码；显式 `--no-code` 保留本地旧代码，`--code 路径` 则采集指定代码目录。导入器提示内容遗漏时，先预览报告，确认接受后加 `--confirm-import`。格式详情见[多平台导入指南](docs/import-formats.md)。

`ctx export` 后直接跟输出路径，**不加 `-o`**；输出包含最近已提交的对话和已保存代码，已有文件不会被覆盖。`ctx codex export` 则直接读取原生会话，只导出对话，参数和用途不同。

### 常用命令速查

| 操作 | 命令 |
| --- | --- |
| 查看 ctx 暂存状态、当前分支、待推送提交 | `ctx status` |
| 检查暂存的代码与对话变化 | `ctx diff --staged` |
| 查看受管文件相对暂存区的变化 | `ctx diff` |
| 查看本地版本记录 | `ctx log` |
| 查看绑定的 Hub 与网页链接 | `ctx remote` |
| 检查登录状态 / 退出登录 | `ctx auth status` / `ctx auth logout` |
| 列出分支 / 新建并切换分支 | `ctx branch` / `ctx switch -c experiment` |
| 切回主分支 / 合并实验分支 | `ctx switch main` / `ctx merge experiment` |
| 导出到新的普通 Git 仓库 | `ctx git export ../context-git` |
| 从 ctx 格式的 Git 仓库导入到新工作区 | `ctx git import ../context-git ../context-copy` |
| 获取机器可读的状态 | `ctx --json status` |
| 查看完整帮助 / 某个命令的参数 | `ctx --help` / `ctx add --help` |

分支操作管理 ctx 中的代码与对话版本；需要将代码文件恢复到目录时使用 `restore`。新分支首次 push 会创建独立的 Hub 库，不继承原分支的远端绑定。代码合并按文件进行，同一文件双方修改会报告冲突。普通 Git 交换使用 ctx 导出的受管格式，Hub 尚未提供 Git wire protocol 服务。

使用自部署站点时，登录用 `ctx auth login --hub https://hub.example.com --username YOUR_USERNAME`，再设置 `CONTEXTHUB_HUB` 环境变量；完整安装、分支、冲突处理及旧式 Git 检查点用法见 [ctx 使用指南](docs/cli.md)。

## 快速启动

需要 Python 3.12+、Git；读取或导入本机 Codex 对话时另需安装 Codex CLI。产品运行不依赖第三方 Python 包。

```bash
cd /path/to/ContextHub
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

当前导出可见时间线中已结束轮次的用户/助手文本和代码块，包括已完成、中断和失败的轮次；中断或失败轮次会标明正文可能不完整，不再因此丢弃后续历史。遇到仍在进行或状态未知的轮次时停止并提示，导出前会再次读取所选历史，拒绝变化中的快照和空正文。若正在当前会话中导出，可用 `ctx codex export --thread 会话ID --before-turn 当前轮次ID -o history.contextpack` 明确排除本轮及后续内容；指定轮次必须存在，此参数仅支持单会话。

时间线模式可重新导出已重建的正文；context 模式的注入内容不保证出现在后续时间线导出中，请保留原始包。图片、附件、工具执行状态和其他不支持的事件会生成兼容警告。`ctx codex export` 和本地连接器导出的是对话；经 `ctx add` 保存的版本还可携带代码文件。`ctx restore` 可恢复内嵌代码，纯对话包仍需自行准备项目目录。包不复制登录会话、源机器权限或隐藏系统指令；常见凭据路径会从代码采集中排除，但正文脱敏和文件排除都不能识别所有敏感内容，公开前请检查暂存差异。

## Fork、编辑与版本管理

阅读页按“对话 → 日期 → 轮次”组织，只显示选中轮次，较长轮次分页；支持搜索和前后轮切换。新采集的 Codex 记录使用源轮次的真实开始时间，已有版本不自动补填时间。没有时间的内容显示“时间未记录”。消息下的“导入与处理记录”是格式转换、脱敏或人工编辑标记；真实代码变化在“代码”页和版本比较中查看。没有关联的消息显示“未记录当时代码”。

1. 打开对话库，用“版本历史”选择要复用的版本，点击“Fork 到我的对话库”。默认创建私有副本，也可明确选择公开。
2. 在自己的最新版本点击“编辑副本”，修改标题、背景和消息，或新增、移除、移动消息。
3. 如果已经在 Codex 中继续对话并导出新的 `.contextpack`，选择“导入对话包作为下一版”，检查内容后用它替换整份草稿。平台不会自动推测不同线程之间的消息对应关系。
4. 填写保存说明，点击“预览变化”，检查后保存。公开库的按钮为“发布新版本”，保存后该版本即可被访问者阅读和下载。
5. 通过“代码”浏览目录和文件，点击消息的“采集时的代码”查看对应快照与 diff。通过“比较版本”查看代码文件及对话的增删修改、顺序变化；选择历史版本可阅读、下载或复制固定版本链接。新版本不会覆盖原始文件，Fork 的修改不会改变上游。

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

## 直接导出原生 Codex 对话

日常版本管理使用上面的 `ctx add / commit / push`。需要单独迁移原生会话时，可调用底层桥接：

```bash
ctx codex list
ctx codex export --thread THREAD_ID --title '会话备份' -o conversation.contextpack
ctx codex import conversation.contextpack --dry-run
ctx codex import conversation.contextpack --cwd /absolute/path/to/project --mode context
```

这些命令与 `python3 -m contexthub list/export/import` 对应，只迁移对话；需要代码与对话一起恢复时使用 `ctx restore`。批量导入逐条返回回执，部分失败时返回非零状态；已成功创建的会话不会回滚，重试前请查看回执。

## 开放格式与存储

`.contextpack` 是可直接解压的 ZIP：包含清单、对话库元数据、规范化 JSON、可阅读 Markdown 和兼容报告；含代码时另带 `code/index.json` 与按哈希保存的文件字节。每个文件都有 SHA-256 校验，JSON 是内容主记录；导入时验证 Markdown 与 JSON 一致。格式版本为 `0.1`。

单包最多 200 段对话、每段最多 2,000 条消息、每段 JSON 不超过 5 MiB，总解压内容不超过 100 MiB。代码最多 128 个快照、每快照 10,000 个文件、单文件 16 MiB，所有历史快照去重后的文件字节合计不超过 64 MiB；服务端请求大小限制仍单独生效。未知格式版本、路径穿越、重复条目、哈希不符和不支持的消息角色会被拒绝。

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
python3 scripts/verify_code_web.py
```

更多细节：[开发贡献指南](CONTRIBUTING.md)、[公网部署验证记录](docs/verification/2026-10-09-public-deployment.md)、[贡献回流实现计划](docs/superpowers/plans/2026-10-09-contributions.md)、[贡献回流验证记录](docs/verification/2026-10-09-contributions.md)、[版本管理验证记录](docs/verification/2026-10-09-versions.md)、[Codex 迁移证据](docs/verification/2026-10-09-transfer.md)。平台源码采用 [MIT](LICENSE)；该许可不自动适用于用户上传的对话内容。

## 产品文档

- [0.7.0 实施范围与验收要求](docs/superpowers/specs/2026-10-10-platform-maturity.md)
- [对话库早期产品与架构设计](docs/superpowers/specs/2026-10-08-contexthub-conversations-design.md)：历史设计，部分限制已在后续版本解决。
- [Codex 导入导出核查](docs/research/2026-10-08-codex-portability.md)
- [早期 Agent 构想](ContextHub_Git_for_AI_Agents_Idea.md)：历史记录，当前定位以对话库设计为准。
