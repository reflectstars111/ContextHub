# ContextHub 0.3：贡献请求与审阅合并

用户在 0.2.0 版本/Fork 功能完成后要求继续开发。本阶段实现公开 Fork 的改进回流：提交固定版本、讨论、三方检查、审阅准确候选、合并发布。

## 范围与交互

公开 Fork 的所有者选定一个已保存且相对 Fork 点有变化的版本，填写标题和说明，向直接上游创建贡献请求。提案固定源库、源版本、上游库和 Fork 点。源库后续保存新版本不会悄悄改变提案；作者可以显式更新提案版本，旧检查随即失效。

双方可以评论、关闭、重新打开提案。匿名用户可读公开提案；私有源不参与本阶段贡献。上游维护者才能检查候选和合并，贡献者不因提出修改而获得写上游的权限。评论不是外部通知，不发送邮件或消息。

网页有固定提案链接，展示源改动和检查后的合并候选差异。维护者明确审阅检查结果后点击“合并并发布新版本”。存在冲突、目标新版本或无实际变化时不能合并。完整候选包可下载检查；差异显示沿用有界摘要和截断提示。

## 三方合并

以 Fork 点为共同基准，比较目标当前版本与固定源版本。对话、消息按稳定 ID 匹配，字段独立合并；单侧修改采用修改侧，双方同样修改接受，双方不同修改同一字段冲突。删除与编辑冲突，同 ID 不同新增内容冲突。顺序采用保守、确定性规则，不猜测矛盾顺序。

平台身份和库来源保留目标值，源库不能覆盖上游身份。已存在的编辑记录不可擦除；两侧在共同审计前缀上追加的记录共同保留。合并结果必须再次通过包格式、ID、角色、数量和大小校验。无模型辅助或自动生成答案。

冲突摘要保留完整数量，详情最多 100 条，每字段最多 2,000 字符；超限明确标记。存在任何冲突则不生成可合并包。暂不提供网页冲突编辑器，作者需调整自己的版本或基于最新上游重新 Fork 后另提修改。

## 存储与一致性

新增贡献和评论表，幂等升级现有数据。Contributions 服务复用 Store 的连接、不可变包和 Git 原语；不改原下载包格式。

检查在 SQLite 写事务中固定源/祖先/目标版本与规则版本，生成不可变候选包及双父 Git commit（目标当前 commit、固定源 commit）。候选不更新上游 head 或公开 ref。目标头变更后，检查动态标记过期；源库仅新增未被提案选中的版本不使检查过期。

合并要求客户端提供所审阅的 check_id 和 candidate_oid，并再次检查提案状态、权限、依赖可见性、规则及预期目标 head。合并直接登记同一候选包与 Git OID，原子写入新版本并更新提案状态。成功重试返回原结果，不重复发布；失败不能推进 head 或 ref。关闭/重开与显式更新源版本清除旧候选。

源、目标或 Fork 点依赖不可访问时，列表、详情、候选下载、评论、检查和合并均阻断。已完成上游版本属于其自身历史，提案源删除不撤销已经发布的版本。候选和评论不从未授权提案中泄漏。

## API

保留 `/api/packages` 以及现有 Cookie/CSRF/同源保护，新增：

- GET `/api/packages/{id}/contributions`：该库收到或发出的可见提案摘要。
- POST `/api/packages/{source_id}/contributions`：`{source_version_id,title,description}`。
- GET `/api/contributions/{id}`：详情、固定源差异、评论、权限和检查结果。
- POST `/api/contributions/{id}/comments`：`{body}`。
- POST `/api/contributions/{id}/status`：`{status:'closed'|'open'}`。
- POST `/api/contributions/{id}/source`：`{source_version_id}`，作者显式更新。
- POST `/api/contributions/{id}/check`：`{expected_target_version_id}`。
- GET `/api/contributions/{id}/candidate?check_id=...`：固定候选包。
- POST `/api/contributions/{id}/merge`：`{check_id,candidate_oid}`。

详情包含 source/target、fork_point_version_id、comparison、comments、permissions、check、merged_version_id。check 为 null 或 `{id,status:'ready'|'conflicts'|'unchanged',stale,base_version_id,source_version_id,candidate_oid,candidate_sha256,rules_version,conflicts,conflict_count,truncated,comparison,checked_at}`。列表省略较大的差异和评论正文。

标题 1–200 字符、说明至多 10,000 字符、评论 1–4,000 字符且每提案至多 200 条。仅作者和上游维护者可以评论和关闭/重开；只有作者能换源版本；已合并提案不能重新打开。

## 验收与边界

纯函数覆盖独立/相同/矛盾修改、增删重排、审计、身份和大输入。存储/API覆盖固定源、候选准确 OID/字节、双父、过期检查、权限、删除依赖、双实例并发合并和失败回滚。浏览器验证两账号提交→评论→检查→合并，以及目标推进后的阻断、固定链接刷新、移动端与旧流程回归。

本阶段不增加服务器草稿、可见性切换、私有协作、自动通知、许可授权流程、原生 Git 传输或图形冲突解决；Codex 恢复能力仍以既有实测为准。
