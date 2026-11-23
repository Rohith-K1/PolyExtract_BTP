"""JATS XML parser (Europe PMC / PMC / many OA publishers) -> DocumentModel.

Superscripts are rendered as '^{..}' so that '10<sup>5</sup>' stays '10^{5}' rather than '105';
subscripts are inlined ('M<sub>n</sub>' -> 'Mn'). Table colspan/rowspan are expanded into a grid.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET

from polyextract.acquisition.document import DocumentModel, Figure, Paragraph, SupplementaryFile, Table

XLINK = "{http://www.w3.org/1999/xlink}href"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def text_of(el: ET.Element | None, *, skip: tuple[str, ...] = ()) -> str:
    """Text content with superscripts marked and footnote cross-refs kept as ^{x}."""
    if el is None:
        return ""
    out: list[str] = []

    def walk(e: ET.Element) -> None:
        name = _local(e.tag)
        if name in skip:
            if e.tail:
                out.append(e.tail)
            return
        if name == "sup":
            out.append("^{" + "".join(e.itertext()).strip() + "}")
        else:
            if e.text:
                out.append(e.text)
            for c in e:
                walk(c)
        if e is not el and e.tail:
            out.append(e.tail)

    walk(el)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _grid(rows: list[ET.Element]) -> list[list[str]]:
    grid: list[list[str]] = []
    pending: dict[int, tuple[str, int]] = {}  # col -> (text, rows_left) from rowspan
    for tr in rows:
        row: list[str] = []
        cells = [c for c in tr if _local(c.tag) in ("td", "th")]
        ci = 0
        col = 0
        while ci < len(cells) or col in pending:
            if col in pending:
                txt, left = pending[col]
                row.append(txt)
                if left <= 1:
                    del pending[col]
                else:
                    pending[col] = (txt, left - 1)
                col += 1
                continue
            c = cells[ci]
            ci += 1
            txt = text_of(c)
            cs = int(c.get("colspan", "1") or 1)
            rs = int(c.get("rowspan", "1") or 1)
            for k in range(cs):
                row.append(txt)
                if rs > 1:
                    pending[col + k] = (txt, rs - 1)
            col += cs
        grid.append(row)
    return grid


def parse_table_wrap(tw: ET.Element, n: int, supplementary: bool = False) -> Table:
    label = text_of(tw.find("./label")) or None
    caption = text_of(tw.find("./caption"))
    header_rows, body_rows = [], []
    for tbl in tw.iter():
        if _local(tbl.tag) != "table":
            continue
        for part in tbl:
            p = _local(part.tag)
            trs = [r for r in part if _local(r.tag) == "tr"] if p in ("thead", "tbody", "tfoot") else []
            if p == "thead":
                header_rows.extend(trs)
            elif p in ("tbody", "tfoot"):
                body_rows.extend(trs)
            elif p == "tr":
                body_rows.append(part)
        break
    header = _grid(header_rows)
    rows = _grid(body_rows)
    if not header and rows and any(_local(c.tag) == "th" for c in body_rows[0]):
        header, rows = rows[:1], rows[1:]
    foot = tw.find("./table-wrap-foot")
    footnotes = [text_of(fn) for fn in foot.iter() if _local(fn.tag) in ("fn", "p")] if foot is not None else []
    footnotes = list(dict.fromkeys(f for f in footnotes if f))
    return Table(
        table_id=tw.get("id") or f"T{n}", label=label, caption=caption, header=header,
        rows=[r for r in rows if any(c.strip() for c in r)], footnotes=footnotes, is_supplementary=supplementary,
    )


def parse_jats(xml_bytes: bytes, *, paper_id: str, doi: str | None = None, source_url: str | None = None,
               license: str | None = None) -> DocumentModel:
    root = ET.fromstring(xml_bytes)
    title = text_of(root.find(".//title-group/article-title"))
    if license is None:
        lic = root.find(".//permissions/license")
        if lic is not None:
            license = lic.get(XLINK) or text_of(lic)[:200] or None

    paragraphs: list[Paragraph] = []
    abstract = root.find(".//abstract")
    if abstract is not None:
        paragraphs.append(Paragraph(text=text_of(abstract), section="Abstract"))

    body = root.find(".//body")

    def walk_sections(el: ET.Element, heading: str | None) -> None:
        for child in el:
            name = _local(child.tag)
            if name == "sec":
                h = text_of(child.find("./title")) or heading
                full = f"{heading} > {h}" if heading and h != heading else h
                walk_sections(child, full)
            elif name == "p":
                t = text_of(child, skip=("table-wrap", "fig"))
                if t:
                    paragraphs.append(Paragraph(text=t, section=heading))

    if body is not None:
        walk_sections(body, None)

    tables = [parse_table_wrap(tw, i + 1) for i, tw in enumerate(root.iter()) if _local(tw.tag) == "table-wrap"]
    # de-duplicate (iter + table-wrap-group can surface the same node only once, but be safe)
    seen, uniq = set(), []
    for t in tables:
        if t.table_id not in seen:
            seen.add(t.table_id)
            uniq.append(t)
    figures = [
        Figure(figure_id=f.get("id") or f"F{i+1}", label=text_of(f.find("./label")) or None, caption=text_of(f.find("./caption")))
        for i, f in enumerate(e for e in root.iter() if _local(e.tag) == "fig")
    ]
    supp = [
        SupplementaryFile(href=s.get(XLINK) or (s.find(".//media").get(XLINK) if s.find(".//media") is not None else ""),
                          label=text_of(s.find("./label")) or None, mimetype=s.get("mimetype"))
        for s in root.iter() if _local(s.tag) == "supplementary-material"
    ]
    return DocumentModel(
        paper_id=paper_id, doi=doi, source_format="jats_xml", source_url=source_url, license=license,
        sha256=hashlib.sha256(xml_bytes).hexdigest(), title=title or None, paragraphs=paragraphs,
        tables=uniq, figures=figures, supplementary=[s for s in supp if s.href],
    )
