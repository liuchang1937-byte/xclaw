# Word / Excel 技能包

两个技能按目录组织，包含 SKILL.md、预置 Python 脚本、按需参考资料、README 和 JSON 示例。
操作的是 `.docx` / `.xlsx` 文件，不连接或操纵已经打开的桌面 Office 窗口。

```text
skills/
├── word/
│   ├── SKILL.md
│   ├── README.md
│   ├── reference.md
│   ├── forms.md
│   ├── examples/create.json
│   └── scripts/word_ops.py, _office_io.py
└── excel/
    ├── SKILL.md
    ├── README.md
    ├── reference.md
    ├── formulas.md
    ├── tables.md
    ├── examples/create.json, update.json
    └── scripts/excel_ops.py, _office_io.py
```

Skill 运行时拆分在 `src/xclaw/core/skill/`，包含 `__init__.py`、`skill_loader.py` 和 `executor.py`。
旧的 `from xclaw.core.skill import Skill, SkillLoader, register_skills_as_tools` 导入保持兼容。

## 能力

| Word | Excel |
| --- | --- |
| 段落、样式、表格、页眉页脚的有限预览 | 工作表和指定单元格范围预览 |
| 根据 JSON 创建标题、段落、列表和表格 | 根据 JSON 创建多工作表、值、日期和格式 |
| 追加内容、跨 run 替换普通文字 | 修改单元格、追加数据行、设置格式 |
| 普通文本 `{{字段}}` 模板填充 | 保存显式公式、CSV 转换、按表头合并与去重 |

默认保存为新文件。输入与输出路径不能超出工作区，无法用 Skill 指令提升权限。
对不支持保真的 Office 部件会拒绝编辑。公式文本可以保存，但 openpyxl 不计算公式；
需要重新计算时返回 `needs_excel_recalculation:true`。脚本验证文件结构，不宣称视觉排版已验证。

## 运行准备

开发环境已支持 office 可选依赖；重新安装时使用：

```powershell
uv sync --extra office --group dev
```

Agent 内的脚本使用 Docker 镜像内的 Python 库，仍需要构建包含办公依赖的新镜像：

```powershell
docker build -f Dockerfile.sandbox -t xclaw-sandbox:latest .
```

重启 Agent，以加载目录型技能。默认执行模式为 execute，必要时在配置中设置：

```yaml
agent:
  execution_mode: execute
  workspace_dir: E:/documents
  # 可选：使用自定义技能目录，未设置时使用项目/安装包自带的技能
  skills_dir: E:/study/project/XClaw/skills
```

工作区可以与技能目录不同；资源读取从已注册的技能目录进行，数据文件来自工作区。
镜像运行时将技能目录挂载到 `/opt/xclaw-skills`，脚本用固定 argv 与隔离 Python 模式运行。
技能目录如果也位于 workspace 内，其容器原路径额外覆盖为只读挂载。
Host 文件工具仍由 ExecutionPolicy 管理；这不等于对宿主机安装目录的操作系统级隔离。
没有 Docker 时不自动降级运行宿主机脚本，可用下方独立 CLI 验证方式。

## Agent 使用流程

向 Agent 发出具体文件任务，例如：

```text
用 Word 技能读取 inputs/template.docx，填入我提供的 name、date、summary，保存为 outputs/filled.docx。
用 Excel 技能把 inputs/week1.xlsx 和 inputs/week2.xlsx 按“编号”合并，输出 outputs/merged.xlsx。
```

模型先看到名称和 description。调用 `word` / `excel` 后才读取完整 SKILL.md；
需要参数格式时调用 `read_skill_resource`，然后准备 JSON 或使用已有输入文件。
脚本请求例如：

```json
{"skill_name":"word","arguments":["create","--spec","inputs/word.json","--output","outputs/report.docx"]}
```

这会调用 `run_skill_script`。入口只允许技能 metadata 声明的 `scripts/*.py`，
模型不能指定任意 Python 程序或修改解释器选项。字符串参数按 argv 原样传递，不做 shell 拼接。
返回 JSON `ok`、输出路径和操作统计。失败时应按错误修正输入，不把失败描述为成功。

## 加载、权限与兼容

- 同时发现 `skills/name/SKILL.md` 与旧 `skills/name.md`；重复名称以目录型优先，不加载参考 Markdown 为技能。
- 启动只读取元数据，正文在调用时读取，参考资料按需读取；名称、目录匹配和元数据类型会校验。
- `tools_needed` / `metadata.tools` 用于报告缺失或当前不可用的工具，不产生权限授权。
- 查看/编辑模式可以加载技能和参考资料；脚本执行包括 inspect 也需要 execute 模式。
- 不把 `allowed-tools` 或其他前置元数据当作绕过 ExecutionPolicy 的许可。
- 技能包随 wheel 分发，两个目录也可以独立复制使用，保留内部辅助模块。
- 文本资源最多 256 KiB，可按 start_line/max_lines 分段读取；SKILL.md 正文限 128 KiB。
- 当前没有实现 Claude Code 的技能激活去重/压缩后重新附加机制，也不通过 Python 文件导入执行插件初始化。

## 独立 CLI

在包含数据文件的工作目录中，使用脚本的实际路径执行：

```powershell
python E:/study/project/XClaw/skills/word/scripts/word_ops.py inspect inputs/report.docx
python E:/study/project/XClaw/skills/excel/scripts/excel_ops.py inspect inputs/tasks.xlsx --range A1:D10
```

需要项目的 office 依赖；不要将这个独立开发验证方式描述为 Agent 的沙箱降级。

## 验证与边界

测试使用真实 python-docx/openpyxl 和临时目录，验证生成文件、源文件保留、格式/类型、模板填充、
公式文本、CSV 安全导出、合并冲突、资源路径边界，以及 Agent → Skill → 资源 → 脚本 → 结果链路。
Agent 模型和 Docker 使用测试替身；真实容器仍需在已安装 Docker 的环境验证。
同时补了流式工具调用索引聚合，以支持 Demo 首轮加载多个技能。

```powershell
uv run --extra office pytest -q
```

不支持 `.doc` / `.xls` / `.xlsm`、宏运行、完整修订处理、复杂嵌入对象编辑，
也未验证桌面 Office 的渲染及公式计算行为。需要这些功能时使用相应应用/渲染引擎并单独验收。
