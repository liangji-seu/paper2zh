/** Keep file-menu geometry and location validation independent from the DOM. */
export function clampMenuPosition({x, y, width, height, viewportWidth, viewportHeight, gutter = 8}) {
  const maxX = Math.max(gutter, viewportWidth - width - gutter);
  const maxY = Math.max(gutter, viewportHeight - height - gutter);
  return {
    left: Math.min(Math.max(gutter, Number(x) || 0), maxX),
    top: Math.min(Math.max(gutter, Number(y) || 0), maxY),
  };
}

export function markdownLocationUrl(jobId) {
  return "/api/jobs/" + encodeURIComponent(String(jobId)) + "/markdown-location";
}

export function markdownDirectoryFromResult(result) {
  const directory = typeof result?.directory === "string" ? result.directory.trim() : "";
  if (!directory) throw new Error("未找到这篇论文的 Markdown 目录。请先完成 Markdown 导出。");
  return directory;
}
