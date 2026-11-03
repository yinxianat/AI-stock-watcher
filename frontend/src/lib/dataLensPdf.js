// PDF export for a Data Lens result.
//
// jsPDF is loaded with a dynamic import inside `exportPdf`, so Vite emits it
// as its own chunk and nobody downloads a PDF writer unless an admin presses
// the button. That is what makes the dependency affordable on a page four
// people will ever open: the cost lands on the press, not on every visit.
//
// The document is laid out as a report rather than a screenshot of the page:
// the question, what the AI made of it, then the rows. All of them, not the
// page on screen — the point of exporting is to have the thing you could not
// read in one sitting.

// Everything that decides what the document says lives in these helpers so
// it can be tested; only `exportPdf` touches jsPDF, which needs a browser.

// A filename that sorts chronologically and says what it is. Date first for
// the sort, the tables second so a folder of these is readable.
export function pdfFilename(result, now = new Date()) {
  const stamp = now.toISOString().slice(0, 16).replace('T', '-').replace(':', '');
  const what = (result?.tables ?? []).join('-') || 'query';
  return `data-lens-${stamp}-${what}.pdf`;
}

// The AI's reading as printable blocks. Returns [] when there is no reading,
// so the caller does not branch.
export function readingBlocks(reading) {
  if (!reading) return [];
  const blocks = [
    { style: 'h2', text: 'What this means' },
    { style: 'bold', text: reading.headline },
    { style: 'body', text: reading.summary },
  ];
  if (reading.findings?.length) {
    blocks.push({ style: 'h3', text: 'Worth acting on' });
    for (const f of reading.findings) {
      blocks.push({ style: 'bullet', text: `[${f.severity}] ${f.detail}` });
      // The evidence travels with the finding. A printed claim about
      // production with no way to check it is worse than no claim: it has to
      // be chased before it can be dismissed, and the rows are right here.
      if (f.evidence) blocks.push({ style: 'sub', text: `Evidence: ${f.evidence}` });
    }
  }
  if (reading.next_queries?.length) {
    blocks.push({ style: 'h3', text: 'Worth asking next' });
    for (const q of reading.next_queries) {
      blocks.push({ style: 'bullet', text: q.question });
      blocks.push({ style: 'code', text: q.sql.replace(/\s+/g, ' ') });
    }
  }
  blocks.push({
    style: 'italic',
    text: `Written by ${reading.model} from ${reading.rows_seen} of `
      + `${reading.rows_total} rows. Check anything you act on against the data.`,
  });
  return blocks;
}

// The provenance line. Printed on every export because a page of numbers
// with no date, no query and no timezone is unreadable a week later — and a
// week later is exactly when someone opens it.
export function provenance(result, now = new Date()) {
  const lines = [
    `${result.row_count} row(s) in ${result.duration_ms} ms`
    + `${result.truncated ? ' — truncated, there is more' : ''}`,
    `Exported ${now.toISOString()}`,
  ];
  if (result.masked) lines.push('Email addresses are masked.');
  if (result.timezone) {
    lines.push(
      `Timestamps are ${result.timezone.stored} with an explicit offset; `
      + `dates are ${result.timezone.display} calendar days.`,
    );
  }
  for (const note of result.notes ?? []) lines.push(note);
  return lines;
}

const MARGIN = 14;
const STYLES = {
  h1: { size: 15, font: ['helvetica', 'bold'], gap: 6 },
  h2: { size: 11.5, font: ['helvetica', 'bold'], gap: 4 },
  h3: { size: 10, font: ['helvetica', 'bold'], gap: 3 },
  bold: { size: 10, font: ['helvetica', 'bold'], gap: 3 },
  body: { size: 9.5, font: ['helvetica', 'normal'], gap: 3 },
  bullet: { size: 9.5, font: ['helvetica', 'normal'], gap: 2, indent: 4, prefix: '• ' },
  sub: { size: 8.5, font: ['helvetica', 'italic'], gap: 3, indent: 8 },
  code: { size: 8, font: ['courier', 'normal'], gap: 3, indent: 8 },
  italic: { size: 8.5, font: ['helvetica', 'italic'], gap: 3 },
};

export async function exportPdf({ sql, result, reading }) {
  const [{ jsPDF }, { default: autoTable }] = await Promise.all([
    import('jspdf'),
    import('jspdf-autotable'),
  ]);

  const doc = new jsPDF({ orientation: 'landscape', unit: 'mm', format: 'a4' });
  const width = doc.internal.pageSize.getWidth();
  const usable = width - MARGIN * 2;
  let y = MARGIN;

  // Break a page before text that would otherwise straddle the bottom edge.
  const write = ({ style, text }) => {
    const s = STYLES[style] ?? STYLES.body;
    doc.setFontSize(s.size);
    doc.setFont(...s.font);
    const indent = s.indent ?? 0;
    const lines = doc.splitTextToSize((s.prefix ?? '') + text, usable - indent);
    const height = lines.length * s.size * 0.42;
    if (y + height > doc.internal.pageSize.getHeight() - MARGIN) {
      doc.addPage();
      y = MARGIN;
    }
    doc.text(lines, MARGIN + indent, y);
    y += height + s.gap;
  };

  write({ style: 'h1', text: 'Data Lens — Little Money Monkey' });
  write({ style: 'h2', text: 'The question' });
  write({ style: 'code', text: sql });
  for (const block of readingBlocks(reading)) write(block);

  write({ style: 'h2', text: 'The result' });
  autoTable(doc, {
    startY: y,
    margin: { left: MARGIN, right: MARGIN },
    head: [result.columns],
    body: result.rows.map((r) => r.map((c) => (c === null ? 'null' : String(c)))),
    styles: { fontSize: 7, cellPadding: 1.2, overflow: 'linebreak' },
    headStyles: { fillColor: [45, 109, 199], fontSize: 7.5 },
    // Repeated on every page: a table of numbers whose headers are three
    // pages back is a table you have to keep flipping to read.
    showHead: 'everyPage',
    didDrawPage: (data) => {
      const page = doc.internal.getNumberOfPages();
      doc.setFontSize(7);
      doc.setFont('helvetica', 'italic');
      doc.text(
        `Page ${data.pageNumber} of ${page}`,
        width - MARGIN,
        doc.internal.pageSize.getHeight() - 6,
        { align: 'right' },
      );
    },
  });

  y = (doc.lastAutoTable?.finalY ?? y) + 6;
  for (const line of provenance(result)) write({ style: 'italic', text: line });

  doc.save(pdfFilename(result));
}
