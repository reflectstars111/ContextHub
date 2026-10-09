"use strict";

// Immutable versions live on the server. A working copy exists only in this tab.
const versionState = { original: null, versions: [], request: 0, preview: null, candidate: null, candidateRequest: 0, saving: false, forkTarget: null };
function isDraft() { return Boolean(state.reader && state.reader.kind === "draft"); }
function versionOwner(data = state.reader) {
  return Boolean(data && state.user && (data.owner_id === state.user.id || (typeof data.author === "object" ? data.author.username : data.author) === state.user.username));
}
function versionURL(id, version) {
  const url = new URL(window.location.href);
  url.search = ""; url.hash = "";
  url.searchParams.set("package", id);
  if (version) url.searchParams.set("version", version);
  return url;
}
function updateVersionURL(data, replace = false) {
  if (state.mode !== "hub" || !data.id) return;
  const url = versionURL(data.id, data.version && data.version.id);
  if (url.href !== window.location.href) window.history[replace ? "replaceState" : "pushState"](null, "", url);
}
function clearVersionURL() {
  if (state.mode === "hub" && window.location.search) window.history.pushState(null, "", window.location.pathname);
}
function leaveDraft() {
  if (!isDraft()) return true;
  if (versionState.saving) { showStatus("正在保存，请等待保存结果。", "warning"); return false; }
  if (!window.confirm("当前草稿尚未保存。离开将丢弃这份草稿，是否继续？")) return false;
  state.reader = versionState.original;
  clearDraftState();
  return true;
}
function clearDraftState() {
  versionState.original = null;
  versionState.preview = null;
  versionState.candidate = null;
  versionState.candidateRequest++;
  $("draft-panel").hidden = true;
  $("draft-comparison").hidden = true;
  $("draft-comparison").replaceChildren();
  clearCandidate();
}
function draftChanged() {
  if (!isDraft()) return;
  versionState.preview = null;
  $("save-version").disabled = true;
  $("draft-status").textContent = "草稿已更新，请重新预览";
  $("draft-comparison").hidden = true;
}
function renderVersionWorkspace(data) {
  const hub = data.kind === "hub" || data.kind === "draft";
  $("version-workspace").hidden = !hub;
  renderPackageContributions(data);
  if (!hub) return;
  const draft = isDraft();
  const version = data.version || {};
  $("version-select").disabled = draft;
  $("copy-version-link").disabled = draft;
  $("compare-versions").disabled = draft;
  $("fork-package").hidden = draft;
  $("edit-package").hidden = draft || !versionOwner(data) || version.id !== data.head_version_id;
  $("draft-panel").hidden = !draft;
  $("download-package").disabled = draft;
  $("delete-package").hidden = draft || !versionOwner(data);
  $("version-position").textContent = draft ? "未保存草稿" : version.id === data.head_version_id ? "最新版本" : "历史版本";
  $("version-description").textContent = version.id ? `v${version.number} · ${version.message || "初始导入"} · ${version.author || data.author || ""} · ${formatDate(version.created_at)}` : "正在读取版本历史…";
  const upstream = $("upstream-info");
  upstream.replaceChildren(); upstream.hidden = !data.upstream;
  if (data.upstream) {
    const source = data.upstream;
    upstream.append(node("span", "", "Fork 自 "));
    upstream.append(action(`${source.author || ""} / ${source.title || "源对话库"} · v${source.number || "?"}`, "text-button", () => perform(() => loadPackage(source.package_id, source.version_id))));
    upstream.append(node("span", "subtle", " · 编辑会保留这条来源关系"));
  }
  if (!draft) {
    $("compare-panel").hidden = true;
    $("version-comparison").replaceChildren();
    updateVersionURL(data);
    perform(() => loadVersions(data));
  }
}
async function loadVersions(data) {
  const request = ++versionState.request;
  const response = await api(`/api/packages/${encodeURIComponent(data.id)}/versions`);
  if (request !== versionState.request || !state.reader || state.reader.id !== data.id || isDraft()) return;
  versionState.versions = response.versions || [];
  for (const id of ["version-select", "compare-from", "compare-to"]) {
    const select = $(id); select.replaceChildren();
    for (const version of versionState.versions) {
      const option = node("option", "", `v${version.number} · ${version.message || "初始导入"}`);
      option.value = version.id;
      select.append(option);
    }
  }
  const current = data.version && data.version.id;
  $("version-select").value = current || "";
  $("compare-to").value = current || "";
  const previous = versionState.versions.find((version) => version.id === (data.version && data.version.parent_id));
  $("compare-from").value = previous ? previous.id : (versionState.versions.at(-1) || {}).id || "";
  $("compare-versions").disabled = versionState.versions.length < 2;
}
function startDraft() {
  if (!state.reader || !versionOwner() || !requireSavedEditor()) return;
  state.readerRequest++;
  if (state.reader.version.id !== state.reader.head_version_id) throw new Error("请先切换到最新版本再创建编辑副本。");
  versionState.original = state.reader;
  versionState.preview = null;
  $("draft-title").value = state.reader.title || "";
  $("draft-description").value = state.reader.description || "";
  $("version-message").value = "";
  $("draft-status").textContent = "尚未预览";
  $("save-version").disabled = true;
  $("save-version").textContent = state.reader.visibility === "public" ? "发布新版本" : "保存新版本";
  $("draft-boundary").textContent = state.reader.visibility === "public" ? "草稿只保存在当前页面。发布后，任何人都能阅读新版本，登录后可下载。历史版本保持不变。" : "草稿只保存在当前页面。保存后仅你可见，历史版本保持不变。";
  clearCandidate();
  openReader({ ...structuredClone(state.reader), kind: "draft" });
  showStatus("已创建编辑副本。可以修改正文，或导入对话包替换整份草稿。保存前请预览变化。");
}
function discardDraft() {
  if (!isDraft() || versionState.saving || !window.confirm("丢弃当前草稿？已保存的版本不会改变。")) return;
  const original = versionState.original;
  clearDraftState();
  openReader(original);
  showStatus("草稿已丢弃，正在阅读已保存的版本。");
}
function versionPayload() {
  if (!isDraft()) throw new Error("请先创建编辑副本。");
  if (!requireSavedEditor()) throw new Error("请先完成当前消息的编辑。");
  const title = $("draft-title").value.trim();
  const message = $("version-message").value.trim();
  if (!title) { $("draft-title").focus(); throw new Error("请填写对话库标题。"); }
  if (!message) { $("version-message").focus(); throw new Error("请填写本次保存说明。"); }
  return { base_version: versionState.original.version.id, title, description: $("draft-description").value.trim(), conversations: state.reader.conversations, message };
}
async function previewDraft() {
  versionState.preview = null;
  $("save-version").disabled = true;
  try { await busy($("preview-version"), "计算变化中…", async () => {
    const payload = versionPayload();
    const signature = JSON.stringify(payload);
    const id = state.reader.id;
    const response = await api(`/api/packages/${encodeURIComponent(id)}/preview`, { method: "POST", json: payload });
    if (!isDraft() || state.reader.id !== id || JSON.stringify(versionPayload()) !== signature) return;
    const comparison = response.comparison || response;
    renderComparison($("draft-comparison"), comparison);
    if (response.compatibility) {
      const warnings = node("div"); renderWarnings(warnings, response); $("draft-comparison").append(warnings);
    }
    const changed = (comparison.library_changes || []).length || (comparison.changes || []).length || comparison.conversation_order_changed;
    versionState.preview = changed ? signature : null;
    $("save-version").disabled = !changed;
    $("draft-status").textContent = changed ? "变化已预览，确认后保存" : "没有内容变化，无需保存新版本";
    $("draft-comparison").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }); } catch (error) {
    if (error.status !== 409) throw error;
    $("draft-status").textContent = "出现新版本，草稿已保留";
    showStatus("另一处已保存了新版本，当前草稿已保留。请复制需要保留的修改，再丢弃草稿并打开最新版本。", "warning");
  }
}
async function saveDraft() {
  if (!isDraft() || versionState.saving) return;
  const payload = versionPayload();
  if (versionState.preview !== JSON.stringify(payload)) { draftChanged(); throw new Error("内容已改变，请重新预览后保存。"); }
  versionState.saving = true;
  $("reader-view").inert = true;
  try {
    await busy($("save-version"), "保存中…", async () => {
      const data = await api(`/api/packages/${encodeURIComponent(state.reader.id)}/versions`, { method: "POST", json: payload });
      clearDraftState();
      openReader({ ...data, kind: "hub" });
      await loadPackages();
      showStatus(`v${data.version.number} 已${data.visibility === "public" ? "发布" : "保存"}，可下载或分享这个固定版本。`);
    });
  } catch (error) {
    if (error.status === 409) {
      versionState.preview = null;
      $("save-version").disabled = true;
      $("draft-status").textContent = "出现新版本，草稿已保留";
      showStatus("另一处已保存了新版本。你的草稿仍在当前页面；请复制需要保留的修改，再丢弃草稿并打开最新版本。", "warning");
    } else throw error;
  } finally {
    versionState.saving = false;
    $("reader-view").inert = false;
    if (isDraft()) $("save-version").disabled = !versionState.preview;
  }
}
function renderDraftConversation(conversation) {
  if (!isDraft() || !conversation) return;
  const box = node("div", "conversation-edit-fields");
  const titleLabel = node("label", "field-label", "对话标题"); titleLabel.htmlFor = "draft-conversation-title";
  const title = node("input", "text-input"); title.id = "draft-conversation-title"; title.value = conversation.title || "";
  title.addEventListener("input", () => { conversation.title = title.value; $("conversation-title").textContent = title.value || "未命名对话"; renderConversationTabs(); draftChanged(); });
  const contextLabel = node("label", "field-label", "上下文说明"); contextLabel.htmlFor = "draft-context";
  const context = node("textarea", "text-input"); context.id = "draft-context"; context.rows = 3; context.value = conversation.context || "";
  context.addEventListener("input", () => { conversation.context = context.value; draftChanged(); });
  box.append(titleLabel, title, contextLabel, context);
  $("conversation-provenance").prepend(box);
  const controls = node("div", "new-message-actions");
  controls.append(node("span", "subtle", "继续整理这段对话"));
  for (const role of ["user", "assistant"]) controls.append(action(role === "user" ? "＋ 用户消息" : "＋ 助手消息", "button button-secondary", () => {
    if (!requireSavedEditor()) return;
    const message = { id: typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `message-${Date.now()}-${Math.random().toString(36).slice(2)}`, role, content: "", provenance: "human_authored", transformations: [] };
    conversation.messages.push(message);
    draftChanged(); renderConversationTabs(); renderConversation();
    const section = $("messages").querySelector(".message:last-of-type");
    editMessage(section, section.querySelector(".message-content"), conversation, message, section.querySelector(".message-actions"));
  }));
  $("messages").after(controls);
}
function moveDraftMessage(conversation, index, direction) {
  if (!requireSavedEditor()) return;
  const target = index + direction;
  if (target < 0 || target >= conversation.messages.length) return;
  [conversation.messages[index], conversation.messages[target]] = [conversation.messages[target], conversation.messages[index]];
  draftChanged(); renderConversation();
}
function clearCandidate() {
  versionState.candidate = null;
  versionState.candidateRequest++;
  $("version-file").value = "";
  $("candidate-preview").hidden = true;
  $("candidate-preview").replaceChildren();
  $("candidate-actions").hidden = true;
}
async function inspectCandidate() {
  const file = $("version-file").files[0];
  if (!file || !isDraft()) return;
  const request = ++versionState.candidateRequest;
  versionState.candidate = null;
  $("candidate-actions").hidden = true;
  $("candidate-preview").hidden = false;
  $("candidate-preview").replaceChildren(node("p", "subtle", "正在检查对话包内容…"));
  try {
    validatePackageFile(file);
    const response = await api("/api/inspect", { method: "POST", headers: { "Content-Type": "application/zip" }, body: file });
    if (!isDraft() || request !== versionState.candidateRequest) return;
    versionState.candidate = response;
    renderInspection($("candidate-preview"), response);
    $("candidate-preview").prepend(node("p", "notice warning", "应用此候选会替换整份草稿，包括标题、描述与所有对话。请检查内容，再点击“使用此包替换草稿”。"));
    $("candidate-actions").hidden = false;
  } catch (error) {
    if (request !== versionState.candidateRequest) return;
    $("candidate-preview").replaceChildren(node("p", "notice error", errorMessage(error)));
  }
}
function applyCandidate() {
  if (!isDraft() || !versionState.candidate || !requireSavedEditor()) return;
  const candidate = versionState.candidate;
  state.reader.conversations = structuredClone(candidate.conversations);
  $("draft-title").value = candidate.library.title || "";
  $("draft-description").value = candidate.library.description || "";
  state.conversationIndex = 0;
  clearCandidate(); draftChanged(); renderConversationTabs(); renderConversation();
  showStatus("候选内容已替换草稿。请填写保存说明并预览变化，原有版本仍然保留。");
}
function openForkDialog(source = state.reader) {
  if (!source || source.kind !== "hub" || isDraft()) return;
  if (!state.user) { openAuth("fork", null, structuredClone(source)); return; }
  versionState.forkTarget = source;
  $("fork-form").reset();
  $("fork-description").textContent = `从「${source.title}」的 v${source.version.number} 创建你的独立对话库。`;
  const privateSource = source.visibility === "private";
  const publicRadio = document.querySelector('input[name="fork-visibility"][value="public"]');
  publicRadio.disabled = privateSource;
  $("fork-public-option").hidden = privateSource;
  $("fork-boundary").textContent = privateSource ? "私有来源只能 Fork 为私有对话库。后续编辑保留来源关系，原库不会改变。" : "Fork 保留所选版本的来源；后续编辑只产生你自己的新版本。";
  $("fork-error").hidden = true;
  updateForkLabel();
  $("fork-dialog").showModal();
}
function updateForkLabel() {
  $("fork-submit").textContent = new FormData($("fork-form")).get("fork-visibility") === "public" ? "确认创建公开 Fork" : "创建私有 Fork";
}
async function submitFork(event) {
  event.preventDefault(); $("fork-error").hidden = true;
  try {
    await busy($("fork-submit"), "创建中…", async () => {
      const visibility = new FormData($("fork-form")).get("fork-visibility");
      const original = versionState.forkTarget;
      if (!original) return;
      const data = await api(`/api/packages/${encodeURIComponent(original.id)}/fork`, { method: "POST", json: { version_id: original.version.id, visibility } });
      $("fork-dialog").close();
      const unchanged = state.reader && state.reader.id === original.id && state.reader.version.id === original.version.id && !isDraft();
      if (unchanged) openReader({ ...data, kind: "hub" });
      await loadPackages();
      showStatus(`已创建${visibility === "public" ? "公开" : "私有"} Fork。${unchanged ? "可以编辑、导出并持续保存新版本。" : "可在对话库列表中找到，当前页面内容已保留。"}`);
    });
  } catch (error) { inlineError("fork-error", error); }
  updateForkLabel();
}
function changeName(kind) { return { added: "新增", removed: "删除", modified: "修改", reordered: "重排", moved: "重排" }[kind] || kind || "变化"; }
function fieldName(field) { return { title: "标题", description: "描述", context: "上下文", source: "来源", tags: "标签", annotations: "编辑记录", provenance: "消息来源", transformations: "消息变更记录" }[field] || field; }
function comparisonValue(value) { return typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "（无）"; }
function diffSides(before, after, message = false) {
  const sides = node("div", "diff-sides");
  for (const [label, value, className] of [["之前", before, "diff-before"], ["之后", after, "diff-after"]]) {
    const side = node("div", className);
    side.append(node("h4", "", label));
    if (value === null || value === undefined) side.append(node("p", "subtle", "（无）"));
    else {
      if (message) side.append(node("span", "diff-role", roleLabel(value.role)));
      side.append(node("pre", "", message ? value.content || "" : comparisonValue(value)));
    }
    sides.append(side);
  }
  return sides;
}
function diffMetadata(field) {
  const technical = ["annotations", "source", "provenance", "transformations"].includes(field.field);
  const box = node(technical ? "details" : "div", technical ? "diff-metadata" : "");
  box.append(node(technical ? "summary" : "h4", "diff-field", `${fieldName(field.field)}${technical ? "有变化 · 展开查看" : ""}`), diffSides(field.before, field.after));
  return box;
}
function renderComparison(container, comparison) {
  container.replaceChildren(); container.hidden = false;
  const summary = comparison.summary || {};
  const labels = { conversations_added: "新增对话", conversations_removed: "删除对话", conversations_modified: "修改对话", messages_added: "新增消息", messages_removed: "删除消息", messages_modified: "修改消息", messages_reordered: "重排消息" };
  const counts = node("div", "diff-summary");
  for (const [key, label] of Object.entries(labels)) counts.append(node("span", "pill", `${label} ${summary[key] || 0}`));
  container.append(counts);
  if (comparison.truncated) container.append(node("p", "notice warning", "变化过多，当前预览已截断。请下载两个完整版本检查全部内容；本页未显示的内容仍会保存在新版本中。"));
  if (comparison.conversation_order_changed) container.append(node("p", "diff-order", "↕ 包内对话顺序已改变"));
  for (const field of comparison.library_changes || []) {
    const box = node("details", "diff-change"); box.open = true;
    box.append(node("summary", "", `对话库${fieldName(field.field)}`), diffSides(field.before, field.after)); container.append(box);
  }
  for (const change of comparison.changes || []) {
    const box = node("details", "diff-change"); box.open = true;
    box.append(node("summary", "", `${changeName(change.kind)} · ${change.title || change.conversation_id}`));
    if (change.order_changed) box.append(node("p", "diff-order", "↕ 消息顺序已改变"));
    for (const metadata of change.metadata || []) {
      box.append(diffMetadata(metadata));
    }
    for (const message of change.messages || []) {
      const item = node("div", `diff-message diff-${message.kind}`);
      item.append(node("h4", "", `${changeName(message.kind)}消息 · ${message.id}`), diffSides(message.before, message.after, true));
      if (message.moved && message.kind !== "moved") item.append(node("p", "diff-order", "↕ 这条消息的位置也已改变"));
      for (const metadata of message.metadata || []) item.append(diffMetadata(metadata));
      box.append(item);
    }
    container.append(box);
  }
  if (!(comparison.changes || []).length && !(comparison.library_changes || []).length && !comparison.conversation_order_changed) container.append(node("p", "subtle", "这两个快照没有内容差异。"));
}
async function compareVersions() {
  const from = $("compare-from").value;
  const to = $("compare-to").value;
  if (!from || !to) return;
  const id = state.reader.id;
  await busy($("run-comparison"), "比较中…", async () => {
    const comparison = await api(`/api/packages/${encodeURIComponent(id)}/compare?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`);
    if (state.reader && state.reader.id === id && !isDraft() && $("compare-from").value === from && $("compare-to").value === to) renderComparison($("version-comparison"), comparison.comparison || comparison);
  });
}
function bindVersionEvents() {
  $("version-select").addEventListener("change", () => perform(() => loadPackage(state.reader.id, $("version-select").value)));
  $("copy-version-link").addEventListener("click", () => perform(async () => { await copyText(versionURL(state.reader.id, state.reader.version.id).href); showStatus("固定版本链接已复制；私有版本仍需拥有者登录。 "); }));
  $("edit-package").addEventListener("click", () => perform(startDraft));
  $("discard-draft").addEventListener("click", discardDraft);
  ["draft-title", "draft-description", "version-message"].forEach((id) => $(id).addEventListener("input", draftChanged));
  $("preview-version").addEventListener("click", () => perform(previewDraft));
  $("save-version").addEventListener("click", () => perform(saveDraft));
  $("version-file").addEventListener("change", inspectCandidate);
  $("apply-candidate").addEventListener("click", applyCandidate);
  $("cancel-candidate").addEventListener("click", clearCandidate);
  $("fork-package").addEventListener("click", () => openForkDialog());
  $("fork-form").addEventListener("submit", submitFork);
  document.querySelectorAll('input[name="fork-visibility"]').forEach((radio) => radio.addEventListener("change", updateForkLabel));
  $("compare-versions").addEventListener("click", () => { $("compare-panel").hidden = false; perform(compareVersions); });
  $("close-comparison").addEventListener("click", () => { $("compare-panel").hidden = true; });
  $("run-comparison").addEventListener("click", () => perform(compareVersions));
  window.addEventListener("beforeunload", (event) => { if (isDraft()) { event.preventDefault(); event.returnValue = ""; } });
  window.addEventListener("popstate", () => perform(async () => {
    if (state.mode !== "hub") return;
    const current = state.reader;
    if (!requireSavedEditor() || !leaveDraft()) { if (current && current.id) updateVersionURL(current); else if (contributionState.current && state.view === "contribution") updateContributionURL(contributionState.current.id); return; }
    const parameters = new URLSearchParams(window.location.search);
    if (parameters.has("contribution")) await loadContribution(parameters.get("contribution"));
    else if (parameters.has("package")) await loadPackage(parameters.get("package"), parameters.get("version"));
    else navigate("library");
  }));
}
