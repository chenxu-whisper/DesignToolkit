# DesignToolkit

DesignToolkit 是一款设计工具集，为设计工作提供便捷实用的工具支持。

## 环境要求

- Python 3.12
- uv

## 初始化环境

```powershell
uv sync --link-mode=copy
```

`--link-mode=copy` 用于规避 uv 缓存目录和项目虚拟环境位于不同文件系统，或当前环境限制硬链接行为时可能出现的 hardlink 问题。

## 运行应用

```powershell
uv run designtoolkit
```

预期输出：

```text
DesignToolkit is ready.
```

## 验证

```powershell
uv run pytest
uv run ruff check .
uv run python -m compileall src tests
```

## 仓库结构

```text
.
├── docs/       # 项目文档和计划
├── scripts/    # 本地自动化脚本
├── src/        # 应用源码
│   └── designtoolkit/
└── tests/      # 自动化测试
```
