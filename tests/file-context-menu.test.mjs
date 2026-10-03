import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import {clampMenuPosition,markdownDirectoryFromResult,markdownLocationUrl} from "../static/file-context-menu.mjs";

const workspaceSource = fs.readFileSync(new URL("../static/workspace.mjs", import.meta.url), "utf8");

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

test("changing files and rebuilding the library close the previous menu owner", () => {
  assert.match(workspaceSource, /function renderList\(\)\{if\(fileMenuJob\)closeFileMenu\(false\)/);
  assert.match(workspaceSource, /button\.addEventListener\("click",\(\)=>\{closeFileMenu\(false\);select\(job\.id\);\}\)/);
  assert.match(workspaceSource, /toggle\.append\(iconSvg\(state\.expanded\.has\(folder\.id\)\?"chevron-down":"chevron-right"\)/);
  assert.match(workspaceSource, /toggle\.setAttribute\("aria-expanded",String\(state\.expanded\.has\(folder\.id\)\)\)/);
  assert.match(workspaceSource, /file-context-menu"\)\.classList\.contains\("open"\)&&!\$\("file-context-menu"\)\.contains\(event\.target\)&&!event\.target\.closest/);
  assert.doesNotMatch(workspaceSource, /document\.addEventListener\("click",event=>\{if\(fileMenuJob/);
  assert.match(workspaceSource, /trigger\?\.setAttribute\?\.\("aria-expanded","false"\)/);
});
