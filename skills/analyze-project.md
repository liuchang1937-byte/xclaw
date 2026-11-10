---
name: analyze-project
description: >
  分析指定目录的项目结构和架构。识别主要模块、模块间关系、架构模式和技术栈，
  并给出改进建议。适用于快速理解一个新项目。
triggers:
  - 分析项目
  - 项目结构
  - 架构分析
  - analyze project
  - 这是什么项目
tools_needed:
  - list_directory
  - read_file
  - search_in_files
---

# 项目架构分析

## 执行流程

### 第 1 步：概览项目结构

使用 `list_directory` 查看项目顶层目录，了解：
- 主要的源码目录（如 src/、lib/、packages/）
- 配置文件（如 pyproject.toml、package.json）
- 文档目录

### 第 2 步：识别模块

对每个主要目录，用 `list_directory` 查看其子目录和关键文件，识别：
- 每个模块的职责（通过 `__init__.py`、模块名、关键类名推断）
- 模块之间的依赖关系（通过 `search_in_files` 搜索 import 语句）

### 第 3 步：深入关键文件

用 `read_file` 读取以下关键文件：
- 项目配置文件（pyproject.toml / package.json / Cargo.toml）
- 入口文件（main.py / index.js / main.rs）
- 核心模块的 `__init__.py` 或主类文件

### 第 4 步：输出分析报告

按以下格式输出：

```
## 📊 项目架构分析报告

### 基本信息
- 项目名称: ...
- 编程语言: ...
- 包管理器: ...
- 主要依赖: ...

### 模块结构
| 模块 | 路径 | 职责 |
|---|---|---|
| ... | ... | ... |

### 架构模式
[说明项目使用的架构模式，如分层架构、微服务、插件系统等]

### 数据流
[描述关键的数据/请求在模块间如何流动]

### 改进建议
1. ...
2. ...
```
