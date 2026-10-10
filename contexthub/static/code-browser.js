"use strict";

// File and diff requests are always pinned to the version open in the reader.
const codeBrowser = { key: null, snapshot: null, directory: "", request: 0, tab: "files", diffLoaded: null };
function portableCodeSummary(code) {
  return code ? structuredClone(Object.fromEntries(Object.entries(code).filter(([key]) => key !== "blobs"))) : null;
}
function codeSummary() { return state.reader && state.reader.code; }
function codeSnapshot() {
  const code = codeSummary();
  return code && (code.snapshots || []).find((item) => item.id === codeBrowser.snapshot);
}
function codeEndpoint(kind, extra = {}) {
  const reader = state.reader;
  if (reader && Object.hasOwn(reader, "codeReplacement")) throw new Error("这是尚未发布的候选代码。发布新版本后可浏览文件内容和代码变化；发布前请使用“预览变化”检查。");
  if (!reader || !reader.id || !reader.version || !reader.version.id) throw new Error("请先上传并保存这个代码与对话版本，再浏览代码文件。");
  const query = new URLSearchParams({ version: reader.version.id, snapshot: codeBrowser.snapshot, ...extra });
  return `/api/packages/${encodeURIComponent(reader.id)}/code${kind ? "/" + kind : ""}?${query}`;
}
function renderCodeRepository(data) {
  const code = data.code, snapshots = code && Array.isArray(code.snapshots) ? code.snapshots : [];
  const key = `${data.id || "local"}:${data.version && data.version.id || ""}:${data.kind}:${JSON.stringify(code || null)}`;
  if (key === codeBrowser.key) return;
  codeBrowser.key = key; codeBrowser.request++; codeBrowser.diffLoaded = null;
  codeBrowser.snapshot = code && code.head || (snapshots.at(-1) || {}).id || null;
  codeBrowser.directory = "";
  const select = $("code-snapshot-select"); select.replaceChildren();
  snapshots.forEach((snapshot, index) => {
    const option = node("option", "", `${index + 1} · ${formatDate(snapshot.created_at)} · ${String(snapshot.id).slice(0, 12)}`);
    option.value = snapshot.id; select.append(option);
  });
  select.value = codeBrowser.snapshot || ""; select.disabled = !snapshots.length;
  $("code-files-tab").disabled = !snapshots.length;
  $("code-diff-tab").disabled = !snapshots.length || Object.hasOwn(data, "codeReplacement");
  $("code-browser-note").textContent = Object.hasOwn(data, "codeReplacement") ? "候选代码尚未发布。目录显示候选文件；使用“预览变化”查看与基础版本的差异，发布后可浏览文件内容。" : !snapshots.length ? "这个版本未上传代码文件。使用 ctx add .、ctx commit、ctx push 同步代码与对话。旧对话没有记录的历史代码不会被推测补齐。" : data.kind === "draft" ? "当前显示基础版本中的代码快照；对话编辑会保留来源，发布时重新检查关联。" : "选择快照查看文件及相对上一快照的真实代码变化。消息关联表示采集时的代码状态。";
  renderCodeSnapshot(); setCodeTab("files");
}
function renderCodeSnapshot() {
  codeBrowser.request++; codeBrowser.diffLoaded = null;
  $("code-file").replaceChildren(); $("code-diff-panel").replaceChildren();
  const summary = $("code-browser-summary"), snapshot = codeSnapshot(); summary.replaceChildren();
  if (snapshot) {
    summary.append(node("span", "", `${(snapshot.files || []).length} 个文件`));
    if (snapshot.branch) summary.append(node("span", "", `分支 ${snapshot.branch}`));
    if (snapshot.git_commit) summary.append(node("code", "", `Git ${snapshot.git_commit.slice(0, 12)}`));
    summary.append(node("span", "", snapshot.created_at ? new Date(snapshot.created_at).toLocaleString("zh-CN") : "时间未记录"));
  }
  renderCodeTree();
}
function renderCodeTree() {
  const tree = $("code-tree"), crumbs = $("code-breadcrumb"), snapshot = codeSnapshot(); tree.replaceChildren(); crumbs.replaceChildren();
  if (!snapshot) return;
  const changeDirectory = (path) => { codeBrowser.directory = path; codeBrowser.request++; $("code-file").replaceChildren(); renderCodeTree(); };
  crumbs.append(action("仓库根目录", "text-button", () => changeDirectory("")));
  let current = "";
  for (const segment of codeBrowser.directory.split("/").filter(Boolean)) {
    current += segment + "/"; const path = current;
    crumbs.append(node("span", "", "/"), action(segment, "text-button", () => changeDirectory(path)));
  }
  const entries = new Map();
  for (const file of snapshot.files || []) {
    if (!file.path.startsWith(codeBrowser.directory)) continue;
    const relative = file.path.slice(codeBrowser.directory.length), slash = relative.indexOf("/");
    const name = slash < 0 ? relative : relative.slice(0, slash);
    if (name) entries.set(name, slash < 0 ? { ...file, name, directory: false } : { name, path: codeBrowser.directory + name + "/", directory: true });
  }
  if (codeBrowser.directory) {
    const parent = codeBrowser.directory.split("/").filter(Boolean).slice(0, -1).join("/");
    tree.append(action("↰ 上一级", "code-tree-row", () => changeDirectory(parent ? parent + "/" : "")));
  }
  for (const file of [...entries.values()].sort((a, b) => Number(b.directory) - Number(a.directory) || a.name.localeCompare(b.name))) {
    const row = action("", "code-tree-row", () => file.directory ? changeDirectory(file.path) : perform(() => openCodeFile(file.path)));
    row.dataset.path = file.path;
    row.disabled = !file.directory && Object.hasOwn(state.reader, "codeReplacement");
    row.append(node("span", "code-entry-name", `${file.directory ? "▸" : "▤"} ${file.name}`), node("span", "code-entry-size", file.directory ? "目录" : `${file.size} B`)); tree.append(row);
  }
  if (!entries.size) tree.append(node("p", "field-help", "此目录没有文件。"));
}
async function openCodeFile(path) {
  const request = ++codeBrowser.request, key = codeBrowser.key, snapshot = codeBrowser.snapshot;
  const box = $("code-file"); box.replaceChildren(node("p", "field-help", "正在读取文件…"));
  try {
    const file = await api(codeEndpoint("file", { path }));
    if (request !== codeBrowser.request || key !== codeBrowser.key || snapshot !== codeBrowser.snapshot) return;
    box.replaceChildren(node("h3", "code-file-name", file.path), node("p", "field-help", `${file.size} B · ${file.sha256}`));
    if (file.binary) box.append(node("p", "notice", "二进制文件不提供文本预览，文件内容保存在完整包中。"));
    else if (typeof file.content === "string") box.append(node("pre", "code-file-content", file.content));
    else box.append(node("p", "field-help", "此文件不提供文本预览。"));
    if (file.truncated) box.append(node("p", "notice", "文件较大，当前仅显示部分内容。"));
  } catch (error) {
    if (request === codeBrowser.request && key === codeBrowser.key) box.replaceChildren(node("p", "notice error", errorMessage(error)));
  }
}
function setCodeTab(tab) {
  codeBrowser.tab = tab;
  $("code-files-panel").hidden = tab !== "files"; $("code-diff-panel").hidden = tab !== "diff";
  $("code-files-tab").setAttribute("aria-pressed", String(tab === "files")); $("code-diff-tab").setAttribute("aria-pressed", String(tab === "diff"));
  if (tab === "diff" && codeSnapshot()) perform(loadCodeDiff);
}
async function loadCodeDiff() {
  const marker = codeBrowser.key + ":" + codeBrowser.snapshot;
  if (codeBrowser.diffLoaded === marker) return;
  const request = ++codeBrowser.request, key = codeBrowser.key, snapshot = codeBrowser.snapshot;
  const box = $("code-diff-panel"); box.replaceChildren(node("p", "field-help", "正在比较代码快照…"));
  try {
    const diff = await api(codeEndpoint("diff"));
    if (request !== codeBrowser.request || key !== codeBrowser.key || snapshot !== codeBrowser.snapshot) return;
    codeBrowser.diffLoaded = marker; box.replaceChildren();
    renderCodeComparison(box, diff);
  } catch (error) {
    if (request === codeBrowser.request && key === codeBrowser.key) box.replaceChildren(node("p", "notice error", errorMessage(error)));
  }
}

function renderCodeComparison(box, diff) {
  const count = diff.summary || {};
    box.append(node("p", "code-diff-summary", `新增 ${count.files_added || 0} · 修改 ${count.files_modified || 0} · 删除 ${count.files_removed || 0} 个文件 · +${count.additions || 0} −${count.deletions || 0} 行`));
    box.append(node("p", "field-help", diff.before_snapshot ? `相对上一快照 ${String(diff.before_snapshot).slice(0, 12)}` : "首次代码快照；尚无上一快照。"));
    for (const file of diff.files || []) {
      const details = node("details", "code-file-diff");
      const labels = { added: "新增", removed: "删除", deleted: "删除", modified: "修改" };
      details.append(node("summary", "", `${labels[file.status] || file.status} · ${file.path} · +${file.additions || 0} −${file.deletions || 0}`));
      if (file.binary) details.append(node("p", "field-help", "二进制文件发生变化。"));
      else details.append(node("pre", "code-patch", file.patch || "文件内容无文本差异（可能仅文件模式变化）。"));
      if (file.truncated) details.append(node("p", "notice", "此文件的差异预览已截断。"));
      box.append(details);
    }
    if (!(diff.files || []).length) box.append(node("p", "field-help", diff.changed ? "文件内容一致，代码快照或消息关联记录发生变化。" : "代码与上一快照一致。"));
    if (diff.truncated) box.append(node("p", "notice", "差异较大，当前仅显示部分内容。"));
}
function showMessageCode(snapshotId) {
  if (!requireSavedEditor()) return;
  if (!(codeSummary().snapshots || []).some((snapshot) => snapshot.id === snapshotId)) return;
  codeBrowser.snapshot = snapshotId; codeBrowser.directory = "";
  $("code-snapshot-select").value = snapshotId; renderCodeSnapshot(); setCodeTab(Object.hasOwn(state.reader, "codeReplacement") ? "files" : "diff");
  setRepositorySection("code-browser-panel", { focus: true });
  const panel = $("code-browser-panel"); panel.scrollIntoView({ block: "start" }); panel.setAttribute("tabindex", "-1"); panel.focus({ preventScroll: true });
}
function renderMessageCode(section, conversation, message) {
  const code = codeSummary(), line = node("div", "message-code-link");
  const binding = code && (code.bindings || []).find((item) => item.conversation_id === conversation.id && item.message_id === message.id);
  if (binding && (code.snapshots || []).some((item) => item.id === binding.snapshot_id)) {
    line.append(action(`采集时的代码 · ${binding.snapshot_id.slice(0, 12)}`, "text-button", () => showMessageCode(binding.snapshot_id)));
  } else line.append(node("span", "", "未记录当时代码"));
  section.append(line);
}
document.addEventListener("DOMContentLoaded", () => {
  $("code-snapshot-select").addEventListener("change", () => { codeBrowser.snapshot = $("code-snapshot-select").value; codeBrowser.directory = ""; renderCodeSnapshot(); setCodeTab(codeBrowser.tab); });
  $("code-files-tab").addEventListener("click", () => setCodeTab("files"));
  $("code-diff-tab").addEventListener("click", () => setCodeTab("diff"));
});
