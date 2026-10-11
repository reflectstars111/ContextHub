"use strict";

// The browser prepares a pinned command; only the user's local CLI talks to Codex.
let codexOpenSelection = null;
function codexOpenChoices() {
  return (state.reader?.conversations || []).filter((item) => !isSourceArchive(item) && (item.messages || []).length);
}
function codexOpenKey(plan) {
  return JSON.stringify([state.reader?.id, state.reader?.version?.id, state.reader?.kind, state.user?.id, currentConversation()?.id, plan.snapshot, plan.message, plan.conversation]);
}
function closeCodexDialog() {
  codexOpenSelection = null;
  if ($("codex-open-dialog").open) $("codex-open-dialog").close();
}
function renderCodexOpenState(plan) {
  const button = $("repository-codex-action");
  const choices = codexOpenChoices();
  const available = Boolean(plan.snapshot && choices.length && (!plan.conversation || choices.some((item) => item.id === plan.conversation)));
  button.hidden = state.mode !== "hub" || state.reader?.kind === "local";
  button.disabled = !available;
  button.title = available ? "恢复所选代码并将对话导入运行 Codex 的电脑或服务器" : !plan.command ? plan.description : "需要内嵌代码和已解析的对话；旧式 Git 检查点请使用终端恢复说明。";
  if ($("codex-open-dialog").open && codexOpenSelection?.key !== codexOpenKey(plan)) closeCodexDialog();
}
function codexOpenQuote(value) {
  return $("codex-open-shell").value === "powershell" ? "'" + String(value).replace(/'/g, "''") + "'" : shellArgument(value);
}
function renderCodexOpenCommand() {
  const selection = codexOpenSelection;
  if (!selection) return;
  const target = $("codex-open-target").value.trim();
  const mode = $("codex-open-mode").value;
  const conversation = $("codex-open-conversation").value;
  const error = !target ? "请填写一个尚不存在的目标目录。" : /[\x00-\x1f\x7f]/.test(target) || target.length > 2048 ? "目录不能包含控制字符，且长度不能超过 2048 个字符。" : !selection.choices.some((item) => item.id === conversation) ? "请选择要导入的对话。" : "";
  $("codex-open-error").textContent = error;
  $("codex-open-error").hidden = !error;
  $("copy-codex-open-command").disabled = Boolean(error);
  const quote = codexOpenQuote;
  const selector = selection.message ? `--message=${quote(selection.message)}` : `--snapshot=${quote(selection.snapshot)}`;
  $("codex-open-command").textContent = error ? "" : `ctx open ${quote(selection.url)} --to=${quote(target)} --codex --mode ${mode} --conversation=${quote(conversation)} ${selector}`;
  $("codex-open-login-command").textContent = `ctx auth login --hub=${quote(new URL(selection.url).origin)} --username=${quote(state.user?.username || "YOUR_USERNAME")}`;
  $("codex-open-install").href = $("codex-open-shell").value === "powershell" ? "/cli#windows" : "/cli#install";
  $("codex-open-mode-help").textContent = mode === "timeline" ? "重建新的用户 / 助手文本时间线。目前仅支持已验证的 Codex 0.153.4；版本不支持时会停止，不会自动切换模式。" : "把对话正文作为上下文注入新会话，兼容更多版本；不保证在 Codex 聊天列表中完整显示或再次导出这些历史消息。";
}
function openCodexDialog() {
  if (!requireSavedEditor() || $("repository-codex-action").disabled) return;
  const plan = repositoryRestorePlan();
  const choices = codexOpenChoices();
  if (!plan.snapshot || !choices.length) return;
  codexOpenSelection = { key: codexOpenKey(plan), url: versionURL(state.reader.id, state.reader.version.id).href,
    snapshot: plan.snapshot, message: plan.message, choices: choices.map(({ id, title }) => ({ id, title })) };
  $("codex-open-selection").textContent = plan.label;
  $("codex-open-scope").textContent = plan.message ? "恢复这条消息采集时的代码，仅导入截至该消息的对话正文。完整原始对话包也会保存在本机。" : "恢复所选代码快照，并导入下面选择的完整对话。代码快照不会自动决定对话的截断位置。";
  const select = $("codex-open-conversation"); select.replaceChildren();
  for (const item of choices) {
    const option = node("option", "", item.title || "未命名对话"); option.value = item.id; select.append(option);
  }
  const current = plan.conversation || currentConversation()?.id;
  select.value = choices.some((item) => item.id === current) ? current : choices[0].id;
  select.disabled = Boolean(plan.message);
  $("codex-open-target").value = "./contexthub-project";
  $("codex-open-copy-status").textContent = "";
  renderCodexOpenCommand();
  $("codex-open-dialog").showModal();
}
document.addEventListener("DOMContentLoaded", () => {
  $("repository-codex-action").addEventListener("click", openCodexDialog);
  for (const id of ["codex-open-target", "codex-open-mode", "codex-open-shell", "codex-open-conversation"]) {
    $(id).addEventListener(id === "codex-open-target" ? "input" : "change", () => { $("codex-open-copy-status").textContent = ""; renderCodexOpenCommand(); });
  }
  $("copy-codex-open-command").addEventListener("click", () => perform(async () => {
    if (state.view !== "reader" || state.reader?.kind !== "hub" || codexOpenSelection?.key !== codexOpenKey(repositoryRestorePlan())) { closeCodexDialog(); return; }
    renderCodexOpenCommand();
    if ($("copy-codex-open-command").disabled || !codexOpenSelection) return;
    await copyText($("codex-open-command").textContent);
    $("codex-open-copy-status").textContent = "已复制。请在运行 Codex 的终端粘贴执行。";
  }));
  $("codex-open-dialog").addEventListener("close", () => { if (!$("codex-open-dialog").open) codexOpenSelection = null; });
});
