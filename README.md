# proj-janitor

扫描、备份并安全清理旧项目的轻量命令行工具。适合在多年积累的项目目录（如 `~/projects`）中先盘点、再归档、最后删除构建产物或整个已备份项目。

## 安全原则

- `scan` 只读：报告项目大小、Git 分支和工作区状态。
- `backup` 优先记录可恢复信息：已推送到 GitHub 的干净项目只写入远程 URL 和提交 SHA；其余项目回退为 `tar.gz` 并记录 SHA-256。
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
projjan backup ~/richard_plus --dest ~/backups/projects --always-archive
```

备份策略按项目自动选择：

- `git`：工作区干净，无 stash、submodule、LFS，所有本地分支和本地 tag 均已推送，且远程是 GitHub URL 时，仅记录远程 URL、分支、上游和提交 SHA，不再生成归档。
- `archive`：其余情况回退为 `tar.gz`，并在清单中记录回退原因（例如 `working tree is not clean`、`remote is not a supported GitHub URL`、`local tag is not pushed: v1`）。
- 存在本地 tag 时，URL-only 判定会执行 `git ls-remote --tags --refs <remote>`，逐个比较远程 tag 对象 ID；远程不可达、tag 缺失或指向不一致都会回退归档。没有本地 tag 时不会发起这次网络查询。
- 远程 URL 可以内嵌 HTTPS 凭据（如 `https://user:token@github.com/...` 或 `https://token@github.com/...`）；URL-only 记录和命令输出会去掉用户名/令牌，只保留 `https://github.com/...`。
- 归档会包含 `.git/config`。如果远程 URL 内嵌凭据，归档也会包含该凭据；对这类项目执行归档前应先清理远程 URL，或确认归档的访问权限。

默认归档会排除构建产物与依赖缓存，避免把 `node_modules`、`target`、虚拟环境打进压缩包；`--include-artifacts` 强制完整归档，`--always-archive` 强制所有项目都归档。

每个项目使用一个稳定的项目目录，不再创建 `backup-.../` 运行目录。纯 `git` 备份只在该目录下写清单：

```text
backups/
└── xtask_todo/
    ├── manifest-20261023-122828.json
    └── manifest-20261023-122832.json
```

需要归档的项目也写入同一个项目目录，归档文件名带运行时间戳：

```text
backups/
├── xtask_todo/
│   ├── manifest-20261023-103117.json
│   └── xtask_todo-20261023-103117.tar.gz
└── another_project/
    ├── manifest-20261023-103117.json
    └── another_project-20261023-103117.tar.gz
```

- 一次运行包含多个项目时，每个项目目录各写一份只包含该项目记录的清单。
- 同一项目不同时间的清单和归档共用项目目录，文件名带时间戳。
- 不同根目录下存在同名项目时，项目目录会追加 8 位路径哈希，例如 `app-1a2b3c4d/`。
- 同一秒内重复备份同一项目时，文件名会追加 `-2`、`-3`，不会覆盖已有文件。
- 旧版本生成的 `backup-YYYYmmdd-HHMMSS/` 目录不会被自动迁移或删除。

清单使用 `schema_version: 2`，顶层记录 `include_artifacts`、`always_archive` 和 `records`。`git` 记录包含 `remote_url`、`branch`、`commit`、`upstream`；`archive` 记录包含归档路径、大小、源大小、SHA-256 和可选的 `fallback_reason`。

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

### 4. 恢复

当前版本不提供 `restore` 子命令。按 `manifest-YYYYmmdd-HHMMSS.json` 中的记录手动恢复：

```bash
# git 策略：重新克隆并检出备份时的提交
git clone <remote_url> <project>
git checkout <commit>

# archive 策略：解压归档
tar -xzf <archive>
```

## 项目结构

```text
proj_janitor/
├── config.py    # 项目标记与清理规则
├── models.py    # 数据模型与大小格式化
├── scanner.py   # 项目发现、大小与 Git 状态
├── backup.py    # URL-only 记录、tar.gz 归档与清单
├── cleaner.py   # 清理计划与执行
└── cli.py       # argparse 命令行入口
tests/           # unittest 测试
```

## 开发

```bash
python3 -m unittest discover -s tests -v
```

当前 49 项测试全部通过。无第三方运行时依赖，要求 Python 3.10+。

## 后续计划

- 支持 `git bundle`，保留完整提交历史并减小归档体积。
- 增量备份与保留策略（例如只保留最近 N 份）。
- 增加 `restore` 子命令（当前按 `manifest-*.json` 手动恢复）。
- 配置文件（排除规则、备份目标、保留策略）。
- 归档校验命令（按 `manifest-*.json` 验证 SHA-256）。
