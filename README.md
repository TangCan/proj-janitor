# proj-janitor

扫描、备份并安全清理旧项目的轻量命令行工具。适合在多年积累的项目目录（如 `~/projects`）中先盘点、再归档、最后删除构建产物或整个已备份项目。

## 安全原则

- `scan` 只读：报告项目大小、Git 分支和工作区状态。
- `backup` 先归档再清理：每个项目生成独立 `tar.gz`，并写入 SHA-256 清单。
- `clean` 默认 dry-run：只匹配白名单内的构建产物目录/文件。
- 永远不删除项目根目录，永远跳过 `.git`，不跟随符号链接。
- 真正删除需要同时提供 `--apply` 和交互确认（或 `--yes`）。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

也可以不安装，直接运行：

```bash
python3 -m proj_janitor --help
```

## 使用

### 1. 盘点

```bash
projjan scan ~/richard_plus
projjan scan ~/richard_plus --json
projjan scan ~/richard_plus --all
```

输出项目名、大小、Git 分支/状态、识别到的项目标记和最后修改时间。默认只识别带 `.git`、`pyproject.toml`、`package.json`、`Cargo.toml` 等标记的目录；`--all` 会把普通目录也列出来。

### 2. 备份

```bash
projjan backup ~/richard_plus/amp_zk ~/richard_plus/bos_dynamic --dest ~/backups/projects
projjan backup ~/richard_plus --dest ~/backups/projects --dry-run
projjan backup ~/richard_plus --dest ~/backups/projects --include-artifacts
```

每次备份创建 `backup-YYYYmmdd-HHMMSS/`：

```text
backup-20261022-153000/
├── amp_zk.tar.gz
├── bos_dynamic.tar.gz
└── manifest.json
```

默认排除构建产物与依赖缓存，避免把 `node_modules`、`target`、虚拟环境打进归档；`--include-artifacts` 可完整归档。`manifest.json` 记录归档路径、大小、源大小和 SHA-256。

### 3. 清理

```bash
projjan clean ~/richard_plus
projjan clean ~/richard_plus --apply
projjan clean ~/richard_plus --apply --yes
projjan clean ~/richard_plus --rules node,python-cache
projjan clean . --list-rules
```

默认展示将被删除的目录/文件及可回收空间，不会删除任何内容。确认后加 `--apply`；非交互环境还需 `--yes`。

| 规则 | 匹配名称 |
| --- | --- |
| `python-cache` | `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `*.pyc`, `.coverage`, `htmlcov` |
| `python-env` | `.venv`, `venv`, `.tox`, `.nox`, `*.egg-info` |
| `node` | `node_modules`, `.next`, `.nuxt`, `.svelte-kit`, `.turbo`, `.parcel-cache`, `coverage` |
| `build` | `build`, `dist`, `out`, `target`, `CMakeFiles`, `.gradle` |
| `editor` | `.cache`, `.DS_Store`, `Thumbs.db` |

推荐流程：

```bash
projjan scan ~/richard_plus
projjan backup ~/richard_plus --dest ~/backups/projects
projjan clean ~/richard_plus
projjan clean ~/richard_plus --apply
```

旧项目确认不再需要后，再手动删除源目录，备份归档保留在 `--dest` 中。

## 项目结构

```text
proj_janitor/
├── config.py    # 项目标记与清理规则
├── models.py    # 数据模型与大小格式化
├── scanner.py   # 项目发现、大小与 Git 状态
├── backup.py    # tar.gz 归档与清单
├── cleaner.py   # 清理计划与执行
└── cli.py       # argparse 命令行入口
tests/           # unittest 测试
```

## 开发

```bash
python3 -m unittest discover -s tests -v
```

无第三方运行时依赖，要求 Python 3.10+。

## 后续计划

- 支持 `git bundle`，保留完整提交历史并减小归档体积。
- 增量备份与保留策略（例如只保留最近 N 份）。
- 从备份归档恢复项目。
- 配置文件（排除规则、备份目标、保留策略）。
- 归档校验命令（按 `manifest.json` 验证 SHA-256）。
