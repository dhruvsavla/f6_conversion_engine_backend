"""
ingestion/extractor/pdf_loader.py

Loads a PBM F6 implementation guide PDF and extracts raw text.

Strategy:
  - Use pdfplumber for text extraction (better table handling than PyPDF2)
  - Extract page by page, preserving page numbers for citation
  - Detect and extract tables separately from prose (tables contain field specs)
  - Return a list of PageContent objects: { page_number, prose_text, tables }

Why pdfplumber over PyMuPDF or PyPDF2:
  - Handles the multi-column layouts common in NCPDP guides
  - Extracts tables as structured data (list of rows), not garbled text
  - Preserves whitespace better for field code detection
"""

import pdfplumber
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TableRow:
    cells: list[str]


@dataclass
class ExtractedTable:
    page_number: int
    headers: list[str]
    rows: list[TableRow]
    raw_text: str           # fallback: the table rendered as plain text


@dataclass
class PageContent:
    page_number: int        # 1-indexed
    prose_text: str         # non-table text on this page
    tables: list[ExtractedTable]
    raw_text: str           # full raw text including tables (for fallback)


@dataclass
class PDFDocument:
    file_path: str
    total_pages: int
    pages: list[PageContent]
    total_tokens_estimate: int   # rough estimate: len(full_text) / 4

    def full_text(self) -> str:
        """All prose + table text concatenated with page markers."""
        parts = []
        for page in self.pages:
            parts.append(f'\n--- PAGE {page.page_number} ---\n')
            parts.append(page.prose_text)
            for table in page.tables:
                parts.append('\n[TABLE]\n' + table.raw_text + '\n[/TABLE]\n')
        return '\n'.join(parts)


class PDFLoader:

    def load(self, pdf_path: str) -> PDFDocument:
        """Load PDF and extract all text and tables."""
        pages = []

        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)

            for page in pdf.pages:
                page_num = page.page_number

                # Extract tables first — pdfplumber identifies table bounding boxes
                tables = []
                for table in page.extract_tables():
                    if not table:
                        continue
                    headers = [str(c or '').strip() for c in (table[0] or [])]
                    rows = [
                        TableRow(cells=[str(c or '').strip() for c in row])
                        for row in table[1:]
                        if any(c for c in row)
                    ]
                    raw = self._table_to_text(headers, rows)
                    tables.append(ExtractedTable(
                        page_number=page_num,
                        headers=headers,
                        rows=rows,
                        raw_text=raw,
                    ))

                # Extract all text (prose + tables merged — pdfplumber handles layout)
                raw_text = page.extract_text(x_tolerance=2, y_tolerance=2) or ''

                pages.append(PageContent(
                    page_number=page_num,
                    prose_text=raw_text,
                    tables=tables,
                    raw_text=raw_text,
                ))

        full = ''.join(p.raw_text for p in pages)
        token_estimate = len(full) // 4

        return PDFDocument(
            file_path=pdf_path,
            total_pages=total_pages,
            pages=pages,
            total_tokens_estimate=token_estimate,
        )

    def _table_to_text(self, headers: list[str], rows: list[TableRow]) -> str:
        """Convert a structured table to readable plain text."""
        lines = ['  '.join(headers)]
        lines.append('-' * 60)
        for row in rows:
            lines.append('  '.join(row.cells))
        return '\n'.join(lines)
