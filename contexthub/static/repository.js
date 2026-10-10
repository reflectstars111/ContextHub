"use strict";

// Repository navigation keeps the selected immutable version in the URL.
// Imported metadata is always rendered as text. A digest match is not a signature.
let renderedRepositoryKey = null;
function checkpointRepositoryURL(value) {
  if (typeof value !== "string" || !value || /[\s\\]/.test(value)) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) return null;
    return url;
  } catch (_) { return null; }
}
function checkpointLink(url, label, className = "") {
  const link = node("a", className, label);
  link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
  return link;
}
function shellArgument(value) { return "'" + String(value).replace(/'/g, "'\\''") + "'"; }
function renderRepository(data) {
  if (typeof renderCodeRepository === "function") renderCodeRepository(data);
  const owner = typeof data.author === "object" ? data.author && data.author.username : data.author;
  const draft = data.kind === "draft", local = data.kind === "local";
  const repositoryKey = `${data.id || "local"}:${data.version && data.version.id || ""}:${data.kind}`;
  const changed = repositoryKey !== renderedRepositoryKey;
  renderedRepositoryKey = repositoryKey;
  $("reader-report").hidden = $("reader-warnings").hidden;
  $("reader-owner").textContent = owner || (local ? "本地会话" : "对话仓库");
  $("repository-visibility").textContent = local ? "本地" : data.visibility === "private" ? "私有" : "公开";
  $("repository-visibility").classList.toggle("private", data.visibility === "private");
  $("repository-about-description").textContent = draft ? $("draft-description").value || "编辑中的对话仓库" : data.description || "保存对话的思考过程，继续开发与协作。";
  $("repository-about-platforms").replaceChildren(platformBadges(data.platforms));
  const facts = $("repository-about-facts"); facts.replaceChildren();
  const conversations = data.conversations || [];
  for (const [label, value] of [["对话", `${conversations.length} 段`], ["消息", `${conversations.reduce((sum, item) => sum + (item.messages || []).length, 0)} 条`], ["当前版本", draft ? "未发布草稿" : data.version ? `v${data.version.number}` : "本地副本"]]) {
    facts.append(node("dt", "", label), node("dd", "", value));
  }
  $("version-history-panel").hidden = local;
  for (const tab of document.querySelectorAll("[data-repository-section]")) {
    const target = $(tab.dataset.repositorySection);
    tab.hidden = Boolean(target && target.hidden);
    tab.id = `repository-tab-${tab.dataset.repositorySection}`;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", tab.dataset.repositorySection);
    if (target) { target.setAttribute("role", "tabpanel"); target.setAttribute("aria-labelledby", tab.id); }
  }
  $("repository-navigation").setAttribute("role", "tablist");
  const active = $("reader-view").dataset.repositoryTab;
  setRepositorySection(changed || !active || $(active).hidden ? "conversation-section" : active);
  const panel = $("code-checkpoint-content"); panel.replaceChildren();
  const checkpoint = data.code_checkpoint;
  const status = draft ? "draft" : checkpoint ? data.code_checkpoint_status === "matched" ? "matched" : "stale" : "none";
  panel.dataset.status = status;
  const labels = { none: "尚未关联代码", matched: "对话摘要匹配", stale: "对话已变化 · 待重新关联", draft: "编辑中 · 尚未验证" };
  panel.append(node("span", `checkpoint-status checkpoint-${status}`, !checkpoint && data.code && !draft ? "代码快照已保存 · 未固定 Git 提交" : labels[status]));
  if (!checkpoint) {
    panel.append(node("p", "", data.code ? "本版已包含代码文件，可在“代码”中浏览；还可额外固定一个 Git 提交检查点。" : "在本地保存检查点，将这一版对话关联到一个代码提交。"));
  } else {
    const repository = checkpointRepositoryURL(checkpoint.repository_url);
    if (repository) panel.append(checkpointLink(repository, repository.hostname + repository.pathname.replace(/\.git$/, ""), "checkpoint-repository"));
    else panel.append(node("p", "", "本地代码仓库"));
    const facts = node("dl", "checkpoint-facts");
    facts.append(node("dt", "", "Commit"));
    const commit = node("dd");
    if (repository && repository.hostname === "github.com" && !repository.port && /^\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/?$/.test(repository.pathname) && /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(checkpoint.commit)) {
      const commitURL = new URL(repository.href);
      commitURL.pathname = commitURL.pathname.replace(/\/$/, "").replace(/\.git$/, "") + "/commit/" + checkpoint.commit;
      commit.append(checkpointLink(commitURL, checkpoint.commit.slice(0, 12), "checkpoint-commit"));
    } else commit.append(node("code", "", String(checkpoint.commit || "").slice(0, 12)));
    commit.title = checkpoint.commit || ""; facts.append(commit);
    facts.append(node("dt", "", "分支"), node("dd", "", checkpoint.branch || "Detached HEAD"));
    facts.append(node("dt", "", "记录时间"), node("dd", "", formatDate(checkpoint.captured_at)));
    panel.append(facts);
    panel.append(node("p", "checkpoint-explanation", status === "matched" ? "关联记录与当前对话内容匹配；恢复时还会在本机核验代码对象。此状态不代表 GitHub 认证。" : draft ? "草稿尚未发布。发布后重新计算对话摘要，编辑后的内容可能需要新检查点。" : "当前对话与记录时的内容不一致。请创建新的代码检查点后再同步恢复。"));
  }
  const guide = $("checkpoint-restore-command");
  if (guide) {
    const pinned = data.kind === "hub" && data.id && data.version && data.version.id;
    const restore = data.code ? "ctx restore HEAD --to NEWDIR" : "ctx restore HEAD --code /path/to/code --to NEWDIR";
    guide.textContent = pinned ? `ctx clone ${shellArgument(versionURL(data.id, data.version.id).href)} conversation-workspace\ncd conversation-workspace\n${restore}` : restore.replace("HEAD", "REF");
    $("repository-restore-description").textContent = data.code ? "此版本已带代码文件，无需原 Git 仓库即可恢复。" : "旧式 Git 检查点需本机拥有对应代码提交。";
    $("repository-capture-guide").textContent = data.code ? "在项目目录采集当前代码与对话：" : "旧式检查点要求对话已保存、代码 Git 工作区干净：";
    $("repository-capture-command").textContent = data.code ? 'ctx add .\nctx commit -m "记录当前实现"' : 'ctx checkpoint --code /path/to/code -m "记录当前实现"';
    $("repository-restore-guide").textContent = data.code ? "先登录并克隆页面所选版本，再恢复到 NEWDIR 新目录：" : "先取得对应代码 Git 仓库，再克隆所选对话版本。请替换代码路径与 NEWDIR：";
  }
}
function setRepositorySection(id, { focus = false, guard = false } = {}) {
  const target = $(id);
  if (!target || target.hidden || !target.hasAttribute("data-repository-panel")) return false;
  if (guard && !requireSavedEditor()) return false;
  $("reader-view").dataset.repositoryTab = id;
  for (const panel of document.querySelectorAll("[data-repository-panel]")) {
    const active = panel.id === id;
    panel.classList.toggle("repository-panel-inactive", !active);
    panel.setAttribute("aria-hidden", String(!active)); panel.inert = !active;
  }
  for (const tab of document.querySelectorAll("[data-repository-section]")) {
    const active = tab.dataset.repositorySection === id;
    tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active));
    if (active) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
  }
  const collaboration = document.querySelector(".repository-collaboration");
  collaboration.hidden = !["package-contributions-panel", "library-settings"].includes(id);
  if (target.tagName === "DETAILS") target.open = true;
  if (focus) { target.setAttribute("tabindex", "-1"); target.focus({ preventScroll: true }); }
  return true;
}
function renderRepositoryHistory(data, versions) {
  const list = $("version-history-list"); list.replaceChildren();
  for (const version of versions) {
    const row = node("article", "version-history-row"), text = node("div");
    text.append(node("strong", "", version.message || "初始导入"), node("p", "field-help", `v${version.number} · ${version.author || ""} · ${formatDate(version.created_at)}`));
    const actions = node("div", "version-history-actions");
    if (version.id === (data.version || {}).id) actions.append(node("span", "pill", "当前版本"));
    actions.append(action("浏览此版本", "button button-small", () => perform(() => loadPackage(data.id, version.id))));
    row.append(text, actions); list.append(row);
  }
  if (!versions.length) list.append(node("p", "field-help", "尚无已保存版本。"));
}
document.addEventListener("DOMContentLoaded", () => {
  $("repository-navigation").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-repository-section]");
    if (!tab) return;
    event.preventDefault();
    setRepositorySection(tab.dataset.repositorySection, { focus: true, guard: true });
  });
  $("repository-navigation").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = [...$("repository-navigation").querySelectorAll("[data-repository-section]")].filter((tab) => !tab.hidden);
    const current = tabs.indexOf(event.target); if (current < 0) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    if (setRepositorySection(tabs[next].dataset.repositorySection, { guard: true })) tabs[next].focus();
  });
});
