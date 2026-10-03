import assert from "node:assert/strict";
import test from "node:test";
import {buildRowOffsets, findVisiblePage} from "../static/reader-geometry.mjs";

test("visible page uses real row geometry without double-counting scrollTop", () => {
  const pagesTop = 22;
  const heights = [842, 610, 910, ...Array(22).fill(842)];
  let top = pagesTop;
  const rows = heights.map((height, index) => {
    const row = {page: index + 1, top};
    top += height + (index ? 19 : 19);
    return row;
  });
  const offsets = buildRowOffsets(rows, pagesTop);
  assert.deepEqual(offsets.slice(0, 4), [
    {page: 1, top: 0},
    {page: 2, top: 861},
    {page: 3, top: 1490},
    {page: 4, top: 2419},
  ]);

  const anchor = 150;
  const scrollerTop = 0;
  assert.equal(findVisiblePage(offsets, scrollerTop, pagesTop, anchor), 1);
  assert.equal(findVisiblePage(offsets, scrollerTop, pagesTop - 712, anchor), 1);
  assert.equal(findVisiblePage(offsets, scrollerTop, pagesTop - 733, anchor), 2);

  const page24Top = offsets[23].top;
  assert.equal(findVisiblePage(offsets, scrollerTop, anchor - page24Top, anchor), 24);
  assert.equal(findVisiblePage(offsets, scrollerTop, anchor - page24Top + 1, anchor), 23);
  assert.equal(findVisiblePage(offsets, scrollerTop, pagesTop - 100000, anchor), 25);
});
