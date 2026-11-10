# Word Skill

提供 .docx 文件读取、创建、追加、文字替换和模板填充。
包内脚本可以独立使用，也可以由 XClaw 的 `word` Skill 激活后通过 `run_skill_script` 调用。

项目内安装依赖：`uv sync --extra office --group dev`。
独立使用需要 Python 3.11+ 与 `python-docx>=1.2.0,<2`。

在包含输入文件的工作目录执行，保持输入输出都在该目录内：

```powershell
python E:/study/project/XClaw/skills/word/scripts/word_ops.py inspect inputs/report.docx
python E:/study/project/XClaw/skills/word/scripts/word_ops.py create --spec inputs/doc.json --output outputs/report.docx
```

安装到其他位置时调整脚本路径。XClaw 的 Docker 模式不使用这些宿主机路径，使用 `run_skill_script`。
指令入口见 [SKILL.md](SKILL.md)，参数见 [reference.md](reference.md)，模板规则见 [forms.md](forms.md)。
技能目录可以整体复制；`scripts/_office_io.py` 是脚本所需的内部辅助模块。
