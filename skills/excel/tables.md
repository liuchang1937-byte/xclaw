# CSV 与数据合并

## 导入 CSV

```text
import-csv inputs/tasks.csv --sheet 任务 --types inputs/types.json --output outputs/tasks.xlsx
```

CSV 要有不重复、非空的表头。默认 UTF-8/UTF-8 BOM 编码、逗号分隔，可用 `--delimiter` 指定单字符分隔符。
所有列默认文本，保留编号前导零，`=...` 不会自动成为公式。
需要数值或日期时指定类型 JSON：

```json
{"数量":"integer","完成率":"number","日期":"date","编号":"text"}
```

支持 text/integer/number/date，数值列中的空值写为 null。日期为 ISO `YYYY-MM-DD`。
不自动猜测百分数、货币、编号或日期，也不自动改变单位。

## 导出 CSV

```text
export-csv inputs/tasks.xlsx --sheet 任务 --output outputs/tasks.csv
```

输出 UTF-8 BOM。公式以字面文本导出；以 `= + - @` 开头的文本添加单引号，避免 CSV 被打开时执行公式。
真实数值保留数值语义。导出不代表已经得到公式计算结果。

## 合并工作簿

```text
merge inputs/week1.xlsx inputs/week2.xlsx --sheet 任务 --key 编号 --output outputs/merged.xlsx
```

每个文件选择同名 sheet；不传 `--sheet` 时分别选择第一张。
第一行表头必须完全一致，包含列顺序。不自动推测或重命名不同字段。
指定业务键时：完全相同的重复行会去重；相同键但不同内容返回冲突错误，原文件不修改。
去重键不能为空。未指定 key 时保留全部数据行。
任何数据行含公式或 Excel 错误值都会拒绝合并，需先提供经确认的值表。
返回 data_rows 和 duplicates_removed，交付时核对行数与源文件范围。
