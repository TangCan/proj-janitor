# proj-janitor

扫描、备份、恢复并安全清理旧项目的轻量命令行工具。适合在多年积累的项目目录（如 `~/projects`）中先盘点、再归档、需要时恢复，最后删除构建产物或整个已备份项目。

## 安全原则

- `scan` 只读：报告项目大小、Git 分支和工作区状态。
- `backup` 优先记录可恢复信息：已推送到 GitHub 的干净项目只写入远程 URL 和提交 SHA；其余项目回退为 `tar.gz` 并记录 SHA-256。
- 备份目录可通过 `BACKUP_DEST` 环境变量指定，也可用 `--backup-dest` 指定；命令行参数优先。恢复仍使用 `--dest`。
- `restore` 默认恢复到 manifest 记录的原始位置，指定 `--dest` 时恢复到新目录；先校验 manifest 和归档 SHA-256，且不覆盖已有目录。
- `clean` 默认 dry-run：只匹配白名单内的构建产物目录/文件。
- 永远不删除项目根目录，永远跳过 `.git`，不跟随符号链接。
- 真正删除需要同时提供 `--apply` 和交互确认（或 `--yes`）。

## 安装

### Rust 命令行版（推荐免 Python 环境）

安装 Rust 工具链后，在仓库根目录运行：

```bash
cargo install --locked --path .
```

安装后可直接使用 `projjan`。发布到 crates.io 后，可改用 `cargo install proj-janitor`；目前尚未发布到 crates.io。Rust 版与 Python 版共用 CLI 名称，若同时安装，后安装的版本会覆盖同名入口；需要并存时可分别安装到不同 `--root` 并使用对应路径。

Rust 版会生成和读取 schema v2 manifest，支持 `scan`、`backup`、`restore`、`clean`。默认备份策略与 Python 版一致：有充分本地证据表明可从 GitHub 恢复时仅记录 URL 与提交，否则创建 `tar.gz` 归档。归档恢复支持 Unix 上的项目内符号链接，拒绝越界链接和特殊文件；Git 策略恢复仍需本机安装 Git。

Rust 版首次从源码安装需要 Cargo 下载依赖；安装完成后的程序不需要 Python。`backup` 仍可用 `BACKUP_DEST` 或 `--backup-dest` 指定目录。

### Python 版

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
projjan backup ~/richard_plus/amp_zk ~/richard_plus/bos_dynamic --backup-dest ~/backups/projects
projjan backup ~/richard_plus --backup-dest ~/backups/projects --dry-run
projjan backup ~/richard_plus --backup-dest ~/backups/projects --include-artifacts
projjan backup ~/richard_plus --backup-dest ~/backups/projects --always-archive
```

也可设置默认备份目录，之后省略选项：

```bash
export BACKUP_DEST=~/backups/projects
projjan backup ~/richard_plus --dry-run
projjan backup ~/richard_plus
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
projjan backup ~/richard_plus --backup-dest ~/backups/projects
projjan clean ~/richard_plus
projjan clean ~/richard_plus --apply
```

旧项目确认不再需要后，再手动删除源目录，备份归档保留在 `BACKUP_DEST` 或 `--backup-dest` 指定的位置。

### 4. 恢复

`restore` 根据 manifest 自动恢复项目。省略 `--dest` 时，恢复到 manifest 中记录的原始 `source` 路径；指定 `--dest` 时，恢复到 `DIR/<项目名>/`：

```bash
# 恢复到原始位置；交互终端会先列出目标并要求确认
projjan restore ~/backups/projects/xtask_todo/manifest-20261023-122828.json

# 非交互脚本显式确认；--json 的实际恢复也必须带 --yes
projjan restore <manifest> --yes

# 恢复到副本目录
projjan restore <manifest> --dest ~/restored

# 只预览计划，不写文件
projjan restore <manifest> --dry-run
projjan restore <manifest> --dest ~/restored --dry-run

# 多记录 manifest 只恢复指定项目
projjan restore <manifest> --dest ~/restored --project xtask_todo

# 输出纯 JSON
projjan restore <manifest> --dest ~/restored --dry-run --json
```

恢复规则：

- 省略 `--dest` 时，目标取自每条记录的 `source`；`source` 必须是绝对路径、文件名等于项目名，且不能包含 `..` 或 NUL。伪造或损坏的 manifest 可能指向任意可写路径，因此实际恢复到原始位置前必须确认 manifest 来源可信。
- 原始位置模式默认要求交互确认；标准输入不是 TTY 时必须显式加 `--yes`，`--json` 的实际恢复也必须加 `--yes`。`--dest` 模式不需要该确认。
- `git` 策略：先 `git clone --no-checkout`，再 `git checkout --detach <commit>`。恢复后是 detached HEAD，不会自动创建或切换原分支；远程必须仍包含该提交。
- `archive` 策略：默认校验 manifest 中的 SHA-256，校验通过后才解压；`--no-verify` 可显式跳过校验。
- 归档会先解压到临时目录，确认顶层目录为项目名后再移动到目标位置；拒绝绝对路径、`..` 路径和越界链接。
- 目标目录已存在或是符号链接时拒绝覆盖；需要恢复副本时请指定 `--dest`，或先自行移走旧目录。
- 多项目恢复按顺序执行；中途失败时，已恢复的项目不会自动回滚。
- manifest 中的归档绝对路径失效时，会尝试 manifest 同目录及 `<项目名>/` 下的同名归档，便于整体移动备份目录。
- 原始位置路径与备份时的机器和目录布局绑定；在另一台机器或不同用户下恢复时，应使用 `--dest`。
- 恢复后如需继续开发 Git 项目，可自行创建分支：

  ```bash
  git -C ~/restored/xtask_todo switch -c main
  ```

`git` 恢复依赖远程仓库和本机凭据配置；私有仓库可使用 `gh auth setup-git`、SSH 或 credential helper。工具只验证 manifest 中的 URL 形式和提交格式，不会预检远程是否仍存在该提交。

## 项目结构

```text
proj_janitor/
├── config.py    # 项目标记与清理规则
├── models.py    # 数据模型与大小格式化
├── scanner.py   # 项目发现、大小与 Git 状态
├── backup.py    # URL-only 记录、tar.gz 归档与清单
├── restore.py   # 清单校验、Git/归档恢复与安全解压
├── cleaner.py   # 清理计划与执行
└── cli.py       # argparse 命令行入口
tests/           # unittest 测试
```

## 开发

```bash
python3 -m unittest discover -s tests -v
```

当前 98 项测试全部通过（2026-10-24，Python 3.12.3）。无第三方运行时依赖，要求 Python 3.10+。

Rust 版开发检查：

```bash
cargo check
cargo test
```

## 后续计划

- 支持 `git bundle`，保留完整提交历史并减小归档体积。
- 增量备份与保留策略（例如只保留最近 N 份）。
- 独立的 `verify` 命令（当前归档校验内置于 `restore`）。
- 配置文件（排除规则、备份目标、保留策略）。
- 恢复时对 Git 远程提交做联网预检。
