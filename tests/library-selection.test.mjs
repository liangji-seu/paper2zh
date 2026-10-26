import assert from "node:assert/strict";
import test from "node:test";
import {chunks, pendingTranslationIds, selectionAfterClick} from "../static/library-selection.mjs";

const visible = ["a", "b", "c", "d"];

test("plain click selects a single row", () => {
  assert.deepEqual(selectionAfterClick(new Set(["a", "b"]), visible, "b", "c", {}), {selected: ["c"], anchor: "c"});
});

test("ctrl click toggles membership without opening", () => {
  const first = selectionAfterClick(new Set(), visible, null, "b", {ctrl: true});
  assert.deepEqual(first, {selected: ["b"], anchor: "b"});
  const second = selectionAfterClick(new Set(first.selected), visible, "b", "d", {ctrl: true});
  assert.deepEqual(second.selected.sort(), ["b", "d"]);
  const third = selectionAfterClick(new Set(second.selected), visible, "d", "b", {ctrl: true});
  assert.deepEqual(third.selected, ["d"]);
});

test("shift click selects the visible range from the anchor", () => {
  assert.deepEqual(selectionAfterClick(new Set(["b"]), visible, "b", "d", {shift: true}), {selected: ["b", "c", "d"], anchor: "b"});
  assert.deepEqual(selectionAfterClick(new Set(), visible, "c", "a", {shift: true}), {selected: ["a", "b", "c"], anchor: "c"});
});

test("batch chunking keeps order and caps the group size", () => {
  assert.deepEqual(chunks([1, 2, 3, 4, 5], 3), [[1, 2, 3], [4, 5]]);
  assert.deepEqual(chunks([], 3), []);
});

test("only untranslated jobs are queued for batch translation", () => {
  const jobs = [
    {id: "a", status: "imported"},
    {id: "b", status: "completed"},
    {id: "c", status: "failed"},
    {id: "d", status: "running"},
    {id: "e", status: "queued"},
  ];
  assert.deepEqual(pendingTranslationIds(jobs, ["a", "b", "c", "d", "e", "missing"]), ["a", "c"]);
});
