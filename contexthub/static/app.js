"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  mode: null, user: null, csrf: null, token: "", view: "library", authMode: "login",
  packages: [], sessions: [], selected: new Set(), reader: null, conversationIndex: 0,
  inspectedFile: null, inspection: null, uploadInspectedFile: null,
  packageRequest: 0, sessionRequest: 0, uploadInspection: 0,
};

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = String(text);
  return item;
}
function action(text, className, handler) {
  const button = node("button", className || "button", text);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}
function showStatus(message, kind = "success") {
  $("status").textContent = message;
  $("status").className = `notice status-message${kind === "error" ? " error" : kind === "warning" ? " warning" : ""}`;
  $("status").setAttribute("role", kind === "error" ? "alert" : "status");
  $("status").hidden = false;
}
function clearStatus() { $("status").hidden = true; }
function errorMessage(error) { return error instanceof Error ? error.message : String(error); }
async function busy(button, label, callback) {
  const previous = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try { return await callback(); }
  finally { button.disabled = false; button.textContent = previous; }
}
async function perform(callback) {
  try { await callback(); }
  catch (error) { showStatus(errorMessage(error), "error"); }
}
async function api(path, options = {}, binary = false) {
  const headers = new Headers(options.headers || {});
  if (path.startsWith("/api/local/")) {
    if (!state.token) throw new Error("缺少本地连接令牌。请用终端启动连接器时显示的完整链接打开页面。");
    headers.set("X-ContextHub-Token", state.token);
  }
  if (options.method && options.method !== "GET" && state.mode === "hub" && state.csrf) headers.set("X-CSRF-Token", state.csrf);
  if (options.json !== undefined) headers.set("Content-Type", "application/json");
  let response;
  try {
    response = await fetch(path, { ...options, credentials: "same-origin", headers,
      body: options.json !== undefined ? JSON.stringify(options.json) : options.body });
  } catch (_) { throw new Error("无法连接服务，请确认 ContextHub 或本地连接器仍在运行。"); }
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try { const body = await response.json(); message = body.error || message; } catch (_) { /* Preserve the HTTP status. */ }
    throw new Error(String(message));
  }
  if (binary) return response.blob();
  if (response.status === 204) return {};
  return response.json();
}
function formatDate(value) {
  if (!value) return "时间未知";
  const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
}
function fileInfo(file) {
  return file ? `${file.name} · ${file.size < 1024 * 1024 ? `${(file.size / 1024).toFixed(1)} KB` : `${(file.size / 1024 / 1024).toFixed(2)} MB`}` : "尚未选择文件";
}
function safeName(title) { return String(title || "conversation").replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").slice(0, 100); }
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = node("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
function warningText(item) {
  const translations = {
    "This package contains selected conversation text and public metadata, not native session state.": "此对话包包含所选对话正文与公开元数据，不包含原生会话状态。",
    "Native UI restoration and paid-model continuation are not verified by package validation.": "对话包格式检查不验证原生界面恢复，也不验证付费模型续聊。",
    "Automatic redaction is best effort; review selected content before publishing.": "自动脱敏无法识别所有敏感信息，请在公开分享前检查所选内容。",
    "Tool messages are historical reference text; importing them must not execute tools.": "工具消息仅作为历史参考文本；导入不会执行其中的工具调用。",
  };
  if (typeof item === "string") return translations[item] || item;
  if (item && typeof item === "object") return item.message || item.warning || item.reason || JSON.stringify(item);
  return String(item);
}
function warningsFrom(data) {
  const output = [];
  const append = (items) => {
    if (Array.isArray(items)) output.push(...items.map(warningText));
    else if (typeof items === "string" && items) output.push(items);
  };
  append(data && data.warnings);
  const compatibility = data && data.compatibility;
  if (Array.isArray(compatibility)) append(compatibility);
  else if (compatibility && typeof compatibility === "object") {
    append(compatibility.warnings);
    append(compatibility.limitations);
    if (compatibility.complete === false) output.push("此包包含不完整的内容，请阅读兼容性报告。");
    if (compatibility.verified === false) output.push("兼容性尚未经过完整验证。");
  }
  for (const conversation of (data && data.conversations) || []) {
    append(conversation.warnings);
    append(conversation.source && conversation.source.warnings);
    if (conversation.source && conversation.source.complete === false) output.push(`「${conversation.title || conversation.id}」的源内容不完整。`);
  }
  return [...new Set(output)];
}
function renderWarnings(container, data) {
  container.replaceChildren();
  const warnings = warningsFrom(data);
  const compatibility = data && data.compatibility;
  container.hidden = !warnings.length && !compatibility;
  if (warnings.length) {
    const box = node("div", "notice warning");
    box.append(node("h3", "", "内容与兼容性说明"));
    const list = node("ul");
    warnings.forEach((warning) => list.append(node("li", "", warning)));
    box.append(list);
    container.append(box);
  }
  if (compatibility) {
    const details = node("details", "provenance");
    details.append(node("summary", "", "查看完整兼容性报告"), node("pre", "", JSON.stringify(compatibility, null, 2)));
    container.append(details);
  }
}
function empty(container, title, description, buttonText, handler) {
  container.replaceChildren();
  const box = node("div", "empty-state");
  box.append(node("span", "empty-symbol", "◇"), node("h2", "", title), node("p", "", description));
  if (buttonText) box.append(action(buttonText, "button button-secondary", handler));
  container.append(box);
}
function navigate(view) {
  if (state.view === "reader" && view !== "reader" && !requireSavedEditor()) return;
  state.view = view;
  for (const name of ["library", "local", "import", "reader", "loading"]) $(`${name}-view`).hidden = name !== view;
  const labels = { library: "对话库", local: "本地会话", import: "导入对话", reader: "阅读对话", loading: "连接中" };
  $("breadcrumb").textContent = labels[view];
  document.querySelectorAll(".nav-item").forEach((button) => {
    const active = button.dataset.view === view || (view === "reader" && button.dataset.view === (state.mode === "connector" ? "local" : "library"));
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  window.scrollTo({ top: 0, behavior: "instant" });
}
function configureNavigation() {
  const nav = $("nav-list");
  nav.replaceChildren();
  const entries = state.mode === "connector" ? [["local", "▤", "本地会话"], ["import", "↓", "导入对话包"]] : [["library", "▦", "对话库"]];
  for (const [view, icon, title] of entries) {
    const button = action("", "nav-item", () => { clearStatus(); navigate(view); });
    button.dataset.view = view;
    button.append(node("span", "nav-icon", icon), node("span", "", title));
    nav.append(button);
  }
  $("mode-label").textContent = state.mode === "connector" ? "本地 Codex 连接器" : "ContextHub · 对话分享";
  $("boundary-note").textContent = state.mode === "connector" ? "仅连接本机 Codex。令牌保存在此标签页，内容不会自动上传。" : "Hub 保存你主动上传的对话包，不会读取电脑上的 Codex 会话。";
  $("account-button").hidden = state.mode === "connector";
}
function updateAccount(session) {
  state.user = session.user || null;
  state.csrf = session.csrf_token || null;
  $("account-name").textContent = state.user ? state.user.username : "";
  $("account-button").textContent = state.user ? "退出登录" : "登录 / 注册";
}
async function loadPackages() {
  const request = ++state.packageRequest;
  const query = $("search-input").value.trim();
  $("package-grid").setAttribute("aria-busy", "true");
  try {
    const response = await api(`/api/packages?q=${encodeURIComponent(query)}`);
    if (request !== state.packageRequest) return;
    state.packages = response.packages || [];
    $("package-count").textContent = `${state.packages.length} 个对话包`;
    const grid = $("package-grid");
    grid.replaceChildren();
    if (!state.packages.length) {
      empty(grid, query ? "没有找到匹配的对话包" : "你的下一段上下文，从这里开始", query ? "试试其他关键词，或清空搜索查看全部对话包。" : "使用本地连接器导出 .contextpack，再上传到这里保存与阅读。", query ? "清空搜索" : "上传第一个对话包", query ? () => { $("search-input").value = ""; perform(loadPackages); } : openUpload);
      return;
    }
    for (const pack of state.packages) {
      const card = node("article", "package-card");
      const top = node("div", "card-top");
      top.append(node("span", "card-icon", "▤"), node("span", `visibility-badge${pack.visibility === "private" ? " private" : ""}`, pack.visibility === "private" ? "仅自己" : "公开"));
      const heading = node("h3");
      heading.append(action(pack.title || "未命名对话包", "card-title", () => perform(() => loadPackage(pack.id))));
      const footer = node("div", "card-bottom");
      const author = node("span", "card-author");
      const username = typeof pack.author === "object" ? pack.author.username : pack.author;
      author.append(node("span", "author-avatar", (username || "?").slice(0, 1).toUpperCase()), node("span", "", username || "未知作者"));
      footer.append(author, node("span", "", `${pack.conversation_count ?? "—"} 段对话 · ${formatDate(pack.created_at)}`));
      card.append(top, heading, node("p", "card-description", pack.description || "这个对话包还没有描述。"), footer);
      grid.append(card);
    }
  } finally { if (request === state.packageRequest) $("package-grid").removeAttribute("aria-busy"); }
}
async function loadPackage(id) {
  clearStatus();
  const pack = await api(`/api/packages/${encodeURIComponent(id)}`);
  openReader({ ...pack, id: pack.id || id, kind: "hub" });
}
function openReader(data) {
  state.reader = data;
  state.conversationIndex = 0;
  const editable = data.kind === "local";
  $("reader-title").textContent = data.title || (editable ? "导出前，检查你的上下文。" : "未命名对话包");
  $("reader-description").textContent = data.description || (editable ? "编辑只作用于本次导出副本。请检查敏感内容，保存后会保留编辑记录。" : "完整保留对话正文、来源与兼容性说明。");
  $("reader-eyebrow").textContent = editable ? "REVIEW BEFORE EXPORT" : "CONVERSATION PACKAGE";
  $("reader-back").textContent = editable ? "← 返回本地会话" : "← 返回对话库";
  $("download-package").textContent = editable ? "导出 .contextpack" : "下载完整对话包";
  $("export-options").hidden = !editable;
  $("export-title").value = data.title || ((data.conversations || []).length === 1 ? data.conversations[0].title || "Codex 对话" : "Codex 对话合集");
  $("export-description").value = data.description || "";
  const author = typeof data.author === "object" ? data.author.username : data.author;
  $("delete-package").hidden = editable || !state.user || author !== state.user.username;
  const metadata = $("reader-metadata");
  metadata.replaceChildren(node("span", "", `${(data.conversations || []).length} 段对话`));
  if (author) metadata.append(node("span", "", `由 ${author} 上传`));
  if (data.visibility) metadata.append(node("span", "visibility-badge", data.visibility === "private" ? "仅自己" : "公开"));
  if (data.created_at) metadata.append(node("span", "", formatDate(data.created_at)));
  if (data.git_commit) metadata.append(node("span", "", `版本 ${String(data.git_commit).slice(0, 8)}`));
  renderWarnings($("reader-warnings"), data);
  renderConversationTabs();
  renderConversation();
  navigate("reader");
}
function renderConversationTabs() {
  const tabs = $("conversation-tabs");
  tabs.replaceChildren();
  (state.reader.conversations || []).forEach((conversation, index) => {
    const tab = action("", `conversation-tab${index === state.conversationIndex ? " active" : ""}`, () => {
      if (!requireSavedEditor()) return;
      state.conversationIndex = index;
      renderConversationTabs(); renderConversation();
    });
    tab.append(node("span", "", conversation.title || `对话 ${index + 1}`), node("small", "", `${(conversation.messages || []).length} 条消息`));
    tab.setAttribute("aria-pressed", String(index === state.conversationIndex));
    tabs.append(tab);
  });
}
function currentConversation() { return state.reader && (state.reader.conversations || [])[state.conversationIndex]; }
function roleLabel(role) { return { user: "你", assistant: "助手", tool: "工具记录", system: "系统记录", developer: "开发者记录" }[role] || role || "未知角色"; }
function provenanceRecord(conversation) {
  return { id: conversation.id, source: conversation.source || {}, context: conversation.context || "", tags: conversation.tags || [], annotations: conversation.annotations || [] };
}
function renderConversation() {
  const conversation = currentConversation();
  const messages = $("messages");
  messages.replaceChildren();
  $("conversation-provenance").replaceChildren();
  $("conversation-title").textContent = conversation ? conversation.title || "未命名对话" : "没有可阅读的对话";
  $("copy-conversation").disabled = !conversation;
  $("save-conversation").disabled = !conversation;
  if (!conversation) { empty(messages, "没有对话内容", "此对话包未包含可阅读的对话。"); return; }
  const provenance = node("details", "provenance");
  provenance.append(node("summary", "", "来源、上下文与编辑记录"), node("pre", "", JSON.stringify(provenanceRecord(conversation), null, 2)), action("复制来源记录", "text-button", () => perform(async () => { await copyText(JSON.stringify(provenanceRecord(conversation), null, 2)); showStatus("来源记录已复制。"); })));
  $("conversation-provenance").append(provenance);
  const editable = state.reader.kind === "local";
  (conversation.messages || []).forEach((message, index) => {
    const section = node("section", `message ${message.role === "assistant" ? "assistant" : "user"}`);
    const header = node("div", "message-heading");
    header.append(node("span", "message-avatar", message.role === "assistant" ? "AI" : message.role === "user" ? "U" : "·"), node("span", "message-label", roleLabel(message.role)));
    const transformations = message.transformations || [];
    const edited = transformations.some((item) => item === "human_edited" || item && (item.type === "human_edited" || item.kind === "human_edited"));
    header.append(node("span", "message-provenance", edited ? "已编辑 · 保留来源" : message.provenance ? `来源：${typeof message.provenance === "string" ? message.provenance : message.provenance.platform || message.provenance.provider || "导入记录"}` : ""));
    const content = node("pre", "message-content", message.content || "");
    if (editable) {
      const controls = node("div", "message-actions");
      controls.append(action("编辑", "text-button", () => editMessage(section, content, conversation, message, controls)), action("移除", "text-button danger", () => {
        if (!window.confirm("从本次导出中移除这条消息？本地原始会话不受影响。")) return;
        conversation.messages.splice(index, 1);
        if (!Array.isArray(conversation.annotations)) conversation.annotations = [];
        conversation.annotations.push({ kind: "human_edited", text: `message_removed at ${new Date().toISOString()}`, message_id: message.id });
        renderConversationTabs(); renderConversation();
        showStatus("消息已从导出副本中移除，编辑记录已保留。");
      }));
      header.append(controls);
    }
    section.append(header, content);
    if (transformations.length) {
      const history = node("details", "provenance");
      history.append(node("summary", "", "查看本条消息的变更记录"), node("pre", "", JSON.stringify(transformations, null, 2)));
      section.append(history);
    }
    messages.append(section);
  });
  if (!(conversation.messages || []).length) empty(messages, "没有可显示的消息", "请查看兼容性说明；如果消息被移除，可以返回列表重新预览原始会话。");
}
function requireSavedEditor() {
  const editor = $("messages").querySelector("textarea");
  if (!editor) return true;
  showStatus("请先保存或取消正在编辑的消息。", "warning");
  editor.focus();
  return false;
}
function editMessage(section, content, conversation, message, controls) {
  if (!requireSavedEditor()) return;
  controls.hidden = true;
  content.hidden = true;
  const textarea = node("textarea", "message-editor");
  textarea.value = message.content || "";
  textarea.setAttribute("aria-label", `编辑${roleLabel(message.role)}的消息`);
  const footer = node("div", "editor-help", "保存后将标记为人工编辑。原始 Codex 会话不会改变。");
  const buttons = node("div", "message-actions");
  buttons.append(action("保存修改", "button button-small", () => {
    if (message.content !== textarea.value) {
      message.content = textarea.value;
      if (!Array.isArray(message.transformations)) message.transformations = [];
      if (!message.transformations.includes("human_edited")) message.transformations.push("human_edited");
      if (!Array.isArray(conversation.annotations)) conversation.annotations = [];
      conversation.annotations.push({ kind: "human_edited", text: `message_edited at ${new Date().toISOString()}`, message_id: message.id });
      showStatus("修改已保存到导出副本，并保留人工编辑记录。");
    }
    renderConversation();
  }), action("取消", "button button-small", renderConversation));
  section.append(textarea, footer, buttons);
  textarea.focus();
}
function markdown(conversation) {
  const parts = [`# ${conversation.title || "未命名对话"}`, "", `来源 ID: ${conversation.id || "未知"}`, "", "## 来源记录", "", "```json", JSON.stringify(provenanceRecord(conversation), null, 2), "```", ""];
  for (const message of conversation.messages || []) {
    parts.push(`## ${roleLabel(message.role)}`, "", message.content || "", "");
    if ((message.transformations || []).length) parts.push(`变更记录: ${JSON.stringify(message.transformations)}`, "");
  }
  return parts.join("\n");
}
function conversationText() {
  if (!requireSavedEditor()) throw new Error("请先保存或取消正在编辑的消息，再复制或下载。");
  const conversation = currentConversation();
  if (!conversation) throw new Error("没有选择对话。");
  return $("copy-format").value === "json" ? JSON.stringify(conversation, null, 2) : markdown(conversation);
}
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return; }
  const area = node("textarea", "sr-only");
  area.value = text;
  document.body.append(area); area.select();
  const copied = document.execCommand("copy");
  area.remove();
  if (!copied) throw new Error("浏览器不允许复制，请使用“保存”下载内容。");
}
async function downloadCurrentPackage() {
  if (!state.reader) return;
  if ($( "messages").querySelector("textarea")) throw new Error("请先保存或取消正在编辑的消息，再导出。");
  await busy($("download-package"), "正在准备…", async () => {
    if (state.reader.kind === "local") {
      const title = $("export-title").value.trim();
      if (!title) { $("export-title").focus(); throw new Error("请为对话包填写标题。"); }
      if (!(state.reader.conversations || []).some((conversation) => (conversation.messages || []).length)) throw new Error("请至少保留一条消息后再导出。");
      const blob = await api("/api/local/export", { method: "POST", json: { conversations: state.reader.conversations, title, description: $("export-description").value.trim(), redact: $("redact-secrets").checked } }, true);
      saveBlob(blob, `${safeName(title)}.contextpack`);
      showStatus("对话包已生成并开始下载。自动脱敏无法覆盖所有敏感信息，请在分享前检查。");
    } else {
      const blob = await api(`/api/packages/${encodeURIComponent(state.reader.id)}/download`, {}, true);
      saveBlob(blob, `${safeName(state.reader.title)}.contextpack`);
      showStatus("完整对话包已开始下载。");
    }
  });
}
async function deleteCurrentPackage() {
  if (!state.reader || !window.confirm(`删除「${state.reader.title || "这个对话包"}」？删除后将无法通过 Hub 下载。`)) return;
  await busy($("delete-package"), "删除中…", async () => {
    await api(`/api/packages/${encodeURIComponent(state.reader.id)}`, { method: "DELETE" });
    state.reader = null; navigate("library"); await loadPackages(); showStatus("对话包已删除。");
  });
}
function setAuthMode(mode) {
  state.authMode = mode;
  const registering = mode === "register";
  $("auth-title").textContent = registering ? "创建你的工作空间" : "欢迎回来";
  $("auth-description").textContent = registering ? "创建账户，保存和管理你的对话包。" : "登录后上传、管理你的对话包。";
  $("auth-submit").textContent = registering ? "创建账户" : "登录";
  $("login-tab").classList.toggle("active", !registering);
  $("register-tab").classList.toggle("active", registering);
  $("password").autocomplete = registering ? "new-password" : "current-password";
  $("password").minLength = registering ? 8 : 1;
  $("auth-error").hidden = true;
}
function openUpload() {
  clearStatus();
  if (!state.user) { setAuthMode("login"); $("auth-dialog").dataset.next = "upload"; $("auth-dialog").showModal(); return; }
  $("upload-error").hidden = true;
  $("upload-dialog").showModal();
}
function inlineError(id, error) { $(id).textContent = errorMessage(error); $(id).hidden = false; }
async function submitAuth(event) {
  event.preventDefault();
  $("auth-error").hidden = true;
  try {
    await busy($("auth-submit"), "请稍候…", async () => {
      const session = await api(`/api/${state.authMode}`, { method: "POST", json: { username: $("username").value.trim(), password: $("password").value } });
      updateAccount(session);
      $("password").value = "";
      const next = $("auth-dialog").dataset.next;
      delete $("auth-dialog").dataset.next;
      $("auth-dialog").close();
      await loadPackages();
      showStatus(state.authMode === "register" ? "账户已创建。" : "已登录。");
      if (next === "upload") openUpload();
    });
  } catch (error) { inlineError("auth-error", error); }
}
async function accountAction() {
  if (!state.user) { delete $("auth-dialog").dataset.next; setAuthMode("login"); $("auth-dialog").showModal(); return; }
  await api("/api/logout", { method: "POST", json: {} });
  updateAccount({ user: null, csrf_token: null });
  state.reader = null;
  state.uploadInspectedFile = null;
  $("upload-submit").disabled = true;
  navigate("library"); await loadPackages(); showStatus("已退出登录。");
}
function renderInspection(container, data) {
  container.replaceChildren();
  container.hidden = false;
  const conversations = data.conversations || [];
  const library = data.library || {};
  const summary = node("div", "inspect-summary");
  summary.append(node("h3", "", library.title || data.title || "对话包内容"), node("p", "", `${conversations.length} 段对话 · ${conversations.reduce((count, conversation) => count + (conversation.messages || []).length, 0)} 条消息`));
  if (library.description || data.description) summary.append(node("p", "", library.description || data.description));
  container.append(summary);
  const warnings = node("div"); renderWarnings(warnings, data); container.append(warnings);
  conversations.forEach((conversation) => {
    const details = node("details", "inspect-conversation");
    details.append(node("summary", "", `${conversation.title || "未命名对话"} · ${(conversation.messages || []).length} 条消息`));
    details.append(node("pre", "", `来源：${JSON.stringify(conversation.source || {}, null, 2)}`));
    for (const message of conversation.messages || []) {
      details.append(node("pre", "", `${roleLabel(message.role)}\n\n${message.content || ""}`));
    }
    container.append(details);
  });
}
async function inspectUploadFile() {
  const file = $("upload-file").files[0];
  const request = ++state.uploadInspection;
  state.uploadInspectedFile = null;
  $("upload-submit").disabled = true;
  $("upload-file-info").textContent = fileInfo(file);
  $("upload-error").hidden = true;
  $("upload-preview").hidden = true;
  $("upload-preview").replaceChildren();
  if (!file) return;
  $("upload-file-info").textContent = `${fileInfo(file)} · 正在检查内容…`;
  try {
    const response = await api("/api/inspect", { method: "POST", headers: { "Content-Type": "application/zip" }, body: file });
    if (request !== state.uploadInspection) return;
    state.uploadInspectedFile = file;
    $("upload-file-info").textContent = `${fileInfo(file)} · 格式检查通过`;
    renderInspection($("upload-preview"), response);
    $("upload-submit").disabled = false;
  } catch (error) {
    if (request !== state.uploadInspection) return;
    $("upload-file-info").textContent = fileInfo(file);
    inlineError("upload-error", error);
  }
}
async function submitUpload(event) {
  event.preventDefault();
  $("upload-error").hidden = true;
  const file = $("upload-file").files[0];
  if (!file || file !== state.uploadInspectedFile) { inlineError("upload-error", new Error("请先选择文件，并等待内容检查完成。")); return; }
  try {
    await busy($("upload-submit"), "上传中…", async () => {
      const visibility = new FormData($("upload-form")).get("visibility");
      const data = await api(`/api/packages?visibility=${encodeURIComponent(visibility)}`, { method: "POST", headers: { "Content-Type": "application/zip" }, body: file });
      $("upload-dialog").close(); $("upload-form").reset();
      state.uploadInspectedFile = null;
      $("upload-file-info").textContent = "尚未选择文件";
      $("upload-preview").replaceChildren(); $("upload-preview").hidden = true;
      if (data.conversations) openReader({ ...data, kind: "hub" }); else await loadPackage(data.id);
      showStatus(visibility === "public" ? "已上传为公开对话包，任何访问此 Hub 的人均可阅读与下载。" : "已上传为私有对话包，仅你的账户可见。");
      await loadPackages();
    });
  } catch (error) { inlineError("upload-error", error); }
  $("upload-submit").disabled = !state.uploadInspectedFile;
  updateUploadLabel();
}
function updateUploadLabel() {
  const visibility = new FormData($("upload-form")).get("visibility");
  $("upload-submit").textContent = visibility === "public" ? "确认公开上传" : "上传为私有对话包";
}
async function loadSessions() {
  const request = ++state.sessionRequest;
  const parameters = new URLSearchParams();
  if ($("include-archived").checked) parameters.set("archived", "1");
  if ($("include-subagents").checked) parameters.set("subagents", "1");
  $("session-rows").setAttribute("aria-busy", "true");
  try {
    const data = await api(`/api/local/sessions?${parameters}`);
    if (request !== state.sessionRequest) return;
    state.sessions = Array.isArray(data) ? data : data.sessions || data.threads || [];
    const ids = new Set(state.sessions.map((session) => session.id || session.thread_id));
    state.selected = new Set([...state.selected].filter((id) => ids.has(id)));
    renderSessions();
    if (warningsFrom(data).length) showStatus(warningsFrom(data).join("；"), "warning");
  } finally { if (request === state.sessionRequest) $("session-rows").removeAttribute("aria-busy"); }
}
function renderSessions() {
  const body = $("session-rows"); body.replaceChildren();
  $("session-count").textContent = `${state.sessions.length} 个会话`;
  $("session-empty").hidden = Boolean(state.sessions.length);
  if (!state.sessions.length) {
    $("session-empty").replaceChildren(node("span", "empty-symbol", "◇"), node("h2", "", "没有找到本地会话"), node("p", "", "请确认 Codex 已创建会话，也可以尝试包含已归档会话或子代理。"));
  }
  for (const session of state.sessions) {
    const id = session.id || session.thread_id;
    const row = node("tr");
    const checkCell = node("td");
    const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = state.selected.has(id);
    checkbox.setAttribute("aria-label", `选择 ${session.title || session.preview || id}`);
    checkbox.addEventListener("change", () => { if (checkbox.checked) state.selected.add(id); else state.selected.delete(id); updateSelection(); });
    checkCell.append(checkbox);
    const title = node("td");
    title.append(node("span", "session-title", session.title || session.name || session.preview || "未命名会话"));
    if (session.preview || session.cwd) title.append(node("span", "session-preview", session.preview || session.cwd));
    let source = session.source;
    if (source && typeof source === "object") source = source.type || source.kind || source.platform || "Codex";
    row.append(checkCell, title, node("td", "", formatDate(session.updated_at || session.updatedAt || session.created_at || session.createdAt)), node("td", "session-source", `${source || "Codex"}${session.archived ? " · 已归档" : ""}`));
    body.append(row);
  }
  updateSelection();
}
function updateSelection() {
  const count = state.selected.size;
  $("preview-selected").disabled = !count;
  $("preview-selected").textContent = count ? `预览所选 ${count} 段对话` : "预览所选对话";
  $("selection-note").textContent = count ? `已选择 ${count} 个会话。预览不会修改原始会话，也不会调用模型。` : "选择会话后即可预览，原始会话不会被修改。";
  $("select-all").checked = state.sessions.length > 0 && count === state.sessions.length;
  $("select-all").indeterminate = count > 0 && count < state.sessions.length;
  $("select-all").disabled = !state.sessions.length;
}
async function previewSelected() {
  if (!state.selected.size) return;
  await busy($("preview-selected"), "读取对话中…", async () => {
    const data = await api("/api/local/preview", { method: "POST", json: { thread_ids: [...state.selected] } });
    openReader({ ...data, kind: "local" });
    if (!(data.conversations || []).length) showStatus("未读取到可导出的对话，请查看兼容性说明。", "warning");
  });
  updateSelection();
}
function resetImportFile() {
  const file = $("import-file").files[0];
  $("import-file-info").textContent = fileInfo(file);
  state.inspectedFile = null; state.inspection = null;
  $("inspect-package").disabled = !file;
  $("import-confirm").disabled = true;
  $("inspect-result").hidden = true; $("inspect-result").replaceChildren();
  $("import-receipt").hidden = true; $("import-receipt").replaceChildren();
}
async function inspectImport() {
  const file = $("import-file").files[0];
  if (!file) return;
  await busy($("inspect-package"), "检查中…", async () => {
    const data = await api("/api/local/inspect", { method: "POST", headers: { "Content-Type": "application/zip" }, body: file });
    if (file !== $("import-file").files[0]) return;
    state.inspectedFile = file; state.inspection = data;
    renderInspection($("inspect-result"), data);
    updateImportButton();
    showStatus("检查完成。请阅读内容与兼容性说明，填写目标工作目录后确认导入。");
  });
}
function updateImportButton() {
  $("import-confirm").disabled = !state.inspectedFile || !$("import-cwd").value.trim();
}
async function importPackage() {
  const cwd = $("import-cwd").value.trim();
  const file = $("import-file").files[0];
  if (!cwd) throw new Error("请填写目标工作目录。");
  if (!file || file !== state.inspectedFile) throw new Error("请先检查当前选择的对话包。");
  await busy($("import-confirm"), "创建新会话中…", async () => {
    const data = await api(`/api/local/import?cwd=${encodeURIComponent(cwd)}`, { method: "POST", headers: { "Content-Type": "application/zip" }, body: file });
    renderReceipt(data);
    state.inspectedFile = null;
    showStatus("导入请求已完成，请查看每个会话的回执与验证范围。");
    await loadSessions();
  });
  updateImportButton();
}
function renderReceipt(data) {
  const container = $("import-receipt");
  container.hidden = false;
  container.replaceChildren(node("h2", "", "导入回执"), node("p", "footnote", "成功返回会话 ID 不代表原生界面与模型续聊已验证，请以回执说明为准。"));
  const results = data.results || [];
  if (!results.length) container.append(node("div", "notice warning", "服务没有返回会话导入回执，请检查连接器终端日志。"));
  results.forEach((result, index) => {
    const box = node("div", "receipt-item");
    box.append(node("h3", "", result.title || `会话 ${index + 1}`));
    const failed = Boolean(result.error) || result.mode === "failed" || result.injected === false;
    if (failed) box.append(node("p", "inline-error", `导入未完成${result.error ? `：${warningText(result.error)}` : "，请查看下方原因；已创建的空会话可能仍然存在。"}`));
    if (result.thread_id) {
      const line = node("p", "", failed ? "已创建会话 ID（内容导入未完成）：" : "新会话 ID："); line.append(node("code", "", result.thread_id)); box.append(line);
      box.append(action("复制会话 ID", "button button-small", () => perform(async () => { await copyText(result.thread_id); showStatus("会话 ID 已复制。"); })));
    }
    if (result.mode) box.append(node("p", "", `恢复方式：${result.mode}`));
    if (result.verified !== undefined) {
      box.append(node("p", "", `服务端验证结果：${typeof result.verified === "boolean" ? result.verified ? "公开 API 内容读回匹配（不代表重启、原生界面或模型续聊已验证）" : "尚未验证" : JSON.stringify(result.verified)}`));
    }
    const warnings = node("div"); renderWarnings(warnings, result); box.append(warnings);
    const details = node("details", "provenance"); details.append(node("summary", "", "完整回执"), node("pre", "", JSON.stringify(result, null, 2))); box.append(details);
    container.append(box);
  });
  container.scrollIntoView({ behavior: "smooth", block: "start" });
}
function bindEvents() {
  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => $(button.dataset.close).close()));
  $("account-button").addEventListener("click", () => perform(accountAction));
  $("open-upload").addEventListener("click", openUpload);
  $("search-form").addEventListener("submit", (event) => { event.preventDefault(); perform(loadPackages); });
  $("login-tab").addEventListener("click", () => setAuthMode("login"));
  $("register-tab").addEventListener("click", () => setAuthMode("register"));
  $("auth-form").addEventListener("submit", submitAuth);
  $("upload-form").addEventListener("submit", submitUpload);
  $("upload-file").addEventListener("change", inspectUploadFile);
  document.querySelectorAll('input[name="visibility"]').forEach((radio) => radio.addEventListener("change", updateUploadLabel));
  $("reader-back").addEventListener("click", () => { clearStatus(); navigate(state.mode === "connector" ? "local" : "library"); });
  $("download-package").addEventListener("click", () => perform(downloadCurrentPackage));
  $("delete-package").addEventListener("click", () => perform(deleteCurrentPackage));
  $("copy-conversation").addEventListener("click", () => perform(async () => { await copyText(conversationText()); showStatus(`${$("copy-format").value === "json" ? "JSON" : "Markdown"} 已复制。`); }));
  $("save-conversation").addEventListener("click", () => perform(async () => {
    const format = $("copy-format").value;
    saveBlob(new Blob([conversationText()], { type: format === "json" ? "application/json;charset=utf-8" : "text/markdown;charset=utf-8" }), `${safeName(currentConversation().title)}.${format === "json" ? "json" : "md"}`);
    showStatus("当前对话已开始下载。");
  }));
  $("refresh-local").addEventListener("click", () => perform(() => busy($("refresh-local"), "刷新中…", loadSessions)));
  ["include-archived", "include-subagents"].forEach((id) => $(id).addEventListener("change", () => perform(loadSessions)));
  $("select-all").addEventListener("change", () => { state.selected = $("select-all").checked ? new Set(state.sessions.map((session) => session.id || session.thread_id)) : new Set(); renderSessions(); });
  $("preview-selected").addEventListener("click", () => perform(previewSelected));
  $("import-file").addEventListener("change", resetImportFile);
  $("inspect-package").addEventListener("click", () => perform(inspectImport));
  $("import-cwd").addEventListener("input", updateImportButton);
  $("import-confirm").addEventListener("click", () => perform(importPackage));
}
function acquireToken() {
  const fragment = window.location.hash.slice(1);
  let token = "";
  if (fragment) {
    const parameters = new URLSearchParams(fragment);
    token = parameters.get("token") || parameters.get("access_token") || "";
    if (!token && !fragment.includes("=")) {
      try { token = decodeURIComponent(fragment); } catch (_) { token = ""; }
    }
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }
  try {
    if (token) sessionStorage.setItem("contexthub-connector-token", token);
    state.token = token || sessionStorage.getItem("contexthub-connector-token") || "";
  } catch (_) { state.token = token; }
}
async function initialize() {
  // The token is removed from the visible URL before any API requests are made.
  acquireToken();
  bindEvents();
  try {
    const config = await api("/api/config");
    state.mode = config.mode === "connector" ? "connector" : "hub";
    configureNavigation();
    if (state.mode === "connector") {
      navigate("local");
      await loadSessions();
    } else {
      updateAccount(await api("/api/session"));
      navigate("library");
      await loadPackages();
    }
  } catch (error) {
    showStatus(errorMessage(error), "error");
    if (!state.mode) {
      empty($("loading-view"), "暂时无法连接工作空间", "确认服务已启动后，重试连接。", "重试", () => window.location.reload());
    }
  }
}
initialize();
