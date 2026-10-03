import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const workspaceSource = fs.readFileSync(new URL("../static/workspace.mjs", import.meta.url), "utf8");
const panStart = workspaceSource.indexOf("const panState=");
const panEnd = workspaceSource.indexOf("async function addAnnotation", panStart);
const listenerLine = workspaceSource.split(/\r?\n/).find(line => line.includes('event.code!=="Space"'));
assert.ok(panStart >= 0 && panEnd > panStart && listenerLine, "workspace pan handlers should be present");

function harness() {
  const listeners = new Map();
  const classes = new Set();
  const released = [];
  const scroll = {
    scrollLeft: 0,
    scrollTop: 0,
    classList: {add: (...names) => names.forEach(name => classes.add(name)), remove: (...names) => names.forEach(name => classes.delete(name))},
    addEventListener: (type, callback) => listeners.set(`scroll:${type}`, callback),
    setPointerCapture: pointerId => { scroll.captured = pointerId; },
    releasePointerCapture: pointerId => { released.push(pointerId); scroll.captured = null; },
  };
  const document = {
    addEventListener: (type, callback) => listeners.set(`document:${type}`, callback),
    querySelectorAll: () => [],
  };
  const window = {
    addEventListener: (type, callback) => listeners.set(`window:${type}`, callback),
    getSelection: () => ({removeAllRanges() {}}),
  };
  const context = {
    document,
    window,
    scroll,
    classes,
    released,
    requestAnimationFrame: callback => callback(),
    cancelAnimationFrame: () => {},
    $: id => id === "scroll" ? scroll : null,
  };
  vm.runInNewContext(workspaceSource.slice(panStart, panEnd) + `\n${listenerLine}`, context);
  return {listeners, classes, scroll, released};
}

const plainTarget = {closest: () => null};
const inputTarget = {closest: selector => selector.includes("input") ? {} : null};
const event = (extra = {}) => ({preventDefault() {}, ...extra});

test("Space repeat is consumed while pan mode is active", () => {
  const h = harness();
  let prevented = false;
  h.listeners.get("document:keydown")({code: "Space", repeat: false, isComposing: false, target: plainTarget, preventDefault: () => {prevented = true;}});
  assert.equal(prevented, true);
  prevented = false;
  h.listeners.get("document:keydown")({code: "Space", repeat: true, isComposing: false, target: plainTarget, preventDefault: () => {prevented = true;}});
  assert.equal(prevented, true);
  assert.equal(h.classes.has("space-pan"), true);
});

test("forms do not intercept Space and pointer pan moves both axes", () => {
  const h = harness();
  let prevented = false;
  h.listeners.get("document:keydown")({code: "Space", repeat: false, isComposing: false, target: inputTarget, preventDefault: () => {prevented = true;}});
  assert.equal(prevented, false);
  assert.equal(h.classes.has("space-pan"), false);
  h.listeners.get("document:keydown")({code: "Space", repeat: false, isComposing: false, target: plainTarget, preventDefault() {}});
  h.listeners.get("scroll:pointerdown")(event({button: 0, pointerId: 7, clientX: 10, clientY: 20}));
  h.listeners.get("scroll:pointermove")(event({pointerId: 7, clientX: 42, clientY: 55}));
  assert.deepEqual([h.scroll.scrollLeft, h.scroll.scrollTop], [-32, -35]);
  assert.equal(h.classes.has("is-panning"), true);
});

test("pointerup, pointercancel, lost capture, keyup and blur release the active pointer", () => {
  const h = harness();
  const keydown = h.listeners.get("document:keydown");
  const keyup = h.listeners.get("document:keyup");
  const down = h.listeners.get("scroll:pointerdown");
  keydown({code: "Space", repeat: false, isComposing: false, target: plainTarget, preventDefault() {}});
  down(event({button: 0, pointerId: 1, clientX: 0, clientY: 0}));
  h.listeners.get("scroll:pointerup")(event({pointerId: 1}));
  assert.deepEqual(h.released, [1]);
  down(event({button: 0, pointerId: 2, clientX: 0, clientY: 0}));
  h.listeners.get("scroll:pointercancel")(event({pointerId: 2}));
  assert.equal(h.classes.has("is-panning"), false);
  assert.deepEqual(h.released, [1, 2]);
  down(event({button: 0, pointerId: 3, clientX: 0, clientY: 0}));
  h.listeners.get("scroll:lostpointercapture")(event({pointerId: 3}));
  assert.deepEqual(h.released, [1, 2, 3]);
  down(event({button: 0, pointerId: 4, clientX: 0, clientY: 0}));
  h.listeners.get("window:blur")();
  assert.deepEqual(h.released, [1, 2, 3, 4]);
  assert.equal(h.classes.has("space-pan"), false);
  keyup({code: "Space"});
  assert.equal(h.classes.has("is-panning"), false);
});
