"use strict";

// Server revisions prevent one browser tab from overwriting another tab's work.
const draftStorage = { epoch: 0, owner: null, revision: 0, signature: null, pending: null, timer: null, conflict: false };
function resetDraftStorage() {
  clearTimeout(draftStorage.timer);
  draftStorage.epoch++;
  draftStorage.owner = null;
  draftStorage.revision = 0; draftStorage.signature = null;
  draftStorage.pending = null; draftStorage.timer = null; draftStorage.conflict = false;
  $("draft-recovery").hidden = true;
}
function privateDraftPayload() {
  if (!isDraft()) throw new Error("请先打开编辑副本。");
  return { base_version: versionState.original.version.id,
    title: $("draft-title").value.trim(), description: $("draft-description").value.trim(),
    ...(Object.hasOwn(state.reader, "code_checkpoint") ? { code_checkpoint: structuredClone(state.reader.code_checkpoint) } : {}),
    conversations: structuredClone(state.reader.conversations), message: $("version-message").value.trim() };
}
function draftSynced() {
  return isDraft() && !draftStorage.pending && !draftStorage.conflict
    && draftStorage.signature === JSON.stringify(privateDraftPayload());
}
function draftStorageStatus(text, error = false) {
  $("draft-autosave-status").textContent = text;
  $("draft-autosave-status").classList.toggle("inline-error", error);
}
function scheduleDraftSave() {
  if (!isDraft() || versionState.saving || draftStorage.conflict) return;
  if (!state.user || state.user.id !== draftStorage.owner) {
    draftStorageStatus("登录已失效或账户已切换；本页草稿保留，可下载备份后重新登录原账户", true);
    return;
  }
  clearTimeout(draftStorage.timer);
  draftStorageStatus("尚有更改未同步到服务器…");
  draftStorage.timer = setTimeout(() => { flushDraftSave().catch(() => {}); }, 800);
}
async function flushDraftSave() {
  clearTimeout(draftStorage.timer);
  const epoch = draftStorage.epoch;
  while (draftStorage.pending) {
    await draftStorage.pending;
    if (epoch !== draftStorage.epoch) return;
  }
  if (!isDraft() || epoch !== draftStorage.epoch) return;
  if (!state.user || state.user.id !== draftStorage.owner) throw new Error("请重新登录草稿所属账户；本页修改仍可下载备份。");
  if (draftStorage.conflict) throw new Error("另一处已修改草稿，请先下载本地备份，再恢复服务器草稿。");
  const payload = privateDraftPayload();
  const signature = JSON.stringify(payload);
  if (draftStorage.signature === signature) return;
  const id = state.reader.id, owner = state.user && state.user.id;
  draftStorageStatus("正在保存私有草稿…");
  const current = () => epoch === draftStorage.epoch && isDraft() && state.reader.id === id && state.user && state.user.id === owner;
  const operation = (async () => {
    try {
      const response = await api(`/api/packages/${encodeURIComponent(id)}/draft`, {
        method: "POST", json: { ...payload, expected_revision: draftStorage.revision } });
      if (!current()) return;
      draftStorage.revision = response.revision;
      draftStorage.signature = signature;
      draftStorageStatus("草稿已保存到服务器，仅你可见");
    } catch (error) {
      if (current()) {
        draftStorage.conflict = error.status === 409;
        draftStorageStatus(draftStorage.conflict ? "另一处已更新草稿，自动保存已暂停；本页内容保留" : `未保存到服务器：${errorMessage(error)}`, true);
      }
      throw error;
    }
  })();
  draftStorage.pending = operation;
  try { await operation; }
  finally { if (draftStorage.pending === operation) draftStorage.pending = null; }
  if (current() && signature !== JSON.stringify(privateDraftPayload())) scheduleDraftSave();
}
async function checkStoredDraft(data) {
  $("draft-recovery").hidden = true;
  if (!versionOwner(data) || data.kind !== "hub") return;
  const request = state.readerRequest, owner = state.user.id;
  const response = await api(`/api/packages/${encodeURIComponent(data.id)}/draft`);
  if (request !== state.readerRequest || !state.user || owner !== state.user.id || isDraft()) return;
  $("draft-recovery").hidden = !response.draft;
  $("edit-package").textContent = response.draft ? "继续编辑草稿" : "编辑副本";
}
async function loadDraftForEditing(data) {
  const request = ++state.readerRequest, owner = state.user.id;
  const response = await api(`/api/packages/${encodeURIComponent(data.id)}/draft`);
  let original = data;
  if (response.draft && response.draft.base_version !== data.version.id) {
    original = { ...await api(`/api/packages/${encodeURIComponent(data.id)}?version=${encodeURIComponent(response.draft.base_version)}`), kind: "hub" };
  }
  if (request !== state.readerRequest || !state.user || owner !== state.user.id) return null;
  resetDraftStorage();
  draftStorage.owner = owner;
  draftStorage.revision = response.revision;
  return { original, draft: response.draft };
}
function downloadDraftBackup() {
  saveBlob(new Blob([JSON.stringify({ format: "contexthub-private-draft-v1", package_id: state.reader.id, ...privateDraftPayload() }, null, 2)], { type: "application/json" }), `${safeName($("draft-title").value)}.draft.json`);
  showStatus("本页草稿已下载；其中可能含私有内容，请妥善保管。");
}
async function reloadStoredDraft() {
  if (!isDraft() || versionState.saving || !window.confirm("恢复服务器草稿将替换本页修改。需要保留时请先下载草稿备份，是否继续？")) return;
  const original = versionState.original, epoch = draftStorage.epoch, owner = state.user && state.user.id;
  const current = () => isDraft() && draftStorage.epoch === epoch && state.reader.id === original.id && state.user && state.user.id === owner;
  clearTimeout(draftStorage.timer);
  versionState.saving = true; $("reader-view").inert = true;
  try {
    while (draftStorage.pending) { try { await draftStorage.pending; } catch (_) { break; } }
    if (!current()) return;
    clearDraftState(); openReader(original);
    versionState.saving = false;
    await startDraft();
  } finally {
    versionState.saving = false; $("reader-view").inert = false;
  }
}
function bindDraftStorageEvents() {
  $("restore-draft").addEventListener("click", () => perform(startDraft));
  $("save-private-draft").addEventListener("click", () => perform(flushDraftSave));
  $("download-draft-backup").addEventListener("click", () => perform(downloadDraftBackup));
  $("reload-server-draft").addEventListener("click", () => perform(reloadStoredDraft));
}
