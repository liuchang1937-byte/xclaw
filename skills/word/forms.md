# Word 模板填写

模板使用普通文本占位符，例如 `{{name}}`、`{{date}}`、`{{summary}}`。
占位符可以位于正文、表格、已有页眉页脚，并可以被 Word 分成多个 run。

1. 运行 `inspect` 获取 `placeholders`。
2. 从用户提供的信息准备字段 JSON，不编造未知字段。
3. 运行以下操作：

```text
fill-template inputs/template.docx --values inputs/values.json --output outputs/filled.docx
```

字段 JSON 示例：

```json
{"name":"张三","date":"2026-10-07","summary":"完成本周任务。"}
```

字段名大小写敏感，与 `{{...}}` 中的内容完全一致。不支持嵌套模板表达式、条件或循环。
字段值按普通文本写入，不解释代码。为避免产生新的未填字段，值中不要包含 `{{...}}`。
任何占位符仍未填写时，脚本返回错误且不生成输出；先补齐真实信息或按用户要求修改模板。
不支持内容控件、Word 域或修订记录的编辑；先使用普通文本模板。
