import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";
import {clampMenuPosition,markdownDirectoryFromResult,markdownLocationUrl} from "../static/file-context-menu.mjs";

const workspaceSource = fs.readFileSync(new URL("../static/workspace.mjs", import.meta.url), "utf8");
const deletionStart = workspaceSource.indexOf("function isDeletedJob");
const deletionEnd = workspaceSource.indexOf("async function runFileMenuAction", deletionStart);
assert.ok(deletionStart >= 0 && deletionEnd > deletionStart, "delete handlers should be present");

test("markdown location keeps the concrete job id in the API target", () => {
  assert.equal(markdownLocationUrl("paper/42"), "/api/jobs/paper%2F42/markdown-location");
});

test("file menu position is clamped inside a small viewport", () => {
  assert.deepEqual(clampMenuPosition({x: 999, y: 999, width: 220, height: 120, viewportWidth: 320, viewportHeight: 240}), {left: 92, top: 112});
  assert.deepEqual(clampMenuPosition({x: -40, y: -20, width: 80, height: 60, viewportWidth: 320, viewportHeight: 240}), {left: 8, top: 8});
});

test("missing markdown directory is reported instead of claiming a copy", () => {
  assert.equal(markdownDirectoryFromResult({directory: " E:/papers/markdown "}), "E:/papers/markdown");
  assert.throws(() => markdownDirectoryFromResult({copied: true}), /Markdown 目录/);
});

function deletionHarness({jobs, activeJob = null, reject = false} = {}) {
  const nodes = new Map();
  for (const id of ["delete-modal", "delete-name", "delete-msg", "submit-delete", "annotation-menu", "empty", "pages", "title", "notice", "progress-wrap", "progress-stage", "progress-value", "progress-fill", "token-usage", "status-text", "status", "page-number", "total", "zoom-label", "open-translate", "open-download"]) {
    const classes = new Set();
    nodes.set(id, {hidden: false, value: "", textContent: "", disabled: false, classList: {add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name)}});
  }
  nodes.get("delete-modal").classList.add("open");
  const state = {jobs: jobs || [], job: activeJob, timer: "poll", docs: {}, annotations: {}, cacheJobOrder: [], selection: null, selectedAnnotation: null, undo: null, jobsRequest: 0, selectRequest: 0, deletedJobIds: new Set(), deletingJob: null, deleteInFlight: null};
  const calls = [];
  const context = {
    state, $: id => nodes.get(id), jobTitle: job => job.title || job.filename || "", path: id => encodeURIComponent(String(id)),
    msg: (id, text) => { nodes.get(id).textContent = text; }, open: () => {}, close: id => nodes.get(id).classList.remove("open"),
    clearTimeout: id => calls.push(["clearTimeout", id]), disposeDocument: key => delete state.docs[key], renderList: () => calls.push(["renderList"]),
    reader: {destroy: () => calls.push(["destroy"])}, post: async (url, body) => { calls.push(["post", url, body]); if (reject) throw Error("拒绝删除"); return {library: {folders: [], documents: {}}}; },
    api: async () => ({folders: [], documents: {}}), loadJobs: async () => calls.push(["loadJobs"]), select: async id => calls.push(["select", id]),
  };
  vm.runInNewContext(workspaceSource.slice(deletionStart, deletionEnd), context);
  return {state, nodes, calls, ...context};
}

test("delete confirmation executes the job request and failed deletion keeps the list", async () => {
  const h = deletionHarness({jobs: [{id: "a"}, {id: "b"}], reject: true});
  h.state.deletingJob = {id: "b", title: "第二篇"};
  await h.submitDelete();
  assert.equal(h.calls[0][0], "post");
  assert.equal(h.calls[0][1], "/api/jobs/b/delete");
  assert.deepEqual({...h.calls[0][2]}, {});
  assert.deepEqual(h.state.jobs.map(job => job.id), ["a", "b"]);
  assert.equal(h.nodes.get("delete-msg").textContent, "拒绝删除");
});

test("deleting a non-current job keeps the current polling timer", async () => {
  const h = deletionHarness({jobs: [{id: "a"}, {id: "b"}], activeJob: {id: "a"}});
  h.state.deletingJob = {id: "b", title: "第二篇"};
  await h.submitDelete();
  assert.equal(h.calls.some(call => call[0] === "clearTimeout"), false);
  assert.deepEqual(h.state.jobs.map(job => job.id), ["a"]);
});

test("deleting the current last job clears the reader state", async () => {
  const h = deletionHarness({jobs: [{id: "a"}], activeJob: {id: "a"}});
  h.state.deletingJob = {id: "a", title: "唯一一篇"};
  await h.submitDelete();
  assert.equal(h.state.job, null);
  assert.equal(h.state.page, 1);
  assert.equal(h.nodes.get("empty").hidden, false);
  assert.equal(h.nodes.get("pages").hidden, true);
  assert.equal(h.nodes.get("notice").textContent, "");
  assert.equal(h.nodes.get("progress-wrap").hidden, true);
});
