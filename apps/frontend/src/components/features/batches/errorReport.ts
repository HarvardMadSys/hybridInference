import type { PreflightReport } from '@/lib/api/batches';

function csvCell(value: unknown): string {
  const text = value === null || value === undefined ? '' : String(value);
  // Quoted, and a leading formula character neutralised for spreadsheets.
  const safe = /^[=+\-@\t\r]/.test(text) ? `'${text}` : text;
  return `"${safe.replace(/"/g, '""')}"`;
}

/**
 * The preflight's error list as a file: CSV for a spreadsheet, or JSONL.
 *
 * Built in the browser from the report (the gateway stores no report). When
 * the list was cut, a last line says how many problems there were in all.
 */
export function errorReportFile(
  report: PreflightReport,
  format: 'csv' | 'jsonl',
): { content: string; mime: string } {
  const truncated = report.errors_truncated
    ? `listed ${report.errors.length} of ${report.error_count} problems`
    : null;
  if (format === 'jsonl') {
    const lines = report.errors.map((error) => JSON.stringify(error));
    if (truncated) lines.push(JSON.stringify({ note: truncated, total: report.error_count }));
    return { content: `${lines.join('\n')}\n`, mime: 'application/jsonl' };
  }
  const rows = [['line', 'custom_id', 'field', 'code', 'message'].map(csvCell).join(',')];
  for (const error of report.errors) {
    rows.push(
      [error.line, error.custom_id, error.field, error.code, error.message].map(csvCell).join(','),
    );
  }
  if (truncated) rows.push(['', '', '', 'note', truncated].map(csvCell).join(','));
  return { content: `${rows.join('\r\n')}\r\n`, mime: 'text/csv' };
}

/** Hand a generated file to the browser to save. */
export function saveText(filename: string, content: string, mime: string): void {
  if (typeof URL.createObjectURL !== 'function') return;
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
