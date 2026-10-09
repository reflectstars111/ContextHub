# ContextHub 对话库版本与 Fork

用户在迁移 MVP 后要求继续开发。此阶段落实原产品设计中的持续演化：保留旧版、修改成新版、按版本 Fork、比较差异。沿用 Python 标准库、SQLite、Git 和原静态网页；不接入模型。

## 用户流程

公开对话库 → 选择固定版本 → Fork 为自己的独立库 → 编辑正文、标题、背景或导入新的对话包 → 查看待发布差异 → 明确保存新版 → 阅读、比较和下载任意旧版。

上传的包成为 v1。库 ID 始终不变，版本 ID 与包内容不可变。历史链接包含库 ID 和版本 ID，可刷新和分享。下载某版保留该版 ZIP 的原始字节。

网页编辑是浏览器内草稿，显式保存才写入平台；未保存修改有离开提示。公开库保存按钮明确称为“发布新版本”。本阶段不提供服务器草稿保存、逐版本撤回、可见性转换、贡献请求或合并。库级 public/private 在创建时固定，各版本沿用该可见性。

Fork 默认私有，可明确选择公开；来自私有库的 Fork 仅允许所有者创建私有副本。Fork 复制选定快照，记录准确来源版本，不复制整套祖先历史。公开 Fork 的 Git commit 可连接公开源 commit。删除上游后，Fork 已取得的独立内容保留，来源信息按当前权限隐藏。

## 数据与一致性

`packages` 继续代表稳定对话库，增加 head 与上游指针；`package_versions` 保存不可变版本、序号、父版本、包哈希、发布说明、作者和时间。旧数据库在事务中幂等迁移为 v1，原 ID、包字节、Git commit 和创建时间不变。

SQLite 写事务使用 BEGIN IMMEDIATE。保存要求 base_version 等于当前 head；不相等返回 409，前端保留草稿。同基准、内容和说明的成功重试返回原结果，不重复创建版本。不同 Store 实例访问同一数据库也不能丢失更新。

数据库是业务权威。公开新版本建立有真实 parent 的 Git commit；私有内容不进入 Git。Git 对象写入后才登记数据库，ref 可从数据库恢复。所有历史、下载、比较、来源和修改接口逐次检查对象权限，删除库后历史与比较同样不可读取。

## 编辑与差异

沿用开放包格式 0.1。服务端生成修订包，匹配相同 ID 时保留原始来源和既有编辑记录。正文或角色修改标记 human_edited；增删和重排写入格式支持的 annotation。新增手写消息标记 human_authored；客户端声明的导入来源不自动验证为真实来源。

差异按稳定对话和消息 ID 匹配，展示标题、背景、说明、消息正文/角色/元数据的增删改及相对顺序变化。新增消息不把其后全部消息算作重排。摘要全量统计，详细消息最多 200 条，每段显示最多 4,000 字符，所有显示截断通过 truncated 标识；完整内容可下载，不静默截断存储。

导入新包作为版本候选时，用户明确以预览后的整份快照替换当前编辑内容；不自动推测跨 Codex 线程的消息对应关系。

## API 契约

保留已有 `/api/packages` 路径。detail 增加 `head_version_id`、`version_count`、`library_source`、`version`、`upstream`。`version` 含 id/number/parent_id/sha256/message/author/created_at/git_commit；不可见来源的 upstream 为 null。

- GET `/api/packages/{id}?version={version_id}`：读取选定版本，省略时当前版本。
- GET `/api/packages/{id}/download?version={version_id}`：下载固定版本。
- GET `/api/packages/{id}/versions`：`{versions:[...]}`，最新在前。
- POST `/api/packages/{id}/versions`：JSON `{base_version,title,description,conversations,message}`，保存并返回 detail。
- POST `/api/packages/{id}/preview`：相同 JSON，返回 `{comparison,compatibility}`，不写入版本。
- GET `/api/packages/{id}/compare?from={version_id}&to={version_id}`：比较两个有权限的库内版本。
- POST `/api/packages/{id}/fork`：JSON `{version_id,visibility}`，创建独立库并返回 detail。

所有写操作继续使用登录 Cookie、CSRF 和同源检查。新版本说明必须为 1–2,000 字符文本。格式大小限制沿用迁移 MVP。Codex 连接器的恢复能力边界保持原有说明。

## 验收

旧库迁移、不可变历史下载、多账号历史 ACL、固定来源 Fork、Git parent、两实例竞争保存、冲突与重试、差异摘要/显示截断和人工编辑记录均有测试。浏览器验证从读取上游到 Fork、编辑、预览、新版、比较、历史下载、固定链接刷新，以及并发冲突时保留草稿；旧迁移流程回归。
