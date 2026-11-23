"""Format-independent document model produced by every parser (JATS XML, HTML, PDF)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Paragraph(BaseModel):
    text: str
    section: str | None = None
    page: int | None = None


class Table(BaseModel):
    table_id: str  # stable id from the source (e.g. JATS @id) or 'T{n}'
    label: str | None = None  # 'Table 1'
    caption: str = ""
    header: list[list[str]] = Field(default_factory=list)  # one or more header rows
    rows: list[list[str]] = Field(default_factory=list)
    footnotes: list[str] = Field(default_factory=list)
    page: int | None = None
    is_supplementary: bool = False

    def column_headers(self) -> list[str]:
        """Merge multi-row headers column-wise: ['Mn (kg/mol)', 'Tg (°C)', ...]."""
        if not self.header:
            return []
        n = max(len(r) for r in self.header)
        cols = []
        for j in range(n):
            parts = []
            for r in self.header:
                if j < len(r) and r[j] and r[j] not in parts:
                    parts.append(r[j])
            cols.append(" ".join(parts).strip())
        return cols

    def row_text(self, i: int) -> str:
        """Verbatim evidence string for one data row: 'header1: v1 | header2: v2 ...'."""
        hdr = self.column_headers()
        row = self.rows[i]
        cells = [f"{hdr[j] if j < len(hdr) else f'col{j+1}'}: {c}" for j, c in enumerate(row)]
        return f"{self.label or self.table_id} | " + " | ".join(cells)


class Figure(BaseModel):
    figure_id: str
    label: str | None = None
    caption: str = ""


class SupplementaryFile(BaseModel):
    href: str
    label: str | None = None
    mimetype: str | None = None


class DocumentModel(BaseModel):
    paper_id: str
    doi: str | None = None
    source_format: str  # 'jats_xml' | 'html' | 'pdf'
    source_url: str | None = None
    license: str | None = None
    sha256: str | None = None
    title: str | None = None
    paragraphs: list[Paragraph] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    figures: list[Figure] = Field(default_factory=list)
    supplementary: list[SupplementaryFile] = Field(default_factory=list)

    def full_text(self) -> str:
        return "\n".join(p.text for p in self.paragraphs)
