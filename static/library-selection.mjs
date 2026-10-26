/* Pure helpers for the library tree: multi-selection, batching and status. */

export function selectionAfterClick(selected, visibleIds, anchorId, targetId, mods = {}) {
  const current = new Set(selected || []);
  const target = String(targetId);
  if (mods.shift && anchorId != null) {
    const ids = (visibleIds || []).map(String);
    const from = ids.indexOf(String(anchorId));
    const to = ids.indexOf(target);
    if (from >= 0 && to >= 0) {
      const [low, high] = from < to ? [from, to] : [to, from];
      return { selected: ids.slice(low, high + 1), anchor: String(anchorId) };
    }
  }
  if (mods.ctrl || mods.meta) {
    if (current.has(target)) current.delete(target);
    else current.add(target);
    return { selected: [...current], anchor: target };
  }
  return { selected: [target], anchor: target };
}

export function chunks(items, size) {
  const list = [...(items || [])];
  const width = Math.max(1, Number(size) || 1);
  const groups = [];
  for (let index = 0; index < list.length; index += width) groups.push(list.slice(index, index + width));
  return groups;
}

export function pendingTranslationIds(jobs, ids) {
  const byId = new Map((jobs || []).map((job) => [String(job.id), job]));
  return [...(ids || [])].map(String).filter((id) => {
    const job = byId.get(id);
    return Boolean(job) && !["completed", "running", "queued"].includes(job.status);
  });
}
