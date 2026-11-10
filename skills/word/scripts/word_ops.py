"""Word 文件 CLI：检查、创建、追加、跨 run 替换与模板填充。"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _office_io import atomic_save, check_package, emit, read_json, run, safe_path  # noqa: E402


def paragraphs(document):
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    seen = set()

    def walk(container):
        for item in container.iter_inner_content():
            if isinstance(item, Paragraph):
                if item._p not in seen:
                    seen.add(item._p)
                    yield item
            elif isinstance(item, Table):
                for row in item.rows:
                    for cell in row.cells:
                        yield from walk(cell)

    yield from walk(document)
    for section in document.sections:
        for part in (
            section.header,
            section.first_page_header,
            section.even_page_header,
            section.footer,
            section.first_page_footer,
            section.even_page_footer,
        ):
            if not part.is_linked_to_previous:
                yield from walk(part)


def load_document(value, *, editing=False):
    from docx import Document

    path = safe_path(value, suffixes={".docx"}, exists=True)
    warnings = check_package(path, editing=editing)
    document = Document(path)
    if editing and document.element.xpath(".//w:ins|.//w:del"):
        raise ValueError("文档存在修订记录，请先在 Word 中处理修订后再使用此脚本编辑")
    return document, path, warnings


def replace_paragraph(paragraph, old, new):
    from docx.text.run import Run

    if not old:
        raise ValueError("替换目标不能为空")
    runs = [Run(element, paragraph) for element in paragraph._p.xpath("./w:r|./w:hyperlink/w:r")]
    text = "".join(item.text for item in runs)
    matches = list(re.finditer(re.escape(old), text))
    if not matches:
        return 0
    if paragraph._p.xpath(".//w:fldChar|.//w:instrText"):
        raise ValueError("目标段落包含 Word 域，不支持安全替换，请使用普通文本模板")
    allowed = {"rPr", "t", "tab", "br", "cr"}
    for match in reversed(matches):
        offsets = []
        offset = 0
        for item in runs:
            offsets.append(offset)
            offset += len(item.text)
        first = next(
            i
            for i, item in enumerate(runs)
            if offsets[i] <= match.start() < offsets[i] + len(item.text)
        )
        last = next(
            i
            for i, item in enumerate(runs)
            if offsets[i] < match.end() <= offsets[i] + len(item.text)
        )
        for item in runs[first : last + 1]:
            if any(child.tag.rsplit("}", 1)[-1] not in allowed for child in item._r):
                raise ValueError("替换范围包含图片或复杂 run 部件，未写入输出")
        start, end = match.start() - offsets[first], match.end() - offsets[last]
        if first == last:
            item = runs[first]
            item.text = item.text[:start] + new + item.text[end:]
        else:
            prefix, suffix = runs[first].text[:start], runs[last].text[end:]
            runs[first].text = prefix + new
            for item in runs[first + 1 : last]:
                item.text = ""
            runs[last].text = suffix
    return len(matches)


def add_blocks(document, blocks):
    if not isinstance(blocks, list) or len(blocks) > 1000:
        raise ValueError("blocks 必须是最多 1000 项的数组")
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("每个 block 必须是对象")
        kind = block.get("type", "paragraph")
        if kind in {"heading", "paragraph", "bullet", "numbered"}:
            text = block.get("text", "")
            if not isinstance(text, str):
                raise ValueError("block.text 必须为字符串")
            if kind == "heading":
                level = block.get("level", 1)
                if not isinstance(level, int) or not 1 <= level <= 6:
                    raise ValueError("标题 level 必须在 1～6 之间")
                document.add_heading(text, level)
            else:
                style = {"bullet": "List Bullet", "numbered": "List Number"}.get(kind)
                document.add_paragraph(text, style)
        elif kind == "table":
            headers, rows = block.get("headers"), block.get("rows", [])
            if not isinstance(headers, list) or not headers or len(headers) > 20:
                raise ValueError("table.headers 必须是 1～20 列的数组")
            if not isinstance(rows, list) or len(rows) > 5000:
                raise ValueError("table.rows 必须是最多 5000 行的数组")
            table = document.add_table(rows=1, cols=len(headers))
            table.style = "Table Grid"
            for cell, value in zip(table.rows[0].cells, headers, strict=True):
                cell.text = str(value)
                for item in cell.paragraphs:
                    for item_run in item.runs:
                        item_run.bold = True
            for row in rows:
                if not isinstance(row, list) or len(row) != len(headers):
                    raise ValueError("表格行的列数必须与 headers 一致")
                for cell, value in zip(table.add_row().cells, row, strict=True):
                    cell.text = "" if value is None else str(value)
        elif kind == "page_break":
            document.add_page_break()
        else:
            raise ValueError(f"未知 block.type: {kind}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("input")
    inspect.add_argument("--limit", type=int, default=30)
    for action in ("create", "append", "replace", "fill-template"):
        command = commands.add_parser(action)
        if action != "create":
            command.add_argument("input")
        if action in {"create", "append"}:
            command.add_argument("--spec", required=True)
        elif action == "replace":
            command.add_argument("--old", required=True)
            command.add_argument("--new", required=True)
        else:
            command.add_argument("--values", required=True)
        command.add_argument("--output", required=True)
        command.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.action == "inspect":
        if not 1 <= args.limit <= 200:
            raise ValueError("limit 必须在 1～200 之间")
        document, source, warnings = load_document(args.input)
        items = [item for item in paragraphs(document) if item.text.strip()]
        placeholders = sorted(
            {key for item in items for key in re.findall(r"\{\{([^{}\n]+)\}\}", item.text)}
        )
        emit(
            {
                "ok": True,
                "file": args.input,
                "paragraph_count": len(items),
                "paragraphs": [
                    {"text": item.text[:1000], "style": item.style.name}
                    for item in items[: args.limit]
                ],
                "tables": [
                    {
                        "rows": len(table.rows),
                        "columns": len(table.columns),
                        "preview": [
                            [cell.text[:300] for cell in row.cells] for row in table.rows[:5]
                        ],
                    }
                    for table in document.tables[:10]
                ],
                "placeholders": placeholders,
                "truncated": len(items) > args.limit,
                "warnings": warnings,
                "layout_verified": False,
            }
        )
        return
    sources = []
    if args.action == "create":
        from docx import Document
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Cm, Pt

        document = Document()
        normal = document.styles["Normal"]
        normal.font.name, normal.font.size = "Calibri", Pt(11)
        fonts = normal.element.get_or_add_rPr().find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            normal.element.rPr.append(fonts)
        fonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        for section in document.sections:
            section.top_margin = section.bottom_margin = Cm(2)
            section.left_margin = section.right_margin = Cm(2.3)
    else:
        document, source, _ = load_document(args.input, editing=True)
        sources.append(source)
    result = {"ok": True, "action": args.action, "layout_verified": False}
    if args.action in {"create", "append"}:
        spec = read_json(args.spec)
        if not isinstance(spec, dict):
            raise ValueError("spec 必须是对象")
        if args.action == "create" and spec.get("title"):
            document.add_heading(str(spec["title"]), 0)
        blocks = spec.get("blocks", [])
        add_blocks(document, blocks)
        result["blocks_added"] = len(blocks)
    elif args.action == "replace":
        count = sum(replace_paragraph(item, args.old, args.new) for item in paragraphs(document))
        if not count:
            raise ValueError("未找到要替换的文字，未创建输出")
        result["replacements"] = count
    else:
        values = read_json(args.values)
        if not isinstance(values, dict) or not values:
            raise ValueError("values 必须是非空字段对象")
        count = 0
        for key, value in values.items():
            for item in paragraphs(document):
                count += replace_paragraph(item, "{{" + key + "}}", str(value))
        missing = sorted(
            {
                key
                for item in paragraphs(document)
                for key in re.findall(r"\{\{([^{}\n]+)\}\}", item.text)
            }
        )
        if missing:
            raise ValueError("仍有未填写的模板字段，未创建输出: " + ", ".join(missing))
        if not count:
            raise ValueError("模板没有匹配的占位符")
        result["replacements"] = count
    output = safe_path(args.output, suffixes={".docx"})
    result["output"] = atomic_save(output, document.save, overwrite=args.overwrite, sources=sources)
    result["paragraph_count"] = len(list(paragraphs(document)))
    emit(result)


if __name__ == "__main__":
    run(main)
