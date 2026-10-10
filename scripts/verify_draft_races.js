#!/usr/bin/env node
"use strict";
// Deterministic regression checks against the actual shipped browser functions.
// No network, live account, UI source edits or production storage are used.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const source = ["drafts.js", "versions.js"].map(name =>
  fs.readFileSync(path.join(root, "contexthub", "static", name), "utf8")).join("\n");
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function fixture() {
  const elements = new Map(), calls = [], opened = [], timers = new Map();
  let nextTimer = 0;
  const state = { reader: { kind: "draft", id: "A", conversations: [] },
    user: { id: "owner", username: "owner" }, readerRequest: 0 };
  const context = {
    state, console, structuredClone, JSON, Error,
    window: { confirm: () => true },
    $(name) {
      if (!elements.has(name)) elements.set(name, { hidden: false, inert: false,
        value: "", textContent: "", disabled: false, replaceChildren() {},
        classList: { toggle() {} } });
      return elements.get(name);
    },
    clearTimeout(id) { timers.delete(id); },
    setTimeout(callback) { const id = ++nextTimer; timers.set(id, callback); return id; },
    api(url, options) {
      const task = deferred(); calls.push({ url, options, ...task }); return task.promise;
    },
    openReader(value) { opened.push(value); state.reader = value; state.readerRequest++; },
    showStatus() {}, errorMessage(error) { return error.message; },
    requireSavedEditor() { return true; },
    perform(fn) { return fn(); },
    async busy(element, label, fn) { return fn(); },
  };
  vm.createContext(context);
  vm.runInContext(source + `
versionState.original = { id: 'A', kind: 'hub', version: { id: 'vA' } };
draftStorage.owner = 'owner';
privateDraftPayload = () => ({ content: globalThis.fixtureText });
globalThis.test = { flushDraftSave, discardDraft, reloadStoredDraft,
  draftStorage, versionState };
`, context, { filename: "actual-draft-browser-functions.js" });
  context.fixtureText = "first edit";
  function switchReader() {
    state.reader = { id: "B", kind: "draft", conversations: [{ title: "unsaved B" }] };
    context.test.versionState.original = { id: "B", kind: "hub", version: { id: "vB" } };
    state.readerRequest++;
    context.test.draftStorage.epoch++;
    context.test.draftStorage.pending = null;
    context.test.draftStorage.revision = 17;
    return state.reader;
  }
  return { ...context, context, calls, opened, timers, switchReader };
}

async function concurrentFlushesSerialize() {
  const f = fixture(), { test } = f.context;
  const first = test.flushDraftSave();
  assert.equal(f.calls.length, 1);
  f.context.fixtureText = "second edit";
  const manualSave = test.flushDraftSave();
  const autosaveOrSecondClick = test.flushDraftSave();
  f.calls[0].resolve({ revision: 1 });
  await tick();
  assert.equal(f.calls.length, 2,
    "Two callers waiting on one save must share the next save, not POST duplicate revisions");
  assert.equal(f.calls[1].options.json.expected_revision, 1);
  assert.equal(f.calls[1].options.json.content, "second edit");
  f.calls[1].resolve({ revision: 2 });
  await Promise.all([first, manualSave, autosaveOrSecondClick]);
  assert.equal(test.draftStorage.conflict, false);
  assert.equal(test.draftStorage.revision, 2);
  assert.equal(test.draftStorage.pending, null);
}

async function discardResponseCannotReplaceAnotherReader() {
  const f = fixture(), { test } = f.context;
  test.draftStorage.revision = 3;
  const discarded = test.discardDraft();
  await tick();
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].options.method, "DELETE");
  assert.equal(f.calls[0].options.json.expected_revision, 3);
  const replacement = f.switchReader();
  f.calls[0].resolve({ deleted: true, revision: 4 });
  await discarded;
  assert.equal(f.state.reader, replacement,
    "A delayed discard response must not replace another library's unsaved reader");
  assert.equal(f.opened.length, 0);
  assert.equal(test.draftStorage.revision, 17);
}

async function accountChangeCannotStrandSettledSavePromise() {
  const f = fixture(), { test } = f.context;
  const saved = test.flushDraftSave();
  const queued = test.flushDraftSave();
  const completed = Promise.allSettled([saved, queued]);
  f.state.user = null; // Session expiry discovered by another in-flight action.
  f.calls[0].resolve({ revision: 1 });
  const results = await completed;
  assert.equal(results[0].status, 'fulfilled');
  assert.equal(results[1].status, 'rejected',
    "A queued save must recheck draft ownership after its awaited predecessor finishes");
  assert.equal(test.draftStorage.pending, null,
    "An account change must still release this save's pending slot; a settled promise loops forever");
  assert.equal(test.draftStorage.revision, 0,
    "A stale account's response must not update the current draft revision");
  await assert.rejects(test.flushDraftSave(),
    "An expired identity must not save using another or missing account");
  assert.equal(f.calls.length, 1);
  f.state.user = { id: 'other-owner', username: 'other-owner' };
  await assert.rejects(test.flushDraftSave(),
    "A different account must not silently claim the previous account's draft");
  assert.equal(f.calls.length, 1);
}

async function waitingDiscardCannotDeleteUsingAnotherDraftRevision() {
  const f = fixture(), { test } = f.context, inFlight = deferred();
  test.draftStorage.pending = inFlight.promise;
  test.draftStorage.revision = 3;
  const discarded = test.discardDraft();
  const replacement = f.switchReader();
  inFlight.resolve();
  await discarded;
  assert.equal(f.calls.length, 0,
    "A discard waiting for autosave must stop after its editing session changes");
  assert.equal(f.state.reader, replacement);
}

async function waitingRestoreCannotReplaceAnotherReader() {
  const f = fixture(), { test } = f.context, inFlight = deferred();
  test.draftStorage.pending = inFlight.promise;
  const restored = test.reloadStoredDraft();
  const replacement = f.switchReader();
  inFlight.resolve();
  await restored;
  assert.equal(f.calls.length, 0,
    "A restore waiting for autosave must stop after its editing session changes");
  assert.equal(f.state.reader, replacement);
  assert.equal(f.opened.length, 0);
}

(async () => {
  for (const check of [concurrentFlushesSerialize, accountChangeCannotStrandSettledSavePromise,
                      discardResponseCannotReplaceAnotherReader,
                      waitingDiscardCannotDeleteUsingAnotherDraftRevision,
                      waitingRestoreCannotReplaceAnotherReader]) {
    await check();
    console.log("PASS " + check.name);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
