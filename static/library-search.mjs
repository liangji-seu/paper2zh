export function normalizeSearchText(value) {
  return String(value ?? "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

function scoreText(text, term) {
  if (!text || !term) return null;
  if (text === term) return 1500;
  const index = text.indexOf(term);
  if (index >= 0) return 1000 - Math.min(300, index * 3) - Math.min(150, text.length - term.length);
  let position = 0;
  let first = -1;
  let last = -1;
  let gaps = 0;
  for (const character of term) {
    const found = text.indexOf(character, position);
    if (found < 0) return null;
    if (first < 0) first = found;
    if (last >= 0) gaps += found - last - 1;
    last = found;
    position = found + 1;
  }
  return 450 - gaps * 4 - Math.min(150, (last - first + 1 - term.length) * 2) - Math.min(100, first);
}

export function searchLibrary(jobs, query, folderPathFor = () => "") {
  const terms = normalizeSearchText(query).split(" ").filter(Boolean);
  if (!terms.length) return [];
  const results = [];
  for (const job of jobs || []) {
    const folder = normalizeSearchText(folderPathFor(job) || "");
    const fields = [
      {text: normalizeSearchText(job.translated_title), weight: 1.3},
      {text: normalizeSearchText(job.display_title), weight: 1.2},
      {text: normalizeSearchText(job.filename), weight: 1.1},
      {text: folder, weight: 0.9},
    ].filter((field) => field.text);
    let score = 0;
    let matched = true;
    for (const term of terms) {
      let best = null;
      for (const field of fields) {
        const value = scoreText(field.text, term);
        if (value !== null) best = best === null ? value * field.weight : Math.max(best, value * field.weight);
      }
      if (best === null) {
        matched = false;
        break;
      }
      score += best;
    }
    if (matched) results.push({job, score});
  }
  results.sort((left, right) => right.score - left.score || String(left.job.filename || "").localeCompare(String(right.job.filename || ""), "zh-CN"));
  return results;
}
