import assert from "node:assert/strict";
import test from "node:test";
import {normalizeSearchText, searchLibrary} from "../static/library-search.mjs";

const jobs = [
  {id: "1", translated_title: "基于表面肌电的外骨骼控制", display_title: "基于表面肌电的外骨骼控制", filename: "sEMG_exoskeleton.pdf"},
  {id: "2", translated_title: "Deep Muscle EMG Physics", display_title: "Deep Muscle EMG Physics", filename: "deep-muscle.pdf"},
  {id: "3", translated_title: "贝叶斯优化", display_title: "贝叶斯优化", filename: "2609.30695v1.pdf"},
];
const paths = {
  "1": "论文库 / 外骨骼文献 / 强化学习",
  "2": "论文库 / 肌骨模型",
  "3": "论文库 / 参考",
};
const result = (query) => searchLibrary(jobs, query, (job) => paths[job.id]).map((item) => item.job.id);

test("normalizes case, width and whitespace", () => {
  assert.equal(normalizeSearchText("  ＳＥＭＧ   Exo "), "semg exo");
});

test("matches translated titles, filenames and folder paths", () => {
  assert.deepEqual(result("表面肌电"), ["1"]);
  assert.deepEqual(result("2609"), ["3"]);
  assert.deepEqual(result("强化学习"), ["1"]);
  const semg = result("SEMG");
  assert.equal(semg[0], "1");
  assert.ok(semg.includes("1"));
});

test("supports multiple terms and fuzzy subsequences", () => {
  assert.deepEqual(result("deep muscle"), ["2"]);
  assert.deepEqual(result("dmp"), ["2"]);
  assert.deepEqual(result("肌电 外骨骼"), ["1"]);
});

test("returns no results for an unknown query and ignores empty queries", () => {
  assert.deepEqual(result("zzzzzz"), []);
  assert.deepEqual(searchLibrary(jobs, "  "), []);
});
