# ctx 0.6.0 Windows 预览版验证记录

日期：2026-10-09（Asia/Shanghai）。本轮增加 Windows 原生客户端；Hub 的生产部署继续使用 Linux。

## 实现范围

- Windows/Linux 共用源码版 `ctx.pyz`，支持相同的本地导入、提交、推送、克隆、拉取、Fork、导出命令。
- PowerShell 5.1+ 当前用户安装器，校验 HTTPS 来源、SHA-256 和版本；安装目录与用户 PATH 增量更新，失败回滚。不改变执行策略，不要求管理员权限。
- Windows 原生 `ctx.exe` 启动器复用所选 Python 安装内 pip/ensurepip 提供的 distlib 启动文件，本轮不分发第三方二进制或联网安装依赖。精简 Python 可以直接运行 `py -3 ctx.pyz`。
- Win32 目录句柄、共享限制、文件锁、原子替换与恢复日志；拒绝重解析点、硬链接和不支持的网络路径。POSIX 保留原有文件操作和锁。
- 当前 Windows 用户 DPAPI 加密登录会话；没有明文降级，不保存密码。
- Windows Codex 原生可执行文件发现及显式路径配置，不通过批处理脚本启动 Codex。
- 安装指南与下载入口，Ubuntu/Windows × Python 3.12/3.13 CI 配置。

## 最终自动验证

- `CONTEXTHUB_TEST_POWERSHELL=/tmp/contexthub-powershell-7.5.3/pwsh python3 -m unittest discover -s tests -q`：**320 项，304 通过、16 项原生 Windows 测试跳过，94.672 秒**。日志 `.artifacts/windows-unittest-final.log`。
- 其中 7 项使用真实 PowerShell 7 执行安装器编排，包含 Legacy 参数模式、中文空格路径、升级与失败回滚。Linux 测试仅将原生 EXE 构造替换为可执行测试夹具，不能视作原生 Windows 安装验收。
- Windows Python + Wine：workspace、safe_fs、credentials、Codex 定位、launcher 共 **61 项，55 通过、6 项链接能力缺失跳过，13.854 秒**。日志 `.artifacts/windows-wine-unit-final.log`。
- 原生 PE 启动文件执行验证包含中文、空格、`&`、`%PATH%`、`!`、管道符、引号、空参数、末尾反斜杠及退出码保留。
- 浏览器验证安装指南在桌面/手机尺寸无横向溢出，脚本可下载，pyz 与 SHA-256 一致且可执行，没有 JavaScript 异常。记录 `.artifacts/ctx-web-verification.json`。
- 公开读取、登录后下载、私有库所有者权限的浏览器回归通过，记录 `.artifacts/public-access-web-verification.json`。
- 独立复审最终启动器、安装器、凭据和文件适配层，没有发现未解决的阻断项。

## Windows 执行验证与边界

当前没有真实 Windows 主机，也没有已连接的 GitHub 远端。**原生 Windows CI 尚未执行**；Windows 版本明确标为预览版。

补充环境：Wine 9.0 + Windows CPython 3.12.10，均解包到 `/tmp`，没有安装系统服务或修改宿主 Python。执行了 Windows 内核文件 API、`LockFileEx`、DPAPI、Unicode 路径、原子写入和恢复测试。这些证据不能替代原生 Windows 验收。

Windows Python 客户端连接临时 Linux Hub 完成：DPAPI 登录→中文与空格目录 init→导入/提交→私有 push→另一目录 clone→编辑/提交/push→pull→导出字节一致→logout→匿名 clone 拒绝。记录：`.artifacts/windows-wine-transfer.json`、`.artifacts/windows-wine-transfer.log`。

Wine 的部分链接创建操作返回成功但没有创建目录项，对应测试明确跳过；跳过条件严格限制为检测到 Wine 且链接不存在。真实 Windows 的 junction 测试仍要求成功创建并验证拒绝访问。

另一次包含 Windows 本地临时 Hub 的 68 项组合测试中，公开上传测试失败。已定位为该临时 Hub 调用 `git init --bare` 时缺少 Windows Git，不是客户端上传或认证错误。保留该测试；CI 增加 `git --version` 前置检查。客户端访问 Linux Hub 不需要本机 Git。

本轮没有读取真实 Codex 会话、在生产创建测试账号或对话，也没有调用模型。Codex 验证限于原生程序定位、合成协议测试及 dry-run。

## 打包与上线

- 最终源码 pyz：64,110 字节，SHA-256 `f57a0b21716678480b998e73e5eaeaa43a57f6f9f071ac3ed675f8555d6bc0f8`。
- 在 Windows Python + Wine 中实际生成 EXE 并运行最终 pyz，版本输出 0.6.0，中文目录及含 `&`、`%PATH%`、`!` 的库标题保持原样。记录 `.artifacts/windows-native-release-check.log`。
- Wheel 无依赖构建及隔离目录入口验证通过，包含 Windows 模块、PowerShell 脚本和更新后的指南。
- 19:27:25 停止 Hub 后完整备份数据、旧版源码及已安装 CLI，然后更新服务；本轮没有数据库结构迁移。
- 备份目录 `.artifacts/backups/20261009-192725-before-windows-cli/`，权限 700。
- Hub 和 Cloudflare Tunnel 均 active。Linux 已安装 CLI 更新至 0.6.0，交互 Bash 验证通过。
- 公网 `/api/config` 返回 0.6.0；`/cli` 显示 Windows 预览版；`/install-ctx.ps1` 与源码一致；pyz 与已安装版本字节一致，SHA-256 匹配。
- 匿名下载对话库、推送仍返回 401；响应保留 `Cache-Control: no-store`。
- 部署与公网证据：`.artifacts/windows-deployment.json`、`.artifacts/windows-live-verification.json`。

## 审查修复

- PowerShell Legacy 参数处理会拆开 Python 探测代码中的双引号。使用单引号构造探测代码，并增加 Legacy 模式及中文空格路径回归。
- 批处理启动器会再次解释 `&`、`%` 等参数。改用原生 EXE，避免在启动时进入 CMD 解释器。
- Windows 目录只请求属性访问不足以建立需要的共享限制。改为读取访问并仅允许共享读取，验证祖先目录不能在操作中被重命名或重新打开写入。

使用方式及已知限制见 [ctx 指南](../cli.md)。

## 实现依据

- [Microsoft CreateFile 文档](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilea)：目录句柄、共享访问和重解析点。
- [Microsoft DPAPI 示例](https://learn.microsoft.com/en-us/windows/win32/seccrypto/example-c-program-using-cryptprotectdata)：当前用户数据加解密。
- [distlib 启动器实现](https://github.com/pypa/distlib/blob/master/distlib/scripts.py)：原生启动文件、shebang 和 ZIP 的构造方式。
