/** Hand a file the page made to the browser to save. */
export function saveFile(filename: string, content: string | Blob, mime: string): void {
  if (typeof URL.createObjectURL !== 'function') return;
  const blob = typeof content === 'string' ? new Blob([content], { type: mime }) : content;
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
