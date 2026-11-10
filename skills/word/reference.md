# Word CLI 参考

通过 `run_skill_script(skill_name="word", arguments=[...])` 调用。脚本入口是 `scripts/word_ops.py`。
操作 JSON 和输入文件放在工作区内。返回一条 JSON；失败时 `ok:false` 且退出码非零。

## 查看

```text
inspect inputs/report.docx --limit 30
```

返回正文、表格与已有页眉页脚段落的有限预览、样式、表格尺寸和占位符。
`limit` 为 1～200；预览不代表完整文本，`truncated:true` 时缩小范围或调整 limit。
不会计算页数、更新目录或渲染排版。

## 创建与追加

```text
create --spec inputs/doc.json --output outputs/report.docx
append inputs/report.docx --spec inputs/additions.json --output outputs/report-v2.docx
```

JSON 格式：

```json
{
  "title": "项目周报",
  "blocks": [
    {"type": "heading", "level": 1, "text": "本周进展"},
    {"type": "paragraph", "text": "完成接口回归验证。"},
    {"type": "bullet", "text": "下周处理剩余问题。"},
    {"type": "table", "headers": ["事项", "状态"], "rows": [["回归测试", "完成"]]},
    {"type": "page_break"}
  ]
}
```

`title` 仅用于 create。block 支持 heading、paragraph、bullet、numbered、table、page_break。
标题级别为 1～6；表格行列必须匹配。最多 1000 个 block，每个表格最多 20 列、5000 行。
新文件使用标准正文、标题和 Table Grid 样式；append 保留原有文档样式。

## 文字替换

```text
replace inputs/report.docx --old 旧名称 --new 新名称 --output outputs/report-v2.docx
```

替换所有匹配的普通文本，包括表格和已有页眉页脚；匹配可以跨越多个 run。
保留未涉及的 run 格式，新文字继承匹配起点的格式。包含域或图片等复杂部件的替换范围会被拒绝。
未找到目标不会生成“成功但没改动”的输出。

## 输出与范围

默认不覆盖任何已有输出；用户明确要求覆盖时附加 `--overwrite`，也可显式覆盖输入文件。
脚本先保存并验证临时 Office 文件，再发布输出；失败时不会用半成品覆盖原文件。
路径限制在脚本当前工作目录，拒绝越界链接及 .git/.ssh/.aws/.codex 等目录。
输入 JSON 限 2 MiB，Office 压缩包解压体积限 32 MiB、最多 10000 个部件，防止超出运行资源预算。
返回的 `layout_verified:false` 是实际限制；视觉验收需要另行打开或渲染文档。
