"""真实 .docx/.xlsx 文件变换与数据保留测试，不需要桌面 Office。"""

import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

docx = pytest.importorskip("docx", reason="需要 uv sync --extra office --group dev")
openpyxl = pytest.importorskip("openpyxl", reason="需要 uv sync --extra office --group dev")
SKILLS = Path(__file__).resolve().parents[1] / "skills"


def spec_file(workspace, data, name="spec.json"):
    path = workspace / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return name


def call(skill, workspace, *arguments, success=True):
    script = SKILLS / skill / "scripts" / f"{skill}_ops.py"
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(script), *arguments],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    data = json.loads(result.stdout)
    assert (result.returncode == 0) == success, result.stderr + result.stdout
    assert data["ok"] == success
    return data


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_word_create_and_inspect_real_paragraphs_and_tables(tmp_path):
    spec = spec_file(
        tmp_path,
        {
            "title": "中文报告",
            "blocks": [
                {"type": "heading", "level": 1, "text": "进展"},
                {"type": "paragraph", "text": "完成验证 😀"},
                {"type": "table", "headers": ["事项", "状态"], "rows": [["接口回归", "完成"]]},
            ],
        },
    )
    created = call("word", tmp_path, "create", "--spec", spec, "--output", "report.docx")
    document = docx.Document(tmp_path / "report.docx")
    assert document.paragraphs[0].text == "中文报告"
    assert document.paragraphs[1].style.name == "Heading 1"
    assert document.tables[0].cell(1, 0).text == "接口回归"
    assert created["layout_verified"] is False
    inspected = call("word", tmp_path, "inspect", "report.docx")
    assert any(item["text"] == "完成验证 😀" for item in inspected["paragraphs"])
    assert inspected["tables"][0]["rows"] == 2


def test_word_replaces_split_runs_without_losing_unrelated_format(tmp_path):
    document = docx.Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("Before ").italic = True
    paragraph.add_run("{{na").bold = True
    paragraph.add_run("me}}")
    paragraph.add_run(" After").underline = True
    document.save(tmp_path / "template.docx")
    original = fingerprint(tmp_path / "template.docx")
    changed = call(
        "word",
        tmp_path,
        "replace",
        "template.docx",
        "--old",
        "{{name}}",
        "--new",
        "张三",
        "--output",
        "changed.docx",
    )
    paragraph = docx.Document(tmp_path / "changed.docx").paragraphs[0]
    assert paragraph.text == "Before 张三 After"
    assert paragraph.runs[0].italic is True
    assert paragraph.runs[1].bold is True
    assert paragraph.runs[-1].underline is True
    assert changed["replacements"] == 1
    assert fingerprint(tmp_path / "template.docx") == original


def test_word_template_fills_headers_and_tables_and_rejects_missing_fields(tmp_path):
    document = docx.Document()
    document.add_paragraph("{{name}} / {{date}}")
    document.sections[0].header.paragraphs[0].text = "负责人：{{name}}"
    document.add_table(rows=1, cols=1).cell(0, 0).text = "{{name}}"
    document.save(tmp_path / "template.docx")
    values = spec_file(tmp_path, {"name": "李明"}, "values.json")
    failed = call(
        "word",
        tmp_path,
        "fill-template",
        "template.docx",
        "--values",
        values,
        "--output",
        "missing.docx",
        success=False,
    )
    assert "date" in failed["error"]
    assert not (tmp_path / "missing.docx").exists()
    spec_file(tmp_path, {"name": "李明", "date": "2026-10-07"}, "values.json")
    result = call(
        "word",
        tmp_path,
        "fill-template",
        "template.docx",
        "--values",
        values,
        "--output",
        "filled.docx",
    )
    output = docx.Document(tmp_path / "filled.docx")
    assert output.paragraphs[0].text == "李明 / 2026-10-07"
    assert output.sections[0].header.paragraphs[0].text == "负责人：李明"
    assert output.tables[0].cell(0, 0).text == "李明"
    assert result["replacements"] == 4


def test_word_append_and_explicit_overwrite(tmp_path):
    document = docx.Document()
    document.add_paragraph("原内容").runs[0].bold = True
    document.save(tmp_path / "source.docx")
    spec = spec_file(tmp_path, {"blocks": [{"type": "paragraph", "text": "新增内容"}]})
    call("word", tmp_path, "append", "source.docx", "--spec", spec, "--output", "appended.docx")
    output = docx.Document(tmp_path / "appended.docx")
    assert [item.text for item in output.paragraphs] == ["原内容", "新增内容"]
    assert output.paragraphs[0].runs[0].bold is True
    call(
        "word",
        tmp_path,
        "replace",
        "source.docx",
        "--old",
        "原内容",
        "--new",
        "新内容",
        "--output",
        "source.docx",
        success=False,
    )
    call(
        "word",
        tmp_path,
        "replace",
        "source.docx",
        "--old",
        "原内容",
        "--new",
        "新内容",
        "--output",
        "source.docx",
        "--overwrite",
    )
    assert docx.Document(tmp_path / "source.docx").paragraphs[0].text == "新内容"


def test_office_refuses_existing_output_and_workspace_escape(tmp_path):
    spec = spec_file(tmp_path, {"blocks": [{"text": "新内容"}]})
    (tmp_path / "protected.docx").write_bytes(b"original")
    call("word", tmp_path, "create", "--spec", spec, "--output", "protected.docx", success=False)
    assert (tmp_path / "protected.docx").read_bytes() == b"original"
    result = call(
        "word", tmp_path, "create", "--spec", spec, "--output", "../outside.docx", success=False
    )
    assert "超出" in result["error"]


def test_word_rejects_signed_document_edit(tmp_path):
    document = docx.Document()
    document.add_paragraph("原内容")
    source = tmp_path / "signed.docx"
    document.save(source)
    with ZipFile(source, "a") as archive:
        archive.writestr("_xmlsignatures/sig1.xml", "<Signature/>")
    original = fingerprint(source)
    result = call(
        "word",
        tmp_path,
        "replace",
        "signed.docx",
        "--old",
        "原内容",
        "--new",
        "新内容",
        "--output",
        "changed.docx",
        success=False,
    )
    assert "复杂 Office" in result["error"]
    assert fingerprint(source) == original


def test_excel_create_preserves_types_formulas_and_formats(tmp_path):
    spec = spec_file(
        tmp_path,
        {
            "sheets": [
                {
                    "name": "任务",
                    "rows": [
                        ["编号", "数量", "日期", "公式", "文本"],
                        ["001", 3, {"date": "2026-10-07"}, {"formula": "=B2*2"}, "=literal"],
                    ],
                    "column_widths": {"A": 15},
                }
            ],
            "styles": [{"sheet": "任务", "range": "B2:B2", "number_format": "0.00"}],
        },
    )
    result = call("excel", tmp_path, "create", "--spec", spec, "--output", "tasks.xlsx")
    book = openpyxl.load_workbook(tmp_path / "tasks.xlsx")
    ws = book["任务"]
    assert ws["A2"].value == "001" and ws["A2"].data_type == "s"
    assert ws["B2"].value == 3 and ws["B2"].number_format == "0.00"
    assert ws["C2"].value.date().isoformat() == "2026-10-07"
    assert ws["D2"].data_type == "f" and ws["D2"].value == "=B2*2"
    assert ws["E2"].data_type == "s" and ws["E2"].value == "=literal"
    assert ws.freeze_panes == "A2"
    assert ws.column_dimensions["A"].width == 15
    assert result["formulas_calculated"] is False
    book.close()
    preview = call(
        "excel", tmp_path, "inspect", "tasks.xlsx", "--sheet", "任务", "--range", "A1:E2"
    )
    assert preview["rows"][1][0] == "001"
    assert preview["rows"][1][3] == "=B2*2"
    values = call("excel", tmp_path, "inspect", "tasks.xlsx", "--data-only")
    assert values["rows"][1][3] is None


def test_excel_update_keeps_other_sheets_and_original_file(tmp_path):
    book = openpyxl.Workbook()
    book.active.title = "任务"
    book.active.append(["ID", "状态"])
    book.active.append(["001", "进行中"])
    other = book.create_sheet("保留")
    other["A1"] = "不要改动"
    other["A1"].font = openpyxl.styles.Font(bold=True)
    book.save(tmp_path / "source.xlsx")
    original = fingerprint(tmp_path / "source.xlsx")
    spec = spec_file(
        tmp_path,
        {
            "set": [{"sheet": "任务", "cell": "B2", "value": "完成"}],
            "append": [{"sheet": "任务", "rows": [["002", "进行中"]]}],
        },
    )
    call("excel", tmp_path, "update", "source.xlsx", "--spec", spec, "--output", "updated.xlsx")
    output = openpyxl.load_workbook(tmp_path / "updated.xlsx")
    assert output["任务"]["B2"].value == "完成"
    assert output["任务"]["A3"].value == "002"
    assert output["保留"]["A1"].value == "不要改动"
    assert output["保留"]["A1"].font.bold
    assert fingerprint(tmp_path / "source.xlsx") == original
    output.close()


def test_csv_import_and_export_preserve_identifiers_and_escape_formulas(tmp_path):
    (tmp_path / "input.csv").write_text(
        "编号,数量,日期,备注\n001,3,2026-10-07,=DANGEROUS()\n", encoding="utf-8"
    )
    types = spec_file(tmp_path, {"数量": "integer", "日期": "date"}, "types.json")
    call("excel", tmp_path, "import-csv", "input.csv", "--types", types, "--output", "book.xlsx")
    book = openpyxl.load_workbook(tmp_path / "book.xlsx")
    ws = book.active
    assert ws["A2"].value == "001"
    assert ws["B2"].value == 3
    assert ws["D2"].value == "=DANGEROUS()" and ws["D2"].data_type == "s"
    book.close()
    call("excel", tmp_path, "export-csv", "book.xlsx", "--output", "export.csv")
    with (tmp_path / "export.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    assert rows[1][0] == "001"
    assert rows[1][3] == "'=DANGEROUS()"


def create_book(path, rows, name="任务"):
    book = openpyxl.Workbook()
    book.active.title = name
    for row in rows:
        book.active.append(row)
    book.save(path)
    book.close()


def test_excel_merge_deduplicates_equal_rows_and_rejects_conflicts(tmp_path):
    create_book(tmp_path / "a.xlsx", [["ID", "状态"], ["001", "完成"]])
    create_book(tmp_path / "b.xlsx", [["ID", "状态"], ["001", "完成"], ["002", "进行中"]])
    result = call(
        "excel", tmp_path, "merge", "a.xlsx", "b.xlsx", "--key", "ID", "--output", "merged.xlsx"
    )
    assert result["data_rows"] == 2 and result["duplicates_removed"] == 1
    create_book(tmp_path / "b.xlsx", [["ID", "状态"], ["001", "失败"]])
    result = call(
        "excel",
        tmp_path,
        "merge",
        "a.xlsx",
        "b.xlsx",
        "--key",
        "ID",
        "--output",
        "conflict.xlsx",
        success=False,
    )
    assert "冲突" in result["error"]
    assert not (tmp_path / "conflict.xlsx").exists()


@pytest.mark.parametrize(
    "rows,error",
    [
        ([["不同表头"], ["数据"]], "表头"),
        ([["ID", "状态"], ["001", "=A2"]], "公式"),
    ],
)
def test_excel_merge_refuses_incompatible_data(tmp_path, rows, error):
    create_book(tmp_path / "a.xlsx", [["ID", "状态"], ["001", "完成"]])
    create_book(tmp_path / "b.xlsx", rows)
    result = call(
        "excel", tmp_path, "merge", "a.xlsx", "b.xlsx", "--output", "bad.xlsx", success=False
    )
    assert error in result["error"]


def test_excel_invalid_range_does_not_modify_original(tmp_path):
    create_book(tmp_path / "source.xlsx", [["ID"], ["001"]])
    original = fingerprint(tmp_path / "source.xlsx")
    spec = spec_file(tmp_path, {"styles": [{"range": "A1:Z1000000", "bold": True}]})
    call(
        "excel",
        tmp_path,
        "update",
        "source.xlsx",
        "--spec",
        spec,
        "--output",
        "bad.xlsx",
        success=False,
    )
    assert fingerprint(tmp_path / "source.xlsx") == original
    assert not (tmp_path / "bad.xlsx").exists()


def test_excel_literal_error_text_is_not_converted_into_error_cell(tmp_path):
    spec = spec_file(tmp_path, {"sheets": [{"name": "Text", "rows": [["Text"], ["#REF!"]]}]})
    result = call("excel", tmp_path, "create", "--spec", spec, "--output", "text.xlsx")
    book = openpyxl.load_workbook(tmp_path / "text.xlsx")
    assert book.active["A2"].data_type == "s"
    assert book.active["A2"].value == "#REF!"
    assert result["needs_excel_recalculation"] is False
    book.close()
