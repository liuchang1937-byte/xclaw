# Excel CLI 参考

通过 `run_skill_script(skill_name="excel", arguments=[...])` 调用固定入口 `scripts/excel_ops.py`。
输入输出都位于工作区；失败返回 `ok:false`，退出码非零，不写入新的工作簿。

## 查看

```text
inspect inputs/tasks.xlsx --sheet 任务 --range A1:D20 --limit 20
```

默认读取第一张 sheet 的前 20 行、最多 20 列。limit 范围 1～200。
默认返回公式字符串；`--data-only` 返回文件中的缓存值，缓存可能为空或过期。
只读模式不会保存输入文件。

## 创建

```text
create --spec inputs/book.json --output outputs/tasks.xlsx
```

```json
{
  "sheets": [{
    "name": "任务",
    "rows": [["编号", "事项", "完成率"], ["001", "接口回归", 0.75]],
    "freeze_panes": "A2",
    "column_widths": {"A": 12, "B": 30},
    "header": true,
    "autofilter": true
  }],
  "styles": [{"sheet": "任务", "range": "C2:C2", "number_format": "0.0%"}]
}
```

值支持文本、有限数值、布尔和 null。`{"date":"2026-10-07"}` / `{"datetime":"2026-10-07T09:00:00"}`
写入真实日期/时间。字符串 `=...` 默认是文本，只有 `{"formula":"=SUM(B2:B4)"}` 写公式。
日期时间不带时区，转换口径由用户任务确定。

## 修改

```text
update inputs/tasks.xlsx --spec inputs/patch.json --output outputs/tasks-v2.xlsx
```

```json
{
  "set": [
    {"sheet": "任务", "cell": "C2", "value": 1, "number_format": "0.0%"},
    {"sheet": "任务", "cell": "D2", "formula": "=IF(C2=1,\"完成\",\"进行中\")"}
  ],
  "append": [{"sheet": "任务", "rows": [["002", "文档整理", 0.5]]}],
  "styles": [{"sheet": "任务", "range": "A1:D1", "bold": true, "fill": "203864", "wrap_text": true}]
}
```

支持修改已有 sheet 的单元格、追加行、设置数字格式/粗体/填色/换行。
不提供删除行、移动列、自动重命名 sheet 等会隐式影响引用的操作；合并单元格请写左上角。
保存时保留普通单元格样式与公式文本；复杂 OOXML 部件不保证完整保真，检测到高风险部件会拒绝编辑。

## 输出限制

默认新建输出，只有明确要求覆盖时使用 `--overwrite`。
输入 JSON 最多 2 MiB；每次数据写入、导出或范围格式操作最多 200000 个单元格。
Office 包最多 32 MiB 解压体积、10000 个部件。更大任务拆分处理，避免超出容器资源预算。
公式计算与视觉渲染不由 openpyxl 完成；见 [formulas.md](formulas.md)。
