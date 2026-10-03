import * as pdfjs from "/static/vendor/pdfjs/pdf.mjs";
import {buildRowOffsets, findVisiblePage} from "/static/reader-geometry.mjs";

pdfjs.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/pdf.worker.mjs";

const ESTIMATED_PAGE = {width: 595, height: 842};
const MAX_LIVE_ROWS = 7;
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

export class ContinuousReader {
  constructor({scroller, pages, getDocument, getAnnotations, onPageChange, onSelection, onSelectionError, onAnnotationClick, onError}) {
    Object.assign(this, {scroller, pages, getDocument, getAnnotations, onPageChange, onSelection, onSelectionError, onAnnotationClick, onError});
    this.generation = 0;
    this.rows = new Map();
    this.slots = new Map();
    this.livePages = new Set();
    this.rowOffsets = [];
    this.rowOffsetsDirty = true;
    this.jobId = null;
    this.pageCount = 0;
    this.visiblePage = 1;
    this.scrollFrame = 0;
    scroller.addEventListener("scroll", () => {
      if (this.scrollFrame) return;
      this.scrollFrame = requestAnimationFrame(() => {
        this.scrollFrame = 0;
        this.updateVisiblePage();
      });
    }, {passive: true});
    document.addEventListener("pointerup", event => {
      if (event.target.closest?.(".annotation-mark")) return;
      setTimeout(() => this.captureSelection(), 0);
    });
  }

  destroy() {
    this.generation++;
    this.observer?.disconnect();
    this.observer = null;
    for (const slot of this.slots.values()) this.releaseSlot(slot);
    this.rows.clear();
    this.slots.clear();
    this.livePages.clear();
    this.rowOffsets = [];
    this.rowOffsetsDirty = true;
    this.pages.replaceChildren();
  }

  captureAnchor() {
    const row = this.rows.get(this.visiblePage);
    if (!row) return {page: this.visiblePage, fraction: 0};
    const top = this.scroller.getBoundingClientRect().top + Math.min(150, this.scroller.clientHeight * .25);
    const rect = row.getBoundingClientRect();
    return {page: this.visiblePage, fraction: clamp((top - rect.top) / Math.max(rect.height, 1), 0, 1)};
  }

  async open({jobId, pageCount, translated, view, zoom, page = 1, preserve = true}) {
    const anchor = preserve && this.jobId === jobId ? this.captureAnchor() : {page, fraction: 0};
    this.destroy();
    this.jobId = jobId;
    this.pageCount = Math.max(1, Number(pageCount) || 1);
    this.visiblePage = clamp(anchor.page, 1, this.pageCount);
    const generation = this.generation;
    const sides = translated ? view === "source" ? ["source"] : view === "translated" ? ["translated"] : ["source", "translated"] : ["source"];
    const available = Math.max(240, this.scroller.clientWidth - 60);
    const fit = Math.max(.45, Math.min(1, (available - 20 * (sides.length - 1)) / (595 * sides.length)));
    const scale = fit * zoom / 100;
    const estimates = {};
    await Promise.all(sides.map(async side => {
      try {
        const doc = await this.getDocument(jobId, side);
        const first = await doc.getPage(1);
        const viewport = first.getViewport({scale});
        estimates[side] = {width: viewport.width, height: viewport.height};
      } catch (_error) {
        estimates[side] = {width: ESTIMATED_PAGE.width * scale, height: ESTIMATED_PAGE.height * scale};
      }
    }));
    if (generation !== this.generation || this.jobId !== jobId) return;
    const fragment = document.createDocumentFragment();
    for (let number = 1; number <= this.pageCount; number++) {
      const row = document.createElement("div");
      row.className = "reader-row";
      row.dataset.page = String(number);
      row.style.minHeight = Math.ceil(Math.max(...sides.map(side => estimates[side].height)) + 19) + "px";
      for (const side of sides) {
        const slot = this.makeSlot(jobId, side, number, estimates[side]);
        row.append(slot.panel);
        this.slots.set(`${number}:${side}`, slot);
      }
      fragment.append(row);
      this.rows.set(number, row);
    }
    this.pages.replaceChildren(fragment);
    this.invalidateRowOffsets();
    this.observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        const number = Number(entry.target.dataset.page);
        if (entry.isIntersecting) this.renderRow(number, generation, scale);
        else this.releaseRow(number);
      }
      this.trimRows();
    }, {root: this.scroller, rootMargin: "900px 0px"});
    for (const row of this.rows.values()) this.observer.observe(row);
    requestAnimationFrame(() => {
      if (generation !== this.generation) return;
      this.restoreAnchor(anchor);
      this.updateVisiblePage();
    });
  }

  makeSlot(jobId, side, page, estimate) {
    const panel = document.createElement("div");
    panel.className = "panel reader-panel";
    panel.dataset.side = side;
    panel.dataset.page = String(page);
    const caption = document.createElement("div");
    caption.className = "caption";
    caption.textContent = `${side === "source" ? "原文" : "译文"} · ${page}`;
    const frame = document.createElement("div");
    frame.className = "page-frame";
    frame.style.width = estimate.width + "px";
    frame.style.height = estimate.height + "px";
    const placeholder = document.createElement("div");
    placeholder.className = "page-placeholder";
    placeholder.textContent = `第 ${page} 页`;
    frame.append(placeholder);
    const error = document.createElement("div");
    error.className = "page-error";
    panel.append(caption, frame, error);
    return {jobId, side, page, panel, frame, placeholder, error, estimate, requestId: 0, loading: false, rendered: false, viewport: null, task: null, canvas: null, text: null, overlay: null};
  }

  renderRow(page, generation, scale) {
    for (const side of ["source", "translated"]) {
      const slot = this.slots.get(`${page}:${side}`);
      if (slot) this.renderSlot(slot, generation, scale);
    }
  }

  async renderSlot(slot, generation, scale) {
    if (slot.rendered || slot.loading) return;
    const requestId = ++slot.requestId;
    slot.loading = true;
    this.livePages.add(slot.page);
    const current = () => generation === this.generation && slot.requestId === requestId && slot.jobId === this.jobId && slot.panel.isConnected;
    try {
      const doc = await this.getDocument(slot.jobId, slot.side);
      if (!current()) return;
      const pdfPage = await doc.getPage(slot.page);
      if (!current()) return;
      const viewport = pdfPage.getViewport({scale});
      slot.viewport = viewport;
      // Keep the real page dimensions as the placeholder size after unloading.
      // This prevents mixed-size PDFs from changing scroll height when a page
      // is released and later rendered again.
      slot.estimate = {width: viewport.width, height: viewport.height};
      this.invalidateRowOffsets();
      slot.frame.style.width = viewport.width + "px";
      slot.frame.style.height = viewport.height + "px";
      const dpr = window.devicePixelRatio || 1;
      const canvas = document.createElement("canvas");
      canvas.className = "pdf-canvas";
      canvas.width = Math.ceil(viewport.width * dpr);
      canvas.height = Math.ceil(viewport.height * dpr);
      canvas.style.width = viewport.width + "px";
      canvas.style.height = viewport.height + "px";
      slot.canvas = canvas;
      slot.frame.append(canvas);
      slot.placeholder.hidden = true;
      slot.error.classList.remove("show");
      slot.task = pdfPage.render({canvasContext: canvas.getContext("2d"), viewport, transform: [dpr, 0, 0, dpr, 0, 0]});
      await slot.task.promise;
      if (!current()) return;
      const text = document.createElement("div");
      text.className = "textLayer";
      text.style.width = viewport.width + "px";
      text.style.height = viewport.height + "px";
      text.style.setProperty("--total-scale-factor", String(scale));
      slot.text = text;
      slot.frame.append(text);
      const content = await pdfPage.getTextContent();
      if (!current()) return;
      slot.textTask = new pdfjs.TextLayer({textContentSource: content, container: text, viewport});
      await slot.textTask.render();
      if (!current()) return;
      const overlay = document.createElement("div");
      overlay.className = "annotation-layer";
      overlay.style.width = viewport.width + "px";
      overlay.style.height = viewport.height + "px";
      slot.overlay = overlay;
      slot.frame.append(overlay);
      slot.rendered = true;
      try {
        const annotations = await this.getAnnotations(slot.jobId, slot.side);
        if (current()) this.drawAnnotations(slot, annotations.annotations || []);
      } catch (error) {
        if (current()) this.onError(`批注暂不可用：${error.message}`);
      }
    } catch (error) {
      if (!current() || error?.name === "RenderingCancelledException") return;
      this.releaseSlot(slot);
      slot.error.textContent = `${slot.side === "source" ? "原文" : "译文"}第 ${slot.page} 页读取失败：${error.message}`;
      slot.error.classList.add("show");
      this.onError(slot.error.textContent);
    } finally {
      if (slot.requestId === requestId) slot.loading = false;
      this.trimRows();
    }
  }

  releaseSlot(slot) {
    slot.requestId++;
    slot.task?.cancel();
    slot.textTask?.cancel?.();
    slot.task = null;
    slot.textTask = null;
    if (slot.canvas) slot.canvas.width = slot.canvas.height = 0;
    slot.canvas?.remove();
    slot.text?.remove();
    slot.overlay?.remove();
    slot.canvas = slot.text = slot.overlay = slot.viewport = null;
    slot.loading = slot.rendered = false;
    slot.placeholder.hidden = false;
    slot.frame.style.width = slot.estimate.width + "px";
    slot.frame.style.height = slot.estimate.height + "px";
    this.invalidateRowOffsets();
    if (!["source", "translated"].some(side => {
      const other = this.slots.get(`${slot.page}:${side}`);
      return other?.loading || other?.rendered;
    })) this.livePages.delete(slot.page);
  }

  releaseRow(page) {
    for (const side of ["source", "translated"]) {
      const slot = this.slots.get(`${page}:${side}`);
      if (slot && (slot.loading || slot.rendered)) this.releaseSlot(slot);
    }
  }

  trimRows() {
    const live = [...this.livePages].filter(page => this.rows.has(page));
    if (live.length <= MAX_LIVE_ROWS) return;
    const center = this.scroller.getBoundingClientRect().top + this.scroller.clientHeight / 2;
    live.sort((a, b) => Math.abs(this.rows.get(a).getBoundingClientRect().top - center) - Math.abs(this.rows.get(b).getBoundingClientRect().top - center));
    for (const page of live.slice(MAX_LIVE_ROWS)) this.releaseRow(page);
  }

  invalidateRowOffsets() {
    this.rowOffsetsDirty = true;
  }

  ensureRowOffsets() {
    if (!this.rowOffsetsDirty) return;
    const pagesTop = this.pages.getBoundingClientRect().top;
    this.rowOffsets = buildRowOffsets([...this.rows].map(([page, row]) => ({page, top: row.getBoundingClientRect().top})), pagesTop);
    this.rowOffsetsDirty = false;
  }

  drawAnnotations(slot, items) {
    if (!slot.overlay || !slot.viewport) return;
    slot.overlay.replaceChildren();
    for (const annotation of items) {
      if (annotation.page !== slot.page) continue;
      for (const rect of annotation.rects) {
        const a = slot.viewport.convertToViewportPoint(rect[0], rect[1]);
        const b = slot.viewport.convertToViewportPoint(rect[2], rect[3]);
        const mark = document.createElement("div");
        mark.className = "annotation-mark" + (annotation.type === "underline" ? " underline" : "");
        mark.style.left = Math.min(a[0], b[0]) + "px";
        mark.style.top = Math.min(a[1], b[1]) + "px";
        mark.style.width = Math.abs(b[0] - a[0]) + "px";
        mark.style.height = Math.abs(b[1] - a[1]) + "px";
        mark.style.backgroundColor = annotation.color;
        mark.style.setProperty("--mark-color", annotation.color);
        mark.style.pointerEvents = "auto";
        mark.title = "点击选择批注，可删除";
        mark.addEventListener("click", event => {
          event.stopPropagation();
          slot.overlay.querySelectorAll(".annotation-mark").forEach(element => element.style.outline = "");
          mark.style.outline = "2px solid #2767d6";
          this.onAnnotationClick({jobId: slot.jobId, side: slot.side, page: slot.page, id: annotation.id});
        });
        slot.overlay.append(mark);
      }
    }
  }

  redrawAnnotations(jobId, side, page, items) {
    const slot = this.slots.get(`${page}:${side}`);
    if (slot?.jobId === jobId) this.drawAnnotations(slot, items);
  }

  captureSelection() {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.anchorNode || !selection.focusNode) return;
    const layerFor = node => (node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement)?.closest?.(".textLayer");
    const first = layerFor(selection.anchorNode);
    const last = layerFor(selection.focusNode);
    if (!first || !this.pages.contains(first)) return;
    if (first !== last) {
      this.onSelectionError("一次只能标注同一页内的文字。");
      return;
    }
    const panel = first.closest(".reader-panel");
    const slot = this.slots.get(`${panel?.dataset.page}:${panel?.dataset.side}`);
    if (!slot?.viewport || !first.contains(selection.anchorNode) || !first.contains(selection.focusNode)) return;
    const range = selection.getRangeAt(0);
    const bounds = slot.frame.getBoundingClientRect();
    const rects = [];
    for (const rect of range.getClientRects()) {
      const x0 = clamp(rect.left - bounds.left, 0, slot.viewport.width);
      const y0 = clamp(rect.top - bounds.top, 0, slot.viewport.height);
      const x1 = clamp(rect.right - bounds.left, 0, slot.viewport.width);
      const y1 = clamp(rect.bottom - bounds.top, 0, slot.viewport.height);
      if (x1 - x0 < 2 || y1 - y0 < 2) continue;
      const a = slot.viewport.convertToPdfPoint(x0, y0);
      const b = slot.viewport.convertToPdfPoint(x1, y1);
      rects.push([Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])]);
    }
    if (rects.length) this.onSelection({jobId: slot.jobId, side: slot.side, page: slot.page, rects, bounds: range.getBoundingClientRect()});
  }

  restoreAnchor(anchor) {
    const row = this.rows.get(clamp(anchor.page, 1, this.pageCount));
    if (!row) return;
    const rect = row.getBoundingClientRect();
    const target = this.scroller.getBoundingClientRect().top + Math.min(150, this.scroller.clientHeight * .25);
    this.scroller.scrollTop += rect.top + clamp(anchor.fraction, 0, 1) * rect.height - target;
  }

  goToPage(number) {
    const page = clamp(Number(number) || 1, 1, this.pageCount);
    const row = this.rows.get(page);
    this.visiblePage = page;
    this.onPageChange(page);
    if (!row) return;
    this.scroller.scrollTop += row.getBoundingClientRect().top - this.scroller.getBoundingClientRect().top - 12;
  }

  updateVisiblePage() {
    if (!this.rows.size) return;
    this.ensureRowOffsets();
    const anchor = Math.min(150, this.scroller.clientHeight * .25);
    const page = findVisiblePage(this.rowOffsets, this.scroller.getBoundingClientRect().top, this.pages.getBoundingClientRect().top, anchor);
    if (page !== this.visiblePage) {
      this.visiblePage = page;
      this.onPageChange(page);
    }
  }
}
