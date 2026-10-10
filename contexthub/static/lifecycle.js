"use strict";

function renderLibrarySettings(data) {
  $("library-settings").hidden = data.kind !== "hub" || !versionOwner(data);
  $("change-visibility").textContent = data.visibility === "public" ? "改为仅自己可见" : "改为公开分享";
}
async function changeLibraryVisibility() {
  const data = state.reader;
  if (!data || !versionOwner(data) || isDraft()) return;
  const visibility = data.visibility === "public" ? "private" : "public";
  const text = visibility === "public"
    ? "公开后，任何人都能阅读此库的全部历史版本，登录后可下载。请先检查所有历史内容。确认公开？"
    : "改为私有后，仅你可访问此库及其历史版本。别人已经下载的内容和独立 Fork 无法撤回。确认修改？";
  if (!window.confirm(text)) return;
  await busy($("change-visibility"), "修改中…", async () => {
    const changed = await api(`/api/packages/${encodeURIComponent(data.id)}/visibility`, {
      method: "POST", json: { visibility, base_version: data.head_version_id } });
    if (state.reader && state.reader.id === data.id && !isDraft()) openReader({ ...changed, kind: "hub" });
    await loadPackages(); showStatus(visibility === "public" ? "对话库及其历史版本已公开。" : "对话库已改为仅自己可见。");
  });
}
async function purgeCurrentLibrary() {
  const data = state.reader;
  if (!data || !versionOwner(data) || isDraft()) return;
  const entered = window.prompt(`永久清理将删除「${data.title}」的版本、草稿和相关记录，不能在网站撤销。其他库仍需使用的共享内容与独立备份会保留。\n请输入库 ID 确认：${data.id}`);
  if (entered === null) return;
  if (entered.trim() !== data.id) throw new Error("库 ID 不匹配，未执行永久清理。");
  await busy($("purge-package"), "清理中…", async () => {
    const result = await api(`/api/packages/${encodeURIComponent(data.id)}/purge`, { method: "POST", json: { confirmation: data.id } });
    state.reader = null; clearDraftState(); navigate("library"); await loadPackages();
    showStatus(`对话库已永久清理，移除 ${result.removed_blobs || 0} 个独占对话包；其他库仍需使用的共享内容予以保留。`);
  });
}
function bindLibrarySettingsEvents() {
  $("change-visibility").addEventListener("click", () => perform(changeLibraryVisibility));
  $("purge-package").addEventListener("click", () => perform(purgeCurrentLibrary));
}
