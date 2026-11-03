// PDF export for a Data Lens result.
//
// jsPDF is loaded with a dynamic import inside `exportPdf`, so Vite emits it
// as its own chunk and nobody downloads a PDF writer unless an admin presses
// the button. That is what makes the dependency affordable on a page four
// people will ever open: the cost lands on the press, not on every visit.
//
// Two layouts, because one does not fit every result
// ---------------------------------------------------
// A grid only works while the columns are few enough to each get a readable
// width. `select * from stock_articles` returns 16 columns, one of which is a
// 600-character JSON blob: on A4 that is 17mm per column, about eleven
// characters per line, so a timestamp wraps to three lines and the blob to
// fifty. The result is a wall of fragments.
//
// So a wide result is printed as records instead — one block per row, field
// name beside value, the value wrapped across the full page width. This is
// what `psql \x` does, for the same reason. The grid is kept for narrow
// results, where lining values up under a heading is what makes them
// comparable, which is the whole point of a table.

// Beyond this many columns a grid cannot give each one a readable width.
// Eight is where A4 landscape still leaves ~33mm per column.
export const GRID_MAX_COLUMNS = 8;
// Roughly how many characters of body text fit across the page at the grid's
// font size. Shared between the columns, which is why the per-cell budget
// depends on how many there are.
const GRID_LINE_BUDGET = 900;
// Below this a cell is too short to say anything useful, so it is the floor
// even when the columns are many.
const GRID_MIN_CELL_CHARS = 40;

// How much of a cell to print before shortening it. A cap is needed at all
// because one tall cell sets the height of its whole row, so a single long
// value costs every other column on that row its legibility — but a fixed
// cap truncates a five-column result that had room to spare.
export function gridCellChars(columnCount) {
  return Math.max(GRID_MIN_CELL_CHARS, Math.round(GRID_LINE_BUDGET / Math.max(1, columnCount)));
}

// Everything that decides what the document says lives in these helpers so
// it can be tested; only `exportPdf` touches jsPDF, which needs a browser.

// A filename that sorts chronologically and says what it is. Date first for
// the sort, the tables second so a folder of these is readable.
export function pdfFilename(result, now = new Date()) {
  const stamp = now.toISOString().slice(0, 16).replace('T', '-').replace(':', '');
  const what = (result?.tables ?? []).join('-') || 'query';
  return `data-lens-${stamp}-${what}.pdf`;
}

// Which layout a result gets. Decided on column count alone: it is what
// drives the available width, and it is knowable before anything is drawn.
export function layoutFor(result) {
  return (result?.columns?.length ?? 0) > GRID_MAX_COLUMNS ? 'records' : 'grid';
}

export function cellText(value) {
  if (value === null || value === undefined) return 'null';
  return String(value);
}

// A cell for the grid, shortened with the original length kept. Saying how
// much was dropped matters: "…" alone leaves the reader unable to tell a
// clipped sentence from a clipped JSON document.
export function gridCell(value, max) {
  const text = cellText(value);
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1)}… (${text.length} chars)`;
}

// Word wrap that also breaks a word too long to fit.
//
// Written here rather than left to jsPDF's splitTextToSize, which does not
// reliably break a single unbroken run longer than the line: a JSON blob or
// a URL then overflows the page edge instead of wrapping, which is one of
// the ways the first version of this export came out unreadable.
export function wrapText(text, maxChars) {
  const out = [];
  for (const paragraph of cellText(text).split('\n')) {
    if (!paragraph) {
      out.push('');
      continue;
    }
    let line = '';
    for (const word of paragraph.split(/\s+/).filter(Boolean)) {
      if (word.length > maxChars) {
        if (line) {
          out.push(line);
          line = '';
        }
        // Hard-break the oversized run, keeping any remainder as the start
        // of the next line so the following words still flow after it.
        let rest = word;
        while (rest.length > maxChars) {
          out.push(rest.slice(0, maxChars));
          rest = rest.slice(maxChars);
        }
        line = rest;
        continue;
      }
      const candidate = line ? `${line} ${word}` : word;
      if (candidate.length > maxChars) {
        out.push(line);
        line = word;
      } else {
        line = candidate;
      }
    }
    if (line) out.push(line);
  }
  return out.length ? out : [''];
}

// One row as field/value pairs for the record layout.
export function recordFields(columns, row) {
  return columns.map((name, i) => ({ name, value: cellText(row[i]) }));
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

// The provenance block. Printed on every export because a page of numbers
// with no date, no query and no timezone is unreadable a week later — and a
// week later is exactly when someone opens it.
export function provenance(result, now = new Date()) {
  const n = result.row_count;
  const lines = [
    `${n} row${n === 1 ? '' : 's'} in ${result.duration_ms} ms`
    + `${result.truncated ? ' — truncated, there is more' : ''}`,
    `Exported ${now.toISOString()}`,
  ];
  if (layoutFor(result) === 'records') {
    lines.push(
      `${result.columns.length} columns is too many for a readable table on `
      + 'A4, so each row is printed as a block of fields instead.',
    );
  } else {
    const cap = gridCellChars(result.columns?.length ?? 1);
    if (result.rows?.some((r) => r.some((c) => cellText(c).length > cap))) {
      lines.push(
        `Values over ${cap} characters are shortened in the table above; `
        + 'use "Copy as TSV" for the full text.',
      );
    }
  }
  const notes = result.notes ?? [];
  // The backend already sends a masking note, and its version names the
  // setting to change. Only say it ourselves if nothing else did — the first
  // render of this printed the sentence twice.
  if (result.masked && !notes.some((note) => /mask/i.test(note))) {
    lines.push('Email addresses are masked.');
  }
  if (result.timezone) {
    lines.push(
      `Timestamps are ${result.timezone.stored} with an explicit offset; `
      + `dates are ${result.timezone.display} calendar days.`,
    );
  }
  for (const note of notes) lines.push(note);
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

// Record layout geometry.
const FIELD_LABEL_MM = 34;   // widest column name in the schema, plus air
const RECORD_VALUE_PT = 8.5;
// Courier is monospace at 0.6 em, so characters-per-line is arithmetic
// rather than measurement — which is what lets `wrapText` be tested without
// a PDF in front of it.
const MONO_CHAR_MM = RECORD_VALUE_PT * 0.3528 * 0.6;

// `save: false` returns the document instead of downloading it, which is how
// the rendered output gets checked outside a browser — the layout is the part
// of this file that cannot be verified by reading it.
export async function exportPdf({ sql, result, reading, save = true }) {
  const [{ jsPDF }, { default: autoTable }] = await Promise.all([
    import('jspdf'),
    import('jspdf-autotable'),
  ]);

  const layout = layoutFor(result);
  // Records read better in portrait: narrower lines, more of them per page.
  // A grid needs the width.
  const doc = new jsPDF({
    orientation: layout === 'records' ? 'portrait' : 'landscape',
    unit: 'mm',
    format: 'a4',
  });
  const width = doc.internal.pageSize.getWidth();
  const height = doc.internal.pageSize.getHeight();
  const usable = width - MARGIN * 2;
  let y = MARGIN;

  const room = (needed) => {
    if (y + needed > height - MARGIN) {
      doc.addPage();
      y = MARGIN;
    }
  };

  const write = ({ style, text }) => {
    const s = STYLES[style] ?? STYLES.body;
    doc.setFontSize(s.size);
    doc.setFont(...s.font);
    const indent = s.indent ?? 0;
    const lines = doc.splitTextToSize((s.prefix ?? '') + text, usable - indent);
    const lineHeight = s.size * 0.42;
    room(lines.length * lineHeight);
    doc.text(lines, MARGIN + indent, y);
    y += lines.length * lineHeight + s.gap;
  };

  const footer = () => {
    doc.setFontSize(7);
    doc.setFont('helvetica', 'italic');
    doc.text(
      `Page ${doc.internal.getCurrentPageInfo().pageNumber}`,
      width - MARGIN, height - 6, { align: 'right' },
    );
  };

  write({ style: 'h1', text: 'Data Lens — Little Money Monkey' });
  write({ style: 'h2', text: 'The question' });
  write({ style: 'code', text: sql });
  for (const block of readingBlocks(reading)) write(block);

  write({ style: 'h2', text: 'The result' });

  if (layout === 'grid') {
    autoTable(doc, {
      startY: y,
      margin: { left: MARGIN, right: MARGIN },
      head: [result.columns],
      body: result.rows.map((r) => r.map((c) => gridCell(c, gridCellChars(result.columns.length)))),
      styles: { fontSize: 7.5, cellPadding: 1.3, overflow: 'linebreak', valign: 'top' },
      headStyles: { fillColor: [45, 109, 199], fontSize: 8 },
      // Repeated on every page: a table whose headers are three pages back
      // is a table you have to keep flipping to read.
      showHead: 'everyPage',
      didDrawPage: footer,
    });
    y = (doc.lastAutoTable?.finalY ?? y) + 6;
  } else {
    const valueChars = Math.max(20, Math.floor((usable - FIELD_LABEL_MM) / MONO_CHAR_MM));
    const lineHeight = RECORD_VALUE_PT * 0.42;
    result.rows.forEach((row, i) => {
      // Keep a record's heading with at least its first lines rather than
      // stranding "Row 7 of 90" alone at the foot of a page.
      room(lineHeight * 4);
      doc.setFontSize(9);
      doc.setFont('helvetica', 'bold');
      doc.text(`Row ${i + 1} of ${result.rows.length}`, MARGIN, y);
      y += lineHeight + 1.5;

      for (const field of recordFields(result.columns, row)) {
        const lines = wrapText(field.value, valueChars);
        room(lineHeight);
        doc.setFontSize(RECORD_VALUE_PT);
        doc.setFont('helvetica', 'normal');
        doc.text(field.name, MARGIN, y);
        doc.setFont('courier', 'normal');
        // Long values may run past a page break, so each line is placed
        // individually and asks for room as it goes.
        lines.forEach((line, n) => {
          if (n > 0) room(lineHeight);
          doc.text(line, MARGIN + FIELD_LABEL_MM, y);
          y += lineHeight;
        });
      }
      y += 3;
      doc.setDrawColor(210);
      doc.line(MARGIN, y - 1.5, width - MARGIN, y - 1.5);
      y += 1.5;
    });
    footer();
  }

  y += 2;
  for (const line of provenance(result)) write({ style: 'italic', text: line });

  if (save) doc.save(pdfFilename(result));
  return doc;
}
