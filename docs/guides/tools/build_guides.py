#!/usr/bin/env python3
"""Deterministically build BSEW HTML and DOCX guides from canonical Markdown.

This tool intentionally supports only the small Markdown vocabulary used by
the three BSEW publications. PDF publication is produced from the visually
verified DOCX.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor


PETROL = "1F5F67"
PETROL_DARK = "173F45"
PETROL_LIGHT = "E8F1F1"
INK = "1B292A"
MUTED = "5B696A"
RULE = "B7C7C6"
WARM = "F7F4EC"
BODY_FONT = "Georgia"
SANS_FONT = "Arial"
MONO_FONT = "Courier New"
PAGE_WIDTH_DXA = 10175  # A4 usable width at 1.8 cm left/right margins.


@dataclass(frozen=True)
class Block:
    kind: str
    value: object
    level: int = 0


def parse_markdown(path: Path) -> list[Block]:
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[Block] = []
    paragraph: list[str] = []
    index = 0

    def flush() -> None:
        if paragraph:
            blocks.append(Block("paragraph", " ".join(x.strip() for x in paragraph)))
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped == "<!-- pagebreak -->":
            flush()
            blocks.append(Block("pagebreak", ""))
            index += 1
            continue
        if stripped.startswith("```"):
            flush()
            language = stripped[3:].strip()
            code: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1
            blocks.append(Block("code", (language, "\n".join(code))))
            continue
        figure = re.match(r'^!\[([^]]*)\]\((\S+)(?:\s+"([^"]+)")?\)$', stripped)
        if figure:
            flush()
            image_path = (path.parent / figure.group(2)).resolve()
            if not image_path.is_file():
                raise FileNotFoundError(f"Figure does not exist: {image_path}")
            alt = figure.group(1).strip()
            caption = (figure.group(3) or alt).strip()
            if not alt or not caption:
                raise ValueError(f"Figure needs alt text and a caption: {image_path}")
            blocks.append(Block("figure", (image_path, alt, caption)))
            index += 1
            continue
        if stripped.startswith("|") and index + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-+", lines[index + 1]):
            flush()
            rows: list[list[str]] = []
            rows.append([x.strip() for x in stripped.strip("|").split("|")])
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append([x.strip() for x in lines[index].strip().strip("|").split("|")])
                index += 1
            blocks.append(Block("table", rows))
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading:
            flush()
            blocks.append(Block("heading", heading.group(2), len(heading.group(1))))
            index += 1
            continue
        if stripped.startswith("> "):
            flush()
            quote = [stripped[2:].strip()]
            index += 1
            while index < len(lines) and lines[index].strip().startswith("> "):
                quote.append(lines[index].strip()[2:].strip())
                index += 1
            blocks.append(Block("callout", " ".join(quote)))
            continue
        bullet = re.match(r"^-\s+(.+)$", stripped)
        number = re.match(r"^\d+\.\s+(.+)$", stripped)
        if bullet or number:
            flush()
            kind = "bullet" if bullet else "number"
            items: list[str] = []
            while index < len(lines):
                current = lines[index].strip()
                match = re.match(r"^-\s+(.+)$", current) if kind == "bullet" else re.match(r"^\d+\.\s+(.+)$", current)
                if not match:
                    break
                items.append(match.group(1))
                index += 1
            blocks.append(Block(kind, items))
            continue
        if not stripped:
            flush()
        else:
            paragraph.append(stripped)
            if line.endswith("  "):
                flush()
        index += 1
    flush()
    return blocks


def set_font(run, name: str, size: float | None = None, color: str | None = None,
             bold: bool | None = None, italic: bool | None = None) -> None:
    run.font.name = name
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), name)
    if size is not None:
        run.font.size = Pt(size)
    if color is not None:
        run.font.color.rgb = RGBColor.from_string(color)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic


INLINE = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)")


def add_inline(paragraph, text: str, *, base_font: str = BODY_FONT, size: float = 10.5,
               color: str = INK) -> None:
    cursor = 0
    for match in INLINE.finditer(text):
        if match.start() > cursor:
            set_font(paragraph.add_run(text[cursor:match.start()]), base_font, size, color)
        token = match.group(0)
        if token.startswith("`"):
            set_font(paragraph.add_run(token[1:-1]), MONO_FONT, size - 0.6, PETROL_DARK)
        elif token.startswith("**"):
            set_font(paragraph.add_run(token[2:-2]), base_font, size, color, bold=True)
        else:
            set_font(paragraph.add_run(token[1:-1]), base_font, size, color, italic=True)
        cursor = match.end()
    if cursor < len(text):
        set_font(paragraph.add_run(text[cursor:]), base_font, size, color)


def shade_cell(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_margins(cell, top: int = 90, start: int = 120, bottom: int = 90, end: int = 120) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for edge, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{edge}"))
        if node is None:
            node = OxmlElement(f"w:{edge}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_table_geometry(table, widths: list[int]) -> None:
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(sum(widths)))
    tbl_w.set(qn("w:type"), "dxa")
    tbl_ind = tbl_pr.find(qn("w:tblInd"))
    if tbl_ind is None:
        tbl_ind = OxmlElement("w:tblInd")
        tbl_pr.append(tbl_ind)
    tbl_ind.set(qn("w:w"), "120")
    tbl_ind.set(qn("w:type"), "dxa")
    grid = table._tbl.tblGrid
    for child in list(grid):
        grid.remove(child)
    for width in widths:
        col = OxmlElement("w:gridCol")
        col.set(qn("w:w"), str(width))
        grid.append(col)
    for row in table.rows:
        for index, cell in enumerate(row.cells):
            tc_w = cell._tc.get_or_add_tcPr().find(qn("w:tcW"))
            if tc_w is None:
                tc_w = OxmlElement("w:tcW")
                cell._tc.get_or_add_tcPr().append(tc_w)
            tc_w.set(qn("w:w"), str(widths[index]))
            tc_w.set(qn("w:type"), "dxa")
            set_cell_margins(cell)


def repeat_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    marker = OxmlElement("w:tblHeader")
    marker.set(qn("w:val"), "true")
    tr_pr.append(marker)


def configure_page(section) -> None:
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(1.8)
    section.bottom_margin = Cm(1.7)
    section.left_margin = Cm(1.8)
    section.right_margin = Cm(1.8)
    section.header_distance = Cm(0.8)
    section.footer_distance = Cm(0.8)


def add_page_field(paragraph) -> None:
    """Append a real PAGE field so reflow cannot duplicate section-start numbers."""
    begin = paragraph.add_run()
    set_font(begin, SANS_FONT, 8, MUTED)
    begin_char = OxmlElement("w:fldChar")
    begin_char.set(qn("w:fldCharType"), "begin")
    begin._r.append(begin_char)

    instruction = paragraph.add_run()
    set_font(instruction, SANS_FONT, 8, MUTED)
    instruction_text = OxmlElement("w:instrText")
    instruction_text.set(qn("xml:space"), "preserve")
    instruction_text.text = " PAGE "
    instruction._r.append(instruction_text)

    separate = paragraph.add_run()
    set_font(separate, SANS_FONT, 8, MUTED)
    separate_char = OxmlElement("w:fldChar")
    separate_char.set(qn("w:fldCharType"), "separate")
    separate._r.append(separate_char)

    result = paragraph.add_run("1")
    set_font(result, SANS_FONT, 8, MUTED)

    end = paragraph.add_run()
    set_font(end, SANS_FONT, 8, MUTED)
    end_char = OxmlElement("w:fldChar")
    end_char.set(qn("w:fldCharType"), "end")
    end._r.append(end_char)


def configure_running_furniture(section, short_title: str) -> None:
    section.header.is_linked_to_previous = False
    section.footer.is_linked_to_previous = False
    section.different_first_page_header_footer = False
    header = section.header.paragraphs[0]
    header.clear()
    header.alignment = WD_ALIGN_PARAGRAPH.LEFT
    set_font(header.add_run(short_title), SANS_FONT, 8.5, MUTED, bold=True)
    p_pr = header._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:color"), RULE)
    borders.append(bottom)
    p_pr.append(borders)

    footer = section.footer.paragraphs[0]
    footer.clear()
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    set_font(footer.add_run("Mirza Saribiyik  |  Page "), SANS_FONT, 8, MUTED)
    add_page_field(footer)


def configure_document(doc: Document, short_title: str) -> None:
    section = doc.sections[0]
    configure_page(section)
    section.different_first_page_header_footer = False

    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name = BODY_FONT
    normal._element.rPr.rFonts.set(qn("w:ascii"), BODY_FONT)
    normal._element.rPr.rFonts.set(qn("w:hAnsi"), BODY_FONT)
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = RGBColor.from_string(INK)
    normal.paragraph_format.space_after = Pt(5)
    normal.paragraph_format.line_spacing = 1.18

    for name, size, before, after, color in (
        ("Heading 1", 18, 13, 6, PETROL_DARK),
        ("Heading 2", 14, 10, 5, PETROL),
        ("Heading 3", 11.5, 8, 4, PETROL_DARK),
    ):
        style = styles[name]
        style.font.name = SANS_FONT
        style._element.rPr.rFonts.set(qn("w:ascii"), SANS_FONT)
        style._element.rPr.rFonts.set(qn("w:hAnsi"), SANS_FONT)
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor.from_string(color)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True

    for name in ("List Bullet", "List Number"):
        style = styles[name]
        style.font.name = BODY_FONT
        style._element.rPr.rFonts.set(qn("w:ascii"), BODY_FONT)
        style._element.rPr.rFonts.set(qn("w:hAnsi"), BODY_FONT)
        style.font.size = Pt(10.5)
        style.paragraph_format.left_indent = Cm(0.65)
        style.paragraph_format.first_line_indent = Cm(-0.35)
        style.paragraph_format.space_after = Pt(3)
        style.paragraph_format.line_spacing = 1.15

    props = doc.core_properties
    props.author = "Mirza Saribiyik"
    props.title = short_title
    props.subject = "Building Stock Energy Workbench guidance and simulation reporting"
    props.keywords = "BSEW, building stock, EnergyPlus, OpenStudio, reproducibility"


def add_cover(doc: Document, title: str, subtitle: str, content: list[Block]) -> None:
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(106)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_font(p.add_run(title), SANS_FONT, 27, PETROL_DARK, bold=True)
    p.paragraph_format.space_after = Pt(8)
    p2 = doc.add_paragraph()
    p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_font(p2.add_run(subtitle), SANS_FONT, 16, PETROL)
    p2.paragraph_format.space_after = Pt(34)
    rule = doc.add_paragraph()
    rule.paragraph_format.space_before = Pt(18)
    rule.paragraph_format.space_after = Pt(16)
    p_pr = rule._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "14")
    bottom.set(qn("w:color"), PETROL)
    borders.append(bottom)
    p_pr.append(borders)
    for block in content:
        if block.kind == "paragraph":
            p4 = doc.add_paragraph()
            is_meta = str(block.value).startswith("**")
            p4.alignment = WD_ALIGN_PARAGRAPH.CENTER if is_meta else WD_ALIGN_PARAGRAPH.LEFT
            add_inline(p4, str(block.value), base_font=SANS_FONT if is_meta else BODY_FONT,
                       size=9.2 if is_meta else 10,
                       color=MUTED if is_meta else INK)
        elif block.kind == "callout":
            add_callout(doc, str(block.value))
        elif block.kind == "heading":
            p4 = doc.add_paragraph()
            p4.alignment = WD_ALIGN_PARAGRAPH.CENTER
            add_inline(p4, str(block.value), base_font=SANS_FONT, size=10, color=PETROL_DARK)


def add_callout(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    p_pr = p._p.get_or_add_pPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), WARM)
    p_pr.append(shading)
    borders = OxmlElement("w:pBdr")
    for edge in ("top", "left", "bottom", "right"):
        border = OxmlElement(f"w:{edge}")
        border.set(qn("w:val"), "single")
        border.set(qn("w:sz"), "6")
        border.set(qn("w:color"), "827B69")
        border.set(qn("w:space"), "5")
        borders.append(border)
    p_pr.append(borders)
    p.paragraph_format.left_indent = Cm(0.16)
    p.paragraph_format.right_indent = Cm(0.16)
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(8)
    add_inline(p, text, size=10, color=PETROL_DARK)


def add_code_box(doc: Document, code: str) -> None:
    p = doc.add_paragraph()
    p_pr = p._p.get_or_add_pPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), "F1F4F4")
    p_pr.append(shading)
    borders = OxmlElement("w:pBdr")
    left = OxmlElement("w:left")
    left.set(qn("w:val"), "single")
    left.set(qn("w:sz"), "18")
    left.set(qn("w:color"), PETROL)
    left.set(qn("w:space"), "6")
    borders.append(left)
    p_pr.append(borders)
    p.paragraph_format.left_indent = Cm(0.18)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.keep_together = True
    set_font(p.add_run(code), MONO_FONT, 8.8, PETROL_DARK)


def add_docx_table(doc: Document, rows: list[list[str]]) -> None:
    if not rows:
        return
    columns = max(len(row) for row in rows)
    table = doc.add_table(rows=len(rows), cols=columns)
    table.style = "Table Grid"
    if columns == 2:
        widths = [2800, PAGE_WIDTH_DXA - 2800]
    elif columns == 3:
        widths = [2600, 3000, PAGE_WIDTH_DXA - 5600]
    else:
        first = 2200
        remaining = PAGE_WIDTH_DXA - first
        widths = [first] + [remaining // (columns - 1)] * (columns - 1)
        widths[-1] += PAGE_WIDTH_DXA - sum(widths)
    set_table_geometry(table, widths)
    repeat_header(table.rows[0])
    for r_index, values in enumerate(rows):
        # A row split across pages loses its field name/unit and leaves an
        # unexplained continuation sentence at the top of the next page.
        # Keep each academic-data row intact and let Word move it as a unit.
        row_properties = table.rows[r_index]._tr.get_or_add_trPr()
        cannot_split = OxmlElement("w:cantSplit")
        row_properties.append(cannot_split)
        for c_index in range(columns):
            cell = table.cell(r_index, c_index)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            if r_index == 0:
                shade_cell(cell, PETROL_LIGHT)
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            value = values[c_index] if c_index < len(values) else ""
            add_inline(p, value, base_font=SANS_FONT, size=8.8 if columns >= 4 else 9.2,
                       color=PETROL_DARK if r_index == 0 else INK)
            if r_index == 0:
                for run in p.runs:
                    run.bold = True
    after = doc.add_paragraph()
    after.paragraph_format.space_after = Pt(0)


def add_docx_caption(doc: Document, number: int, heading: str) -> None:
    paragraph = doc.add_paragraph(style="Caption")
    paragraph.paragraph_format.space_before = Pt(4)
    paragraph.paragraph_format.space_after = Pt(4)
    paragraph.paragraph_format.keep_with_next = True
    set_font(
        paragraph.add_run(f"Table {number}. {heading}"),
        SANS_FONT,
        8.8,
        MUTED,
        bold=True,
    )


def add_docx_figure(doc: Document, image_path: Path, alt: str, caption: str,
                    number: int) -> None:
    """Add a stable inline figure with explicit accessibility metadata."""
    image = doc.add_paragraph()
    image.alignment = WD_ALIGN_PARAGRAPH.CENTER
    image.paragraph_format.keep_with_next = True
    shape = image.add_run().add_picture(str(image_path), width=Cm(16.2))
    shape._inline.docPr.set("descr", alt)
    shape._inline.docPr.set("title", caption)

    paragraph = doc.add_paragraph(style="Caption")
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Pt(3)
    paragraph.paragraph_format.space_after = Pt(8)
    set_font(
        paragraph.add_run(f"Figure {number}. {caption}"),
        SANS_FONT,
        9.2,
        MUTED,
        italic=True,
    )


def caption_heading(heading: str) -> str:
    return re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", heading).strip()


def build_docx(blocks: list[Block], output: Path) -> None:
    title = str(blocks[0].value)
    subtitle = str(blocks[1].value)
    first_break = next(i for i, block in enumerate(blocks) if block.kind == "pagebreak")
    doc = Document()
    short_title = f"Building Stock Energy Workbench - {subtitle}"
    configure_document(doc, short_title)
    add_cover(doc, title, subtitle, blocks[2:first_break])
    running = doc.add_section(WD_SECTION.NEW_PAGE)
    configure_page(running)
    table_no = 0
    figure_no = 0
    last_heading = "Document table"
    configure_running_furniture(running, short_title)
    for block in blocks[first_break + 1:]:
        if block.kind == "pagebreak":
            # A page break is presentation, not a new document section. Keeping
            # one running section prevents Word/LibreOffice from alternating or
            # dropping header/footer content when prose reflows across pages.
            doc.add_page_break()
        elif block.kind == "heading":
            last_heading = str(block.value)
            level = min(max(block.level, 1), 3)
            p = doc.add_paragraph(style=f"Heading {level}")
            add_inline(p, str(block.value), base_font=SANS_FONT,
                       size={1: 18, 2: 14, 3: 11.5}[level],
                       color=PETROL_DARK if level != 2 else PETROL)
            for run in p.runs:
                run.bold = True
        elif block.kind == "paragraph":
            p = doc.add_paragraph()
            add_inline(p, str(block.value))
        elif block.kind in {"bullet", "number"}:
            for index, item in enumerate(block.value, start=1):
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Cm(0.65)
                p.paragraph_format.first_line_indent = Cm(-0.45)
                p.paragraph_format.space_after = Pt(3)
                # Keep a single list item intact where it fits on one page.
                # Otherwise Word may leave the bullet on one page and carry
                # the final line to the next page without its marker.
                p.paragraph_format.keep_together = True
                marker = "•" if block.kind == "bullet" else f"{index}."
                set_font(p.add_run(f"{marker}\t"), BODY_FONT, 10.5, INK)
                add_inline(p, str(item))
        elif block.kind == "callout":
            add_callout(doc, str(block.value))
        elif block.kind == "code":
            _, code = block.value
            add_code_box(doc, code)
        elif block.kind == "table":
            table_no += 1
            add_docx_caption(doc, table_no, caption_heading(last_heading))
            add_docx_table(doc, block.value)
        elif block.kind == "figure":
            figure_no += 1
            image_path, alt, caption = block.value
            add_docx_figure(doc, image_path, alt, caption, figure_no)
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output)


def inline_html(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)
    escaped = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', escaped)
    # A run identifier such as ALL_VALENC-A_REAL carries an internal hyphen, and
    # a browser is entitled to break a line there.  When it does, the rendered
    # PDF no longer contains the identifier as one token, so the citation and
    # run-identity checks read a name that was never written.  Keep any token
    # that mixes an underscore with a hyphen on one line.  The substitution is
    # applied outside markup only, so link targets are never touched.
    token = re.compile(r"\b([A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*)\b")
    parts = re.split(r"(<[^>]*>)", escaped)
    for index, part in enumerate(parts):
        if part.startswith("<"):
            continue
        parts[index] = token.sub(
            lambda m: f'<span class="nb">{m.group(1)}</span>'
            if "_" in m.group(1) and "-" in m.group(1)
            else m.group(1),
            part,
        )
    escaped = "".join(parts)
    return escaped


def image_data_uri(path: Path) -> str:
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{payload}"


def build_html(blocks: list[Block], output: Path,
               navigation: list[tuple[str, str]]) -> None:
    title = str(blocks[0].value)
    subtitle = str(blocks[1].value)
    body: list[str] = [
        '<header class="site-header"><a class="brand" href="#top">BSEW</a>',
        '<nav aria-label="Guide navigation">' + ''.join(
            f'<a href="{html.escape(href)}">{html.escape(label)}</a>'
            for href, label in navigation) + '</nav></header>',
        '<main id="top">',
        '<section class="cover" aria-labelledby="document-title">',
        f'<h1 id="document-title">{html.escape(title)}</h1>',
        f'<p class="subtitle">{html.escape(subtitle)}</p>',
    ]
    first_break = next(i for i, block in enumerate(blocks) if block.kind == "pagebreak")
    for block in blocks[2:first_break]:
        if block.kind == "paragraph":
            body.append(f'<p>{inline_html(str(block.value))}</p>')
        elif block.kind == "callout":
            body.append(f'<aside class="callout">{inline_html(str(block.value))}</aside>')
        elif block.kind == "heading":
            body.append(f'<h2 class="cover-small">{inline_html(str(block.value))}</h2>')
    body.append('</section>')
    section_open = False
    heading_ids: set[str] = set()
    table_no = 0
    figure_no = 0
    last_heading = "Document table"
    for block in blocks[first_break + 1:]:
        if block.kind == "pagebreak":
            if section_open:
                body.append('</section>')
            body.append('<section class="document-page">')
            section_open = True
        elif block.kind == "heading":
            last_heading = str(block.value)
            visible_level = min(block.level + 1, 4)
            slug = re.sub(r"[^a-z0-9]+", "-", str(block.value).lower()).strip("-") or "section"
            base = slug
            counter = 2
            while slug in heading_ids:
                slug = f"{base}-{counter}"
                counter += 1
            heading_ids.add(slug)
            body.append(f'<h{visible_level} id="{slug}">{inline_html(str(block.value))}</h{visible_level}>')
        elif block.kind == "paragraph":
            body.append(f'<p>{inline_html(str(block.value))}</p>')
        elif block.kind in {"bullet", "number"}:
            tag = "ul" if block.kind == "bullet" else "ol"
            body.append(f'<{tag}>')
            body.extend(f'<li>{inline_html(str(item))}</li>' for item in block.value)
            body.append(f'</{tag}>')
        elif block.kind == "callout":
            body.append(f'<aside class="callout">{inline_html(str(block.value))}</aside>')
        elif block.kind == "code":
            language, code = block.value
            body.append(f'<pre aria-label="{html.escape(language or "Command")}"><code>{html.escape(code)}</code></pre>')
        elif block.kind == "table":
            table_no += 1
            rows = block.value
            caption = f"Table {table_no}. {caption_heading(last_heading)}"
            body.append(f'<div class="table-scroll" tabindex="0" role="region" aria-label="{html.escape(caption)}"><table>')
            body.append(f'<caption>{html.escape(caption)}</caption>')
            body.append('<thead><tr>' + ''.join(f'<th scope="col">{inline_html(cell)}</th>' for cell in rows[0]) + '</tr></thead><tbody>')
            for row in rows[1:]:
                body.append('<tr>' + ''.join((f'<th scope="row">{inline_html(cell)}</th>' if index == 0 else f'<td>{inline_html(cell)}</td>') for index, cell in enumerate(row)) + '</tr>')
            body.append('</tbody></table></div>')
        elif block.kind == "figure":
            figure_no += 1
            image_path, alt, caption = block.value
            body.append(
                '<figure class="document-figure">'
                f'<img src="{image_data_uri(image_path)}" alt="{html.escape(alt)}">'
                f'<figcaption>Figure {figure_no}. {html.escape(caption)}</figcaption>'
                '</figure>')
    if section_open:
        body.append('</section>')
    body.append('</main>')
    css = """
:root{--petrol:#1f5f67;--dark:#173f45;--ink:#1b292a;--muted:#5b696a;--rule:#b7c7c6;--pale:#e8f1f1;--warm:#f7f4ec;color-scheme:light}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:var(--ink);background:#f5f7f6;font:16px/1.58 Georgia,'Times New Roman',serif}
a{color:#145963;text-underline-offset:.18em;overflow-wrap:anywhere}.site-header{position:sticky;top:0;z-index:5;display:flex;justify-content:space-between;align-items:center;gap:18px;min-height:54px;padding:0 max(20px,calc((100vw - 1120px)/2));background:#173f45;color:#fff;border-bottom:3px solid #79aeb1;font-family:Arial,sans-serif}.site-header a{color:#fff}.brand{font-weight:800;letter-spacing:.12em;text-decoration:none}.site-header nav{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:14px}.site-header nav a{font-size:14px;font-weight:700}
main{width:min(100% - 32px,1120px);margin:26px auto 70px}.cover,.document-page{background:#fff;border:1px solid #cfdbda;box-shadow:0 4px 18px rgba(23,63,69,.07);padding:clamp(28px,5vw,72px);margin:0 auto 26px}.cover{min-height:680px;display:flex;flex-direction:column;justify-content:flex-start;text-align:center}.cover h1{max-width:900px;margin:16px auto 10px;color:var(--dark);font:800 clamp(30px,5vw,54px)/1.02 Arial,sans-serif;letter-spacing:.015em}.subtitle{margin:0 auto 32px;color:var(--petrol);font:500 clamp(20px,2.4vw,28px)/1.2 Arial,sans-serif}.cover p{max-width:76ch;margin-left:auto;margin-right:auto}.cover-small{font:700 16px/1.3 Arial,sans-serif;color:var(--dark);margin:20px 0 8px}
h2,h3,h4{font-family:Arial,sans-serif;color:var(--dark);line-height:1.18;scroll-margin-top:74px}h2{font-size:28px;margin:0 0 22px;border-bottom:2px solid var(--petrol);padding-bottom:10px}h3{font-size:20px;margin:28px 0 10px;color:var(--petrol)}h4{font-size:16px;margin:22px 0 8px}p,li{max-width:76ch}code{font-family:'Courier New',monospace;font-size:.92em;color:#164f56}pre{overflow:auto;padding:18px;background:#f1f4f4;border-left:4px solid var(--petrol);font:14px/1.5 'Courier New',monospace}.callout{max-width:82ch;margin:20px 0;padding:16px 18px;background:var(--warm);border:1px solid #d7caa9;border-left:5px solid #936c18}.document-figure{margin:24px auto;max-width:100%;break-inside:avoid}.document-figure img{display:block;max-width:100%;height:auto;margin:auto;border:1px solid var(--rule)}.document-figure figcaption{margin:8px auto 0;max-width:88ch;color:var(--muted);font:italic 13px/1.4 Arial,sans-serif;text-align:center}.table-scroll{overflow-x:auto;margin:18px 0;border:1px solid var(--rule)}.table-scroll:focus{outline:3px solid #d09521;outline-offset:2px}table{width:100%;border-collapse:collapse;font:14px/1.42 Arial,sans-serif;min-width:680px}caption{padding:9px 12px;background:#f5f7f6;color:var(--muted);font-weight:700;text-align:left}th,td{padding:10px 12px;border-bottom:1px solid #d6dfde;text-align:left;vertical-align:top}thead th{background:var(--pale);color:var(--dark)}tbody th{font-weight:700;width:22%}li{margin:.35em 0}.nb{white-space:nowrap}
@media(max-width:600px){body{font-size:16px}.site-header{padding:0 16px}.site-header nav a{font-size:12px}main{width:min(100% - 20px,1120px);margin-top:12px}.cover,.document-page{padding:24px 20px}.cover{min-height:560px}h2{font-size:24px}table{font-size:13px}}
@media print{@page{size:A4 portrait;margin:17mm 18mm}body{background:#fff;font-size:10.5pt;line-height:1.42}.site-header{display:none}main{width:auto;margin:0}.cover,.document-page{border:0;box-shadow:none;padding:0;margin:0;break-after:page}.cover{height:255mm;min-height:0}h2{font-size:18pt}h3{font-size:14pt}h4{font-size:11.5pt}.callout,.document-figure{break-inside:avoid}.document-figure figcaption{font-size:9.5pt}table{font-size:9.5pt}thead{display:table-header-group}a{color:#000;text-decoration:underline}}
"""
    document = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><title>' + html.escape(f"{title} - {subtitle}") + '</title><style>' + css + '</style></head><body>' + ''.join(body) + '</body></html>'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--publication-date", required=True)
    parser.add_argument(
        "--status", choices=("provisional", "final"), default="provisional",
        help="Control publication filenames and manifest status.",
    )
    args = parser.parse_args()

    suffix = f" (Provisional {args.publication_date})" if args.status == "provisional" else ""
    specs = [
        ("installation-guide.md", f"Building Stock Energy Workbench - Installation Guide{suffix}.docx", "installation-guide.html"),
        ("user-guide.md", f"Building Stock Energy Workbench - User Guide{suffix}.docx", "user-guide.html"),
        ("valencia-simulation-report.md", f"Building Stock Energy Workbench - Valencia Simulation Report{suffix}.docx", "valencia-simulation-report.html"),
    ]
    navigation = [
        ("installation-guide.html", "Installation Guide"),
        ("user-guide.html", "User Guide"),
        ("valencia-simulation-report.html", "Valencia Simulation Report"),
    ]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for source_name, docx_name, html_name in specs:
        source = args.source_dir / source_name
        blocks = parse_markdown(source)
        docx_output = args.output_dir / docx_name
        html_output = args.output_dir / html_name
        build_docx(blocks, docx_output)
        build_html(blocks, html_output, [item for item in navigation if item[0] != html_name])
        outputs.extend([docx_output, html_output])
    manifest = {
        "product": "Building Stock Energy Workbench",
        "document_status": args.status,
        "publication_date": args.publication_date,
        "software_commit": args.commit,
        # The figure plan is an authoritative publication source just like the
        # Markdown: it fixes which real screens must appear and records their
        # privacy/legibility acceptance.  Hash every top-level source file so
        # that changing that contract cannot leave a green manifest behind.
        "source": {
            path.name: sha256(path)
            for path in sorted(
                item
                for item in args.source_dir.iterdir()
                if item.is_file()
                and not item.name.startswith(".")
                and item.suffix.lower() in {".md", ".json"}
            )
        },
        "outputs": {path.name: sha256(path) for path in outputs},
        "note": (
            "PDF hashes are added after render verification. "
            + ("Final ALL_VALENC-A_REAL evidence is pending."
               if args.status == "provisional"
               else "Settled ALL_VALENC-A_REAL evidence is included.")
        ),
    }
    manifest_name = "document-manifest.provisional.json" if args.status == "provisional" else "document-manifest.json"
    (args.output_dir / manifest_name).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
