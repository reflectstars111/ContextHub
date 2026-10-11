"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  mode: null, user: null, csrf: null, token: "", view: "library", authMode: "login",
  packages: [], sessions: [], selected: new Set(), reader: null, conversationIndex: 0, turnKey: null, turnPage: 0,
  inspectedFile: null, inspection: null, uploadInspectedFile: null, uploadSnapshot: null, uploadPending: false, uploadLimit: null, authIntent: null,
  packageRequest: 0, sessionRequest: 0, uploadInspection: 0, readerRequest: 0,
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
    const error = new Error(String(message));
    error.status = response.status;
    throw error;
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
function configureUploadLimit(config) {
  const limits = [config.max_body_bytes, config.max_package_bytes].filter((value) => Number.isFinite(value) && value > 0);
  state.uploadLimit = limits.length ? Math.min(...limits) : null;
  document.querySelectorAll("[data-upload-limit]").forEach((element) => {
    element.textContent = state.uploadLimit ? `当前服务单个对话包上限：${formatByteLimit(state.uploadLimit)}。` : "文件大小限制以服务端检查结果为准。";
  });
}
function formatByteLimit(bytes) {
  return `${Number((bytes / 1024 / 1024).toFixed(2))} MiB`;
}
function validatePackageFile(file) {
  if (file && state.uploadLimit && file.size > state.uploadLimit) throw new Error(`文件超过当前服务的 ${formatByteLimit(state.uploadLimit)} 上限，请减少选中的对话后重新导出。`);
}
const PLATFORM_LABELS = Object.freeze({ codex: "Codex", chatgpt: "ChatGPT", claude: "Claude", deepseek: "DeepSeek", other: "其他" });
function normalizePlatform(value) {
  const aliases = { "codex-cli": "codex", "codex cli": "codex", "openai codex": "codex", "openai-codex": "codex", "chat-gpt": "chatgpt", "chat gpt": "chatgpt", "openai chatgpt": "chatgpt", "claude-code": "claude", "claude code": "claude", "anthropic claude": "claude", "deep-seek": "deepseek", "deep seek": "deepseek", "deepseek-harness": "deepseek", "deepseek harness": "deepseek", "dsh": "deepseek" };
  const key = typeof value === "string" ? value.trim().toLowerCase() : "";
  return Object.hasOwn(PLATFORM_LABELS, key) ? key : Object.hasOwn(aliases, key) ? aliases[key] : "other";
}
function platformLabel(value) { return PLATFORM_LABELS[normalizePlatform(value)]; }
function platformBadges(platforms) {
  const badges = node("div", "platform-badges");
  const values = Array.isArray(platforms) && platforms.length ? [...new Set(platforms)] : ["other"];
  values.forEach((platform) => {
    const badge = node("span", "platform-badge", platformLabel(platform));
    badge.title = "声明的来源分类，不代表平台认证或支持原平台回导";
    badges.append(badge);
  });
  return badges;
}
function conversationPlatform(conversation) {
  const source = conversation && conversation.source || {};
  const fields = ["platform", "adapter", "provider", "platform_claimed"];
  const hasClaim = fields.some((field) => typeof source[field] === "string" && source[field].trim());
  const claims = fields.map((field) => (hasClaim ? source : state.reader && state.reader.library_source || {})[field]);
  for (const claim of claims) {
    const platform = normalizePlatform(claim);
    if (platform !== "other" || typeof claim === "string" && claim.trim().toLowerCase() === "other") return platform;
  }
  return "other";
}
function isSourceArchive(conversation) { return Boolean(conversation && conversation.source && conversation.source.kind === "source_archive"); }
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
    "Reconstructed user/assistant text timeline with new IDs and import timestamps; original turn boundaries, timestamps and native state are not restored": "以新会话 ID 和导入时间重建用户/助手正文；不保留原始回合边界、时间和完整运行状态。",
    "Tools, attachments, hidden reasoning, instructions, permissions and credentials are not migrated; no model turn was started": "未迁移工具、附件、隐藏推理、指令权限和凭据；没有启动模型调用。",
    "Native GUI rendering and model continuation are not tested by this import": "此次导入不验证原生图形界面呈现或模型续聊。",
    "Hidden conversation messages were omitted; private internal text is not imported.": "已省略隐藏消息，不导入内部私有文本。",
    "User system-instruction messages were omitted.": "已省略用户设置的系统指令消息。",
    "Analysis/reasoning or unsupported message channels were omitted; only visible conversation text is imported.": "已省略分析、推理或不支持的消息通道，仅导入可见对话正文。",
    "Tool-directed messages were omitted; imported text cannot execute tools.": "已省略发给工具的消息；导入正文不会执行工具。",
    "This package contains selected conversation text and public metadata, not native session state.": "此对话包包含所选对话正文与公开元数据，不包含原生会话状态。",
    "Native UI restoration and paid-model continuation are not verified by package validation.": "对话包格式检查不验证原生界面恢复，也不验证付费模型续聊。",
    "Automatic redaction is best effort; review selected content before publishing.": "自动脱敏无法识别所有敏感信息，请在公开分享前检查所选内容。",
    "Tool messages are historical reference text; importing them must not execute tools.": "工具消息仅作为历史参考文本；导入不会执行其中的工具调用。",
    "Reasoning/thinking content was omitted; only visible conversation text is imported.": "已略过思考过程内容，仅导入可见的对话正文。",
    "Image/audio/video content was omitted; media bytes are not imported.": "已略过图片、音频或视频，不导入这些媒体文件。",
    "Tool calls and tool results were omitted; imported text cannot execute tools.": "已略过工具调用和工具结果；导入的文本不会执行工具。",
    "Attachments/files were omitted; only visible conversation text is imported.": "已略过附件和文件，仅导入可见的对话正文。",
    "Unsupported content blocks were omitted; review the extracted text before saving.": "已略过暂不支持的内容块，请在保存前检查提取的正文。",
    "Non-text message content was omitted.": "已略过消息中的非文本内容。",
    "Messages with unknown roles were omitted.": "已略过无法识别角色的消息。",
    "System instruction messages were omitted; privileged instructions are not portable.": "已略过系统指令消息；不会迁移其指令权限。",
    "Developer instruction messages were omitted; privileged instructions are not portable.": "已略过开发者指令消息；不会迁移其指令权限。",
    "Messages without readable text were omitted.": "已略过没有可读正文的消息。",
    "Only the ChatGPT current_node branch is imported; other branches were omitted.": "仅导入 ChatGPT 当前选中的对话分支，其他分支已略过。",
    "Fragment-based exports have no verified stable schema; review the extracted response text.": "此类片段格式尚无经过验证的稳定规范，请检查提取的回复正文。",
    "Session logs are imported as historical visible text in file order, not a reconstructed active branch or resumable native session.": "会话日志按文件顺序保存为可见的历史正文；没有重建当前分支或可恢复的原生会话。",
    "Injected runtime/plugin context was omitted; only user-authored input is imported.": "已略过运行环境或插件注入的上下文，仅导入用户撰写的输入。",
    "System/developer instruction events were omitted.": "已略过系统或开发者指令事件。",
    "Runtime, progress, and other non-message events were omitted.": "已略过运行状态、进度等非消息事件。",
    "Other ZIP files, account data, attachments, and subagent logs were omitted.": "已略过 ZIP 中的其他文件、账户资料、附件及子代理日志。",
    "Original text is preserved without automatic redaction; it may contain secrets, account data, or private paths. Review it before sharing.": "原文将保留且不会自动脱敏，可能包含密钥、账户资料或私人路径。请在分享前检查。",
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
  append(data && data.import_report && data.import_report.warnings);
  if (data && data.import_report && data.import_report.partial) output.push("部分内容未转换为对话正文，请阅读导入报告中的遗漏说明。");
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
  container.hidden = !warnings.length && !compatibility && !(data && data.import_report);
  if (warnings.length) {
    const box = node("div", "notice warning");
    box.append(node("h3", "", "内容与兼容性说明"));
    const list = node("ul");
    warnings.forEach((warning) => list.append(node("li", "", warning)));
    box.append(list);
    container.append(box);
  }
  if (data && data.import_report) {
    const details = node("details", "provenance");
    details.append(node("summary", "", "查看完整导入报告"), node("pre", "", JSON.stringify(data.import_report, null, 2)));
    container.append(details);
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
  if (state.view === "reader" && view !== "reader" && (!requireSavedEditor() || !leaveDraft())) return false;
  if (view !== "reader" && typeof closeCodexDialog === "function") closeCodexDialog();
  rememberContributionComment();
  if (view !== "contribution") invalidateContributionReads();
  if (view !== "reader") { contributionState.listRequest++; state.readerRequest++; }
  if (!["reader", "contribution"].includes(view) && ["reader", "contribution"].includes(state.view)) clearVersionURL();
  state.view = view;
  for (const name of ["library", "local", "import", "reader", "contribution", "loading"]) $(`${name}-view`).hidden = name !== view;
  const labels = { library: "对话库", local: "本地会话", import: "导入对话", reader: "阅读对话", contribution: "贡献请求", loading: "连接中" };
  $("breadcrumb").textContent = labels[view];
  document.querySelectorAll(".nav-item").forEach((button) => {
    const active = button.dataset.view === view || (["reader", "contribution"].includes(view) && button.dataset.view === (state.mode === "connector" ? "local" : "library"));
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  window.scrollTo({ top: 0, behavior: "instant" });
  return true;
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
  $("mode-label").textContent = state.mode === "connector" ? "本地 Codex 连接器" : "Vibe coding · 对话版本管理";
  $("boundary-note").textContent = state.mode === "connector" ? "仅连接本机 Codex。令牌保存在此标签页，内容不会自动上传。" : "Hub 保存你主动上传的对话文件。来源分类不代表平台认证，也不保证原平台回导。";
  $("account-button").hidden = state.mode === "connector";
}
function updateAccount(session) {
  state.user = session.user || null;
  state.csrf = session.csrf_token || null;
  if (isDraft() && (!state.user || state.user.id !== draftStorage.owner)) {
    clearTimeout(draftStorage.timer);
    draftStorageStatus("登录已失效或账户已切换；自动保存暂停，本页内容仍可下载备份", true);
  }
  $("account-name").textContent = state.user ? state.user.username : "";
  $("account-button").textContent = state.user ? "退出登录" : "登录 / 注册";
  if (state.view === "reader" && state.reader && state.reader.kind === "hub") renderVersionWorkspace(state.reader);
  if (state.view === "contribution" && contributionState.current) perform(() => loadContribution(contributionState.current.id));
}
async function loadPackages() {
  const request = ++state.packageRequest;
  const query = $("search-input").value.trim();
  $("package-grid").setAttribute("aria-busy", "true");
  try {
    const parameters = new URLSearchParams({ q: query });
    const platform = $("platform-filter").value;
    if (platform !== "all") parameters.set("platform", platform);
    const response = await api(`/api/packages?${parameters}`);
    if (request !== state.packageRequest) return;
    state.packages = response.packages || [];
    $("package-count").textContent = `${state.packages.length}`;
    const grid = $("package-grid");
    grid.replaceChildren();
    if (!state.packages.length) {
      const filtered = query || platform !== "all";
      empty(grid, filtered ? "没有找到匹配的对话包" : "你的下一段上下文，从这里开始", filtered ? "试试其他来源或关键词，或清空筛选查看全部对话包。" : "上传 Codex、ChatGPT、Claude、DeepSeek 的对话文件，或按来源保存文本存档。", filtered ? "清空筛选" : "上传第一个对话包", filtered ? () => { $("search-input").value = ""; $("platform-filter").value = "all"; perform(loadPackages); } : openUpload);
      return;
    }
    for (const pack of state.packages) {
      const card = node("article", "package-card");
      const heading = node("h3");
      const footer = node("div", "card-bottom");
      const author = node("span", "card-author");
      const username = typeof pack.author === "object" ? pack.author.username : pack.author;
      heading.append(node("span", "card-icon", "▤"), node("span", "repository-row-owner", `${username || "未知作者"} / `), action(pack.title || "未命名对话包", "card-title", () => perform(() => loadPackage(pack.id))), node("span", `visibility-badge${pack.visibility === "private" ? " private" : ""}`, pack.visibility === "private" ? "私有" : "公开"));
      author.append(node("span", "author-avatar", (username || "?").slice(0, 1).toUpperCase()), node("span", "", username || "未知作者"));
      footer.append(author, node("span", "", `${pack.conversation_count ?? "—"} 段对话${pack.version_count ? ` · ${pack.version_count} 个版本` : ""} · ${formatDate(pack.created_at)}`));
      card.append(heading, node("p", "card-description", pack.description || "这个对话包还没有描述。"), platformBadges(pack.platforms), footer);
      grid.append(card);
    }
  } finally { if (request === state.packageRequest) $("package-grid").removeAttribute("aria-busy"); }
}
async function loadPackage(id, version = null, { location = null, replace = false } = {}) {
  if (!requireSavedEditor() || !leaveDraft()) return;
  rememberContributionComment();
  invalidateContributionReads();
  const request = ++state.readerRequest;
  clearStatus();
  let pack;
  try { pack = await api(`/api/packages/${encodeURIComponent(id)}${version ? `?version=${encodeURIComponent(version)}` : ""}`); }
  catch (error) {
    if (request !== state.readerRequest) return;
    // Failed history navigation must not label old content with a different
    // version's address. Initial private links stay available for login.
    if (location && state.view === "reader" && state.reader && state.reader.kind === "hub") {
      const previous = readerLocationState.lastURL ? new URL(readerLocationState.lastURL).searchParams : null;
      openReader(state.reader, { location: previous, replace: true });
      const recovery = state.readerRequest;
      if (previous) await applyReaderLocation(previous);
      if (recovery !== state.readerRequest) return;
    }
    throw error;
  }
  if (request !== state.readerRequest) return;
  if (isDraft() && (!requireSavedEditor() || !leaveDraft())) return;
  openReader({ ...pack, id: pack.id || id, kind: "hub" }, { location, replace });
  if (location) await applyReaderLocation(location);
}
function openReader(data, { location = null, replace = false } = {}) {
  // Any reader transition supersedes earlier asynchronous package and proposal reads.
  rememberContributionComment();
  invalidateContributionReads();
  state.readerRequest++;
  state.reader = data;
  state.conversationIndex = 0;
  state.turnKey = null; state.turnPage = 0;
  $("turn-search").value = "";
  const editable = data.kind === "local";
  $("reader-title").textContent = data.title || (editable ? "导出前，检查你的上下文。" : "未命名对话包");
  $("reader-description").textContent = data.description || (editable ? "编辑只作用于本次导出副本。请检查敏感内容，保存后会保留编辑记录。" : "完整保留对话正文、来源与兼容性说明。");
  $("reader-eyebrow").textContent = editable ? "REVIEW BEFORE EXPORT" : "CONVERSATION PACKAGE";
  $("reader-back").textContent = editable ? "← 返回本地会话" : "← 返回对话库";
  $("download-package").textContent = editable ? "导出 .contextpack" : "下载完整对话包";
  $("download-package").disabled = false;
  $("export-options").hidden = !editable;
  $("export-title").value = data.title || ((data.conversations || []).length === 1 ? data.conversations[0].title || "Codex 对话" : "Codex 对话合集");
  $("export-description").value = data.description || "";
  const author = typeof data.author === "object" ? data.author.username : data.author;
  $("delete-package").hidden = editable || !state.user || author !== state.user.username;
  const metadata = $("reader-metadata");
  metadata.replaceChildren(node("span", "", `${(data.conversations || []).length} 段对话`));
  if (data.kind === "hub") metadata.append(platformBadges(data.platforms));
  if (author) metadata.append(node("span", "", `由 ${author} 上传`));
  if (data.visibility) metadata.append(node("span", "visibility-badge", data.visibility === "private" ? "仅自己" : "公开"));
  if (data.created_at) metadata.append(node("span", "", formatDate(data.created_at)));
  if (data.git_commit) metadata.append(node("span", "", `版本 ${String(data.git_commit).slice(0, 8)}`));
  renderWarnings($("reader-warnings"), data);
  renderConversationTabs();
  renderConversation();
  renderVersionWorkspace(data);
  navigate("reader");
  if (!location) updateReaderLocation({ replace });
}
function renderConversationTabs() {
  const tabs = $("conversation-tabs");
  tabs.replaceChildren();
  (state.reader.conversations || []).forEach((conversation, index) => {
    const tab = action("", `conversation-tab${index === state.conversationIndex ? " active" : ""}`, () => {
      if (!requireSavedEditor()) return;
      state.conversationIndex = index;
      state.turnKey = null; state.turnPage = 0; $("turn-search").value = "";
      if (typeof codeBrowser !== "undefined" && codeBrowser.origin && codeBrowser.origin.conversation_id !== conversation.id) {
        codeBrowser.origin = null;
        if (typeof renderCodeOrigin === "function") renderCodeOrigin();
      }
      renderConversationTabs(); renderConversation(); updateReaderLocation();
    });
    tab.append(node("span", "", conversation.title || `对话 ${index + 1}`), node("small", "", `${isSourceArchive(conversation) ? "原文存档 · " : ""}${(conversation.messages || []).length} 条消息`));
    tab.setAttribute("aria-pressed", String(index === state.conversationIndex));
    tabs.append(tab);
  });
}
function currentConversation() { return state.reader && (state.reader.conversations || [])[state.conversationIndex]; }
function roleLabel(role) { return { user: "你", assistant: "助手", tool: "工具记录", system: "系统记录", developer: "开发者记录" }[role] || role || "未知角色"; }
function provenanceRecord(conversation) {
  return { id: conversation.id, source: conversation.source || {}, context: conversation.context || "", tags: conversation.tags || [], annotations: conversation.annotations || [] };
}
// Selection is view state only; message order and export contents stay intact.
function messageDate(message) {
  const value = message.created_at || (message.provenance && message.provenance.created_at);
  if (value === undefined || value === null || value === "") return null;
  const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? null : date;
}
function conversationTurns(conversation) {
  const turns = [];
  (conversation.messages || []).forEach((message, index) => {
    const source = message.provenance && typeof message.provenance === "object" ? message.provenance : {};
    const turnId = source.turn_id || null;
    let turn = turns.at(-1);
    if (!turn || (turnId && turn.sourceId !== turnId) || (!turnId && message.role === "user")) {
      turn = { key: String(message.id || index), number: turns.length + 1, sourceId: turnId, entries: [], date: null, title: "" };
      turns.push(turn);
    }
    turn.entries.push({ message, index });
    if (!turn.date) turn.date = messageDate(message);
    if (!turn.title || message.role === "user" && turn.entries.length === 1) turn.title = String(message.content || "").replace(/\s+/g, " ").slice(0, 70);
  });
  return turns;
}
function selectConversationMessage(conversation, messageId) {
  const turn = conversationTurns(conversation).find((item) => item.entries.some(({ message }) => message.id === messageId));
  if (!turn) return;
  state.turnKey = turn.key;
  state.turnPage = Math.floor(turn.entries.findIndex(({ message }) => message.id === messageId) / 20);
  $("turn-search").value = "";
}
function renderTurnDirectory(conversation) {
  const turns = conversationTurns(conversation), directory = $("turn-directory"), navigation = $("turn-navigation");
  directory.replaceChildren(); navigation.replaceChildren();
  const query = $("turn-search").value.trim().toLocaleLowerCase();
  const matches = turns.filter((turn) => !query || turn.entries.some(({ message }) => String(message.content || "").toLocaleLowerCase().includes(query)));
  let selected = matches.find((turn) => turn.key === state.turnKey) || matches.at(-1);
  if (!selected) {
    directory.append(node("p", "field-help", query ? "没有匹配的轮次" : "没有对话轮次"));
    navigation.append(node("p", "field-help", query ? "换个关键词搜索，或清空搜索查看全部轮次。" : ""));
    return [];
  }
  if (selected.key !== state.turnKey) state.turnPage = 0;
  state.turnKey = selected.key;
  const selectedDay = selected.date ? selected.date.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }) : "时间未记录";
  let day = null, group = null;
  const choose = (turn) => {
    if (!requireSavedEditor()) return;
    state.turnKey = turn.key; state.turnPage = 0; renderConversation(); updateReaderLocation();
  };
  for (const turn of matches) {
    const date = turn.date ? turn.date.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }) : "时间未记录";
    if (date !== day) {
      day = date; group = node("details", "turn-day");
      group.open = date === selectedDay;
      group.append(node("summary", "", date)); directory.append(group);
    }
    const button = action("", `turn-item${turn === selected ? " active" : ""}`, () => choose(turn));
    button.dataset.turnKey = turn.key;
    button.setAttribute("aria-pressed", String(turn === selected));
    button.append(node("span", "turn-item-title", turn.title || "空白消息"));
    const time = turn.date ? turn.date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : `第 ${turn.number} 轮`;
    button.append(node("small", "", `${time} · ${turn.entries.length} 条消息`)); group.append(button);
  }
  const position = matches.indexOf(selected), pages = Math.ceil(selected.entries.length / 20);
  state.turnPage = Math.min(Math.max(0, state.turnPage), pages - 1);
  const previous = action("← 上一轮", "button button-small", () => choose(matches[position - 1]));
  const next = action("下一轮 →", "button button-small", () => choose(matches[position + 1]));
  previous.disabled = position === 0; next.disabled = position === matches.length - 1;
  navigation.append(previous, node("span", "turn-position", `第 ${selected.number} / ${turns.length} 轮 · ${selected.entries.length} 条消息`), next);
  if (pages > 1) {
    const older = action("较早消息", "button button-small", () => { if (requireSavedEditor()) { state.turnPage--; renderConversation(); updateReaderLocation(); } });
    const newer = action("较新消息", "button button-small", () => { if (requireSavedEditor()) { state.turnPage++; renderConversation(); updateReaderLocation(); } });
    older.disabled = state.turnPage === 0; newer.disabled = state.turnPage === pages - 1;
    navigation.append(older, node("span", "field-help", `本轮第 ${state.turnPage + 1} / ${pages} 页`), newer);
  }
  return selected.entries.slice(state.turnPage * 20, (state.turnPage + 1) * 20);
}
function renderConversation() {
  const conversation = currentConversation();
  const messages = $("messages");
  messages.replaceChildren();
  document.querySelectorAll(".new-message-actions").forEach((item) => item.remove());
  $("conversation-provenance").replaceChildren();
  $("conversation-title").textContent = conversation ? conversation.title || "未命名对话" : "没有可阅读的对话";
  $("copy-conversation").disabled = !conversation;
  $("save-conversation").disabled = !conversation;
  const archive = isSourceArchive(conversation);
  $("save-archive").hidden = !archive;
  $("save-archive").disabled = !archive || (conversation.messages || []).length !== 1;
  const editedArchive = archive && (conversation.messages || []).some((message) => (message.transformations || []).some((change) => change === "human_edited" || change && (change.kind === "human_edited" || change.type === "human_edited")));
  $("save-archive").textContent = editedArchive ? "下载当前存档文本" : "下载存档文本";
  if (!conversation) { $("turn-directory").replaceChildren(); $("turn-navigation").replaceChildren(); empty(messages, "没有对话内容", "此对话包未包含可阅读的对话。"); if (typeof renderRepositoryRestore === "function") renderRepositoryRestore(); return; }
  const sourceLine = node("div", "conversation-source");
  sourceLine.append(node("span", "platform-badge", `声明来源：${platformLabel(conversationPlatform(conversation))}`));
  if (archive) sourceLine.append(node("span", "archive-badge", "原文存档"));
  $("conversation-provenance").append(sourceLine);
  if (archive) $("conversation-provenance").append(node("p", "notice archive-note", "此内容按原文存档，未解析为可恢复的会话。下载存档文本保存的是当前版本正文；编辑后不再等同于原始导出。"));
  const provenance = node("details", "provenance");
  provenance.append(node("summary", "", "来源、上下文与编辑记录"), node("pre", "", JSON.stringify(provenanceRecord(conversation), null, 2)), action("复制来源记录", "text-button", () => perform(() => copyProvenance(conversation))));
  $("conversation-provenance").append(provenance);
  const editable = state.reader.kind === "local" || isDraft();
  const visibleMessages = renderTurnDirectory(conversation);
  visibleMessages.forEach(({ message, index }) => {
    const section = node("section", `message ${message.role === "assistant" ? "assistant" : "user"}`);
    section.dataset.messageId = message.id || "";
    const header = node("div", "message-heading");
    header.append(node("span", "message-avatar", message.role === "assistant" ? "AI" : message.role === "user" ? "U" : "·"), node("span", "message-label", roleLabel(message.role)));
    const transformations = message.transformations || [];
    const edited = transformations.some((item) => item === "human_edited" || item && (item.type === "human_edited" || item.kind === "human_edited"));
    const declaredPlatform = message.provenance && typeof message.provenance === "object" && message.provenance.platform || conversationPlatform(conversation);
    header.append(node("span", "message-provenance", `${edited ? "已编辑 · " : ""}声明来源：${platformLabel(declaredPlatform)}`));
    const content = node("pre", "message-content", message.content || "");
    if (editable) {
      const controls = node("div", "message-actions");
      controls.append(action("编辑", "text-button", () => editMessage(section, content, conversation, message, controls)), action("移除", "text-button danger", () => {
        if (!requireSavedEditor()) return;
        if (!window.confirm(isDraft() ? "从当前草稿中移除这条消息？已保存的版本不会改变。" : "从本次导出中移除这条消息？本地原始会话不受影响。")) return;
        conversation.messages.splice(index, 1);
        if (!Array.isArray(conversation.annotations)) conversation.annotations = [];
        conversation.annotations.push({ kind: "human_edited", text: `message_removed at ${new Date().toISOString()}`, message_id: message.id });
        draftChanged(); renderConversationTabs(); renderConversation();
        showStatus(isDraft() ? "消息已从草稿中移除，请预览并保存新版本。" : "消息已从导出副本中移除，编辑记录已保留。");
      }));
      if (isDraft()) {
        const up = action("上移", "text-button", () => moveDraftMessage(conversation, index, -1));
        const down = action("下移", "text-button", () => moveDraftMessage(conversation, index, 1));
        up.disabled = index === 0; down.disabled = index === conversation.messages.length - 1;
        controls.append(up, down);
      }
      header.append(controls);
    }
    const recordedTime = messageDate(message);
    if (recordedTime) header.append(node("time", "message-time", `${transformations.includes("codex-turn-start-timestamp") ? "轮次开始 · " : ""}${recordedTime.toLocaleString("zh-CN")}`));
    section.append(header, content);
    if (typeof renderMessageCode === "function") renderMessageCode(section, conversation, message);
    if (transformations.length) {
      const history = node("details", "provenance");
      history.append(node("summary", "", "导入与处理记录"), node("pre", "", JSON.stringify(transformations, null, 2)));
      section.append(history);
    }
    messages.append(section);
  });
  if (!(conversation.messages || []).length) empty(messages, "没有可显示的消息", "请查看兼容性说明；如果消息被移除，可以返回列表重新预览原始会话。");
  renderDraftConversation(conversation);
  if (typeof renderRepositoryRestore === "function") renderRepositoryRestore();
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
  controls.hidden = true; content.hidden = true;
  const originalMessage = structuredClone(message);
  const originalAnnotations = structuredClone(conversation.annotations || []);
  const draft = isDraft();
  let recorded = false;
  const textarea = node("textarea", "message-editor");
  textarea.value = message.content || "";
  textarea.setAttribute("aria-label", `编辑${roleLabel(message.role)}的消息`);
  const apply = () => {
    if (message.content === textarea.value) return;
    message.content = textarea.value;
    if (!Array.isArray(message.transformations)) message.transformations = [];
    if (!message.transformations.includes("human_edited")) message.transformations.push("human_edited");
    if (!recorded) {
      if (!Array.isArray(conversation.annotations)) conversation.annotations = [];
      conversation.annotations.push({ kind: "human_edited", text: `message_edited at ${new Date().toISOString()}`, message_id: message.id });
      recorded = true;
    }
    draftChanged();
  };
  if (draft) textarea.addEventListener("input", apply);
  const footer = node("div", "editor-help", draft ? "输入会自动保存为仅自己可见的草稿。完成编辑后，预览并发布新版本。" : "保存后将标记为人工编辑。原始 Codex 会话不会改变。");
  const buttons = node("div", "message-actions");
  buttons.append(action("保存修改", "button button-small", () => {
    apply(); renderConversation();
    showStatus(draft ? "修改已加入私有草稿，请查看服务器保存状态。" : "修改已保存到导出副本，并保留人工编辑记录。");
  }), action("取消", "button button-small", () => {
    if (draft) {
      for (const key of Object.keys(message)) delete message[key];
      Object.assign(message, originalMessage);
      conversation.annotations = originalAnnotations;
      draftChanged();
    }
    renderConversation();
  }));
  section.append(textarea, footer, buttons); textarea.focus();
}
function markdown(conversation) {
  const parts = [`# ${conversation.title || "未命名对话"}`, "", `来源 ID: ${conversation.id || "未知"}`, "", "## 来源记录", "", "```json", JSON.stringify(provenanceRecord(conversation), null, 2), "```", ""];
  for (const message of conversation.messages || []) {
    parts.push(`## ${roleLabel(message.role)}`, "", message.content || "", "");
    if ((message.transformations || []).length) parts.push(`处理记录: ${JSON.stringify(message.transformations)}`, "");
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
function packageDownloadDescriptor(reader) {
  const version = reader.version;
  return Object.freeze({
    path: `/api/packages/${encodeURIComponent(reader.id)}/download${version ? `?version=${encodeURIComponent(version.id)}` : ""}`,
    filename: `${safeName(reader.title)}${version ? `-v${version.number}` : ""}.contextpack`,
  });
}
async function requestPlatformDownload(download) {
  if (!state.user) { openAuth("download", download); return; }
  // A session may expire or change in another tab. Check it even for the
  // single-conversation files generated from an already rendered snapshot.
  const session = await api("/api/session");
  if (!session.user) {
    updateAccount(session);
    openAuth("download", download);
    return;
  }
  try {
    if (download.accessPath) await api(download.accessPath);
    if (download.operation === "copy") {
      await copyText(download.text);
      showStatus(`已复制「${download.filename}」的内容。`);
    } else {
      const blob = download.path ? await api(download.path, {}, true) : download.blob;
      saveBlob(blob, download.filename);
      showStatus(`已开始下载「${download.filename}」。`);
    }
  } catch (error) {
    if (error.status !== 401) throw error;
    updateAccount({ user: null, csrf_token: null });
    openAuth("download", download);
  }
}
async function copyProvenance(conversation) {
  const text = JSON.stringify(provenanceRecord(conversation), null, 2);
  if (state.mode === "connector") { await copyText(text); showStatus("来源记录已复制。"); return; }
  const reader = state.reader;
  const version = reader.version;
  await requestPlatformDownload(Object.freeze({ operation: "copy", text,
    filename: `${safeName(conversation.title)}-来源记录.json`,
    accessPath: `/api/packages/${encodeURIComponent(reader.id)}${version ? `?version=${encodeURIComponent(version.id)}` : ""}`,
  }));
}
async function copyCurrentConversation() {
  const format = $("copy-format").value;
  const text = conversationText();
  if (state.mode === "connector") { await copyText(text); showStatus(`${format === "json" ? "JSON" : "Markdown"} 已复制。`); return; }
  const reader = state.reader;
  const version = reader.version;
  await requestPlatformDownload(Object.freeze({ operation: "copy", text,
    filename: `${safeName(currentConversation().title)}.${format === "json" ? "json" : "md"}`,
    accessPath: `/api/packages/${encodeURIComponent(reader.id)}${version ? `?version=${encodeURIComponent(version.id)}` : ""}`,
  }));
}
async function saveCurrentConversation() {
  const format = $("copy-format").value;
  const content = conversationText();
  const conversation = currentConversation();
  const reader = state.reader;
  const blob = new Blob([content], { type: format === "json" ? "application/json;charset=utf-8" : "text/markdown;charset=utf-8" });
  const filename = `${safeName(conversation.title)}.${format === "json" ? "json" : "md"}`;
  if (state.mode === "connector") { saveBlob(blob, filename); showStatus("当前对话已开始下载。"); return; }
  const version = reader.version;
  await requestPlatformDownload(Object.freeze({ blob, filename,
    accessPath: `/api/packages/${encodeURIComponent(reader.id)}${version ? `?version=${encodeURIComponent(version.id)}` : ""}`,
  }));
}
async function saveCurrentArchive() {
  if (!requireSavedEditor()) return;
  const conversation = currentConversation();
  if (!isSourceArchive(conversation) || (conversation.messages || []).length !== 1) throw new Error("存档文本下载需要一条完整的存档正文，请使用 JSON 保存当前对话。");
  const reader = state.reader;
  const version = reader.version;
  let filename = safeName(conversation.source.title || conversation.title || "archive");
  if (!/\.(json|jsonl|txt|md)$/i.test(filename)) filename += ".txt";
  const blob = new Blob([conversation.messages[0].content], { type: "text/plain;charset=utf-8" });
  if (state.mode === "connector") { saveBlob(blob, filename); return; }
  await requestPlatformDownload(Object.freeze({ blob, filename,
    accessPath: `/api/packages/${encodeURIComponent(reader.id)}${version ? `?version=${encodeURIComponent(version.id)}` : ""}`,
  }));
}
async function downloadCurrentPackage() {
  const reader = state.reader;
  if (!reader) return;
  if (isDraft()) throw new Error("请先预览并保存新版本，再下载完整对话包。");
  if ($("messages").querySelector("textarea")) throw new Error("请先保存或取消正在编辑的消息，再导出。");
  if (reader.kind !== "local") {
    await requestPlatformDownload(packageDownloadDescriptor(reader));
    return;
  }
  await busy($("download-package"), "正在准备…", async () => {
    const title = $("export-title").value.trim();
    if (!title) { $("export-title").focus(); throw new Error("请为对话包填写标题。"); }
    if (!(reader.conversations || []).some((conversation) => (conversation.messages || []).length)) throw new Error("请至少保留一条消息后再导出。");
    const blob = await api("/api/local/export", { method: "POST", json: { conversations: reader.conversations, title, description: $("export-description").value.trim(), redact: $("redact-secrets").checked } }, true);
    saveBlob(blob, `${safeName(title)}.contextpack`);
    showStatus("对话包已生成并开始下载。自动脱敏无法覆盖所有敏感信息，请在分享前检查。");
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
  if (state.authIntent && state.authIntent.pending) return;
  state.authMode = mode;
  const registering = mode === "register";
  $("auth-title").textContent = registering ? "创建你的工作空间" : "欢迎回来";
  const downloading = state.authIntent && state.authIntent.next === "download";
  const transfer = downloading && state.authIntent.download.operation === "copy" ? "复制" : "下载";
  $("auth-description").textContent = downloading ? `${registering ? "注册" : "登录"}后将继续${transfer}「${state.authIntent.download.filename}」。公开内容可以直接阅读，内容导出需要账户。` : state.authIntent && state.authIntent.next === "fork" ? `${registering ? "注册" : "登录"}后可从所选固定版本创建自己的 Fork。` : registering ? "创建账户，保存和管理你的对话包。" : "登录后上传、下载和管理你的对话包。";
  $("auth-submit").textContent = registering ? "创建账户" : "登录";
  $("login-tab").classList.toggle("active", !registering);
  $("register-tab").classList.toggle("active", registering);
  $("password").autocomplete = registering ? "new-password" : "current-password";
  $("password").minLength = registering ? 10 : 1;
  $("auth-error").hidden = true;
}
function invalidateAuthIntent() { state.authIntent = null; }
function isCurrentAuthIntent(intent) { return Boolean(intent && state.authIntent === intent && $("auth-dialog").open); }
function openAuth(next = null, download = null, fork = null) {
  state.authIntent = { next, download, fork, pending: false };
  delete $("auth-dialog").dataset.next;
  $("auth-submit").disabled = false;
  $("login-tab").disabled = false;
  $("register-tab").disabled = false;
  $("password").value = "";
  $("auth-error").textContent = "";
  setAuthMode("login");
  if (!$("auth-dialog").open) $("auth-dialog").showModal();
}
function openUpload() {
  clearStatus();
  if (!state.user) { openAuth("upload"); return; }
  $("upload-error").hidden = true;
  $("upload-dialog").showModal();
}
function inlineError(id, error) { $(id).textContent = errorMessage(error); $(id).hidden = false; }
async function submitAuth(event) {
  event.preventDefault();
  const intent = state.authIntent;
  if (!isCurrentAuthIntent(intent) || intent.pending) return;
  $("auth-error").hidden = true;
  const mode = state.authMode;
  let authenticated = false;
  intent.pending = true;
  $("auth-submit").disabled = true;
  $("auth-submit").textContent = "请稍候…";
  $("login-tab").disabled = true;
  $("register-tab").disabled = true;
  try {
    const session = await api(`/api/${mode}`, { method: "POST", json: { username: $("username").value.trim(), password: $("password").value } });
    if (!isCurrentAuthIntent(intent)) return;
    authenticated = true;
    updateAccount(session);
    $("password").value = "";
    invalidateAuthIntent();
    $("auth-dialog").close();
    showStatus(mode === "register" ? "账户已创建。" : "已登录。");
    // The selected download is an immutable click-time descriptor, independent
    // of any reader/detail requests triggered by the account transition.
    if (intent.next === "download") await perform(() => requestPlatformDownload(intent.download));
    await loadPackages();
    if (intent.next === "upload") openUpload();
    else if (intent.next === "fork") openForkDialog(intent.fork);
    else if (!intent.next && !state.reader) {
      const parameters = new URLSearchParams(window.location.search);
      if (parameters.has("contribution")) await loadContribution(parameters.get("contribution"));
      else if (parameters.has("package")) await loadPackage(parameters.get("package"), parameters.get("version"), { location: parameters, replace: true });
    }
  } catch (error) {
    if (isCurrentAuthIntent(intent)) inlineError("auth-error", error);
    else if (authenticated && state.authIntent === null) showStatus(errorMessage(error), "error");
  } finally {
    intent.pending = false;
    if (isCurrentAuthIntent(intent)) {
      $("auth-submit").disabled = false;
      $("login-tab").disabled = false;
      $("register-tab").disabled = false;
      $("auth-submit").textContent = mode === "register" ? "创建账户" : "登录";
    }
  }
}
async function accountAction() {
  if (!state.user) { openAuth(); return; }
  if (!requireSavedEditor() || !leaveDraft()) return;
  await api("/api/logout", { method: "POST", json: {} });
  updateAccount({ user: null, csrf_token: null });
  state.reader = null;
  invalidateUploadInspection();
  navigate("library"); await loadPackages(); showStatus("已退出登录。");
}
function renderInspection(container, data) {
  container.replaceChildren();
  container.hidden = false;
  const conversations = data.conversations || [];
  const library = data.library || {};
  const summary = node("div", "inspect-summary");
  summary.append(node("h3", "", library.title || data.title || "对话包内容"), node("p", "", `${conversations.length} 段对话 · ${conversations.reduce((count, conversation) => count + (conversation.messages || []).length, 0)} 条消息`));
  const code = data.code;
  const codeHead = code && (code.snapshots || []).find((snapshot) => snapshot.id === code.head);
  if (codeHead) summary.append(node("p", "", `${codeHead.files.length} 个代码文件 · ${code.snapshots.length} 个代码快照 · ${(code.bindings || []).length} 条消息关联`));
  if (library.description || data.description) summary.append(node("p", "", library.description || data.description));
  if (data.platforms) summary.append(platformBadges(data.platforms));
  if (data.import_report) summary.append(node("p", "field-help", data.import_report.mode === "archive" ? "原文存档：保留 UTF-8 文本，不恢复原平台会话。" : "统一正文：仅转换支持的内容，不保证可导回原平台。"));
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
function uploadParameters(file) {
  return new URLSearchParams({ filename: file.name, platform: $("upload-platform").value, mode: $("upload-mode").value });
}
function invalidateUploadInspection() {
  state.uploadInspection++;
  state.uploadInspectedFile = null;
  state.uploadSnapshot = null;
  $("upload-submit").disabled = true;
  $("upload-confirm").checked = false;
  $("upload-confirmation").hidden = true;
  $("upload-preview").hidden = true;
  $("upload-preview").replaceChildren();
}
function updateUploadAvailability() {
  const snapshot = state.uploadSnapshot;
  const current = snapshot && $("upload-file").files[0] === snapshot.file && uploadParameters(snapshot.file).toString() === snapshot.parameters;
  $("upload-submit").disabled = state.uploadPending || !current || snapshot.requiresConfirmation && !$("upload-confirm").checked;
}
async function inspectUploadFile() {
  const file = $("upload-file").files[0];
  invalidateUploadInspection();
  const request = state.uploadInspection;
  $("upload-file-info").textContent = fileInfo(file);
  $("upload-error").hidden = true;
  if (!file) return;
  const parameters = uploadParameters(file).toString();
  $("upload-file-info").textContent = `${fileInfo(file)} · 正在检查内容…`;
  try {
    validatePackageFile(file);
    const response = await api(`/api/inspect?${parameters}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
    if (request !== state.uploadInspection) return;
    state.uploadInspectedFile = file;
    state.uploadSnapshot = Object.freeze({ file, parameters, requiresConfirmation: Boolean(response.import_report && response.import_report.requires_confirmation) });
    $("upload-file-info").textContent = `${fileInfo(file)} · 格式检查通过`;
    renderInspection($("upload-preview"), response);
    $("upload-confirmation").hidden = !state.uploadSnapshot.requiresConfirmation;
    updateUploadAvailability();
  } catch (error) {
    if (request !== state.uploadInspection) return;
    $("upload-file-info").textContent = fileInfo(file);
    inlineError("upload-error", error);
  }
}
async function submitUpload(event) {
  event.preventDefault();
  if (state.uploadPending) return;
  $("upload-error").hidden = true;
  const snapshot = state.uploadSnapshot;
  const file = $("upload-file").files[0];
  if (!snapshot || !file || file !== snapshot.file || uploadParameters(file).toString() !== snapshot.parameters) { inlineError("upload-error", new Error("请先选择文件，并等待当前来源和导入方式的内容检查完成。")); return; }
  if (snapshot.requiresConfirmation && !$("upload-confirm").checked) { inlineError("upload-error", new Error("请先阅读导入报告并勾选确认。")); return; }
  state.uploadPending = true;
  const controls = [$("upload-file"), $("upload-platform"), $("upload-mode"), $("upload-confirm"), ...document.querySelectorAll('input[name="visibility"]')];
  const visibility = new FormData($("upload-form")).get("visibility");
  controls.forEach((control) => { control.disabled = true; });
  try {
    await busy($("upload-submit"), "上传中…", async () => {
      const parameters = new URLSearchParams(snapshot.parameters);
      parameters.set("visibility", visibility);
      if (snapshot.requiresConfirmation) parameters.set("confirm_import", "1");
      const previousReader = state.reader;
      const data = await api(`/api/packages?${parameters}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
      $("upload-dialog").close(); $("upload-form").reset();
      invalidateUploadInspection();
      $("upload-file-info").textContent = "尚未选择文件";
      if (state.reader === previousReader && !isDraft()) {
        if (data.conversations) openReader({ ...data, kind: "hub" }); else await loadPackage(data.id);
      }
      showStatus(visibility === "public" ? "已上传为公开对话包，任何人可阅读，登录后可下载。" : "已上传为私有对话包，仅你的账户可见。");
      await loadPackages();
    });
  } catch (error) { inlineError("upload-error", error); }
  finally {
    state.uploadPending = false;
    controls.forEach((control) => { control.disabled = false; });
    updateUploadAvailability();
    updateUploadLabel();
  }
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
    validatePackageFile(file);
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
    const data = await api(`/api/local/import?cwd=${encodeURIComponent(cwd)}&mode=${encodeURIComponent($("import-mode").value)}`, { method: "POST", headers: { "Content-Type": "application/zip" }, body: file });
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
    if (result.mode) box.append(node("p", "", `恢复方式：${result.mode === "timeline" ? "原生文本时间线重建" : result.mode === "context" ? "上下文注入" : result.mode}`));
    if (result.mode === "timeline" && !failed) {
      box.append(node("p", "", "已重建新会话的文本时间线；回执分别记录内容读回、持久化与 resume 检查。没有运行模型或迁移原机器工具权限。"));
    }
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
  $("turn-search").addEventListener("input", () => {
    if (!requireSavedEditor()) return;
    state.turnPage = 0; renderConversation(); updateReaderLocation({ replace: true });
  });
  bindVersionEvents();
  bindContributionEvents();
  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => $(button.dataset.close).close()));
  $("account-button").addEventListener("click", () => perform(accountAction));
  $("open-upload").addEventListener("click", openUpload);
  $("platform-filter").addEventListener("change", () => perform(loadPackages));
  $("search-form").addEventListener("submit", (event) => { event.preventDefault(); perform(loadPackages); });
  $("login-tab").addEventListener("click", () => setAuthMode("login"));
  $("register-tab").addEventListener("click", () => setAuthMode("register"));
  $("auth-form").addEventListener("submit", submitAuth);
  $("auth-dialog").addEventListener("cancel", invalidateAuthIntent);
  $("auth-dialog").addEventListener("close", () => { if (!$("auth-dialog").open) invalidateAuthIntent(); });
  document.querySelectorAll('[data-close="auth-dialog"]').forEach((button) => button.addEventListener("click", invalidateAuthIntent));
  $("upload-form").addEventListener("submit", submitUpload);
  $("upload-file").addEventListener("change", inspectUploadFile);
  $("upload-platform").addEventListener("change", inspectUploadFile);
  $("upload-mode").addEventListener("change", inspectUploadFile);
  $("upload-confirm").addEventListener("change", updateUploadAvailability);
  document.querySelectorAll('input[name="visibility"]').forEach((radio) => radio.addEventListener("change", updateUploadLabel));
  $("reader-back").addEventListener("click", () => { clearStatus(); navigate(state.mode === "connector" ? "local" : "library"); });
  $("download-package").addEventListener("click", () => perform(downloadCurrentPackage));
  $("delete-package").addEventListener("click", () => perform(deleteCurrentPackage));
  $("copy-conversation").addEventListener("click", () => perform(copyCurrentConversation));
  $("save-conversation").addEventListener("click", () => perform(saveCurrentConversation));
  $("save-archive").addEventListener("click", () => perform(saveCurrentArchive));
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
    configureUploadLimit(config);
    configureNavigation();
    if (state.mode === "connector") {
      navigate("local");
      await loadSessions();
    } else {
      updateAccount(await api("/api/session"));
      const parameters = new URLSearchParams(window.location.search);
      navigate("library");
      await loadPackages();
      if (parameters.has("contribution")) await loadContribution(parameters.get("contribution"));
      else if (parameters.has("package")) await loadPackage(parameters.get("package"), parameters.get("version"), { location: parameters, replace: true });
    }
  } catch (error) {
    showStatus(errorMessage(error), "error");
    if (!state.mode) {
      empty($("loading-view"), "暂时无法连接工作空间", "确认服务已启动后，重试连接。", "重试", () => window.location.reload());
    }
  }
}
initialize();
