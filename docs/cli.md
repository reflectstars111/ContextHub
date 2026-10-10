# ctx：ContextHub 的终端工具

适用 **Linux / 远程服务器 / VS Code 终端**，当前 0.7.0 提供 **Windows 10/11 原生预览版**，需要 Python 3.12+，无需 WSL。管理对话不需要模型服务或第三方 Python 库；本地 Codex 桥接另需安装 Codex CLI。Windows 11 本机已验证全部 `ctx` 命令、内核路径、DPAPI、锁与安装器，详见[重构验证记录](verification/2026-10-09-windows-cli-refactor.md)。0.7.0 新增功能的原生 Windows CI 尚待运行，因此保留预览标记。

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

确认校验通过后再安装。可将 PATH 设置加入 `~/.bashrc`；升级时重复下载、校验和安装。`ctx.pyz` 是可查看源码的 ZIP，包含 MIT 许可证；也可以用 `python3 ctx.pyz --help` 运行。网站仅打包明确列出的产品源码，不包含服务器数据、凭据、用户对话或项目文件。

源码开发安装：在项目根目录运行 `python3 -m venv .venv`，然后 `.venv/bin/python -m pip install -e .`，使用 `.venv/bin/ctx`；无需安装时可用 `python3 -m contexthub.ctx`。项目尚未发布到 PyPI，不要从同名未知包安装。

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

## 本地导入与提交

```bash
ctx init my-dialogues --title '我的对话库' --description '项目思考与问题整理'
cd my-dialogues
ctx inspect ../first.contextpack
ctx add ../first.contextpack ../second.contextpack
ctx add ../claude-export.json --platform claude
ctx add ../deepseek.json --platform deepseek
ctx diff --staged
ctx commit -m '整理第一批对话'
ctx status
ctx log
```

支持的文件、识别规则和遗漏提示与[网站导入器](import-formats.md)一致。不能解析的 UTF-8 文本可显式 `ctx add notes.md --platform other --mode archive --confirm-import`。出现兼容提示时，先检查文件和命令输出，再决定是否加入 `--confirm-import`。添加文件会导入并暂存受支持内容；不会原样复制任意项目文件。源文件保持原位。

批量 add 先解析全部文件，再执行一次工作区写入；某个文件不合格时不导入其余文件。add 暂存整个受管对话快照，包括已有对话尚未暂存的编辑，可先用 diff 检查。相同对话 ID 且内容不同会被拒绝，明确接受替换后使用 `--replace`。不会凭标题自动猜测两段对话是否相同。

工作目录结构：

```text
my-dialogues/
  library.json             # 库标题、说明、对话 ID 与顺序
  conversations/*.json     # 可编辑的规范化对话；文件名由稳定 ID 计算
  .contexthub/              # 暂存状态、不可变提交、对象与远程绑定
```

在 VS Code 中编辑标题、背景、标签或消息正文后：

```bash
ctx diff
ctx add .
ctx diff --staged
ctx commit -m '补充背景与修正回答'
```

`add .` 仅读取 `library.json` 和其中指定的对话，暂存全部受管 JSON 的改动；不会扫描代码目录。移除对话需要从 `library.json` 的 `conversation_ids` 列表移除相应 ID；至少保留一段对话。已有来源和审计记录保留，正文修改标记为人工编辑。`commit` 只提交暂存区，不自动暂存后续编辑。

本地工作不需要网络。每次提交保存完整、可校验的对话快照；首版没有对象清理功能，磁盘随版本增长。不要手动改 `.contexthub/` 内部文件。

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

上传、克隆、下载、Fork、发布与拉取均需登录；他人的私有库无法访问。复制网页分享链接不传递密码或令牌。推送前要求工作区干净，避免“以为未提交编辑也已上传”的误解。

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

独立字段变化可合并；同字段不同修改、删除/编辑及不确定的顺序冲突要求人工解决。合并保存两个父提交，Hub 仍按线性公开版本接收当前分支快照，本地和 Git 导出保留完整分支 DAG。当前不提供 force、rebase、reset 或对象 GC。

## 与普通 Git 仓库互通

```bash
ctx git export ../dialogues-git     # 目标目录必须不存在
# dialogues-git 是普通 Git 仓库，可自行添加 remote 并 git push
# 在另一台机器用普通 git clone 下载之后：
ctx git import ../dialogues-git ../restored-dialogues
cd ../restored-dialogues
ctx branch
```

需要本机安装 Git。导出包含规范 JSON、快照和格式标记，保留本地分支及父子关系；不包含 Hub 凭据、绑定或项目杂项。Git 中直接编辑受管 JSON 并 commit 后，再导入会生成带审计记录的 ctx 版本；仅改 README 或空 Git 提交也可导入，push 跳过无内容变化的快照。

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

格式上限沿用 contextpack：200 段对话、每段 2,000 条消息、每段 JSON 5 MiB、总解压内容 100 MiB；公网当前单次请求最多 16 MiB，过大时分库管理。工作区写入使用文件锁、原子状态切换和恢复日志；不覆盖未跟踪文件，拒绝受管路径中的符号链接或硬链接。中断恢复时若发现外部编辑冲突，会停止并保留恢复材料。
