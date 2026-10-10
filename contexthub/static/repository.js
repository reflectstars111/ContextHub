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
  for (const tab of document.querySelectorAll("[data-repository-section]")) {
    const target = $(tab.dataset.repositorySection);
    tab.hidden = Boolean(target && target.hidden);
    if (changed) {
      const active = tab.dataset.repositorySection === "conversation-section";
      tab.classList.toggle("active", active);
      if (active) tab.setAttribute("aria-current", "location"); else tab.removeAttribute("aria-current");
    }
  }
  const panel = $("code-checkpoint-content"); panel.replaceChildren();
  const checkpoint = data.code_checkpoint;
  const status = draft ? "draft" : checkpoint ? data.code_checkpoint_status === "matched" ? "matched" : "stale" : "none";
  panel.dataset.status = status;
  const labels = { none: "尚未关联代码", matched: "对话摘要匹配", stale: "对话已变化 · 待重新关联", draft: "编辑中 · 尚未验证" };
  panel.append(node("span", `checkpoint-status checkpoint-${status}`, labels[status]));
  if (!checkpoint) {
    panel.append(node("p", "", "在本地保存检查点，将这一版对话关联到一个代码提交。"));
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
    guide.textContent = pinned ? `ctx clone ${shellArgument(versionURL(data.id, data.version.id).href)} conversation-workspace\ncd conversation-workspace\nctx restore HEAD --code /path/to/code --to NEWDIR` : "ctx restore REF --code /path/to/code --to NEWDIR";
  }
}
document.addEventListener("DOMContentLoaded", () => {
  $("repository-navigation").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-repository-section]");
    if (!tab) return;
    event.preventDefault();
    const target = $(tab.dataset.repositorySection);
    if (!target || target.hidden) return;
    if (target.tagName === "DETAILS") target.open = true;
    for (const item of document.querySelectorAll("[data-repository-section]")) {
      item.classList.toggle("active", item === tab);
      if (item === tab) item.setAttribute("aria-current", "location"); else item.removeAttribute("aria-current");
    }
    target.scrollIntoView({ block: "start", behavior: "instant" });
    target.setAttribute("tabindex", "-1"); target.focus({ preventScroll: true });
  });
});
