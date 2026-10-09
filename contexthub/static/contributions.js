"use strict";

// Proposals pin source versions. A merge requires a separately reviewed check.
const contributionState = {
  current: null, request: 0, listRequest: 0, sourceRequest: 0, reviewed: null,
  proposal: null, mutation: false, commentDrafts: new Map(), sourceVersions: [],
};
function contributionURL(id) {
  const url = new URL(window.location.href);
  url.search = ""; url.hash = "";
  url.searchParams.set("contribution", id);
  return url;
}
function updateContributionURL(id) {
  const url = contributionURL(id);
  if (url.href !== window.location.href) window.history.pushState(null, "", url);
}
function rememberContributionComment() {
  if (state.view === "contribution" && contributionState.current) contributionState.commentDrafts.set(contributionState.current.id, $("contribution-comment").value);
}
function invalidateContributionReads() {
  contributionState.request++;
  contributionState.sourceRequest++;
}
function contributionStatus(status) { return { open: "待合并", closed: "已关闭", merged: "已合并" }[status] || status; }
function contributionAuthor(author) { return typeof author === "object" && author ? author.username || "未知作者" : author || "未知作者"; }
function checkKey(check) { return check ? `${check.id}:${check.candidate_oid || ""}` : null; }
function isCurrentContribution(id, request) {
  return state.view === "contribution" && contributionState.current && contributionState.current.id === id && request === contributionState.request;
}
function renderPackageContributions(data) {
  const saved = data.kind === "hub";
  $("package-contributions-panel").hidden = !saved;
  $("open-contribution").hidden = !saved || !versionOwner(data) || !data.upstream || data.visibility !== "public";
  const privateFork = saved && versionOwner(data) && Boolean(data.upstream) && data.visibility !== "public";
  $("contribution-boundary").hidden = !privateFork;
  $("contribution-boundary").textContent = "只有公开的直接 Fork 可以向上游提交贡献。这个私有 Fork 无法提交；当前版本不支持修改对话库可见性。";
  if (!saved) { contributionState.listRequest++; return; }
  $("package-contribution-count").textContent = "";
  if (data.visibility !== "public") { contributionState.listRequest++; $("package-contributions").replaceChildren(node("p", "contribution-empty", "私有对话库不参与公开贡献。公开的直接 Fork 才能提交贡献请求。")); return; }
  $("package-contributions").replaceChildren(node("p", "subtle", "正在读取贡献请求…"));
  perform(() => loadPackageContributions(data));
}
async function loadPackageContributions(data) {
  const request = ++contributionState.listRequest;
  try {
    const response = await api(`/api/packages/${encodeURIComponent(data.id)}/contributions`);
    if (request !== contributionState.listRequest || !state.reader || state.reader.id !== data.id || isDraft()) return;
    const items = response.contributions || [];
    $("package-contribution-count").textContent = String(items.length);
    const container = $("package-contributions"); container.replaceChildren();
    if (!items.length) { container.append(node("p", "contribution-empty", "还没有贡献请求。公开 Fork 的拥有者可从所选版本提交改进。")); return; }
    for (const item of items) {
      const row = node("div", "contribution-list-item");
      const body = node("div");
      const sourceId = item.source && item.source.package_id || item.source_package_id;
      const direction = sourceId === data.id ? "提交给上游" : "收到的贡献";
      body.append(action(item.title || "贡献请求", "text-button contribution-list-title", () => perform(() => loadContribution(item.id))));
      body.append(node("p", "subtle", `${direction} · ${contributionAuthor(item.author)} · ${formatDate(item.created_at)}`));
      row.append(body, node("span", `pill contribution-status-${item.status}`, contributionStatus(item.status)));
      container.append(row);
    }
  } catch (error) {
    if (request !== contributionState.listRequest) return;
    $("package-contributions").replaceChildren(node("p", "inline-error", errorMessage(error)));
  }
}
function isCurrentProposal(proposal) {
  return Boolean(proposal && contributionState.proposal === proposal && $("contribution-dialog").open);
}
function invalidateContributionProposal() {
  contributionState.proposal = null;
}
function openContributionDialog() {
  const data = state.reader;
  if (!data || isDraft() || !versionOwner(data) || !data.upstream || data.visibility !== "public") return;
  contributionState.proposal = { package_id: data.id, source_version_id: data.version.id, reader: data, pending: false };
  $("contribution-form").reset();
  $("contribution-submit").disabled = false;
  $("contribution-submit").textContent = "提交贡献请求";
  $("contribution-form-error").hidden = true;
  $("contribution-proposal-route").textContent = `从「${data.title}」v${data.version.number} 提交到「${data.upstream.title || "上游对话库"}」。`;
  $("contribution-dialog").showModal();
}
async function submitContribution(event) {
  event.preventDefault();
  const proposal = contributionState.proposal;
  if (!isCurrentProposal(proposal) || proposal.pending) return;
  $("contribution-form-error").hidden = true;
  const title = $("contribution-title-input").value.trim();
  const description = $("contribution-description-input").value.trim();
  if (!title) { inlineError("contribution-form-error", new Error("请填写贡献标题。")); return; }
  const request = state.readerRequest;
  proposal.pending = true;
  $("contribution-submit").disabled = true;
  $("contribution-submit").textContent = "提交中…";
  try {
    const data = await api(`/api/packages/${encodeURIComponent(proposal.package_id)}/contributions`, {
      method: "POST", json: { source_version_id: proposal.source_version_id, title, description },
    });
    const sameReader = request === state.readerRequest && state.reader === proposal.reader && !isDraft();
    const sameForm = isCurrentProposal(proposal)
      && $("contribution-title-input").value.trim() === title
      && $("contribution-description-input").value.trim() === description;
    if (sameForm) {
      invalidateContributionProposal();
      $("contribution-dialog").close();
      if (sameReader) openContribution(data);
    } else if (sameReader) {
      // An earlier dialog may finish after a new one is opened. Only refresh
      // the reader list; the newer form and its pending button belong to it.
      perform(() => loadPackageContributions(proposal.reader));
    }
    showStatus("贡献请求已提交，提案固定在所选版本。后续 Fork 编辑不会自动改变这份提案。");
  } catch (error) {
    if (isCurrentProposal(proposal)) inlineError("contribution-form-error", error);
  } finally {
    proposal.pending = false;
    if (isCurrentProposal(proposal)) {
      $("contribution-submit").disabled = false;
      $("contribution-submit").textContent = "提交贡献请求";
    }
  }
}
async function loadContribution(id) {
  if (!requireSavedEditor() || !leaveDraft()) return;
  rememberContributionComment();
  state.readerRequest++;
  const request = ++contributionState.request;
  clearStatus();
  const data = await api(`/api/contributions/${encodeURIComponent(id)}`);
  if (request !== contributionState.request || isDraft()) return;
  openContribution(data);
}
function openContribution(data) {
  rememberContributionComment();
  state.readerRequest++;
  contributionState.listRequest++;
  contributionState.sourceRequest++;
  contributionState.request++;
  contributionState.reviewed = null;
  state.reader = null;
  contributionState.current = data;
  $("contribution-comment").value = contributionState.commentDrafts.get(data.id) || "";
  navigate("contribution");
  updateContributionURL(data.id);
  renderContribution(data);
}
function renderContribution(data) {
  const previous = contributionState.current;
  rememberContributionComment();
  contributionState.current = data;
  if (!previous || previous.id !== data.id || checkKey(previous.check) !== checkKey(data.check)) contributionState.reviewed = null;
  const permissions = data.permissions || {};
  $("contribution-title").textContent = data.title;
  $("contribution-description").textContent = data.description || "作者还没有补充说明。";
  $("contribution-status").textContent = contributionStatus(data.status);
  $("contribution-status").className = `pill contribution-status-${data.status}`;
  $("contribution-byline").textContent = `${contributionAuthor(data.author)} 发起 · ${formatDate(data.created_at)}`;
  const source = data.source || {}; const target = data.target || {};
  $("contribution-source").replaceChildren(action(`${contributionAuthor(source.author)} / ${source.title || "来源对话库"} · v${source.number || "?"}`, "text-button", () => perform(() => loadPackage(source.package_id, source.version_id))));
  $("contribution-target").replaceChildren(action(`${contributionAuthor(target.author)} / ${target.title || "上游对话库"}`, "text-button", () => perform(() => loadPackage(target.package_id))));
  $("contribution-version-note").textContent = source.head_version_id && source.head_version_id !== source.version_id ? "这个 Fork 已有更新版本，但提案仍使用上方固定版本。源作者可以明确更新提案版本。" : "提案固定在上方源版本；合并检查会结合上游当前版本和 Fork 起点计算候选内容。";
  renderComparison($("contribution-source-comparison"), data.comparison || {});
  $("contribution-update-panel").hidden = !permissions.can_update;
  if (permissions.can_update) perform(() => loadContributionSourceVersions(data));
  renderContributionCheck(data);
  const comments = data.comments || [];
  $("contribution-comments").replaceChildren();
  if (!comments.length) $("contribution-comments").append(node("p", "contribution-empty", "还没有讨论。源作者与上游拥有者可以在这里交流修改。"));
  for (const comment of comments) {
    const item = node("article", "contribution-comment");
    item.append(node("p", "comment-byline", `${contributionAuthor(comment.author)} · ${formatDate(comment.created_at)}`), node("p", "comment-body", comment.body));
    $("contribution-comments").append(item);
  }
  $("contribution-comment").value = contributionState.commentDrafts.get(data.id) || "";
  $("contribution-comment-form").hidden = !permissions.can_comment || comments.length >= 200;
  $("contribution-comment-boundary").textContent = comments.length >= 200 ? "这份贡献请求已达到 200 条评论上限。" : permissions.can_comment ? "讨论对所有能阅读这份公开贡献请求的人可见。" : "只有源作者与上游拥有者可以评论；其他人可以阅读提案与讨论。";
  $("contribution-close").hidden = !permissions.can_close;
  $("contribution-reopen").hidden = !permissions.can_reopen;
  updateContributionButtons();
}
function renderContributionCheck(data) {
  const check = data.check;
  const summary = $("contribution-check-summary"); summary.replaceChildren();
  $("contribution-check").hidden = !(data.permissions || {}).can_check;
  $("contribution-check").textContent = check ? "重新检查合并" : "检查合并";
  $("contribution-check-actions").hidden = !check;
  $("contribution-check-comparison").hidden = true;
  $("contribution-check-comparison").replaceChildren();
  $("contribution-conflicts").hidden = true;
  $("contribution-conflicts").replaceChildren();
  $("contribution-download-candidate").hidden = !check || !check.candidate_oid || check.status === "conflicts";
  $("contribution-review-check").hidden = !check || !check.comparison;
  $("contribution-merge-controls").hidden = !(data.permissions || {}).can_check || data.status !== "open";
  $("contribution-merged-version").hidden = !data.merged_version_id;
  $("contribution-merged-version").replaceChildren();
  if (data.merged_version_id) $("contribution-merged-version").append(node("p", "notice", "这份贡献已合并，提案与合并记录仍可阅读。"), action("阅读合并后的固定版本", "button button-secondary", () => perform(() => loadPackage(data.target.package_id, data.merged_version_id))));
  if (!check) {
    summary.append(node("p", "subtle", data.status === "open" ? "尚未生成合并候选。上游拥有者可以检查这份提案，查看准备发布的完整变化。" : "这份贡献请求没有合并检查记录。"));
    return;
  }
  const label = { ready: "合并候选已生成", conflicts: "存在需要双方处理的冲突", unchanged: "合并不会带来内容变化" }[check.status] || "合并检查已完成";
  summary.append(node("p", `notice${check.stale || check.status === "conflicts" ? " warning" : ""}`, check.stale ? "上游或提案版本已变化，这次检查已失效。请重新检查后再查看候选。" : label));
  summary.append(node("p", "field-help", `检查时间：${formatDate(check.checked_at)}。${check.status === "ready" ? "本次候选将成为上游的公开新版本，请展开检查变化后再决定合并。" : check.status === "unchanged" ? "无需发布重复版本；可以关闭这份贡献请求。" : "系统不会替你选择冲突内容。请在 Fork 中解决后保存新版本，再更新提案版本。"}`));
  if (check.status === "conflicts") {
    const container = $("contribution-conflicts"); container.hidden = false;
    container.append(node("h3", "", `${check.conflict_count || (check.conflicts || []).length} 处冲突`));
    for (const conflict of check.conflicts || []) {
      const box = node("details", "diff-change conflict-change"); box.open = true;
      box.append(node("summary", "", `${conflict.path || "内容"} · ${conflict.kind || "冲突"}`));
      const sides = node("div", "conflict-sides");
      for (const [label, value] of [["共同起点", conflict.base], ["上游当前内容", conflict.target], ["提案内容", conflict.source]]) {
        const side = node("div"); side.append(node("h4", "", label), node("pre", "", comparisonValue(value))); sides.append(side);
      }
      box.append(sides); container.append(box);
    }
    if (check.truncated) container.append(node("p", "notice warning", "冲突列表过长，当前展示已截断。请分别下载完整源版本与上游版本检查，解决后重新提交。"));
  }
  if (contributionState.reviewed === checkKey(check) && check.comparison) {
    renderComparison($("contribution-check-comparison"), check.comparison);
    $("contribution-review-check").textContent = "重新查看本次合并变化";
  } else $("contribution-review-check").textContent = "查看本次合并变化";
}
function updateContributionButtons() {
  const data = contributionState.current;
  if (!data) return;
  const check = data.check; const permissions = data.permissions || {};
  const reviewed = Boolean(check && contributionState.reviewed === checkKey(check));
  $("contribution-merge").disabled = contributionState.mutation || !permissions.can_merge || data.status !== "open" || !check || check.status !== "ready" || check.stale || !reviewed;
  $("contribution-merge-note").textContent = check && check.stale ? "当前检查已失效，请重新检查。评论输入会保留。" : reviewed ? "你已打开本次候选的变化。确认后会将这些内容发布为上游的公开新版本。" : "请先点击“查看本次合并变化”，确认候选内容后再合并。";
  for (const id of ["contribution-check", "contribution-close", "contribution-reopen", "contribution-comment-submit", "contribution-update-source"]) $(id).disabled = contributionState.mutation;
  $("contribution-update-source").disabled = contributionState.mutation || !$("contribution-source-version").value || $("contribution-source-version").value === (data.source || {}).version_id;
  $("contribution-source-version").disabled = contributionState.mutation;
}
function reviewContributionCheck() {
  const data = contributionState.current;
  const check = data && data.check;
  if (!check || !check.comparison) return;
  renderComparison($("contribution-check-comparison"), check.comparison);
  if (check.comparison.truncated) $("contribution-check-comparison").prepend(node("p", "notice warning", "本次合并预览已截断。请下载完整候选包检查其余内容，再决定是否发布。"));
  contributionState.reviewed = checkKey(check);
  $("contribution-review-check").textContent = "重新查看本次合并变化";
  updateContributionButtons();
  $("contribution-check-comparison").scrollIntoView({ behavior: "smooth", block: "start" });
}
async function loadContributionSourceVersions(data) {
  const request = ++contributionState.sourceRequest;
  const response = await api(`/api/packages/${encodeURIComponent(data.source.package_id)}/versions`);
  if (request !== contributionState.sourceRequest || state.view !== "contribution" || !contributionState.current || contributionState.current.id !== data.id) return;
  contributionState.sourceVersions = response.versions || [];
  const select = $("contribution-source-version");
  const previous = select.dataset.contribution === data.id ? select.value : data.source.version_id;
  select.replaceChildren(); select.dataset.contribution = data.id;
  for (const version of contributionState.sourceVersions) {
    const option = node("option", "", `v${version.number} · ${version.message || "初始导入"}${version.id === data.source.version_id ? "（当前提案）" : ""}`);
    option.value = version.id; select.append(option);
  }
  select.value = contributionState.sourceVersions.some((version) => version.id === previous) ? previous : data.source.version_id;
  updateContributionButtons();
}
async function mutateContribution(operation, payload, message, onSuccess) {
  const data = contributionState.current;
  if (!data || contributionState.mutation) return;
  const request = contributionState.request;
  contributionState.mutation = true;
  updateContributionButtons();
  try {
    const response = await api(`/api/contributions/${encodeURIComponent(data.id)}/${operation}`, { method: "POST", json: payload });
    if (onSuccess) onSuccess(response);
    if (!isCurrentContribution(data.id, request)) return;
    rememberContributionComment();
    if (operation === "check" || operation === "source") contributionState.reviewed = null;
    renderContribution(response);
    showStatus(message);
  } catch (error) {
    if (!isCurrentContribution(data.id, request)) return;
    if (error.status !== 409) throw error;
    contributionState.reviewed = null;
    if (contributionState.current.check) contributionState.current.check.stale = true;
    renderContributionCheck(contributionState.current);
    showStatus("上游或提案版本已变化。请重新加载贡献请求，再重新检查合并；未提交评论会保留。", "warning");
    const retry = action("重新加载贡献请求", "button button-small", () => perform(() => loadContribution(data.id)));
    $("contribution-check-summary").append(retry);
  } finally {
    contributionState.mutation = false;
    if (state.view === "contribution") updateContributionButtons();
  }
}
async function submitContributionComment(event) {
  event.preventDefault();
  const data = contributionState.current;
  const body = $("contribution-comment").value.trim();
  const original = $("contribution-comment").value;
  if (!data || !body) { $("contribution-comment").focus(); return; }
  await mutateContribution("comments", { body }, "评论已提交。", () => {
    if (contributionState.commentDrafts.get(data.id) === original) contributionState.commentDrafts.delete(data.id);
    if (state.view === "contribution" && contributionState.current.id === data.id && $("contribution-comment").value === original) $("contribution-comment").value = "";
  });
}
function bindContributionEvents() {
  $("open-contribution").addEventListener("click", openContributionDialog);
  $("contribution-form").addEventListener("submit", submitContribution);
  $("contribution-dialog").addEventListener("cancel", invalidateContributionProposal);
  $("contribution-dialog").addEventListener("close", () => {
    // A queued close event from an older dialog must not invalidate a reopen.
    if (!$("contribution-dialog").open) invalidateContributionProposal();
  });
  document.querySelectorAll('[data-close="contribution-dialog"]').forEach((button) => button.addEventListener("click", invalidateContributionProposal));
  $("contribution-back").addEventListener("click", () => perform(() => loadPackage(contributionState.current.target.package_id)));
  $("contribution-copy-link").addEventListener("click", () => perform(async () => { await copyText(contributionURL(contributionState.current.id).href); showStatus("贡献请求链接已复制。"); }));
  $("contribution-check").addEventListener("click", () => perform(() => mutateContribution("check", { expected_target_version_id: contributionState.current.target.head_version_id }, "合并检查已完成。请查看本次候选变化。")));
  $("contribution-review-check").addEventListener("click", reviewContributionCheck);
  $("contribution-merge").addEventListener("click", () => perform(async () => {
    const data = contributionState.current; const check = data && data.check;
    if ($("contribution-merge").disabled || !check || contributionState.reviewed !== checkKey(check)) return;
    await mutateContribution("merge", { check_id: check.id, candidate_oid: check.candidate_oid }, "贡献已合并，并发布为上游的新版本。");
    await loadPackages();
  }));
  $("contribution-download-candidate").addEventListener("click", () => perform(async () => {
    const data = contributionState.current; const check = data && data.check;
    if (!check || !check.candidate_oid) return;
    await requestPlatformDownload(Object.freeze({
      path: `/api/contributions/${encodeURIComponent(data.id)}/candidate?check_id=${encodeURIComponent(check.id)}`,
      filename: `${safeName(data.title)}-merge-candidate.contextpack`,
    }));
  }));
  $("contribution-source-version").addEventListener("change", updateContributionButtons);
  $("contribution-update-source").addEventListener("click", () => perform(() => mutateContribution("source", { source_version_id: $("contribution-source-version").value }, "提案版本已更新。上游需要重新检查合并候选。")));
  $("contribution-comment").addEventListener("input", rememberContributionComment);
  $("contribution-comment-form").addEventListener("submit", (event) => { event.preventDefault(); perform(() => submitContributionComment(event)); });
  $("contribution-close").addEventListener("click", () => perform(() => mutateContribution("status", { status: "closed" }, "贡献请求已关闭，讨论与提案仍然保留。")));
  $("contribution-reopen").addEventListener("click", () => perform(() => mutateContribution("status", { status: "open" }, "贡献请求已重新打开，请重新检查候选内容。")));
  window.addEventListener("beforeunload", (event) => {
    rememberContributionComment();
    if ([...contributionState.commentDrafts.values()].some((value) => value.trim()) || ($("contribution-dialog").open && ($("contribution-title-input").value.trim() || $("contribution-description-input").value.trim()))) { event.preventDefault(); event.returnValue = ""; }
  });
}
