# Excel Skill

提供 .xlsx 检查、创建、单元格编辑、格式设置、CSV 转换和数据合并。
项目内安装依赖：`uv sync --extra office --group dev`。
独立脚本需要 Python 3.11+、`openpyxl>=3.1.5,<4`。

在输入文件所在的工作目录执行：

```powershell
python E:/study/project/XClaw/skills/excel/scripts/excel_ops.py inspect inputs/tasks.xlsx --range A1:D10
python E:/study/project/XClaw/skills/excel/scripts/excel_ops.py create --spec inputs/book.json --output outputs/tasks.xlsx
```

安装到其他位置时调整脚本路径。XClaw Docker 模式使用 `run_skill_script`，不使用宿主机路径。
入口见 [SKILL.md](SKILL.md)，通用参数见 [reference.md](reference.md)，公式限制见 [formulas.md](formulas.md)，
CSV 和合并规则见 [tables.md](tables.md)。可以整体复制目录，保留内部辅助脚本 `_office_io.py`。
