"""Excel 文件 CLI：检查、创建、编辑、CSV 转换和按表头合并。"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from copy import copy
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _office_io import (  # noqa: E402
    MAX_OUTPUT_CELLS,
    atomic_save,
    check_package,
    emit,
    read_json,
    run,
    safe_path,
)


def workbook(value, *, editing=False, data_only=False):
    from openpyxl import load_workbook

    source = safe_path(value, suffixes={".xlsx"}, exists=True)
    warnings = check_package(source, excel=True, editing=editing)
    book = load_workbook(source, data_only=data_only, read_only=not editing, keep_links=True)
    return book, source, warnings


def sheet(book, name=None):
    if name is None:
        return book.worksheets[0]
    if name not in book.sheetnames:
        raise ValueError(f"工作表不存在: {name}")
    return book[name]


def valid_name(name):
    if not isinstance(name, str) or not name or len(name) > 31 or re.search(r"[\\/*?:\[\]]", name):
        raise ValueError("工作表名称必须为 1～31 字符，不能包含 Excel 保留字符")
    return name


def bounds(area):
    from openpyxl.utils.cell import range_boundaries

    result = range_boundaries(area)
    left, top, right, bottom = result
    if any(value is None for value in result) or not (
        1 <= left <= right <= 16384 and 1 <= top <= bottom <= 1048576
    ):
        raise ValueError("需要明确的 Excel 单元格范围，如 A1:D20")
    if (right - left + 1) * (bottom - top + 1) > MAX_OUTPUT_CELLS:
        raise ValueError("操作范围超过 200000 个单元格，请分批处理")
    return result


def assign(cell, value):
    if isinstance(value, dict):
        if set(value) == {"date"}:
            cell.value = date.fromisoformat(value["date"])
            cell.number_format = "yyyy-mm-dd"
        elif set(value) == {"datetime"}:
            parsed = datetime.fromisoformat(value["datetime"])
            if parsed.tzinfo is not None:
                raise ValueError("Excel datetime 不支持时区，请先明确转换为本地时间")
            cell.value = parsed
            cell.number_format = "yyyy-mm-dd hh:mm"
        elif "formula" in value and set(value) <= {"formula", "number_format"}:
            formula = value["formula"]
            if not isinstance(formula, str) or not formula.startswith("=") or len(formula) < 2:
                raise ValueError("formula 必须是以 '=' 开头的公式字符串")
            cell.value = formula
            if "number_format" in value:
                cell.number_format = value["number_format"]
        else:
            raise ValueError("对象型单元格仅支持 date、datetime 或 formula")
    elif value is None or isinstance(value, (str, bool, int, float, date, datetime)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Excel 数值必须是有限数")
        cell.value = value
        # 用户数据以 '=' 开头也按文本保存，只有显式 formula 字段产生公式。
        if isinstance(value, str):
            cell.data_type = "s"
    else:
        raise ValueError(f"不支持单元格值类型: {type(value).__name__}")


def put_rows(ws, rows, *, start=1):
    if not isinstance(rows, list) or any(not isinstance(row, list) for row in rows):
        raise ValueError("rows 必须是二维数组")
    if sum(len(row) for row in rows) > MAX_OUTPUT_CELLS:
        raise ValueError("写入超过 200000 个单元格，请分批处理")
    if start + len(rows) - 1 > 1048576 or any(len(row) > 16384 for row in rows):
        raise ValueError("数据超出 Excel 行列上限")
    for row_index, row in enumerate(rows, start):
        for column, value in enumerate(row, 1):
            assign(ws.cell(row_index, column), value)


def format_sheet(ws, spec):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils.cell import column_index_from_string, get_column_letter

    if spec.get("header", True) and ws.max_row:
        for cell in ws[1]:
            cell.font = Font(name="Calibri", bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="203864")
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.row_dimensions[1].height = 28
    ws.freeze_panes = spec.get("freeze_panes", "A2" if spec.get("header", True) else None)
    if ws.freeze_panes:
        bounds(ws.freeze_panes)
    if spec.get("autofilter", True) and ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    widths = spec.get("column_widths", {})
    if not isinstance(widths, dict):
        raise ValueError("column_widths 必须是列字母到宽度的对象")
    for column in range(1, min(ws.max_column, 100) + 1):
        letter = get_column_letter(column)
        longest = max(
            (
                len(str(ws.cell(row, column).value or ""))
                for row in range(1, min(ws.max_row, 50) + 1)
            ),
            default=0,
        )
        ws.column_dimensions[letter].width = min(45, max(12, longest * 1.5 + 3))
    for letter, width in widths.items():
        if not re.fullmatch(r"[A-Za-z]{1,3}", letter) or column_index_from_string(letter) > 16384:
            raise ValueError("column_widths 键必须是合法列字母")
        if not isinstance(width, (int, float)) or not 1 <= width <= 100:
            raise ValueError("列宽必须在 1～100 之间")
        ws.column_dimensions[letter.upper()].width = width


def apply_styles(book, styles):
    from openpyxl.styles import PatternFill

    if not isinstance(styles, list):
        raise ValueError("styles 必须是数组")
    for spec in styles:
        ws = sheet(book, spec.get("sheet"))
        left, top, right, bottom = bounds(spec["range"])
        for row in ws.iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right):
            for cell in row:
                if "number_format" in spec:
                    cell.number_format = spec["number_format"]
                if "bold" in spec:
                    font = copy(cell.font)
                    font.bold = bool(spec["bold"])
                    cell.font = font
                if "fill" in spec:
                    color = str(spec["fill"]).lstrip("#")
                    if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
                        raise ValueError("fill 必须是 6 位十六进制 RGB 色值")
                    cell.fill = PatternFill("solid", fgColor=color)
                if "wrap_text" in spec:
                    alignment = copy(cell.alignment)
                    alignment.wrap_text = bool(spec["wrap_text"])
                    cell.alignment = alignment


def save_book(book, args, *, sources=(), details=None):
    from openpyxl.workbook.properties import CalcProperties

    formula_count = sum(
        cell.data_type == "f" for ws in book.worksheets for cell in ws._cells.values()
    )
    if formula_count:
        book.calculation = CalcProperties(calcMode="auto", fullCalcOnLoad=True, forceFullCalc=True)
    output = safe_path(args.output, suffixes={".xlsx"})
    saved = atomic_save(output, book.save, overwrite=args.overwrite, sources=sources)
    emit(
        {
            "ok": True,
            "action": args.action,
            "output": saved,
            "sheets": book.sheetnames,
            "formulas_calculated": False,
            "needs_excel_recalculation": bool(formula_count),
            "formula_count": formula_count,
            **(details or {}),
        }
    )


def main():
    from openpyxl import Workbook
    from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("input")
    inspect.add_argument("--sheet")
    inspect.add_argument("--range", dest="area")
    inspect.add_argument("--limit", type=int, default=20)
    inspect.add_argument("--data-only", action="store_true")
    for action in ("create", "update", "import-csv", "export-csv", "merge"):
        command = commands.add_parser(action)
        if action in {"update", "export-csv", "import-csv"}:
            command.add_argument("input")
        elif action == "merge":
            command.add_argument("inputs", nargs="+")
        if action in {"create", "update"}:
            command.add_argument("--spec", required=True)
        if action in {"merge", "export-csv", "import-csv"}:
            command.add_argument("--sheet")
        if action in {"import-csv", "export-csv"}:
            command.add_argument("--delimiter", default=",")
        if action == "import-csv":
            command.add_argument("--types")
        if action == "merge":
            command.add_argument("--key")
        command.add_argument("--output", required=True)
        command.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.action == "inspect":
        if not 1 <= args.limit <= 200:
            raise ValueError("limit 必须在 1～200 之间")
        book, source, warnings = workbook(args.input, data_only=args.data_only)
        try:
            ws = sheet(book, args.sheet)
            left, top, right, bottom = bounds(
                args.area
                or f"A1:{get_column_letter(min(ws.max_column, 20))}{min(ws.max_row, args.limit)}"
            )
            bottom = min(bottom, top + args.limit - 1)
            values = [
                [cell.value for cell in row]
                for row in ws.iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right)
            ]
            emit(
                {
                    "ok": True,
                    "file": args.input,
                    "sheets": book.sheetnames,
                    "sheet": ws.title,
                    "max_row": ws.max_row,
                    "max_column": ws.max_column,
                    "range": f"{get_column_letter(left)}{top}:{get_column_letter(right)}{bottom}",
                    "rows": values,
                    "data_only": args.data_only,
                    "cached_values_may_be_missing": args.data_only,
                    "warnings": warnings,
                }
            )
        finally:
            book.close()
        return
    sources = []
    if args.action == "create":
        spec = read_json(args.spec)
        if (
            not isinstance(spec, dict)
            or not isinstance(spec.get("sheets"), list)
            or not spec["sheets"]
        ):
            raise ValueError("spec 必须包含非空 sheets 数组")
        book = Workbook()
        book.remove(book.active)
        for item in spec["sheets"]:
            name = valid_name(item["name"])
            if name.casefold() in {existing.casefold() for existing in book.sheetnames}:
                raise ValueError("工作表名称重复")
            ws = book.create_sheet(name)
            put_rows(ws, item.get("rows", []))
            format_sheet(ws, item)
        apply_styles(book, spec.get("styles", []))
    elif args.action == "update":
        book, source, _ = workbook(args.input, editing=True)
        sources.append(source)
        spec = read_json(args.spec)
        if not isinstance(spec, dict):
            raise ValueError("spec 必须是对象")
        for item in spec.get("set", []):
            coordinate = item["cell"]
            if not re.fullmatch(r"[A-Za-z]{1,3}[1-9][0-9]*", coordinate):
                raise ValueError("set.cell 必须是单个单元格坐标")
            bounds(coordinate)
            ws = sheet(book, item.get("sheet"))
            row, column = coordinate_to_tuple(coordinate)
            value = {"formula": item["formula"]} if "formula" in item else item.get("value")
            assign(ws.cell(row, column), value)
            if "number_format" in item:
                ws.cell(row, column).number_format = item["number_format"]
        for item in spec.get("append", []):
            ws = sheet(book, item.get("sheet"))
            put_rows(ws, item["rows"], start=ws.max_row + 1)
        apply_styles(book, spec.get("styles", []))
    elif args.action == "import-csv":
        source = safe_path(args.input, suffixes={".csv"}, exists=True)
        sources.append(source)
        if len(args.delimiter) != 1:
            raise ValueError("delimiter 必须是单个字符")
        with source.open(encoding="utf-8-sig", newline="") as stream:
            rows = []
            cell_count = 0
            for row in csv.reader(stream, delimiter=args.delimiter):
                rows.append(row)
                cell_count += len(row)
                if cell_count > MAX_OUTPUT_CELLS:
                    raise ValueError("CSV 超过 200000 个单元格，请分批处理")
        if not rows:
            raise ValueError("CSV 为空")
        if any(not name.strip() for name in rows[0]) or len(set(rows[0])) != len(rows[0]):
            raise ValueError("CSV 表头必须非空且不重复")
        types = read_json(args.types) if args.types else {}
        if not isinstance(types, dict) or set(types) - set(rows[0]):
            raise ValueError("types 必须是 CSV 列名到类型的对象")
        for row in rows[1:]:
            if len(row) != len(rows[0]):
                raise ValueError("CSV 行的列数不一致")
            for index, name in enumerate(rows[0]):
                kind = types.get(name, "text")
                value = row[index]
                if kind == "text":
                    continue
                if not value:
                    row[index] = None
                elif kind == "integer":
                    row[index] = int(value)
                elif kind == "number":
                    row[index] = float(value)
                elif kind == "date":
                    row[index] = {"date": value}
                else:
                    raise ValueError(f"未知 CSV 列类型: {kind}")
        book = Workbook()
        book.active.title = valid_name(args.sheet or "数据")
        put_rows(book.active, rows)
        format_sheet(book.active, {})
    elif args.action == "export-csv":
        if len(args.delimiter) != 1:
            raise ValueError("delimiter 必须是单个字符")
        book, source, _ = workbook(args.input)
        try:
            ws = sheet(book, args.sheet)
            if ws.max_row * ws.max_column > MAX_OUTPUT_CELLS:
                raise ValueError("导出范围超过 200000 个单元格")
            target = safe_path(args.output, suffixes={".csv"})

            def writer(path):
                with path.open("w", encoding="utf-8-sig", newline="") as stream:
                    output = csv.writer(stream, delimiter=args.delimiter)
                    for row in ws.iter_rows(values_only=True):
                        output.writerow(
                            [
                                "'" + value
                                if isinstance(value, str) and value.startswith(("=", "+", "-", "@"))
                                else value
                                for value in row
                            ]
                        )

            saved = atomic_save(target, writer, overwrite=args.overwrite, sources=(source,))
            emit(
                {
                    "ok": True,
                    "output": saved,
                    "formula_export": "literal_text",
                    "csv_formula_prefix_escaped": True,
                }
            )
        finally:
            book.close()
        return
    else:
        rows, headers, seen, duplicates = [], None, {}, 0
        for value in args.inputs:
            current, source, _ = workbook(value)
            sources.append(source)
            try:
                ws = sheet(current, args.sheet)
                if ws.max_row * ws.max_column > MAX_OUTPUT_CELLS:
                    raise ValueError("合并输入超过单元格上限")
                iterator = ws.iter_rows()
                first = next(iterator, ())
                columns = tuple(cell.value for cell in first)
                if not columns or any(
                    not isinstance(item, str) or not item.strip() for item in columns
                ):
                    raise ValueError("合并输入必须有非空文本表头")
                if len(set(columns)) != len(columns):
                    raise ValueError("合并输入表头重复")
                if headers is None:
                    headers = columns
                    rows.append(list(columns))
                elif columns != headers:
                    raise ValueError("合并输入的表头或列顺序不一致，请先明确字段映射")
                key_index = headers.index(args.key) if args.key else None
                for cells in iterator:
                    if any(cell.data_type == "e" for cell in cells):
                        raise ValueError("合并数据包含 Excel 错误值，请先处理源表错误")
                    if any(cell.data_type == "f" for cell in cells):
                        raise ValueError("合并数据含公式，不能跨工作簿直接复制公式；请先提供值表")
                    values = [cell.value for cell in cells]
                    if all(value is None for value in values):
                        continue
                    if key_index is not None:
                        key_value = values[key_index]
                        if key_value is None or key_value == "":
                            raise ValueError("去重键为空")
                        key = (type(key_value).__name__, key_value)
                        if key in seen:
                            if seen[key] != values:
                                raise ValueError(f"相同业务键存在冲突数据: {key_value}")
                            duplicates += 1
                            continue
                        seen[key] = values
                    rows.append(values)
                    if len(rows) * len(headers) > MAX_OUTPUT_CELLS:
                        raise ValueError("合并结果超过 200000 个单元格")
            finally:
                current.close()
        book = Workbook()
        book.active.title = "合并数据"
        put_rows(book.active, rows)
        format_sheet(book.active, {})
    try:
        details = (
            {"duplicates_removed": duplicates, "data_rows": len(rows) - 1}
            if args.action == "merge"
            else None
        )
        save_book(book, args, sources=sources, details=details)
    finally:
        book.close()


if __name__ == "__main__":
    run(main)
