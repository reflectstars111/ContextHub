# ctx：ContextHub 的终端工具

适用 **Linux / 远程服务器 / VS Code 终端**，当前 0.10.0 提供 **Windows 10/11 原生预览版**，需要 Python 3.12+，无需 WSL。管理对话不需要模型服务或第三方 Python 库；默认采集项目代码需要 Git；本地 Codex 桥接另需安装 Codex CLI。Windows 11 本机已验证此前版本的 `ctx` 命令、内核路径、DPAPI、锁与安装器，详见[重构验证记录](verification/2026-10-09-windows-cli-refactor.md)。0.10.0 新增功能的原生 Windows CI 尚待运行，因此保留预览标记。

## 安装

### Windows（PowerShell）

使用带 pip 或 ensurepip 的完整 Python 安装；安装器复用其中的 Windows 原生启动器，不额外下载依赖。先运行 `py -3 --version`，确认使用 Python 3.12 或更新版本。下载安装脚本，查看内容后执行：

```powershell
Invoke-WebRequest https://contexthub.reflectstars.dev/install-ctx.ps1 -OutFile .\install-ctx.ps1
Get-Content .\install-ctx.ps1
.\install-ctx.ps1
ctx --version
```

安装器仅操作当前用户目录 `%LOCALAPPDATA%\ContextHub\bin`，不需要管理员权限；检查 Python、下载 SHA-256 和工具版本后安装 `ctx.pyz` / `ctx.exe`，将该目录加入当前会话和用户 PATH。新终端可直接使用 `ctx`。升级时重新运行安装器；验证或替换失败时保留旧版本。支持 `-PythonExecutable 'C:\具体路径\python.exe'` 指定解释器，以及 `-InstallDir` 自定义目录。

脚本不修改 PowerShell 执行策略。若策略禁止脚本，或使用不带 pip/ensurepip 的精简 Python，直接下载并校验工具后运行，无需放宽策略：

```powershell
Invoke-WebRequest https://contexthub.reflectstars.dev/ctx.pyz -OutFile .\ctx.pyz
Invoke-WebRequest https://contexthub.reflectstars.dev/ctx.pyz.sha256 -OutFile .\ctx.pyz.sha256
$expected = (Get-Content .\ctx.pyz.sha256 -Raw).Split(' ')[0].Trim()
if ((Get-FileHash .\ctx.pyz -Algorithm SHA256).Hash -ne $expected) { throw 'SHA-256 mismatch' }
py -3 .\ctx.pyz --version
py -3 .\ctx.pyz auth login --username alice
```

此方式后续用 `py -3 C:\完整路径\ctx.pyz` 替换下文的 `ctx`。路径有空格时加引号。安装器还支持离线安装 `-ArchivePath .\ctx.pyz -Sha256 校验值`；自部署可使用 `-HubUrl https://自己的Hub`，运行时的站点选择仍使用 `--hub` 或 `CONTEXTHUB_HUB`。

Windows 本地工作区和凭据目录仅支持普通本地磁盘路径；拒绝网络共享、网络映射盘、junction、符号链接和其他重解析点。不要将工作区放在依赖这些特性的同步目录中。此版本适配客户端，Hub 服务器的生产部署继续使用 Linux。

从源码在 Windows 验证或使用全部命令：

```powershell
python -m unittest discover -v
python -m contexthub.ctx --help
python -m contexthub.distribution --output .\ctx.pyz
python .\ctx.pyz --version
```

`python -m contexthub.ctx`、`ctx.pyz` 和原生 `ctx.exe` 共用命令实现；输出及重定向采用 UTF-8。PowerShell 5.1 读取保存的输出时使用 `Get-Content -Encoding UTF8`。Windows 的本地 Hub 也支持存储预算与进程间锁，生产部署建议仍按 Linux 指南执行。

### Linux

网站提供[安装教程](https://contexthub.reflectstars.dev/cli)、[单文件工具](https://contexthub.reflectstars.dev/ctx.pyz)及 SHA-256 文件：

```bash
mkdir -p ~/.local/bin
curl -fS https://contexthub.reflectstars.dev/ctx.pyz -o ctx.pyz
curl -fS https://contexthub.reflectstars.dev/ctx.pyz.sha256 -o ctx.pyz.sha256
sha256sum -c ctx.pyz.sha256
install -m 755 ctx.pyz ~/.local/bin/ctx
export PATH="$HOME/.local/bin:$PATH"
ctx --version
```

确认校验通过后再安装。可将 PATH 设置加入 `~/.bashrc`；升级时重复下载、校验和安装。`ctx.pyz` 是可查看源码的 ZIP，包含 MIT 许可证；也可以用 `python3 ctx.pyz --help` 运行。网站提供的 ctx 工具只打包产品源码，不包含服务器数据、凭据或用户上传的内容。

源码目录可直接运行 `./ctx`，无需安装；也可使用 `python3 -m contexthub.ctx`。需要安装入口时，在项目根目录运行 `python3 -m venv .venv`，然后 `.venv/bin/python -m pip install -e .`，使用 `.venv/bin/ctx`。项目尚未发布到 PyPI，不要从同名未知包安装。

## 登录与站点选择

先在 Hub 注册账号：

```bash
ctx auth login --username alice
ctx auth status
ctx auth logout

# 自部署 Hub（公网必须使用 HTTPS）
ctx auth login --hub https://hub.example.com --username alice
export CONTEXTHUB_HUB=https://hub.example.com
```

密码交互输入，不作为命令参数、不保存。自动化可用 `--password-stdin` 从安全的标准输入传入一行密码，避免将密码写入脚本或 shell 历史。会话最多有效 7 天，过期后重新登录。

Linux 会话存储在 `~/.config/contexthub/credentials.json`，配置目录权限 700、文件 600；支持 `XDG_CONFIG_HOME`。Windows 默认为 `%LOCALAPPDATA%\ContextHub\credentials.dpapi`，通过当前用户 DPAPI 加密，无明文降级；更换 Windows 账号或机器需重新登录，不能靠复制该文件迁移会话。两者均支持 `CONTEXTHUB_CONFIG_DIR` 覆盖，且按完整站点来源（协议、域名和端口）隔离。

凭据文件含登录会话，应按秘密管理，不进入工作区或上传包。HTTP 仅允许字面 loopback IP（例如 `http://127.0.0.1:8765`）；客户端拒绝重定向，避免将内容或凭据转发给其他站点。退出会清除本地令牌，并尝试撤销服务器会话。

## 直接跟踪 Codex 对话

在开发目录中首次运行：

```bash
ctx init --title '项目开发对话'
ctx add --codex                 # 当前会话 + 当前 Git 工作区代码
ctx diff --staged
ctx commit -m '保存开发进度'
ctx push

# 继续与 Codex 对话后
ctx add .
ctx diff --staged
ctx commit -m '完成一个开发步骤'
ctx push
```

`add --codex` 直接从本机 Codex 读取可见正文、辅助脱敏，并记住所选会话，不生成中间 `.contextpack`。首次选择顺序是 `--thread ID`、Codex 提供的当前会话、工作区目录完全匹配的唯一会话。找不到或有多个候选时会停止，并提示用 `ctx codex list` 和 `ctx add --codex --thread ID` 明确选择；不会自动挑选最近的其他项目。无参数的 `ctx add` 会刷新已有绑定，尚未绑定时识别当前会话。

在当前 Codex 会话内部运行时，会自动排除正在执行采集的最后一轮，下次 add 再收录；也可用 `--before-turn ID` 明确截止位置，仅支持单段会话。采集保留已结束轮次的用户与助手正文，中断或失败轮次标明可能不完整，不导入工具执行、图片或隐藏指令。不会调用模型、修改原始会话或重放工具。

后续 `ctx add .` 读取当前分支的全部已跟踪会话及当前代码文件，再一起暂存；任一读取失败都不会部分更新。相同内容不会因为导出时间变化生成空版本。`commit` 只保存暂存区；`status / commit / push` 不暗中采集新对话。

如果在 JSON 中人工改写或删除了已跟踪对话，自动刷新会停止并保留编辑。用 `ctx add . --no-refresh` 可以离线暂存人工版本，同时仍采集代码；明确决定恢复原生采集内容时，再用 `ctx add . --replace`。`--replace` 会覆盖所选已跟踪对话的当前内容，先检查 `ctx diff`。已有人工版本和拉取合并的修改也受相同保护。

跟踪绑定按本地分支保存在 `.contexthub`，不会进入上传包、普通 Git 导出或远端 clone。代码仓库内的这个目录含本机状态，不应通过代码 Git 提交；本项目的根目录忽略规则已排除 `.contexthub/`、`library.json`、`code.json` 和 `conversations/`。新状态需使用 0.10.0 或更新 CLI，升级后不要用旧 CLI 写入工作区。其他平台暂通过具体导出文件接入。

## 默认保存代码与对话

0.10.0 的 `ctx add` 默认保存当前 Git 工作区的普通代码文件：已跟踪文件使用当前工作树字节，包含未暂存和未提交的修改；未跟踪文件按 Git 忽略规则筛选。ctx 不执行 `git commit`，不修改 Git 暂存区。代码和对话一起进入 ctx 暂存区，`ctx diff --staged` 可检查文件增删和文本 diff，随后 `commit / push` 保存并上传同一个版本。

```bash
ctx add .                           # 代码 + 已跟踪对话
ctx add . --code ../another-project  # 明确指定代码 Git 工作区
ctx add . --no-code                  # 只更新对话，保留已保存的代码
ctx add . --no-refresh --no-code     # 只暂存本地人工对话编辑
```

没有 Git 仓库时，只更新对话并给出提示，不会扫描其他目录。Git/ContextHub 本地状态目录和常见凭据、私钥文件会被排除，`ctx --json add .` 可查看排除清单；文件排除不保证发现所有秘密，提交和公开前请检查暂存内容。符号链接、硬链接、子模块、LFS 指针等不支持的文件会停止采集。代码快照是工作树的保存状态，记录中的 Git commit 只是来源信息，不表示文件与该提交完全一致。

每次采集只将当前对话最新一轮绑定到保存时代码；没有轮次 ID 时，仅绑定最后一条消息。已有绑定保留，旧消息不会被批量指向当前代码，也不会推测过去的执行状态。新采集的 Codex 消息带真实轮次开始时间；网页按日期、轮次组织，默认只显示最新轮次，无时间则显示“时间未记录”，已有版本不会自动补填日期。

已携带代码的 `.contextpack` 默认保留其中的代码与关联，必须逐包执行 `ctx add 文件.contextpack`。显式 `--no-code` 会保留当前工作区旧代码；显式 `--code 路径` 会改为采集指定 Git 工作区，以显式选项为准。只含对话的文件仍可批量导入，并按上述默认规则同时采集当前项目代码。

## 本地文件导入与提交

```bash
ctx init my-dialogues --title '我的对话库' --description '项目思考与问题整理'
cd my-dialogues
ctx inspect ../first.contextpack
ctx add ../first.contextpack
ctx add ../second.contextpack  # 含代码的包需逐个导入
ctx add ../claude-export.json --platform claude
ctx add ../deepseek.json --platform deepseek
ctx diff --staged
ctx commit -m '整理第一批对话'
ctx status
ctx log
```

支持的文件、识别规则和遗漏提示与[网站导入器](import-formats.md)一致。不能解析的 UTF-8 文本可显式 `ctx add notes.md --platform other --mode archive --confirm-import`。出现兼容提示时，先检查文件和命令输出，再决定是否加入 `--confirm-import`。文件参数用于导入受支持的对话格式；项目文件通过默认代码采集保存，不把任意文件参数解释成对话。源文件保持原位。

纯对话文件的批量 add 先解析全部文件，再执行一次工作区写入；某个文件不合格时不导入其余文件。add 暂存整个受管对话快照，包括已有对话尚未暂存的编辑，可先用 diff 检查。相同对话 ID 且内容不同会被拒绝，明确接受替换后使用 `--replace`。不会凭标题自动猜测两段对话是否相同。

工作目录结构：

```text
my-dialogues/
  library.json             # 库标题、说明、对话 ID 与顺序
  conversations/*.json     # 可编辑的规范化对话；文件名由稳定 ID 计算
  code.json                # 可选：受管代码快照与文件内容，不建议手工修改
  .contexthub/              # 暂存状态、不可变提交、对象与远程绑定
```

在 VS Code 中编辑标题、背景、标签或消息正文后：

```bash
ctx diff
ctx add .
ctx diff --staged
ctx commit -m '补充背景与修正回答'
```

没有原生来源绑定时，`add .` 读取 `library.json` 和其中指定的对话；有绑定时会先刷新对应会话，`--no-refresh` 可跳过刷新。两种情况默认都会采集当前 Git 工作区代码；用 `--no-code` 保留已有代码。移除对话需要从 `library.json` 的 `conversation_ids` 列表移除相应 ID；至少保留一段对话。已有来源和审计记录保留，正文修改标记为人工编辑。`commit` 只提交暂存区，不自动暂存后续编辑。

本地工作不需要网络。每次提交保存完整、可校验的代码与对话快照；首版没有对象清理功能，磁盘随版本增长。不要手动改 `.contexthub/` 内部文件。

## 推送、克隆与拉取

```bash
ctx push                         # 首次创建私有库，后续推送新提交
ctx remote                       # 显示绑定与网页链接

# 仅在第一次推送时明确公开
ctx push --visibility public

# 在另一台机器登录后，复制网站分享链接
ctx clone 'https://contexthub.reflectstars.dev/?package=PACKAGE_ID' my-copy
cd my-copy
ctx pull
```

也可 `ctx clone PACKAGE_ID --hub https://hub.example.com`；支持分享链接中的固定 `version` 或 `--version VERSION_ID`。目标必须为空或不存在。clone 下载所选快照作为本地起点，不复制全部远程历史；网站仍可查看、比较和下载全部历史版本。`ctx log` 展示本地提交及拉取记录。

首次 push 按最旧到最新顺序发布所有本地提交，提交说明保留；每个成功响应后保存同步位置，失败不会丢弃未推送提交。每个本地分支使用持久的新建标识创建独立库，即使内容相同也不会绑定其他库；响应丢失后重试仍指向同一库。此功能需要支持该能力的 0.7.0 或更新 Hub，旧 Hub 会在上传前被拒绝，请同步升级服务端和 CLI。库公开性可在网页设置中修改；push 不更改可见性，并检查已绑定的可见性是否一致。若网页改变了公开性，push 会安全停止；检查当前可见性后显式 pull 更新绑定，再决定是否 push。发生过可见性变更的库会拒绝缺少此检查的旧客户端发布，请升级 ctx。

上传、克隆、下载、Fork、发布与拉取均需登录；他人的私有库无法访问。复制网页分享链接不传递密码或令牌。推送前要求 ctx 工作区干净；代码不必先做 Git 提交，但必须先由 `ctx add` 采集并 `ctx commit`。push 上传已保存版本，不重新扫描代码或 Codex。

pull 要求工作区没有未提交编辑。有待推送提交时，会按共同基准尝试三方合并独立变化。服务端以基准版本进行并发检查；如果网页或另一台机器已发布新版，陈旧 push 会失败，不覆盖远端。网络失败后可重试 push，同基准、内容与说明不会重复创建版本。

遇到冲突时，现有工作区和提交保留，远端快照保存在输出中指定的 `incoming/...` 分支。比较双方后编辑受管 JSON，执行 `ctx add .` 和 `ctx merge incoming/实际分支名 --resolved`，明确提交自己的解决结果；也可放弃解决，保留两条分支继续整理。不会静默选边或覆盖远端。

## 分支与三方合并

```bash
ctx branch                    # 列出分支、HEAD 与绑定
ctx switch -c improve         # 创建分支并切换
# 编辑后 ctx add . && ctx commit -m '补充说明'
ctx switch main
ctx merge improve
ctx log
```

分支共享不可变对象，各有独立 HEAD 和 Hub 绑定。新分支不继承原分支的远程库绑定，首次 push 会创建自己的库；切回原分支继续使用原绑定。切换要求没有未提交修改。旧版单分支工作区自动兼容迁移为 main。

对话的独立字段变化可合并；同字段不同修改、删除/编辑及不确定的顺序冲突要求人工解决。代码独立文件的变化可三方合并；双方不同地修改同一文件会报告冲突，不进行智能代码文本拼接。合并保存两个父提交，Hub 仍按线性公开版本接收当前分支快照，本地和 Git 导出保留完整分支 DAG。当前不提供 force、rebase、reset 或对象 GC。

## 从网页一步导入本机 Codex

项目页的“在本机 Codex 中继续”会生成固定版本的命令，自动带上所选对话及关联消息或快照。选择 Bash / PowerShell、填写本机的新目录后，复制到运行 Codex 的终端执行。首次需安装或更新 ctx、安装 Codex CLI，并用网站账号执行 `ctx auth login`；网页和终端的登录状态各自保存。

```bash
ctx open 'https://contexthub.reflectstars.dev/?package=PACKAGE_ID&version=VERSION_ID' --to ./my-project --codex --mode context --conversation=CONVERSATION_ID
```

`open` 无需当前目录已有 ctx 工作区。它验证 Hub 版本与包内容，临时下载并恢复 `code/`、`conversations/`、`receipt.json`，成功后清理传输工作区；`conversations/` 保留完整原始包。添加 `--codex` 才创建新的 Codex 会话，终端会打印正确工作目录下的 `codex resume` 命令，执行后继续开发。

- URL 必须带 `version`，或使用 `ctx open PACKAGE_ID --version VERSION_ID --to NEWDIR`。使用网页生成的完整命令；含阅读位置参数的“复制当前位置”链接不用于此入口。
- `--message=ID` 与 `--snapshot=ID` 二选一，语义与下方 `restore` 相同。多段对话导入 Codex 时必须用 `--conversation=ID` 选择；网页会自动填写。
- 代码快照必须存在于包内。没有代码的包应下载后用 `ctx codex import 文件.contextpack --cwd 已有项目目录` 导入，旧式 Git 检查点使用原来的 `restore --code`。
- 网页默认明确选择 `--mode context`；不保证历史消息完整显示或再次导出。需要重建文本时间线时选择 `timeline`，目前仅支持 Codex 0.153.4。终端省略 `--mode` 时仍沿用 `timeline` 默认值。
- `--resume` 只核验同一来源、版本、快照、对话和导入方式的既有完整结果，不覆盖修改，不重复导入。若代码已恢复但 Codex 导入失败或状态不确定，保留目录及回执并提示检查，不自动创建第二份会话。

公开或私有库均须终端登录才能下载。这个命令不自动启动模型；不会复制源机器登录凭据、附件或工具执行状态。

## 代码与对话一起恢复

内嵌代码需要 0.10.0 或更新的 ctx 与 Hub。push、clone、Fork、导出包和受管 Git 格式互通都会保留代码文件、代码快照及消息关联。clone 下载的是所选 ctx 版本；它携带该包已记录的代码快照，不自动克隆代码原仓库的 Git 历史。

```bash
ctx clone 'https://contexthub.reflectstars.dev/?package=PACKAGE_ID&version=VERSION_ID' my-dialogues
cd my-dialogues
ctx restore HEAD --to ../restored-project
ctx restore HEAD --snapshot SNAPSHOT_ID --to ../restored-snapshot
ctx restore HEAD --message MESSAGE_ID --conversation CONVERSATION_ID --to ../restored-message
```

无需 `--code` 或原 Git 仓库。目标必须不存在，生成 `code/` 源文件、`conversations/` 中保留完整原始包的 ctx 工作区，以及 `receipt.json`；原项目不变，`code/` 不伪造 `.git` 或历史提交。`--snapshot` 与 `--message` 二选一；消息必须已有明确代码关联，缺失时会拒绝恢复，不猜测历史。消息选择改变恢复的代码快照，完整对话包仍保留。

显式添加 `--codex` 可在恢复后的 `code/` 中创建新会话，多对话时用 `--conversation ID` 选择。配合 `--message` 时，仅将截至该消息的正文导入 Codex；仅选代码快照不会推测对话截断点。timeline 默认只支持已验证的 Codex 0.153.4，其他版本可显式 `--mode context`，原有附件、工具状态和执行环境限制不变。

`--resume` 只核验已有恢复结果，不覆盖文件；原生导入状态不确定时不会重复创建会话，请检查回执。代码关联表示采集时代码，不能证明消息生成时的原生执行状态。

代码上限为 128 个快照、每快照 10,000 个文件、单文件 16 MiB、全部快照去重后的文件字节 64 MiB；代码、对话及其他包内容合计遵守 100 MiB 解压上限。服务端还可配置更小的请求体限制。0.10.0 代码采集与恢复尚未完成原生 Windows 验收，Windows 仍为预览。

## 旧式 Git 提交检查点

旧包只保存 Git 提交关联而未携带代码文件时，继续使用以下流程。它与上面的内嵌工作树快照恢复分开：

代码关联要求 ctx 和 Hub 均为 0.8.0 或更新版本；旧版 Hub 会拒绝含关联字段的包。在对话工作区内，将已保存的对话与代码提交建立检查点。代码仓库由 Git 管理，ctx 不自动提交你的代码。先处理代码和对话的未提交修改，再运行：

```bash
ctx checkpoint --code ../my-project -m '登录功能完成：保存对话与代码对应关系'
ctx push
```

检查点包含准确的代码 commit/tree、对话内容摘要和可选的公开仓库地址；本机路径不随对话包传输。它是一个新的对话提交，随上传、下载、Fork 和 Git 格式互通保留。网页的“内容匹配”表示当前对话与记录一致，不是代码真实性认证。对话编辑后旧检查点过期，需要重新捕获；两条分支的代码检查点不会按字段拼接成未经确认的新状态。

在另一台机器先取得对应代码仓库，并克隆网站上的固定对话版本，然后恢复：

```bash
ctx clone 'https://contexthub.reflectstars.dev/?package=PACKAGE_ID&version=VERSION_ID' my-dialogues
cd my-dialogues
ctx restore HEAD --code ../my-project --to ../restored-login
```

目标目录必须不存在。恢复结果包括 `code/`（位于固定提交的独立 Git 仓库）、`conversations/`（对应的 ctx 工作区）和 `receipt.json`。原来的代码、未提交编辑与对话工作区保留。恢复使用检查点的固定对象，不使用代码分支当前指向的位置；代码来源必须已在本机拥有那个提交。ctx 不自动联网拉取私有代码。

需要同时创建可继续的 Codex 会话时，显式添加 `--codex`；有多段对话时，用包内的对话 ID 选择其中一段：

```bash
ctx restore HEAD --code ../my-project --to ../restored-login-codex --codex --conversation CONVERSATION_ID
# 明确需要兼容注入方式时添加 --mode context
```

Codex 会话使用恢复后的 `code/` 作为项目目录，工具状态、附件与运行环境不会随代码恢复。默认 timeline 模式遵循已有 Codex 版本限制。恢复回执区分代码/对话配对恢复和原生会话导入；请求原生导入后若失败，不会将整项操作报告为成功。

`--resume` 用于检查同一目标的已有恢复结果；它不会盲目覆盖文件或在原生导入结果不确定时重复创建会话。具体失败及后续处理以回执为准。配对准备失败时不发布目标目录，可以修复原因后重试；原生导入开始后的不确定结果需人工检查回执，不能盲目重试。

首版支持 SHA-1 / SHA-256 普通 Git 工作区及 linked worktree。独立恢复仓库的分支名为 `restored`，仅保留指定提交的浅历史，不复制远端设置。最多 50,000 个文件和目录、单文件 32 MiB、代码总量及传输包各 256 MiB；子模块、符号链接、Git LFS 指针及存在大小写冲突或 Windows 保留名称的路径会被拒绝。

检查点捕获要求工作文件与 Git 对象字节一致，不运行 clean/smudge 过滤器；自动转为 CRLF 的工作区也会停止，请使用与对象字节一致的独立 checkout。原有工作区不会被自动转换。如果 ctx 对话工作区位于代码目录内，需明确将其加入 Git 忽略规则，或将两者放在不同目录。

## 与普通 Git 仓库互通

```bash
ctx git export ../dialogues-git     # 目标目录必须不存在
# dialogues-git 是普通 Git 仓库，可自行添加 remote 并 git push
# 在另一台机器用普通 git clone 下载之后：
ctx git import ../dialogues-git ../restored-dialogues
cd ../restored-dialogues
ctx branch
```

需要本机安装 Git。导出包含规范 JSON、完整包快照和格式标记，保留本地分支、父子关系及包内代码文件；不包含 Hub 凭据、本机来源绑定或未采集的项目文件。Git 中直接编辑受管 JSON 并 commit 后，再导入会生成带审计记录的 ctx 版本；仅改 README 或空 Git 提交也可导入，push 跳过无内容变化的快照。

导入只读取已提交的本地分支，不读取未提交工作树、执行 hooks、filters 或源仓库配置。它接收 ctx 导出格式，不盲目解释任意代码仓库。当前支持 SHA-1 Git 仓库、每个提交最多两个父节点、10,000 个提交和 512 MiB 快照总量；detached HEAD 先用 Git 创建或切换分支。导入目标必须不存在。原样往返保留 ctx 快照与 ID，普通 Git 编辑后再导出的 Git OID 可能变化。Hub 不直接提供 Git wire protocol HTTPS 服务。

## Fork 与复用

```bash
ctx fork 'https://contexthub.reflectstars.dev/?package=PACKAGE_ID' my-fork
cd my-fork
# 编辑受管 JSON 后
ctx add .
ctx commit -m '增加自己的补充'
ctx push
ctx export ../shared.contextpack
```

Fork 固定源版本，默认私有，创建后绑定自己的库，不修改原库。需要贡献回上游时，创建时使用 `--visibility public`，再在网站提交贡献请求；私有来源只能由其所有者 Fork 为私有副本。如果 Fork 已在服务器创建但下载失败，可在网站找到该副本后 clone；再次 fork 会创建另一份副本。导出默认使用已提交的 HEAD，不含未提交改动，不覆盖已有输出文件。

## Codex 导出与导入

```bash
ctx codex list
ctx codex export --thread THREAD_ID_1 --thread THREAD_ID_2 \
  --title 'Codex 项目讨论' -o codex.contextpack

# 已 init 的工作目录
ctx add codex.contextpack
ctx commit -m '保存 Codex 对话'
ctx push

# 另一台机器 clone 后
ctx export ../downloaded.contextpack
ctx codex import ../downloaded.contextpack --dry-run
ctx codex import ../downloaded.contextpack --cwd /absolute/path/to/project
```

Codex 默认 `--mode timeline` 在 **0.153.4** 重建新的原生用户/助手文本时间线，导入后检查公开 API 读回、独立进程持久化与 resume，不触发模型调用。保留可见正文与顺序，重新生成会话/消息 ID 和时间，不恢复原始回合边界、附件、工具状态或权限。此模式依赖特定本地日志结构，其他版本写入前安全拒绝；可显式 `ctx codex import file.contextpack --cwd 项目路径 --mode context` 使用兼容注入模式。不会悄悄从时间线模式降级为上下文模式。

合成隔离验证记录见[Codex 原生文本时间线](research/2026-10-10-codex-native-timeline.md)。Claude/DeepSeek 当前可导入管理和导出可移植包，不承诺回导原平台继续聊天。

Windows 会识别 PATH 中的 `codex.exe` 及常见 npm 安装目录中的原生程序，不通过 `codex.cmd` shell 启动。无法自动找到时，在 PowerShell 中设置 `$env:CONTEXTHUB_CODEX_EXE = 'C:\完整路径\codex.exe'`；不会下载或自动安装 Codex。本次仅验证原生程序定位、协议合成测试及 dry-run，未在真实 Windows 上读取会话或调用模型。

## Windows 卸载与验收

先 `ctx auth logout`，再删除 `%LOCALAPPDATA%\ContextHub\bin` 并从用户 PATH 移除该项。不要将工作区放在安装目录内；卸载不会主动删除其他位置的对话库。若需要清除本机登录配置，再单独删除 `credentials.dpapi`。

仓库提供 `.github/workflows/cli-cross-platform.yml`，在 Ubuntu/Windows × Python 3.12/3.13/3.14 运行全量回归，包含真实 Windows EXE 命令、锁、DPAPI、junction、中文路径及 PowerShell 安装器。本次执行的是本机验证，尚未取得远端 CI 结果。Wine 不提供的链接创建能力会明确标为跳过，真实 Windows 不跳过对应的 junction 检查。

## 脚本使用与限制

`ctx --json status` 等命令输出 JSON，成功退出 0、失败退出 1、键盘中断退出 130。错误写 stderr；认证输出不含 cookie、密码和 CSRF 令牌。`ctx codex` 保留旧桥接命令自己的 JSON 回执。

格式上限沿用 contextpack：200 段对话、每段 2,000 条消息、每段 JSON 5 MiB、总解压内容 100 MiB；代码另受上述快照及文件字节限额约束。服务端请求上限以 `/api/config` 和上传提示为准，过大时分库管理。工作区写入使用文件锁、原子状态切换和恢复日志；不覆盖未跟踪文件，拒绝受管路径中的符号链接或硬链接。中断恢复时若发现外部编辑冲突，会停止并保留恢复材料。
