export function buildRowOffsets(rows, pagesTop) {
  return rows.map(({page, top}) => ({page, top: top - pagesTop}));
}

export function findVisiblePage(rowOffsets, scrollerTop, pagesTop, anchor) {
  const target = scrollerTop + anchor - pagesTop;
  let low = 0;
  let high = rowOffsets.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (rowOffsets[middle].top <= target) low = middle + 1;
    else high = middle;
  }
  return rowOffsets[Math.max(0, low - 1)]?.page || 1;
}
