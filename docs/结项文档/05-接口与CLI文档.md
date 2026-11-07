# proj_janitor 接口与 CLI 文档

## 1. 文档信息

| 项目 | 内容 |
| --- | --- |
| 项目名称 | proj-janitor（proj_janitor） |
| 项目描述 | 本地项目盘点、备份、恢复与安全清理命令行工具 |
| 文档名称 | 接口与 CLI 文档 |
| 文档版本 | 2026-11-05 `backup --incremental` 增量备份（CR-093） |
| 源码提交 | `abf91c9b391236e524e4a316dc15912274e6e234`（v0.7.13 / python-v0.1.13 发布提交，含 CR-108） |
| 源码标签 | `v0.7.13` / `python-v0.1.13` 为最新发布标签；含 CR-097 LFS 可用性探测 |
| 文档日期 | 2026-11-05 |
| 项目类型 | 自研开源、本地 CLI、小型单仓库项目 |
| 结项状态 | 功能开发与自测完成，待内部验收 |
| 文档状态 | 已编制，接口评审与签字信息待补充 |
| 编制依据 | 代码基线源码、`README.md`、`tests/`、Git 提交记录与结项文档清单 |

> 本文前 1–17 章描述稳定 CLI/schema 接口；部分旧的版本快照保留其编写时状态。当前 Rust 已发布版本为 0.7.13（`v0.7.13`），Python 0.1.13 已发布至 PyPI。正式验收、评审或签字只有在实际记录存在时才可认定，本次不补造此类记录。

> 说明：本文为结项阶段回溯编制的接口与 CLI 文档，依据 `v0.1.0` 基线及 post-v0.1.0 变更整理，不代表存在经批准的外部接口契约。命令行为以代码基线源码为准；文中示例路径、项目名、时间和输出均为示意，实际结果随环境变化。正式接口评审、审批人和签字信息待补充。

## 2. 概述

### 2.1 目的与范围

本文说明 `proj_janitor` 当前版本（`v0.1.0` 基线加 post-v0.1.0 变更）对用户和自动化脚本暴露的接口，包括：

- 命令行入口、子命令、参数、输出和退出码；
- `scan`、`clean` 和 `restore` 的 JSON 输出；
- `backup` 生成的按项目备份清单 `manifest-<时间戳>.json`（schema v2）；
- 可供 Python 调用的模块级函数及其输入、输出、异常和副作用；
- 错误分类、Git 可恢复性失败原因、兼容性和明确不支持的外部接口。

本文覆盖的接口是代码基线已实现行为，不构成对未来版本的兼容性承诺。Python 模块整体属于内部实现，除本文列出的函数外，不保证稳定的导入路径、函数签名或行为。

### 2.2 接口分类

| 接口类型 | 提供方式 | 主要消费者 | 稳定性 |
| --- | --- | --- | --- |
| CLI | `projjan` 控制台脚本、`python3 -m proj_janitor` | 用户、Shell 脚本 | v0.1.0 行为；后续可能调整 |
| 文本输出 | 标准输出/标准错误 | 用户、终端日志 | 面向人阅读，不建议作为机器协议 |
| JSON 输出 | `scan --json`、`clean --json`、`restore --json` | 自动化脚本 | 无独立 schema 版本号，字段可能变化 |
| 备份清单 | `DEST/<project>/manifest-<timestamp>.json` | 恢复、审计、外部工具 | `schema_version: 2` |
| Python API | `proj_janitor` 包内函数 | 本仓库测试、内部扩展 | 内部接口，非稳定公共 API |
| 文件系统接口 | 读取项目、写入归档、读取 manifest/归档、写入恢复目录、删除目标 | 操作系统 | 受本地权限和文件系统语义约束 |
| Git 接口 | 本机 `git` 子进程 | 扫描、备份决策、tag 校验和恢复 | 依赖外部 Git 可执行文件 |
| HTTP API | 无 | 无 | 当前不提供 |

### 2.3 通用约定

| 约定 | 说明 |
| --- | --- |
| 标准输出 | 正常结果、表格、JSON、dry-run 提示和成功摘要写入标准输出。 |
| 标准错误 | 参数错误、根路径错误、无项目提示、拒绝删除和逐项删除错误写入标准错误。 |
| 退出码 | `0` 表示成功；`1` 表示根路径/项目处理/删除失败、无项目可备份或恢复失败；`2` 表示用法错误、拒绝删除或用户取消；POSIX 信号中断返回 `128 + 信号编号`。详见第 9 节。 |
| 路径 | 项目路径在扫描后解析为绝对路径；`~` 在根路径和备份目标上展开；输出中的路径按当前平台字符串表示。 |
| 体积 | 统一以字节整数传递；文本输出使用 `B`、`KiB`、`MiB`、`GiB`、`TiB`，非字节单位保留一位小数。 |
| 时间 | 文本扫描结果使用本地时间 `YYYY-MM-DD HH:MM`；JSON 的 `last_modified` 使用带时区偏移的 ISO 8601；manifest 的 `created_at` 使用 UTC ISO 8601。 |
| 编码 | JSON 和 manifest 使用 UTF-8、`ensure_ascii=False`；终端输出受当前终端编码影响。 |
| 网络 | 不调用 GitHub API，不监听端口；URL-only 在存在本地 tag 时执行 `git ls-remote`，`restore` 的 Git 策略会克隆远程仓库；其余 Git 命令本地执行。 |
| 配置 | `.proj-janitor.toml` 提供 `backup`/`prune`/`verify`/`restore` 默认值（含 `[verify].check_remote`、`[restore].check_remote`）；取值优先级为命令行 > 环境变量 > 配置文件。项目标记和清理规则仍由 `config.py` 静态定义。 |
| 并发 | 无线程、异步任务、文件锁或事务；备份的归档和 manifest 用排他创建避免覆盖，并发撞名时该项目失败、重跑可使用新后缀；不保证源项目在备份期间保持不变；`restore` 不覆盖已有目标。 |

### 2.4 明确不在范围内

当前版本不提供以下接口：

- 恢复覆盖、回滚、断点续传、进度显示或远程提交预检；
- HTTP、RPC、WebSocket、GUI 或数据库接口；
- 远程备份上传、云对象存储、增量备份、加密、压缩算法选择；
- 自定义清理规则文件、每项目排除文件；
- tag、detached HEAD 的完整备份；干净仓库可轻量备份 Git LFS 指针及受支持子模块，需安装 Git LFS，且恢复时默认 GitHub endpoint、LFS 对象和子模块固定提交仍可访问；
- GitHub API 认证、远端提交存在性预检或自动恢复演练；
- 归档内容秘密扫描；
- Windows/macOS 的正式兼容性承诺。

### 2.5 配置文件

`.proj-janitor.toml` 提供常用选项的默认值；`backup`、`prune`、`verify`、`restore` 均接受 `--config PATH` 显式指定；未指定时从当前目录向上逐级查找，找不到则使用内置默认。

```toml
[backup]
dest = "/data/backups"   # 未传 --backup-dest 且未设 BACKUP_DEST 时使用
keep = 3                 # 成功备份后每个项目保留的最近 manifest 数；未传 --keep 时使用
exclude = ["node_modules", "*.log"]   # archive 策略归档时跳过匹配的文件/目录
incremental = true          # archive 策略生成差异快照；未传 --incremental 时使用
fail_on_secrets = false     # 为 true 时 archive 遇敏感路径则拒绝备份；可用 --fail-on-secrets / --no-fail-on-secrets 覆盖
exclude_secrets = false     # 为 true 时 archive 省略敏感路径；可用 --exclude-secrets / --no-exclude-secrets 覆盖
progress = false            # 为 true 时 stderr 输出打包进度；dry-run 下估算排除后文件数/体积；可用 --progress / --no-progress 覆盖
check_space = false         # 为 true 时按未压缩估算拒绝空间不足的 archive 备份；可用 --check-space / --no-check-space 覆盖
fail_on_insecure_dest = false  # 为 true 时拒绝 group/other 可访问的备份目录（Unix）；可用 --fail-on-insecure-dest / --no-fail-on-insecure-dest 覆盖
encrypt_passphrase_file = ""  # 若设置，用 openssl 加密 archive/bundle；可用 --encrypt-passphrase-file / --no-encrypt 覆盖

[prune]
keep = 3                 # 未传 --keep 时使用

[verify]
check_remote = false     # 为 true 时 verify 默认联网预检；可用 --no-check-remote 关闭

[restore]
check_remote = false     # 为 true 时 restore 默认联网预检；可用 --no-check-remote 关闭
require_dest = false     # 为 true 时必须传 --dest，禁止按 manifest source 原位恢复；可用 --require-dest / --no-require-dest 覆盖
decrypt_passphrase_file = ""  # 解密加密归档的口令文件；可用 --decrypt-passphrase-file 覆盖
```

- **优先级**：命令行选项 > 环境变量（`BACKUP_DEST`） > 配置文件 > 内置默认/报错。未提供任何备份目标时报错 `set BACKUP_DEST, pass --backup-dest, or configure [backup].dest`；`prune` 未提供保留数时报错 `set --keep or configure [prune].keep`；`backup` 的保留数按 命令行 > 配置文件 `[backup].keep` 解析。
- **严格校验**：未知键、非法值（如 `keep` 为 0 或负数、空 `dest`）会被拒绝并报出文件与原因。
- **格式**：TOML。Rust 版内置 `toml` 解析；Python 版用标准库 `tomllib`（Python 3.11+；Python 3.10 由依赖自动安装 `tomli` 回退），除此之外无第三方运行时依赖。
- **排除规则 `[backup].exclude`**：字符串数组，为 archive（tar.gz）策略在归档时跳过匹配的文件/目录。模式为 glob（`*`、`?`、`[seq]`、`[!seq]`、`**`），按项目根相对路径逐段匹配；匹配到目录则跳过其整个子树。不支持花括号展开（`{a,b}`），空模式被拒绝。`git` 与 `git_bundle` 策略不受影响（`git` 只记录远程信息、`git_bundle` 打包整个仓库历史）。仅通过配置文件设置，无命令行参数。
- **增量备份 `[backup].incremental`**：布尔值（默认 `false`）。为 `true` 时 `backup` 默认对 archive 策略生成差异快照；命令行 `--incremental` 可单独开启（优先级高于配置）。仅 archive 策略生效，`git`/`git_bundle` 策略不受影响。
- **敏感路径硬失败 `[backup].fail_on_secrets`**：布尔值（默认 `false`）。为 `true` 时 archive 策略若启发式检测到常见敏感路径（与 CR-099 告警同一套规则），则拒绝该项目备份、不写归档/manifest，并以非零退出；默认仍仅告警。命令行 `--fail-on-secrets` / `--no-fail-on-secrets` 可覆盖配置。
- **敏感路径排除 `[backup].exclude_secrets`**：布尔值（默认 `false`）。为 `true` 时 archive 打包跳过上述敏感路径并输出 `note: … excluding secret paths`；与 `fail_on_secrets` 同时开启时优先排除（不拒绝）。命令行 `--exclude-secrets` / `--no-exclude-secrets` 可覆盖。

## 3. 程序入口

### 3.1 控制台脚本

安装后使用 `projjan`：

```bash
projjan --help
projjan scan ~/projects
```

`pyproject.toml` 声明的入口点为：

```toml
[project.scripts]
projjan = "proj_janitor.cli:main"
```

### 3.2 模块入口

未安装或需要固定解释器时，可使用模块方式：

```bash
python3 -m proj_janitor --help
python3 -m proj_janitor scan ~/projects
```

`proj_janitor/__main__.py` 调用 `main()` 并将返回值交给 `SystemExit`，因此退出码与 `projjan` 控制台脚本一致。

### 3.3 Python 入口

```python
from proj_janitor.cli import main

exit_code = main(["scan", "/path/to/projects"])
```

`main(argv: Sequence[str] | None = None) -> int`：

- `argv` 为 `None` 时使用 `sys.argv[1:]`；
- 解析参数并分派到 `scan`、`backup`、`prune`、`restore`、`verify` 或 `clean`；
- 返回整数退出码；
- 参数错误由 `argparse` 直接抛出 `SystemExit`，不会以普通返回值返回。

`build_parser() -> argparse.ArgumentParser` 返回配置好的解析器。除测试或嵌入场景外，不建议依赖其中的子解析器结构。

### 3.4 全局选项与帮助

| 形式 | 行为 |
| --- | --- |
| `projjan --help` | 打印全局帮助并退出 `0`。 |
| `projjan <command> --help` | 打印子命令帮助并退出 `0`。 |
| `projjan --version` | 打印 `projjan 0.1.0` 并退出 `0`。 |
| `projjan` | 缺少必需子命令，`argparse` 输出用法错误并退出 `2`。 |

`--version` 是全局选项，按 `argparse` 语义应放在子命令之前；例如使用 `projjan --version`，不要在 `scan` 等子命令后依赖同名的全局选项。

### 3.5 命令总览

| 命令 | 用途 | 默认是否写入 |
| --- | --- | --- |
| `scan` | 发现项目，输出体积、修改时间、项目标记和 Git 状态 | 只读 |
| `backup` | 按 Git 可恢复性记录位置或创建 `tar.gz`，并按项目生成 manifest | 写入备份目录 |
| `prune` | 保留每个项目目录最新 N 份清单；只删除过期清单及其引用的 bundle/归档 | 默认只预览 |
| `restore` | 按 manifest 恢复 Git 项目或归档项目 | 写入恢复目标 |
| `verify` | 校验 archive/bundle SHA-256 或 Git-only manifest 元数据；`--check-remote` 可选联网预检远端引用（含子模块） | 只读 |
| `clean` | 按白名单生成清理计划；确认后删除构建产物 | 默认 dry-run |

## 4. `--version`

### 4.1 语法

```bash
projjan --version
```

### 4.2 输出

```text
projjan 0.1.0
```

### 4.3 说明

- 版本字符串由 `%(prog)s` 和 `proj_janitor.__version__` 组成，当前为 `0.1.0`。
- `--version` 由 `argparse` 的 `version` action 处理，打印后退出 `0`，不会执行任何子命令。
- 无 JSON 版本输出；自动化脚本应解析纯文本或从 Python 包读取 `__version__`。

## 5. `scan`

### 5.1 语法

```text
projjan scan ROOT [ROOT ...] [--all] [--json]
```

`ROOT` 可以是一个或多个目录。命令只读，不创建、修改或删除文件。

### 5.2 参数

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `ROOT...` | 是 | 无 | 一个或多个扫描根路径；支持 `~` 展开。 |
| `--all` | 否 | 关闭 | 当根目录本身不是项目时，把没有已知项目标记的直接子目录也纳入扫描。 |
| `--json` | 否 | 关闭 | 以 JSON 数组输出；错误仍写入标准错误。 |

### 5.3 项目发现语义

发现逻辑由 `scanner.discover_projects()` 实现，规则如下：

1. 根路径展开 `~` 后必须存在且为目录，否则抛出根路径错误。
2. 如果根目录自身包含任一项目标记，则只扫描根目录自身，不再扫描其子目录。
3. 否则只检查根目录的直接子目录，不递归发现嵌套项目。
4. 直接子目录必须同时满足：是目录、不是符号链接、名称不以 `.` 开头、非空。
5. 默认只保留包含至少一个项目标记的子目录；`--all` 放宽“包含项目标记”这一条，但仍跳过隐藏目录、符号链接和空目录。
6. 多个根路径按命令行顺序处理；每个根内部按名称排序；CLI 按解析后的项目路径去重，保留首次出现的项目。
7. 项目路径在 `scan_project()` 中解析为绝对路径，项目名为路径最后一段。

项目标记共 16 个，定义于 `config.PROJECT_MARKERS`：

```text
.git
.hg
.svn
pyproject.toml
setup.py
setup.cfg
requirements.txt
package.json
Cargo.toml
go.mod
CMakeLists.txt
Makefile
meson.build
build.gradle
build.gradle.kts
pom.xml
```

### 5.4 体积、修改时间与 Git 状态

**体积与修改时间**（`measure_tree()`）：

- 使用 `os.walk(..., followlinks=False)`；
- 从遍历中剔除符号链接目录；
- 只把普通文件计入 `size_bytes`，不跟随符号链接；
- 取遍历到的最新 `st_mtime`，转换为带时区信息的本地 `datetime`；
- 没有任何可遍历文件时，`last_modified` 为 `null`；
- 遍历或读取文件元数据遇到 `OSError` 时抛出错误，不返回不完整的体积结果；
- `size_bytes` 是文件字节数之和，不是磁盘占用，也不包含目录元数据。

**Git 状态**（`read_git_state()`）：

- 不存在 `.git` 时返回 `null`；
- 存在 `.git` 时执行 `git rev-parse --abbrev-ref HEAD` 和 `git status --porcelain`；
- `branch` 可能为 `null`（命令失败或无分支名）；
- `dirty` 为 `true`（有输出）、`false`（无输出）或 `null`（状态命令失败）。

文本中的状态标签：

| 情况 | BRANCH | STATE |
| --- | --- | --- |
| 无 Git 信息 | `-` | `-` |
| 有 Git 但分支读取失败 | `-` | `clean` / `dirty` / `unknown` |
| 工作区状态读取失败 | 分支名或 `-` | `unknown` |
| 工作区干净 | 分支名 | `clean` |
| 工作区有改动 | 分支名 | `dirty` |

### 5.5 文本输出

有项目时输出固定表头：

```text
PROJECT  SIZE  BRANCH  STATE  MARKERS  MODIFIED
-------  ----  ------  -----  -------  --------
```

列含义：

| 列 | 内容 |
| --- | --- |
| `PROJECT` | 项目目录名 |
| `SIZE` | `human_size()` 格式化后的体积 |
| `BRANCH` | Git 分支名；无 Git 或读取失败为 `-` |
| `STATE` | `clean`、`dirty`、`unknown` 或 `-` |
| `MARKERS` | 识别到的项目标记，逗号加空格连接；没有则为 `-` |
| `MODIFIED` | 本地时间 `YYYY-MM-DD HH:MM`；无文件时间为 `-` |

无项目且未使用 `--json` 时，向标准错误输出：

```text
No projects found.
```

### 5.6 JSON 输出

`--json` 时输出 JSON 数组，不输出表头。字段见第 10.1 节。

示例（示意）：

```json
[
  {
    "name": "demo",
    "path": "/home/user/projects/demo",
    "size_bytes": 18432,
    "last_modified": "2026-10-20T18:42:11.123456+08:00",
    "markers": [
      ".git",
      "pyproject.toml"
    ],
    "git": {
      "branch": "main",
      "dirty": false
    }
  }
]
```

### 5.7 退出码与错误

| 情况 | 退出码 | 输出 |
| --- | --- | --- |
| 所有根路径处理成功 | `0` | 文本表格或 JSON。 |
| 没有发现项目，但根路径均有效 | `0` | 文本模式输出 `No projects found.` 到标准错误；JSON 模式输出 `[]`。 |
| 任一根本身无法读取/不存在/不是目录 | `1` | 该项错误到标准错误；其他根继续处理，已有结果仍输出。 |
| 参数缺失或非法 | `2` | `argparse` 用法错误。 |

`--json` 模式下，根路径错误不会写入 JSON 数组，而是继续写入标准错误；因此调用方应同时检查退出码和标准错误。

### 5.8 示例

```bash
projjan scan ~/projects
projjan scan ~/projects --all
projjan scan ~/projects ~/archive --json
```

## 6. `backup`

### 6.1 语法与参数

```text
projjan backup ROOT [ROOT ...] [--backup-dest DEST] [--config PATH] [--keep N]
                 [--yes] [--dry-run]
                 [--include-artifacts] [--always-archive]
                 [--exclude-git-metadata] [--incremental]
                 [--fail-on-secrets | --no-fail-on-secrets]
                 [--exclude-secrets | --no-exclude-secrets]
                 [--progress | --no-progress]
                 [--check-space | --no-check-space]
                 [--fail-on-insecure-dest | --no-fail-on-insecure-dest]
                 [--encrypt-passphrase-file FILE | --no-encrypt]
```

备份目标按 **命令行 > 环境变量 `BACKUP_DEST` > 配置文件 `[backup].dest`** 的优先级解析；三者均未提供时报错 `set BACKUP_DEST, pass --backup-dest, or configure [backup].dest`。空的 `BACKUP_DEST` 按未设置处理。备份只接受 `--backup-dest`，恢复仍使用 `--dest`。`--keep N` 的值按 **命令行 > 配置文件 `[backup].keep`** 解析；成功备份后（无失败）会按项目仅保留最近 N 份 manifest 及其引用归档。`[backup].exclude` 中的模式会在 archive 策略归档时跳过匹配的文件/目录（glob，按项目根相对路径逐段匹配，目录匹配则跳过整个子树）；`git` 与 `git_bundle` 策略不受影响。`--incremental` 让 archive 策略生成差异快照（值按 命令行 > 配置文件 `[backup].incremental` 解析，命令行只能开启、配置也可开启）：仅归档相对上一次快照变更/新增的文件与符号链接，记录本快照的完整文件清单 `inventory` 与删除列表 `deleted`（`incremental.base` 指向上一个快照的清单文件名，串成链）；完整快照不写 `incremental`。恢复时先解出链上最近的完整快照，再逐级应用差异（先删除 `deleted` 路径，再叠加差异归档）。`--fail-on-secrets` / `--no-fail-on-secrets` 按显式 flag > `[backup].fail_on_secrets` 解析：开启时 archive 遇敏感路径则拒绝该项目备份（stderr `error: … refusing backup`，非零退出，不写归档）。

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `ROOT...` | 是 | 无 | 一个或多个项目根路径；发现规则与 `scan` 相同，但不启用“未标记目录”。 |
| `--backup-dest DEST` | 条件必填 | `BACKUP_DEST` → `[backup].dest` | 备份根目录；三者均未提供时必须提供。不存在时在实际运行中创建。支持 `~` 展开。 |
| `--config PATH` | 否 | 自动发现 `.proj-janitor.toml` | 显式指定配置文件；未指定时从当前目录向上逐级查找。 |
| `--keep N` | 否 | `[backup].keep` | 成功备份后每个项目保留的最近 manifest 数（含本次）；不传且配置未设置则不保留。必须 ≥ 1。 |
| `--yes` | 否 | 关闭 | `--keep` 生效时跳过交互确认；非交互执行且未加 `--yes` 时只打印保留计划、不删除。 |
| `--dry-run` | 否 | 关闭 | 只展示计划，不创建目录、归档或 manifest（`--keep` 不生效）。 |
| `--include-artifacts` | 否 | 关闭 | 完整归档，不排除构建产物和依赖缓存；同时强制所有项目走归档策略。 |
| `--always-archive` | 否 | 关闭 | 强制所有项目归档，即使满足 GitHub URL-only 记录条件。 |
| `--exclude-git-metadata` | 否 | 关闭 | Rust 0.2.6 起支持；仅对 archive 生效，跳过项目中的 `.git` 路径；恢复后没有 Git 历史/分支/tag/remote。Python 0.1.0 尚不支持。 |
| `--incremental` | 否 | `[backup].incremental` | archive 策略生成差异快照；默认关闭。完整快照归档全部文件并记录 `inventory`；差异快照仅归档相对上一次快照变更/新增的文件与符号链接，并记录 `incremental.base` 与 `deleted`。`git`/`git_bundle` 策略不受影响。 |
| `--fail-on-secrets` | 否 | `[backup].fail_on_secrets` | 检测到常见敏感路径时拒绝 archive 备份（不写归档/manifest，非零退出）。默认关闭时仅 stderr 告警（CR-099）。与 `--no-fail-on-secrets` 互斥。 |
| `--no-fail-on-secrets` | 否 | 无 | 即使配置开启 `fail_on_secrets`，仍仅告警并继续归档。 |
| `--exclude-secrets` | 否 | `[backup].exclude_secrets` | 打包时跳过常见敏感路径（stderr `note: … excluding`）。与 `--fail-on-secrets` 同时开启时优先排除。与 `--no-exclude-secrets` 互斥。 |
| `--no-exclude-secrets` | 否 | 无 | 即使配置开启 `exclude_secrets`，仍将敏感路径打入归档（并按告警/硬失败策略处理）。 |

### 6.2 策略选择

`prepare_backup()` 对每个项目按以下顺序决定策略：

| 顺序 | 条件 | 策略 | 原因字段 |
| --- | --- | --- | --- |
| 1 | `--include-artifacts` | `archive` | `include_artifacts` |
| 2 | `--always-archive` | `archive` | `always_archive` |
| 3 | Git 可恢复性检查通过 | `git` | 无 |
| 4 | 工作区干净、无 stash、非浅克隆、无子模块/LFS，且提交可由本地 refs 携带 | `git_bundle` | Git-only 不适用原因 |
| 5 | 其他情况 | `archive` | 可恢复性或 bundle 不适用原因 |

说明：

- `--include-artifacts` 和 `--always-archive` 都会跳过 Git 可恢复性检查。此时归档记录不会携带 `remote`、`remote_url`、`branch`、`commit`、`upstream` 或 `verified`。
- `--exclude-git-metadata` 不改变源项目；归档中的 `.git` 目录或文件会被跳过；archive 策略启用此选项时，manifest 顶层记录 `exclude_git_metadata: true`，即使源项目没有 `.git`。需要保留 Git 历史时不要启用此项。
- 该选项不强制 archive 策略；若最终选择 Git URL-only，则不会生成本地归档。
- Git 策略记录本地 Git 引用证据（`verified: "local-ref"`）；存在本地 tag 时会联网查询远程 tag，但不抓取远程提交，也不证明远端仍存在该提交。
- Git bundle 记录本地分支、tag 和这些 refs 可达的提交历史，不保存 remote 配置、reflog、stash 或悬空对象；恢复后检出记录的分支/提交且不配置 remote。为避免凭据落入备份，若 Git-only 可恢复性检查发现敏感远程/配置，bundle 也不会覆盖该阻止结果。
- HTTPS GitHub URL 可内嵌 PAT；`remote_url` 在写入记录、manifest 和 CLI 输出前会去除用户名、密码/token、查询字符串和片段。
- 归档策略会记录 `fallback_reason`；如果没有具体原因，使用 `not recoverable from GitHub`。
- Git 可恢复性检查的完整失败原因见第 13.3 节。
- Git bundle 命名为 `<项目名>-<时间戳>.bundle`，清单使用 `strategy: "git_bundle"`，bundle 路径复用 `archive` 字段，并记录 `branch`、`commit`、`sha256`、大小和原 Git-only 不适用原因。

### 6.3 dry-run 行为与输出

`--dry-run` 调用 `prepare_backup()`，只计算每个项目的备份目录、清单路径和计划，不创建任何文件。备份目录由 `--backup-dest` 指定，或在省略该选项时从 `BACKUP_DEST` 读取。
备份目录不能位于待备份项目内部；CLI 和 Python API 会在写入前拒绝这种配置，避免将归档再次收入自身。
归档会保留项目内符号链接；绝对目标或规范化后越出项目根目录的链接会使该项目备份失败。项目内的父相对链接（例如 `../config.toml`）仍可正常备份和恢复。
归档成员路径采用正斜杠；成员路径包含反斜杠/冒号或 Windows 不支持的组件，符号链接目标包含冒号/Windows 不支持的组件，路径在大小写转换或 Unicode NFC 规范化后冲突，或在 POSIX/Windows 任一平台语义下越出项目根目录时，该项目备份会失败。
归档和 manifest 使用排他创建，不会覆盖已有文件；并发备份若争用相同文件名，冲突项目会报错，可重试以生成带新后缀的文件名。

输出格式：

```text
Dry run. Manifest would be: /path/to/backups/demo/manifest-20261023-135243.json
  [git] demo: https://github.com/owner/demo.git @ f5bb224a9cc8
  [archive] old-tool: 12.4 MiB -> /path/to/backups/old-tool/old-tool-20261023-135243.tar.gz (working tree is not clean)
```

- 每个项目各有一行清单路径；Git 行显示脱敏后的远程 URL 和提交 SHA 前 12 位，缺失时分别显示 `-`。
- 归档行显示源体积、项目目录下的目标归档路径和原因。
- dry-run 的退出码为 `0`，除非项目发现阶段有根路径失败（`1`）。

### 6.4 实际运行与输出

非 dry-run 时调用 `run_backup()`：

1. 再次执行 `prepare_backup()` 计算每个项目的计划、项目目录和唯一文件名。
2. 对每个项目创建 `DEST/<项目名>/`；Git-only 策略直接写记录，bundle 策略写 `.bundle`，归档策略写 `tar.gz`，后两者都计算 SHA-256。
3. 每个项目单独写入 `DEST/<项目名>/manifest-<时间戳>.json`，清单只包含该项目的一条记录。
4. 输出摘要：

```text
Backed up 2 project(s).
  [git] demo: https://github.com/owner/demo.git @ f5bb224a9cc8
  [archive] old-tool: 12.4 MiB  sha256:9f2c1a7b3d4e  /path/to/backups/old-tool/old-tool-20261023-135243.tar.gz  (working tree is not clean)
Manifest: /path/to/backups/demo/manifest-20261023-135243.json
Manifest: /path/to/backups/old-tool/manifest-20261023-135243.json
```

归档行中的 SHA-256 只显示前 12 位；完整值在 manifest 中。`fallback_reason` 存在时在末尾以括号显示。批量运行会为每个项目各打印一行 `Manifest:`。

### 6.5 目录与归档命名

- 每个项目使用稳定目录：`DEST/<项目名>/`，不创建按运行时间命名的运行目录。
- 不同根目录下项目名相同、大小写不同或 Unicode 规范化等价时，项目目录名追加 8 位路径哈希，例如 `app-1a2b3c4d/`。
- 归档默认名为 `<项目名>-YYYYmmdd-HHMMSS.tar.gz`，与清单放在同一项目目录。
- Git bundle 默认名为 `<项目名>-YYYYmmdd-HHMMSS.bundle`；先在私有临时目录生成，再独占创建最终文件并计算 SHA-256。
- 同一秒内重复备份同一项目时，清单和归档文件名追加 `-2`、`-3` 等后缀，不覆盖已有文件。
- 归档使用 `tar.add(project.path, arcname=project.name)`，因此归档内顶层目录是项目名。
- 未使用 `--include-artifacts` 时，按 `artifact_patterns()` 排除构建产物和依赖缓存；匹配的是路径中的任一段名称。
- 归档会包含 `.git` 目录；该行为不等同于 `git bundle`，也不保证恢复完整 Git 历史的所有边界情况。若远程 URL 或 `.gitmodules` 内嵌凭据，归档也会保留；备份前应清理或限制归档访问。

### 6.6 退出码与错误

| 情况 | 退出码 | 说明 |
| --- | --- | --- |
| 全部项目处理成功 | `0` | manifest 已写入。 |
| 没有发现项目 | `1` | 标准错误输出 `No projects found.`。 |
| `DEST` 存在但不是目录 | `1` | 标准错误输出 `error: destination is not a directory: {dest}`。 |
| 某项目归档或写 manifest 失败 | `1` | 报错后继续处理其他项目；失败项目的部分输出会尝试清理。 |
| 未提供 `--backup-dest`、未设置 `BACKUP_DEST` 且配置文件无 `[backup].dest` | `1` | 标准错误输出 `set BACKUP_DEST, pass --backup-dest, or configure [backup].dest`。 |
| 参数缺失或非法 | `2` | `argparse` 用法错误。 |

### 6.7 示例

```bash
projjan backup ~/projects/demo --backup-dest ~/backups/projects --dry-run
projjan backup ~/projects --backup-dest ~/backups/projects
projjan backup ~/projects --backup-dest ~/backups/projects --include-artifacts
projjan backup ~/projects --backup-dest ~/backups/projects --always-archive
```

也可设置默认目录后省略该选项：

```bash
export BACKUP_DEST=~/backups/projects
projjan backup ~/projects --dry-run
```

## 7. `clean`

### 7.1 语法与参数

```text
projjan clean ROOT [ROOT ...] [--rules RULES] [--apply] [--yes]
                [--json] [--list-rules]
```

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `ROOT...` | 是 | 无 | 一个或多个项目根路径；发现规则与 `backup` 相同。 |
| `--rules RULES` | 否 | 全部规则 | 逗号分隔的规则名；空项被忽略，全部为空时报错。 |
| `--apply` | 否 | 关闭 | 真正删除计划项；未提供时仅 dry-run。 |
| `--yes` | 否 | 关闭 | 跳过交互确认；仍需要 `--apply`。 |
| `--json` | 否 | 关闭 | 输出计划 JSON。注意其与 dry-run/确认文本混用。 |
| `--list-rules` | 否 | 关闭 | 打印规则及模式后退出 `0`。 |

`--list-rules` 仍受 `ROOT...` 必填约束。由于实现先检查 `--list-rules`、后收集项目，提供任意占位根路径即可打印规则，根路径本身不会被验证。

### 7.2 清理规则

规则定义于 `config.ARTIFACT_RULES`，顺序固定：

| 规则 | 匹配名称 |
| --- | --- |
| `python-cache` | `__pycache__`、`.pytest_cache`、`.mypy_cache`、`.ruff_cache`、`*.pyc`、`.coverage`、`htmlcov` |
| `python-env` | `.venv`、`venv`、`.tox`、`.nox`、`*.egg-info` |
| `node` | `node_modules`、`.next`、`.nuxt`、`.svelte-kit`、`.turbo`、`.parcel-cache`、`coverage` |
| `build` | `build`、`dist`、`out`、`target`、`CMakeFiles`、`.gradle` |
| `editor` | `.cache`、`.DS_Store`、`Thumbs.db` |

`--list-rules` 输出格式：

```text
python-cache: __pycache__, .pytest_cache, .mypy_cache, .ruff_cache, *.pyc, .coverage, htmlcov
python-env: .venv, venv, .tox, .nox, *.egg-info
node: node_modules, .next, .nuxt, .svelte-kit, .turbo, .parcel-cache, coverage
build: build, dist, out, target, CMakeFiles, .gradle
editor: .cache, .DS_Store, Thumbs.db
```

### 7.3 清理计划语义

`build_cleanup_plan(project, rule_names)`：

- 把项目路径解析为绝对路径；
- 从项目根开始递归遍历，`followlinks=False`；
- 遇到 `.git` 目录直接跳过，不进入、不删除；
- 跳过符号链接目录和符号链接文件；
- 目录名匹配某规则时，记录目录并停止递归进入该目录；
- 文件名匹配某规则时，记录文件；
- 同一路径只按规则顺序匹配一次，规则按配置顺序排列；
- 目录体积使用 `measure_tree()` 递归统计；文件体积使用 `stat(follow_symlinks=False).st_size`；
- Python 和 Rust 在遍历或体积统计遇到错误时报告失败；`clean --apply` 有任何根目录或计划失败时不会删除部分计划；
- 最终按 `(project 字符串, path 字符串)` 排序。

`--rules` 只做非空逗号拆分，未知规则在计划阶段由 `resolve_rules()` 校验。未知规则错误格式为：

```text
unknown rule(s): foo, bar; available: python-cache, python-env, node, build, editor
```

CLI 会在每个项目上捕获该 `ValueError` 并输出：

```text
error: {项目名}: unknown rule(s): ...
```

### 7.4 文本输出

无计划项时，标准输出：

```text
Nothing to clean.
```

有计划项时：

```text
PROJECT  RULE          KIND  SIZE      PATH
-------  ------------  ----  --------  ------------------------------
demo     python-cache  dir   1.2 MiB   /home/user/projects/demo/__pycache__
demo     build         dir   3.4 MiB   /home/user/projects/demo/dist

2 item(s), 4.6 MiB reclaimable.
```

dry-run 且存在计划项时，表格和汇总之后还会输出：

```text
Dry run. Re-run with --apply to delete these items.
```

### 7.5 JSON 输出

`--json` 时 stdout 只输出计划数组，字段见第 10.2 节。确认提示、执行摘要和错误写入 stderr，因此 stdout 可直接交给 JSON 解析器：

| 场景 | 标准输出内容 |
| --- | --- |
| `--json`，dry-run，有计划项 | 仅 JSON 数组 |
| `--json`，dry-run，无计划项 | 仅 `[]` |
| `--json --apply`，非交互且无 `--yes` | JSON 数组，随后拒绝删除信息在标准错误，退出 `2` |
| `--json --apply --yes` | stdout 为 JSON 数组；执行摘要写入 stderr |
| `--json --apply`，交互确认 | stdout 为 JSON 数组；确认提示写入 stderr |

修复自提交 `c4fa6d9` 起，stdout 保持单一 JSON 文档；自动化调用方仍应检查退出码并处理 stderr 错误信息。

### 7.6 删除与确认

- 只有 `--apply` 才会进入删除流程。
- 没有计划项时，即使提供 `--apply` 也不删除，退出码取决于此前项目发现/计划错误。
- 未提供 `--yes` 时：
  - 标准输入不是 TTY，输出 `error: refusing to delete without --yes in a non-interactive session` 到标准错误，退出 `2`；
  - 是 TTY 时提示 `Delete N item(s)? [y/N] `；
  - 输入 `y` 或 `yes`（去除首尾空白、忽略大小写）才继续；
  - 其他输入输出 `Aborted.` 并退出 `2`。
- `apply_cleanup()` 对每个计划项：
  - POSIX 上打开并核对计划时记录的项目根身份，逐层打开目标父目录，通过目录文件描述符相对删除；拒绝越界目标和作为计划目标的符号链接；
  - 递归删除时不跟随目录树内的符号链接，并在打开目标目录后用 `fstat` 再核对身份；Windows Python 使用根目录身份及路径校验回退；
  - 目标已不存在时跳过，不计数；
  - 单个 `OSError`/`ValueError` 记录到 `errors`，继续处理后续项。
- 成功摘要：

```text
Removed 2 item(s), freed 4.6 MiB.
```

逐项错误随后以 `error: {path}: {message}` 写入标准错误。

### 7.7 退出码与错误

| 情况 | 退出码 | 说明 |
| --- | --- | --- |
| 计划/删除全部成功 | `0` | 无计划项或成功删除均可能为 `0`。 |
| 根路径失败、未知规则、计划构建失败 | `1` | 错误到标准错误；其他项目继续处理。 |
| 删除阶段有逐项错误 | `1` | 已成功项保留，错误汇总到标准错误。 |
| 非交互环境未提供 `--yes` 就 `--apply` | `2` | 拒绝删除。 |
| 交互确认输入非 `y`/`yes` | `2` | 输出 `Aborted.` |
| 参数缺失或非法 | `2` | `argparse` 用法错误。 |
| `--list-rules` | `0` | 打印规则后直接返回。 |

### 7.8 示例

```bash
projjan clean ~/projects
projjan clean ~/projects --rules node,python-cache
projjan clean ~/projects --json
projjan clean ~/projects --apply
projjan clean ~/projects --apply --yes
projjan clean . --list-rules
```

## 8. `restore`

### 8.1 语法与参数

```text
projjan restore MANIFEST [--dest DIR] [--project NAME] [--decrypt-passphrase-file FILE]
                   [--dry-run] [--no-verify] [--yes]
                   [--check-remote | --no-check-remote]
                   [--require-dest | --no-require-dest]
                   [--json]
```

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `MANIFEST` | 是 | 无 | `backup` 生成的 schema v2 清单路径。 |
| `--dest DIR` | 否 | 原始位置 | 恢复父目录；指定后恢复到 `DIR/<项目名>/`。省略时恢复到每条记录的 `source`。支持 `~` 展开。 |
| `--project NAME` | 否 | 全部记录 | 只恢复指定项目；可重复指定。 |
| `--dry-run` | 否 | 关闭 | 只校验并展示计划，不创建恢复目录。 |
| `--no-verify` | 否 | 关闭 | 跳过归档 SHA-256 校验；不推荐用于不可信归档。 |
| `--yes` | 否 | 关闭 | 原始位置恢复时跳过交互确认；非交互环境或 `--json` 的实际恢复必须显式提供。`--dest` 模式不使用该确认。 |
| `--check-remote` | 否 | `[restore].check_remote` | 开始恢复前对 Git-only 记录执行一次 `git ls-remote` 远端引用预检（同 `verify --check-remote`），并对记录的子模块逐项预检；任一记录或子模块远端不可达时整批不开始并返回 `1`。不下载对象、不改动 manifest。 |
| `--require-dest` | 否 | `[restore].require_dest` | 必须提供 `--dest`；省略时拒绝原位恢复并返回 `1`（缓解 R-14）。与 `--no-require-dest` 互斥。 |
| `--json` | 否 | 关闭 | 输出纯 JSON，不混入人类提示文本。 |

### 8.2 行为

1. 读取 UTF-8 JSON manifest，要求 `schema_version` 为 `2`，`records` 为非空对象数组。
2. 校验项目名不含绝对路径、`/`、`\`、`..` 或 NUL；还原目标在大小写或 Unicode 规范化后重合时拒绝恢复。
3. 未指定 `--dest` 时，逐条读取记录的 `source` 作为目标：
   - `source` 必须是非空、绝对路径，不能包含 NUL 或 `..`；
   - `source` 的文件名必须等于项目名，不能是文件系统根；
   - 解析父目录；父目录存在但不是目录时拒绝；
   - 目标已存在或是符号链接时拒绝覆盖。
4. 指定 `--dest` 时，目标固定为 `--dest/<项目名>/`；`--dest` 已存在但不是目录时拒绝。
5. 同一批恢复中目标路径重复或在大小写转换/Unicode NFC 规范化后冲突时拒绝，避免跨平台覆盖或中途失败。
6. 预检所有目标路径后才会开始恢复；任一目标已存在或是符号链接时整批不开始。
7. `git` 策略：校验远程 URL 形式和 7～64 位十六进制提交号；初始化临时仓库，将脱敏 URL 配置为 origin；使用 Git 临时配置环境从原始 URL fetch heads/tags 并禁写 `FETCH_HEAD`；执行 `git checkout --detach <commit>` 并验证 HEAD，最后移动到目标目录。旧版 Git 不支持该环境配置时，只有不含敏感信息的 URL 才回退到命令参数传递。
8. `git_bundle` 策略：校验 bundle SHA-256、结构、分支和提交；将 bundle 复制到私有临时目录后 fetch 本地 heads/tags，检出记录的分支与提交并核对 HEAD，最后发布到目标路径。不会写入 remote URL。
9. `archive` 策略：默认按 manifest 中的 SHA-256 校验归档；校验通过后解压到临时目录，要求顶层目录名等于项目名，拒绝绝对路径、重复成员、未知成员类型、反斜杠成员名、Windows 保留设备名/非法字符/尾随点空格、含冒号的成员路径/链接目标、POSIX/Windows 分隔符下的 `..`、大小写或 Unicode 规范化后重合的归档路径、硬链接、特殊文件和越界符号链接，再移动到目标目录。
10. 原始位置模式在规划完成后要求确认：交互终端列出目标并读取 `y/N`；标准输入不是 TTY 且未给 `--yes` 时拒绝；`--json` 的实际恢复未给 `--yes` 时拒绝。dry-run 不需要确认。
11. 多项目按顺序恢复；中途失败时不回滚已恢复项目，错误会列出已完成项目和目标，临时目录会清理。
12. `--check-remote` 时（仅 `git` 策略；`archive`/`git_bundle` 忽略该选项），在每个 Git-only 计划进入恢复前先执行一次 `git ls-remote`（单条最长 30 秒，`GIT_TERMINAL_PROMPT=0`，参数列表调用不经 shell），只读取引用列表、不下载对象；URL 含用户名/口令/query/fragment 时为 `remote-skipped` 不联网。对记录的子模块（`submodules` 字段），对每个子模块的 URL+固定提交同样执行一次预检（含凭据脱敏）。任一记录或子模块 `remote-unreachable` 时整批不开始，报 `cannot restore <项目>: remote <脱敏URL> is unreachable (<原因>)`（子模块则为 `cannot restore <项目>: submodule <path> remote <脱敏URL> is unreachable (<原因>)`）并返回退出码 `1`。该预检发生在 SHA-256 校验与确认之后、创建任何目标目录之前。
13. Git-only 恢复后是 detached HEAD，不自动创建或切换分支；默认不联网预检远程是否仍包含该提交，需要时加 `--check-remote`。Git bundle 恢复回原始分支并清除远程配置。
14. 原始位置的 `source` 与备份时的机器和目录布局绑定；跨机器、跨用户或目录已迁移时应使用 `--dest`。

### 8.3 输出

人类可读 dry-run（指定 `--dest`）：

```text
Restore plan: 1 project(s)
  [archive] demo: /path/to/backups/demo/demo-20261023-135243.tar.gz (sha256 verified) -> /path/to/restored/demo
Dry run. Re-run without --dry-run to restore.
```

省略 `--dest` 的 dry-run 会额外提示目标来自原始位置：

```text
Restore plan: 1 project(s)
Targets are the original backup locations:
  [archive] demo: /path/to/backups/demo/demo-20261023-135243.tar.gz (sha256 verified) -> /home/user/projects/demo
Dry run. Re-run without --dry-run to restore.
```

原始位置实际恢复前的交互确认：

```text
About to restore to original locations:
  /home/user/projects/demo
Restore 1 project(s)? [y/N]
```

实际恢复：

```text
Restored 1 project(s).
  [archive] demo -> /home/user/projects/demo
```

Git 项目计划行格式为 `[git] <项目名>: <脱敏URL> @ <12位提交> -> <目标路径>`。

`--json` 的顶层结构：

```json
{
  "dry_run": true,
  "mode": "original",
  "projects": [
    {
      "project": "demo",
      "strategy": "archive",
      "target": "/home/user/projects/demo",
      "archive": "/path/to/backups/demo/demo-20261023-135243.tar.gz",
      "remote_url": null,
      "commit": null,
      "sha256": "9f2c...",
      "sha256_verified": true,
      "in_place": true
    }
  ]
}
```

`mode` 为 `"original"`（省略 `--dest`）或 `"dest"`（指定 `--dest`）；`in_place` 只在 dry-run 计划中出现。非 dry-run 时 `projects` 元素只包含 `project`、`strategy` 和 `target`；`dry_run` 为 `false`。

### 8.4 退出码与错误

| 情况 | 退出码 | 说明 |
| --- | --- | --- |
| 全部恢复成功或 dry-run 成功 | `0` | 目标目录已创建或仅输出计划。 |
| manifest 不存在/不可读/结构非法 | `1` | 标准错误输出 `error: ...`。 |
| `--project` 指定的项目不存在 | `1` | 不开始恢复。 |
| 项目名不安全、`source` 非法或目标重复 | `1` | 不开始恢复。 |
| 目标已存在或为符号链接 | `1` | 拒绝覆盖，不开始恢复。 |
| 归档缺失、SHA-256 不匹配、解压失败 | `1` | 不创建目标目录。 |
| Git URL/提交非法、init/fetch/checkout/HEAD 校验失败 | `1` | 清理临时目录，不创建目标目录。 |
| `--check-remote` 探测到任一 Git-only 远端或子模块远端不可达 | `1` | 报 `cannot restore ... is unreachable`，不创建目标目录。 |
| 原始位置恢复未给 `--yes` 且非交互 | `2` | 标准错误输出拒绝原因。 |
| 原始位置恢复使用 `--json` 但未给 `--yes` | `2` | 标准输出为空，不写文件。 |
| 用户在确认时输入 `n` 或 EOF | `2` | 输出 `Aborted.`，不写文件。 |
| 参数缺失或非法 | `2` | `argparse` 用法错误。 |

### 8.5 示例

```bash
# 恢复到 manifest 记录的原始位置；交互确认
projjan restore ~/backups/projects/xtask_todo/manifest-20261023-122828.json

# 非交互执行原始位置恢复
projjan restore <manifest> --yes

# 恢复到副本目录
projjan restore <manifest> --dest ~/restored
projjan restore <manifest> --dest ~/restored --dry-run
projjan restore <manifest> --dest ~/restored --project xtask_todo
projjan restore <manifest> --dest ~/restored --json --dry-run

# 恢复前确认每个 Git-only 远端仍可访问；不可达则整批不开始
projjan restore <manifest> --dest ~/restored --check-remote
```

## 9. 退出码

| 退出码 | 通用含义 | 典型场景 |
| --- | --- | --- |
| `0` | 成功 | 扫描完成；备份及 manifest 写入完成；恢复完成或 dry-run 成功；清理计划为空或删除完成；`--version`/`--help`/`--list-rules`。 |
| `1` | 部分失败或无可处理对象 | 根路径错误；`backup` 无项目；`DEST` 不是目录；manifest/归档无效；SHA-256 不匹配；恢复目标已存在；Git 恢复失败；`verify --check-remote` 或 `restore --check-remote` 存在不可达远端；清理或 prune 计划/删除出现错误。 |
| `2` | 用法错误或拒绝执行 | `argparse` 参数错误；缺少子命令；非交互 clean/prune apply 未给 `--yes`；JSON prune apply 未给 `--yes`；用户取消确认。 |
| `130` / `143` | POSIX 信号中断 | 分别收到 SIGINT / SIGTERM；Git 恢复会先尝试终止 Git 进程树并清理临时目录。 |

补充：

- `scan` 的“没有项目”本身不是错误；只有根路径失败才返回 `1`。
- `backup` 的“没有项目”返回 `1`，且不检查目标目录。
- `restore` 的 manifest/归档校验、`source` 非法、目标已存在、目标重复和 Git 恢复失败均返回 `1`；原始位置恢复缺少 `--yes`、用户取消或参数错误返回 `2`。
- `verify` 在任一记录为 `remote-unreachable` 时返回 `1`（标准错误输出 `error: one or more remotes could not be reached`），但仍会先输出完整报告；`remote-moved` 和 `remote-skipped` 返回 `0`。
- `clean` 的 `--list-rules` 在参数解析通过后直接返回 `0`，不验证根路径。
- Python API 不统一返回退出码；调用方需要捕获异常或检查返回对象。

## 10. JSON 输出

### 10.1 `scan --json` 数组元素

顶层为 JSON 数组。每个元素结构：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string | 项目目录名。 |
| `path` | string | 解析后的绝对路径。 |
| `size_bytes` | integer | 普通文件字节数之和，不跟随符号链接。 |
| `last_modified` | string \| null | 带本地时区偏移的 ISO 8601；无文件时间为 `null`。 |
| `markers` | array[string] | 识别到的项目标记，按 `PROJECT_MARKERS` 顺序。 |
| `git` | object \| null | 无 `.git` 时为 `null`；否则包含 `branch` 和 `dirty`。 |
| `git.branch` | string \| null | 当前分支名；读取失败为 `null`。 |
| `git.dirty` | boolean \| null | `true` 有改动、`false` 干净、`null` 无法判断。 |

示例：

```json
[
  {
    "name": "demo",
    "path": "/home/user/projects/demo",
    "size_bytes": 18432,
    "last_modified": "2026-10-20T18:42:11.123456+08:00",
    "markers": [".git", "pyproject.toml"],
    "git": {"branch": "main", "dirty": false}
  },
  {
    "name": "notes",
    "path": "/home/user/projects/notes",
    "size_bytes": 4096,
    "last_modified": null,
    "markers": [],
    "git": null
  }
]
```

说明：

- 没有 `schema_version` 字段；字段集合是 v0.1.0 的实现结果。
- 根路径错误不会进入数组；请同时读取标准错误和退出码。
- `size_bytes` 不是 `du` 的磁盘块占用。
- `last_modified` 的时区偏移由运行环境决定，不是固定 UTC。

### 10.2 `clean --json` 数组元素

顶层为 JSON 数组。每个元素结构：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `project` | string | 项目绝对路径（`CleanupAction.project` 的字符串形式）。 |
| `path` | string | 计划删除项的绝对路径。 |
| `rule` | string | 命中的规则名。 |
| `kind` | string | `"dir"` 或 `"file"`。 |
| `size_bytes` | integer | 目录为树内文件字节数；文件为 `st_size`。 |

示例：

```json
[
  {
    "project": "/home/user/projects/demo",
    "path": "/home/user/projects/demo/node_modules",
    "rule": "node",
    "kind": "dir",
    "size_bytes": 1048576
  },
  {
    "project": "/home/user/projects/demo",
    "path": "/home/user/projects/demo/dist/bundle.js",
    "rule": "build",
    "kind": "file",
    "size_bytes": 32768
  }
]
```

说明：

- 没有 `schema_version` 字段。
- `clean --json` 的 stdout 为单个 JSON 文档；确认提示、摘要和错误写入 stderr。详见第 7.5 节。
- 计划项按 `(project, path)` 字符串排序；不保证按体积或规则名排序。
- `size_bytes` 是计划时估算值；删除前文件可能变化，实际释放空间以操作系统结果为准。执行前会比较计划时记录的目标身份；目标路径被替换时会报错并跳过。

### 10.3 `restore --json` 对象

顶层为对象，包含 `dry_run`、`mode` 和 `projects` 数组：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `dry_run` | boolean | 是否为 dry-run。 |
| `mode` | string | `"original"`（省略 `--dest`）或 `"dest"`（指定 `--dest`）。 |
| `projects[].project` | string | 项目名。 |
| `projects[].strategy` | string | `"git"`、`"git_bundle"` 或 `"archive"`。 |
| `projects[].target` | string | 恢复目标绝对路径。 |
| `projects[].archive` | string/null | artifact 路径；Git-only 策略为 `null`。 |
| `projects[].remote_url` | string/null | 脱敏后的远程 URL；`git_bundle` 和 archive 为 `null`。 |
| `projects[].commit` | string/null | manifest 中的提交号；archive 策略为 `null`。 |
| `projects[].sha256` | string/null | manifest 中的归档或 bundle 哈希；Git-only 策略为 `null`。 |
| `projects[].sha256_verified` | boolean | dry-run 计划是否已完成哈希校验；非 dry-run 结果不包含该字段。 |
| `projects[].in_place` | boolean | 是否为原始位置模式；只在 dry-run 计划中出现。 |

非 dry-run 时 `projects` 元素只包含 `project`、`strategy` 和 `target`；`mode` 仍保留。

### 10.4 `verify --json` 对象

顶层为对象，包含 `manifest` 和 `projects` 数组：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `manifest` | string | 解析后的清单绝对路径。 |
| `projects[].project` | string | 项目名。 |
| `projects[].strategy` | string | `"git"`、`"git_bundle"` 或 `"archive"`。 |
| `projects[].status` | string | archive/`git_bundle` 为 `"verified"`；Git-only 默认 `"metadata-only"`，加 `--check-remote` 后为下表中的远端状态。 |
| `projects[].archive` | string/null | artifact 路径；Git-only 策略为 `null`。 |
| `projects[].sha256` | string/null | 实测归档摘要；Git-only 策略为 `null`。 |
| `projects[].commit` | string/null | manifest 中的提交号；archive 策略为 `null`。 |
| `projects[].remote` | object/缺失 | 仅 `verify --check-remote` 的 Git-only 记录出现；其它记录和默认模式都不含该字段。 |
| `projects[].remote.url` | string | 脱敏后的远端 URL。 |
| `projects[].remote.branch` | string/null | manifest 记录的分支；缺失或为空时为 `null`。 |
| `projects[].remote.reason` | string/null | `no-ref-matches-commit`、`ls-remote-failed`、`ls-remote-timed-out`、`credentials-in-remote-url`；`remote-verified` 时为 `null`。 |
| `projects[].remote.refs` | array[string] | 指向该提交的远端引用，按字典序去重；注解 tag 已去掉 `^{}` 后缀。非 `remote-verified` 时为空数组。 |
| `projects[].submodules` | array[object]/缺失 | 仅 `verify --check-remote` 且 Git-only 记录含 `submodules` 时出现：每项 `{path, url, branch, reason, refs, status}`，`status` 为下表中的子模块远端状态，`reason`/`refs` 含义与 `remote.*` 相同。 |

Git-only 记录的 `--check-remote` 状态：

| `status` | 含义 |
| --- | --- |
| `remote-verified` | 至少一个远端引用指向记录的提交。 |
| `remote-moved` | 远端可达，但没有引用指向该提交；恢复按 SHA 抓取，提交仍可能从其它引用可达。 |
| `remote-unreachable` | `git ls-remote` 失败或超过 30 秒，无法确认远端；命令返回 `1`。 |
| `remote-skipped` | URL 含凭据、query 或 fragment；不会把该 URL 传给 Git，也不联网。 |

Git-only 记录的 `projects[].status` 在含 `submodules` 且任一子模块远端为 `remote-unreachable` 时同样为 `remote-unreachable`（项目级 `remote` 对象仅含 `url`/`branch`/`reason`/`refs`，子模块状态见 `projects[].submodules`）。

### 10.5 JSON 使用建议

- 把标准输出与标准错误分开采集。
- 同时检查退出码：`scan` 可能输出部分结果但返回 `1`；`clean --json` 的 stdout 保持纯 JSON，错误信息写入 stderr。
- 不要依赖字段顺序；不要假设未来版本一定保留同样的字段集合。
- 处理路径时按 JSON 字符串解析，不要按空白或分隔符二次切分。
- 对 `last_modified` 使用支持时区偏移的 ISO 8601 解析器。

## 11. 备份清单 `manifest-<timestamp>.json`（schema v2）

### 11.1 顶层结构

`DEST/<项目名>/manifest-<时间戳>.json` 使用 UTF-8、缩进 2、末尾换行。每个清单只包含当前项目的一条记录。顶层字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | integer | 固定为 `2`。 |
| `created_at` | string | manifest 生成时的 UTC ISO 8601 时间。 |
| `tool` | string | 固定为 `"proj-janitor"`。 |
| `version` | string | 当前工具版本，`"0.1.0"`。 |
| `include_artifacts` | boolean | 本次运行是否使用 `--include-artifacts`。 |
| `always_archive` | boolean | 本次运行是否使用 `--always-archive`。 |
| `exclude_git_metadata` | boolean | 该项目使用 archive 策略且启用排除选项时为 `true`；此值记录选项状态，不表示源项目一定含有 `.git`；旧 manifest 缺省时视为 `false`。 |
| `records` | array[object] | 每个清单只包含当前项目的一条记录；批量运行按项目分别写清单。 |

### 11.2 记录字段

记录由 `BackupRecord` 经 `dataclasses.asdict()` 序列化，所有记录包含相同字段集合：

| 字段 | 类型 | Git 记录 | Git bundle 记录 | 归档记录 |
| --- | --- | --- | --- | --- |
| `project` | string | 项目名 | 项目名 | 项目名 |
| `source` | string | 项目绝对路径 | 项目绝对路径 | 项目绝对路径 |
| `archive` | string \| null | `null` | bundle 绝对路径 | 归档绝对路径 |
| `archive_bytes` | integer | `0` | bundle 文件大小 | 归档文件大小 |
| `source_bytes` | integer | 扫描时源体积 | 扫描时源体积 | 扫描时源体积 |
| `sha256` | string \| null | `null` | bundle 完整 SHA-256 | 归档完整 SHA-256 |
| `strategy` | string | `"git"` | `"git_bundle"` | `"archive"` |
| `remote` | string \| null | Git remote 名，如 `origin` | `null` | `null` |
| `remote_url` | string \| null | 经校验并脱敏的 GitHub URL | `null` | `null` |
| `branch` | string \| null | 本地分支名 | bundle 时的活动分支 | `null` |
| `commit` | string \| null | `HEAD` 提交 SHA | bundle 时的 `HEAD` SHA | `null` |
| `upstream` | string \| null | 上游引用，如 `origin/main` | `null` | `null` |
| `verified` | string \| null | `"local-ref"` | `"local-bundle"` | `null` |
| `fallback_reason` | string \| null | `null` | Git-only 不适用原因 | 归档原因 |
| `inventory` | array[object] \| null | `null` | `null` | 本快照的完整文件清单：每项 `{path, sha256, size}`（path 为项目根相对、POSIX 分隔） |
| `incremental` | object \| null | `null` | `null` | 差异快照才有：`{base: <上一快照清单文件名>, deleted: [path...]}`；完整快照为 `null` |
| `submodules` | array[object] \| null | Git 记录：`[{path, url, commit}]`，为备份时 `.gitmodules` 中的每个子模块记录其路径、远程 URL 与父树中固定的 gitlink 提交 | `null` | `null` |
| `lfs_objects` | array[object\|string] \| null | Git 记录：新备份为 `[{oid,size}]`（`git lfs ls-files --long` + `HEAD:<path>` pointer 的精确字节数）；旧清单中的裸 OID 字符串仍可读。`verify`/`restore --check-remote` 会探测可用性（本地对象库或 HTTPS LFS batch，不下载） | `null` | `null` |

`fallback_reason` 的常见值：

- `include_artifacts`
- `always_archive`
- Git 可恢复性失败原因（见第 13.3 节）
- `not recoverable from GitHub`

### 11.3 示例

```json
{
  "schema_version": 2,
  "created_at": "2026-10-22T11:30:00.123456+00:00",
  "tool": "proj-janitor",
  "version": "0.1.0",
  "include_artifacts": false,
  "always_archive": false,
  "exclude_git_metadata": false,
  "records": [
    {
      "project": "demo",
      "source": "/home/user/projects/demo",
      "archive": null,
      "archive_bytes": 0,
      "source_bytes": 18432,
      "sha256": null,
      "strategy": "git",
      "remote": "origin",
      "remote_url": "https://github.com/owner/demo.git",
      "branch": "main",
      "commit": "f5bb224a9cc8541523019a569e13f50dd86944f9",
      "upstream": "origin/main",
      "verified": "local-ref",
      "fallback_reason": null
    },
    {
      "project": "old-tool",
      "source": "/home/user/projects/old-tool",
      "archive": "/home/user/backups/old-tool/old-tool-20261022-193000.tar.gz",
      "archive_bytes": 13002342,
      "source_bytes": 22984704,
      "sha256": "9f2c1a7b3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcd",
      "strategy": "archive",
      "remote": null,
      "remote_url": null,
      "branch": null,
      "commit": null,
      "upstream": null,
      "verified": null,
      "fallback_reason": "working tree is not clean"
    }
  ]
}
```

### 11.4 语义与限制

- `created_at` 使用 `datetime.now(timezone.utc).isoformat()`，通常带 `+00:00` 偏移。
- `archive_bytes` 是归档文件自身大小；`source_bytes` 是备份前扫描到的源目录普通文件字节数。默认排除构建产物时，二者不可直接比较。
- `sha256` 是归档文件的 SHA-256，不是源文件清单，也不是对归档内容逐文件的哈希。
- `remote` 是 Git remote 名称，`remote_url` 是通过语法校验的 URL；二者不同。
- Git 记录的 `verified: "local-ref"` 只表示本地引用检查通过；存在本地 tag 时会查询远程 tag，但不表示已联网确认远端存在该提交。
- 归档记录不保存 Git 可恢复性检查的详细字段；只保存 `fallback_reason`。
- 每个项目处理完成后写入自己的 manifest；如果中途异常，可能只有部分归档或部分 manifest（DEF-003）。
- manifest 可能包含本地绝对路径、Git URL、分支和提交信息；归档包含项目跟踪文件和 `.git/config`，其中可能有敏感内容或凭据（包括 `.gitmodules`）。工具不加密、不做秘密扫描。
- `restore` 读取 schema v2 manifest；Git 策略在临时仓库 fetch heads/tags 后检出指定提交，归档策略校验 SHA-256 后安全解压。指定 `--dest` 时恢复到 `DIR/<项目名>/`；省略时使用记录中的 `source` 作为目标，因此 manifest 可影响写入位置，必须来自可信来源。恢复不覆盖已有目标，也不会回滚部分完成的批量恢复。

## 12. Python 内部 API

### 12.1 扫描模块 `proj_janitor.scanner`

| 函数 | 签名 | 返回/异常 | 说明 |
| --- | --- | --- | --- |
| `discover_projects` | `(root: Path, include_unmarked: bool = False) -> list[Project]` | `list[Project]`；根不存在时 `FileNotFoundError`，不是目录时 `NotADirectoryError`，遍历错误可能为 `OSError` | 展开 `~`，按第 5.3 节规则发现并扫描项目。 |
| `scan_project` | `(path: Path) -> Project` | `Project` | 解析路径，收集标记、体积、修改时间和 Git 状态。 |
| `measure_tree` | `(path: Path) -> tuple[int, datetime \| None]` | 字节数、带时区本地时间或 `None`；遍历/元数据错误抛 `OSError` | 不跟随符号链接，只统计普通文件。 |
| `read_git_state` | `(path: Path) -> GitState \| None` | `GitState` 或 `None` | 无 `.git` 返回 `None`；Git 命令失败可能产生空字段。 |
| `is_github_remote_url` | `(remote: str) -> bool` | `bool` | 纯语法校验；HTTPS 允许内嵌 PAT；不联网、不验证仓库存在。 |
| `sanitize_remote_url` | `(remote: str) -> str` | 可持久化 URL | 去除 HTTPS userinfo、查询字符串和片段；非 HTTPS 原样返回。 |
| `inspect_git_recoverability` | `(project: Project, patterns: Iterable[str] = ()) -> GitRecovery` | `GitRecovery` | 执行第 13.3 节检查；存在本地 tag 时调用 `git ls-remote --tags --refs`；失败以 `reason` 返回。 |
| `_verify_local_tags` | `(path: Path, remote: str) -> str \| None` | 通过返回 `None`，否则返回首个 tag 失败原因 | 比较本地 tag 与远程同名 tag 的原始对象 ID。 |
| `_remote_tag_refs` | `(path: Path, remote: str) -> dict[str, str] \| None` | tag 名到对象 ID 的映射，查询失败返回 `None` | 解析 `git ls-remote --tags --refs`。 |
| `format_projects` | `(projects: Iterable[Project]) -> str` | 文本表格 | 空输入返回表头和分隔线，不返回 `No projects found.`。 |
| `_git_run` / `_git_output` / `_git_ok` / `_git_count` | 私有辅助函数 | 子进程结果/字符串/布尔/整数或 `None` | 固定 10 秒超时；捕获 `OSError` 和 `SubprocessError`。 |

### 12.2 备份模块 `proj_janitor.backup`

| 函数 | 签名 | 返回/异常 | 说明 |
| --- | --- | --- | --- |
| `prepare_backup` | `(projects, backup_dest: Path, include_artifacts=False, always_archive=False, exclude_git_metadata=False) -> tuple[list[Path], list[BackupPlan]]` | 每项目 manifest 路径、计划列表 | 备份根目录参数为 `backup_dest`；只计算，不创建目录。 |
| `run_backup` | `(projects, backup_dest: Path, include_artifacts=False, dry_run=False, always_archive=False, exclude_git_metadata=False, exclude_patterns=(), incremental=False, fail_on_secrets=False) -> tuple[list[Path], list[BackupRecord], list[tuple[str, str]]]` | 每项目 manifest 路径、记录列表、失败列表 | `exclude_git_metadata=True` 时 archive 不打包 `.git`；`exclude_patterns` 为 `[backup].exclude` 的 glob 模式，archive 归档时跳过匹配文件/目录；`incremental=True` 时对 archive 策略生成差异快照（仅归档相对上次快照变更/新增文件并记录 `inventory`/`incremental`），Git 策略本身不产生归档；`fail_on_secrets=True` 时 archive 遇敏感路径则记入失败列表且不写归档。 |
| `backup_project` | `(project, archive_dir: Path, patterns=(), archive_name=None, fallback_reason=None, exclude_git_metadata=False) -> BackupRecord` | 归档记录 | `exclude_git_metadata=True` 时输出工作树快照，不含 Git 历史。 |
| `sha256_file` | `(path: Path) -> str` | 十六进制 SHA-256 | 以 1 MiB 块读取。 |
| `_make_filter` | 私有 | tarfile 过滤器 | 校验符号链接目标，并按路径段匹配排除模式。 |
| `_project_directory_names` | 私有 | 项目路径到稳定目录名的映射 | 同名、大小写不同或 Unicode 规范化等价的项目追加 8 位路径哈希。 |
| `_unique_file` / `_unique_artifact_name` | 私有 | 候选文件路径/归档名 | 已存在时追加 `-2`、`-3`；实际写入采用排他创建，竞态冲突记为该项目失败，不覆盖原文件。 |

### 12.3 清理模块 `proj_janitor.cleaner`

| 函数 | 签名 | 返回/异常 | 说明 |
| --- | --- | --- | --- |
| `resolve_rules` | `(rule_names=None) -> Mapping[str, tuple[str, ...]]` | 规则映射；未知规则抛 `ValueError` | `None` 返回全部规则；否则按请求顺序构造子集。 |
| `build_cleanup_plan` | `(project: Path, rule_names=None) -> list[CleanupAction]` | 计划列表；遍历或 stat 错误抛 `OSError`，未知规则抛 `ValueError` | 解析项目路径，递归遍历并排序；不返回不完整计划。 |
| `apply_cleanup` | `(actions: Iterable[CleanupAction]) -> CleanupResult` | `CleanupResult` | 删除计划项；逐项捕获 `OSError`/`ValueError` 到 `errors`，不抛给调用方。 |
| `format_cleanup_plan` | `(actions) -> str` | 文本 | 空输入返回 `Nothing to clean.`。 |
| `format_rules` | `() -> str` | 文本 | 按配置顺序输出规则及模式。 |
| `_match` / `_action` | 私有 | 规则名/`CleanupAction` | 名称匹配和体积计算。 |

`CleanupResult` 为不可变数据类，字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `removed` | integer | 成功删除项数。 |
| `freed_bytes` | integer | 成功项的计划体积之和。 |
| `errors` | tuple[string, ...] | 逐项错误文本，格式 `"{path}: {message}"`。 |

### 12.4 恢复模块 `proj_janitor.restore`

| 函数/类型 | 签名 | 返回/异常 | 说明 |
| --- | --- | --- | --- |
| `restore_from_manifest` | `(manifest_path: Path, dest: Path \| None, projects=(), dry_run=False, verify=True, check_remote=False) -> tuple[list[RestorePlan], list[RestoreResult]]` | 计划列表和结果列表；失败抛 `RestoreError` | 先规划并校验，再按顺序恢复；`dest=None` 表示原始位置模式；`dry_run=True` 不写文件；`check_remote=True` 时在写入前预检 Git-only 远端（同 `execute_restore`）。 |
| `plan_restore` | `(manifest_path: Path, dest: Path \| None, projects=(), verify=True) -> list[RestorePlan]` | 恢复计划；失败抛 `RestoreError` | 校验 schema v2、项目名、`source`/目标不存在、目标重复、archive/bundle SHA-256 和 Git 字段；`dest=None` 时使用记录的 `source`。 |
| `verify_manifest` | `(manifest_path: Path, projects=(), check_remote=False) -> list[dict]` | 每项目校验结果；失败抛 `RestoreError` | 只读检查 archive/bundle SHA-256；Git-only 策略默认只校验 URL/commit 元数据、不联网；`check_remote=True` 时用 `git ls-remote` 确认提交是否仍被远端引用指向。 |
| `execute_restore` | `(plans: Iterable[RestorePlan], verify=True, check_remote=False) -> list[RestoreResult]` | 结果列表；文件系统错误抛 `RestoreError` | 先预检待恢复归档的 SHA-256，再创建目标父目录并按顺序执行；解包时对同一文件句柄复核摘要；`check_remote=True` 时对每个 Git-only 计划调用 `_probe_remote`，任一 `remote-unreachable` 抛 `RestoreError`，缺失 `remote_url`/`commit` 抛 `RestoreError`；不做交互确认。 |
| `load_manifest` | `(path: Path) -> tuple[Path, dict]` | 规范化路径和 JSON 对象 | 要求 `schema_version == 2`、`records` 为非空对象数组。 |
| `RestoreError` | `RuntimeError` 子类 | 异常 | CLI 捕获后输出 `error: ...` 并返回 `1`。 |
| `GitTimeoutError` | `RestoreError` 子类 | 异常 | Git 子进程超时；`verify --check-remote` 将其映射为 `remote-unreachable` / `ls-remote-timed-out`。 |
| `RestorePlan` | 数据类 | 不可变 | 项目、策略、目标、归档、远程 URL、提交、分支、SHA-256、是否已校验、是否原始位置模式。 |
| `RestoreResult` | 数据类 | 不可变 | 项目、策略、恢复目标。 |

内部函数：`_record_source` 校验并规范化原始位置；`_restore_git` init 临时仓库、写入脱敏 origin、通过 Git 临时配置环境 fetch 后 `checkout --detach` 并验证 HEAD，Git 子进程每步超时为 600 秒；fetch 不写入包含 URL 的 `FETCH_HEAD`；旧版 Git 只对不含敏感信息的 URL 回退到参数传递；`_run_git` 遇到敏感 Git URL 时不回显原始错误文本，超时时终止进程树并抛出 `GitTimeoutError`（`RestoreError` 子类）；`_probe_remote` 用 `git ls-remote` 做远端预检，单条最长等待 30 秒，返回 `(status, reason, refs)`；`_restore_archive` 校验并安全解压；`_safe_extract`、`_archive_parts` 负责成员路径校验；`redact_remote_url` 用于 URL 脱敏。

### 12.5 模型与配置

`proj_janitor.models` 暴露不可变数据类：

| 类型 | 字段 |
| --- | --- |
| `GitState` | `branch: str \| None`，`dirty: bool \| None` |
| `Project` | `path`，`name`，`markers`，`size_bytes`，`last_modified`，`git`；属性 `dirty_label`、`branch_label` |
| `CleanupAction` | `project`，`path`，`rule`，`kind`，`size_bytes`，内部项目根及目标身份字段 |
| `GitRecovery` | `eligible`，`reason`，`remote`，`remote_url`，`branch`，`commit`，`upstream`，`verified` |
| `BackupRecord` | 第 11.2 节所列 14 个字段 |
| `BackupPlan` | `project`，`archive_name`，`strategy`，`recovery`，`reason` |
| `RestorePlan` | `project`，`strategy`，`target`，`archive`，`remote_url`，`commit`，`branch`，`sha256`，`hash_verified`，`in_place` |
| `RestoreResult` | `project`，`strategy`，`target` |
| `human_size` | `(size_bytes: int) -> str` |

`proj_janitor.config` 暴露：

| 名称 | 类型 | 说明 |
| --- | --- | --- |
| `PROJECT_MARKERS` | `tuple[str, ...]` | 16 个项目标记。 |
| `CONFIG_NAME` | `str` | 配置文件默认名 `.proj-janitor.toml`。 |
| `ConfigError` | `ValueError` 子类 | 配置缺失、解析失败或未知键时抛出。 |
| `BackupConfig` | 数据类 | `dest: Path \| None`，`keep`，`exclude`，`incremental`，`fail_on_secrets`。 |
| `PruneConfig` | 数据类 | `keep: int \| None`。 |
| `Config` | 数据类 | `backup: BackupConfig`，`prune: PruneConfig`。 |
| `load_config` | `(path: Path \| None = None) -> Config \| None` | 显式路径或从当前目录向上发现；找不到返回 `None`；配置非法抛 `ConfigError`。 |
| `ARTIFACT_RULES` | `dict[str, tuple[str, ...]]` | 5 组清理规则，顺序固定。 |
| `ALL_RULES` | `tuple[str, ...]` | 规则名元组。 |
| `artifact_patterns` | `(include_artifacts=False) -> tuple[str, ...]` | 展开并去重所有排除模式；`True` 返回空元组。 |

### 12.6 副作用、异常与并发

| 函数/流程 | 读取 | 写入 | 删除 | 网络 | 备注 |
| --- | --- | --- | --- | --- | --- |
| `scan`/`discover_projects`/`scan_project` | 项目目录、`.git`、Git 命令 | 无 | 无 | 无 | Git 命令只读。 |
| `prepare_backup` | 项目、Git 状态 | 无 | 无 | 无 | 仅计算路径和计划；存在本地 tag 时只读查询远程。 |
| `run_backup` | 项目、Git 状态 | 项目目录、归档、manifest | 失败时清理本次部分输出 | 无 | 单项目 `OSError`/`tarfile.TarError` 记入失败列表并继续处理后续项目。 |
| `restore_from_manifest` | manifest、归档、远程仓库 | 恢复目标目录（`--dest` 或原始 `source`） | 无 | Git 克隆 | 目标已存在时拒绝；多项目中途失败不回滚；原始位置模式由 CLI 负责确认，Python API 不做确认。 |
| `build_cleanup_plan` | 项目目录、文件元数据 | 无 | 无 | 无 | `os.walk` 不跟随链接。 |
| `apply_cleanup` | 目标存在性、类型 | 无 | 目录/文件 | 无 | 不进行确认，调用方负责授权。 |

- 模块不使用线程、异步任务、锁或数据库。
- 备份归档和 manifest 使用排他创建，避免并发覆盖；竞争同一名字时该项目报告失败，不自动重试。多个进程仍可能同时读取正在变化的源项目，清理计划与删除之间也没有锁。
- 项目扫描/备份使用的 Git 检查命令超时为 10 秒；Git 恢复的 init、remote add、fetch、checkout 和 HEAD 校验单步超时为 600 秒，超时会尝试终止 Git 进程树、清理临时目录并报告恢复失败。
- `KeyboardInterrupt` 不转换为 CLI 错误码；备份清理当前项目正在生成的输出后重新抛出，已完成项目不回滚；恢复和清理可能留下部分结果。
- Python API 的错误契约是“常规业务失败尽量用返回值/结果对象表达，文件系统和归档异常可能向上传播”；`restore` 的常规失败统一抛 `RestoreError`。

## 13. 错误清单

### 13.1 CLI 消息

| 场景 | 流 | 消息 |
| --- | --- | --- |
| `scan`/`backup` 无项目 | stderr | `No projects found.` |
| `backup` 目标不是目录 | stderr | `error: destination is not a directory: {dest}` |
| `backup` dry-run 标题 | stdout | `Dry run. Manifest would be: {path}`；多项目为 `Dry run. Manifests would be:` |
| `backup` 完成 | stdout | `Backed up {n} project(s).` |
| `backup` manifest 路径 | stdout | `Manifest: {path}`；多项目为 `Manifests:` |
| `restore` dry-run 标题 | stdout | `Restore plan: {n} project(s)` |
| `restore` 原始位置 dry-run 提示 | stdout | `Targets are the original backup locations:` |
| `restore` dry-run 结尾 | stdout | `Dry run. Re-run without --dry-run to restore.` |
| `restore` 原始位置确认标题 | stdout | `About to restore to original locations:` |
| `restore` 原始位置确认提示 | stdout | `Restore {n} project(s)? [y/N] ` |
| `restore` 原始位置非交互拒绝 | stderr | `error: refusing to restore to original locations without --yes in a non-interactive session` |
| `restore` 原始位置 JSON 缺少 `--yes` | stderr | `error: --yes is required for original-location restore with --json` |
| `restore` 用户取消 | stdout | `Aborted.` |
| `restore` 完成 | stdout | `Restored {n} project(s).` |
| `restore` 失败 | stderr | `error: {RestoreError 文本}` |
| `clean` dry-run 提示 | stdout | `Dry run. Re-run with --apply to delete these items.` |
| `clean` 非交互拒绝删除 | stderr | `error: refusing to delete without --yes in a non-interactive session` |
| `clean` 确认提示 | stdout | `Delete {n} item(s)? [y/N] ` |
| `clean` 用户取消 | stdout | `Aborted.` |
| `clean` 删除完成 | stdout | `Removed {n} item(s), freed {size}.` |
| `clean` 逐项删除错误 | stderr | `error: {path}: {message}` |
| `clean` 规则/计划错误 | stderr | `error: {项目名}: {message}` |
| 根路径错误 | stderr | `error: {异常文本}` |
| `--rules` 为空 | stderr（argparse） | `at least one rule is required`（由 `ArgumentTypeError` 产生） |

### 13.2 Python 异常

| 异常 | 触发点 | 处理建议 |
| --- | --- | --- |
| `FileNotFoundError` | `discover_projects` 根不存在 | CLI 捕获并计入失败；API 调用方自行处理。 |
| `NotADirectoryError` | 根不是目录 | 同上。 |
| `OSError` | 目录遍历、归档、manifest 写入、删除、恢复读取/写入 | 扫描/清理计划由 CLI 报错并返回失败；备份按项目记录失败并清理部分输出；`restore` 将归档读取错误转为 `RestoreError`。 |
| `ValueError` | 未知规则、目标越界、符号链接拒绝 | 计划阶段由 CLI 捕获；删除阶段由 `apply_cleanup` 收集。 |
| `RuntimeError` | 内部计划不变量被破坏（缺少 recovery 或 archive name） | 属于程序缺陷/异常状态，不应在正常输入下出现。 |
| `RestoreError` | manifest/归档无效、`source` 非法、SHA-256 不匹配、目标已存在/重复、Git 恢复失败 | CLI 捕获后输出 `error:` 并返回 `1`。 |
| `tarfile.TarError` | 归档读写失败 | `run_backup` 按项目记录失败并清理部分输出；直接调用 `backup_project` 时异常向上传播；`restore` 捕获并转为 `RestoreError`。 |
| `subprocess.SubprocessError` | Git 子进程超时等 | `_git_run` 捕获并返回 `None`，映射为具体 Git 失败原因；`restore` 的 Git 失败转为 `RestoreError`。 |
| `KeyboardInterrupt` | 用户中断 | 不转换为 CLI 错误码；备份会清理正在生成的项目输出后重新抛出，但已完成项目不回滚；恢复和清理可能留下部分结果。 |

### 13.3 Git 可恢复性失败原因

`inspect_git_recoverability()` 按顺序检查，首个失败即返回。失败原因文本是 manifest 的 `fallback_reason` 来源之一：

| 顺序 | 检查/条件 | `reason` |
| --- | --- | --- |
| 1 | 不存在 `.git` | `not a git repository` |
| 2 | 无法读取符号分支（detached HEAD） | `detached HEAD` |
| 3 | `git status --porcelain --untracked-files=all` 失败 | `git status failed` |
| 4 | 工作区有改动或未跟踪文件 | `working tree is not clean` |
| 5 | `git stash list` 失败 | `cannot inspect stashes` |
| 6 | 存在 stash | `stash entries are not included` |
| 7 | 当前分支没有 remote 或 remote 为 `.` | `branch has no upstream remote` |
| 8 | 无法解析 `@{upstream}` | `branch has no upstream` |
| 9 | 无法读取 remote URL | `remote URL is unavailable` |
| 10 | URL 不是受支持的 GitHub URL | `remote is not a supported GitHub URL` |
| 11 | 无法读取 `HEAD` | `HEAD is unavailable` |
| 12 | `HEAD` 不是 upstream 的祖先 | `HEAD is not contained in upstream` |
| 13 | 无法计算 `@{upstream}..HEAD` 数量 | `cannot compare HEAD with upstream` |
| 14 | `HEAD` 领先 upstream | `HEAD is ahead of upstream` |
| 14p | 项目名不可移植到 restore 目标 | `unsafe project name: {name}` |
| 14a | 需要归档但主 remote URL 含凭据或 query/fragment 数据 | `remote URL contains credentials or query data` |
| 14b | 需要归档但本地 Git 配置含 HTTP Authorization header | `Git config contains HTTP extraHeader` |
| 14c | 无法读取本地 Git 凭据配置 | `cannot safely inspect Git config for credentials` |
| 14d | 本地 Git 配置含 credential password | `Git config contains credential passwords` |
| 14e | 本地 Git 配置含内联 credential helper | `Git config contains an inline credential helper` |
| 14f | credential-store 文件位于项目内 | `Git config points to a credential file inside the project` |
| 14g | LFS endpoint URL 含凭据或 query/fragment | `LFS config contains credentials or query data` |
| 14h | HTTP proxy/LFS service URL 含凭据或 query/fragment | `Git config contains credentials or query data in a service URL` |
| 14i | credential helper 含无法识别的参数（包括编码值）；仅允许标准 cache timeout 和 store file 参数 | `Git config contains sensitive credential helper arguments` |
| 14j | Git URL rewrite 的键或值含凭据 | `Git config contains credentials in a URL rewrite` |
| 14k | 配置了自定义 SSH command | `Git config contains a custom SSH command` |
| 14l | `.gitmodules` 无法安全解析 | `cannot safely inspect .gitmodules for credentials` |
| 15 | 子模块 URL 含凭据或 query/fragment 数据 | `submodule URL contains credentials or query data` |
| 16 | 子模块 URL 不受支持 | `submodule URL is not a supported GitHub URL` |
| 17 | 子模块状态或配置无法检查 | `cannot inspect submodules` / `cannot inspect submodule configuration` |
| 18 | Git LFS 未安装 | `Git LFS is not installed` |
| 19 | 存在 `.lfsconfig` 或自定义 LFS endpoint | `Git LFS is not supported` / `custom Git LFS endpoints are not supported` |
| 20 | Git LFS endpoint 配置无法检查 | `cannot inspect Git LFS configuration` |
| 21 | 忽略文件列举失败 | `cannot inspect ignored files` |
| 22 | 某忽略路径不属于允许的 artifact 模式/生成路径 | `ignored path is not an artifact: {path}` |
| 23 | 本地分支列举失败 | `cannot list local branches` |
| 24 | 远程跟踪引用列举失败 | `cannot list remote-tracking refs` |
| 25 | 没有远程跟踪引用 | `no remote-tracking refs` |
| 26 | 某本地分支比较失败 | `cannot inspect local branch: {branch}` |
| 27 | 某本地分支未完全推送 | `local branch is not fully pushed: {branch}` |
| 28 | 本地 tag 列举失败 | `cannot list tags` |
| 29 | 本地 tag 未推送到远程 | `local tag is not pushed: {name}` |
| 30 | 本地 tag 与远程同名 tag 对象不一致 | `local tag differs from remote: {name}` |
| 31 | 远程 tag 查询失败 | `cannot verify local tags on remote` |

补充：

- 检查 22 的允许模式来自本次备份的 `artifact_patterns()` 与固定生成路径规则；默认排除构建产物，`--include-artifacts` 传入空模式，因此任何忽略文件都会阻止 URL-only。
- 检查 18 的 LFS 探测使用 `git grep -l filter=lfs -- *.gitattributes`；返回码非 `0`/`1` 视为检查失败。
- 检查 21 使用 `git -c core.quotePath=false ls-files --others --ignored --exclude-standard --directory -z`。
- 检查 23/24 使用 `for-each-ref`；检查 25 要求至少存在一个 `refs/remotes/{remote}/` 引用。
- 检查 28 使用 `git for-each-ref refs/tags` 读取本地 tag；检查 29/30/31 通过 `git ls-remote --tags --refs` 比较远程 tag 的原始对象 ID。没有本地 tag 时不发起远程查询。
- 扫描/备份中的 Git 检查调用超时为 10 秒；恢复 Git 子进程每步超时为 600 秒；`verify --check-remote` 与 `restore --check-remote` 的单次 `git ls-remote` 探测超时为 30 秒。超时会尝试终止 Git 进程树；超时或无法启动 Git 会映射为对应失败原因或 `None` 字段。

### 13.4 处理策略

| 阶段 | 策略 |
| --- | --- |
| 参数解析 | 交给 `argparse`，退出 `2`。 |
| 根路径发现 | 单项失败输出到标准错误，继续其他根，最终退出 `1`。 |
| 清理规则/计划 | 单项目失败输出到标准错误，继续其他项目，最终退出 `1`；`--apply` 有计划失败时不执行任何删除。 |
| 备份归档 | 单项目捕获 `OSError`/`tarfile.TarError`，清理部分输出并继续其他项目；最终退出 `1`。 |
| manifest 写入 | 单项目捕获 `OSError`，清理本次 manifest/归档并继续其他项目；最终退出 `1`。 |
| 恢复 | 将 manifest、归档、SHA-256、目标和 Git 失败转为 `RestoreError`，CLI 输出 `error:` 并返回 `1`；多项目中途失败不回滚已完成项目，错误列出已完成目标。 |
| 清理删除 | 逐项捕获并汇总，已成功项不回滚。 |
| Git 常规失败 | 转为 `GitRecovery.reason` 或空字段，不抛给用户。 |
| 中断 | 不捕获 `KeyboardInterrupt`；可能留下部分备份、恢复或删除结果。 |

## 14. 兼容性与使用边界

### 14.1 运行环境

| 项目 | 说明 |
| --- | --- |
| Python | `>=3.10`；开发和自测使用 Python 3.12.3。 |
| 依赖 | 仅 Python 标准库（Python 3.10 另加 `tomli`）；无其它第三方运行时依赖。 |
| 构建 | `setuptools>=77`；`pyproject.toml` 声明项目元数据。 |
| 外部命令 | Git 状态和可恢复性检查需要本机 `git`；含 Git LFS 属性的仓库还需 `git-lfs`；清理不需要 Git。 |
| 操作系统 | 正式自测环境为 Linux；Windows/macOS 未做正式验证。 |
| 终端 | 交互确认依赖 `sys.stdin.isatty()`；非交互环境必须 `--yes`。 |
| 编码 | manifest 固定 UTF-8；终端输出受 locale 影响。 |

### 14.2 路径与文件系统

- 项目发现只处理根目录本身或直接子目录，不递归发现嵌套项目。
- 根路径和备份目标支持 `~` 展开；项目路径在扫描后解析为绝对路径。
- 扫描、清理计划和归档排除逻辑不跟随符号链接；符号链接目录不会被递归进入。
- POSIX Python 清理通过已打开的目录文件描述符相对删除，Rust 使用目录 capability；执行前都会校验清理目标的计划身份。Python Windows 仍走路径回退，校验与 unlink 之间仍有窄 TOCTOU 窗口。
- 归档包含 `.git`，默认只排除 artifact 规则命中的路径；不会自动排除 secrets、日志或用户数据。
- 硬链接按普通文件内容统计和归档，不保留链接关系；FIFO、设备文件和 socket 不归档，恢复拒绝特殊或未知成员类型。

### 14.3 版本与接口稳定性

- CLI 版本为 `0.1.0`；`--version` 输出 `projjan 0.1.0`。
- `scan --json` 和 `clean --json` 没有 `schema_version`，属于 v0.1.0 实现格式，后续可能调整。
- manifest 有 `schema_version: 2`；字段变更应通过版本号区分，但 v0.1.0 不提供迁移工具。
- Python 模块是内部实现；本文件列出的函数签名用于说明当前行为，不构成公共 API 承诺。
- 没有配置文件兼容性、数据库迁移或插件接口。

### 14.4 已知接口限制

| 限制 | 影响 | 规避/说明 |
| --- | --- | --- |
| `clean --json` JSON 解析失败 | stdout 被外层脚本或命令包装污染 | 检查原始 stdout、stderr 和退出码；当前 CLI 有纯 JSON 回归测试。 |
| `--list-rules` 可不带 `ROOT` | 独立输出规则列表 | 使用 `projjan clean --list-rules`。 |
| 并发备份撞名不自动重试 | 冲突项目会失败但不会覆盖已有归档/manifest | 重新运行备份，文件名会追加后缀；如同一项目仍在变化，应先停止写入再备份。 |
| manifest 最后写入 | 中途失败可能只有归档没有清单 | 检查退出码、项目备份目录和 manifest 是否存在。 |
| `restore` 默认不覆盖已有目录 | 目标存在时整批规划失败 | 指定 `--dest`，或先自行移走旧目录；可用 `--force`（需 `--yes`）在暂存成功后替换已存在目录。 |
| 原始位置依赖 manifest 的 `source` | 伪造/损坏的 manifest 可把写入导向任意可写路径；换机器后路径通常不存在 | 仅对可信 manifest 使用；跨机器/跨用户时使用 `--dest`；实际恢复前用 `--dry-run` 核对目标。 |
| 原始位置恢复需要确认 | 非交互脚本或 `--json` 未给 `--yes` 时返回 `2` | 交互终端输入 `y`，或在可信脚本中显式加 `--yes`。 |
| 原始位置不可并发/不可重复 | 目标已存在即拒绝，不会合并或覆盖 | 先确认目标已移走；需要保留旧目录时使用 `--dest` 恢复到新位置。 |
| `restore` 多项目不回滚 | 中途失败会留下已恢复项目 | 逐个项目恢复，或失败后人工清理已完成目标。 |
| `restore` 不预检远程提交 | 远程删除/强推后 Git 恢复可能失败 | 恢复前先运行 `verify --check-remote`，或直接加 `restore --check-remote` 让恢复在写入前内置预检；重要项目保留归档。 |
| Git 恢复为 detached HEAD | 不自动恢复原分支 | 恢复后按需创建分支：`git switch -c main`。 |
| `verify` 对 Git 策略默认只检查 manifest 元数据 | 不确认远程仓库或提交仍可访问 | 默认保持离线；需要确认远程可用性时显式加 `--check-remote`，该选项只做引用列表查询，不下载对象。 |
| `verify --check-remote` 只匹配引用指向 | `remote-moved` 不等于提交已丢失；`remote-verified` 也不证明对象完整可拉取 | 结合 `remote.reason`/`refs` 判断，必要时实际恢复或 `git fetch` 目标提交验证。 |
| 归档包含 `.git` | 体积较大，可能包含敏感历史或内嵌 PAT | 使用 `--include-artifacts` 前评估；不要把归档视为最小化备份；PAT 归档按敏感数据处理。 |
| Git URL-only 仅本地证据 | 不保证远端实际可克隆 | 恢复前联网验证；重要项目使用 `--always-archive`。 |
| 规则按名称匹配 | 可能误匹配同名目录 | 先 dry-run 审查计划；必要时缩小根范围。 |
| 并发清理无锁 | 同时清理同一项目时计划和删除可能竞争 | 串行执行清理；备份文件使用排他创建，冲突后重跑；备份时避免修改源项目。 |

### 14.5 恢复说明

当前版本提供 `restore`，优先使用：

```bash
# 恢复到原始位置：先预览，再交互确认
projjan restore <manifest> --dry-run
projjan restore <manifest>

# 恢复到指定副本目录
projjan restore <manifest> --dest <dir> --dry-run
projjan restore <manifest> --dest <dir>

# 非交互脚本恢复原始位置
projjan restore <manifest> --yes
```

恢复前应确认：

- manifest 的 `schema_version` 为 `2`；目标路径不存在且不是符号链接；
- 省略 `--dest` 时，manifest 的 `source` 指向预期的原始路径，且当前机器/目录布局与备份时一致；manifest 来源可信；
- 指定 `--dest` 时，目标 `--dest/<项目名>/` 不存在；
- Git 记录的 `commit` 在远端实际存在（`verified` 仅为 `local-ref`，不代表远程 HEAD 可克隆）；
- 归档记录的 SHA-256 与文件一致；`--dry-run` 会提前校验；
- 归档来自可信来源，且不会把不受信任的 `.git` 内容直接投入生产使用。

需要手工兜底时，可按 manifest 执行：

```bash
git clone <remote_url> <project>
git -C <project> checkout --detach <commit>

tar -xzf <archive>
```

手工恢复不会获得工具的路径/链接校验、SHA-256 校验和覆盖保护，风险由操作者承担。

## 15. HTTP API

### 15.1 结论

当前版本（`v0.1.0` 基线加 post-v0.1.0 变更）不提供 HTTP API、REST 接口、GraphQL、RPC、WebSocket 或任何网络服务端；也不调用 GitHub API 或其他远程接口。网络访问仅限 `git ls-remote` 的 tag 查询和 `restore` 的 Git 克隆。因此不存在以下内容：

- 服务地址、端口、路由和 HTTP 方法；
- 请求/响应 schema、状态码和错误码；
- 认证、授权、限流、幂等和重试语义；
- OpenAPI/Swagger 文档；
- 跨域、TLS、代理和网关配置。

### 15.2 替代集成方式

需要在自动化平台中集成时，可：

1. 以子进程方式调用 `projjan`，采集标准输出、标准错误和退出码；
2. 直接导入 `proj_janitor` 的 Python 函数（内部接口，需接受版本变化风险）；
3. 读取 `scan --json`、`clean --json`、`restore --json` 和 `manifest-<timestamp>.json` 进行后续处理。

这些方式都不改变“本地 CLI、无网络服务”的产品边界。若未来需要 HTTP 服务，应作为独立需求重新设计，并补充认证、并发、审计和错误协议，不能把当前 CLI 输出直接视为 HTTP API 契约。

## 16. Rust CLI 与当前接口补充（2026-10-27）

本文前 1–15 章中的 Python 函数签名仍描述源码元数据版本 `0.1.0`。Rust CLI 是独立二进制 `projjan`，不提供 Python API；两者共享 schema v2 manifest。

### 16.1 Rust CLI 命令

| 子命令 | 用法 | 主要参数/语义 |
| --- | --- | --- |
| `scan` | `projjan scan ROOT...` | `--all` 包含未标记目录；`--json` 输出 JSON |
| `backup` | `projjan backup ROOT...` | `--backup-dest DIR` 或 `BACKUP_DEST`；命令行优先；另有 `--config PATH`、`--keep N`（成功备份后每项目保留最近 N 份清单）、`--yes`、`--dry-run`、`--include-artifacts`、`--always-archive`、Rust 0.2.6 起支持的 `--exclude-git-metadata`、`--incremental`（archive 策略差异快照）、`--fail-on-secrets` / `--exclude-secrets`（及 `[backup]` 对应键）；`[backup].exclude` 通过配置文件设置 archive 排除模式（无命令行参数） |
| `restore` | `projjan restore MANIFEST` | `--dest DIR` 恢复到副本目录；省略时以清单 `source` 为原始目标并要求确认；`--project NAME` 可重复；支持 `--dry-run`、`--no-verify`、`--yes`、`--check-remote`（自 Rust 0.5.0 起发布；写入前预检 Git-only 远端及记录的子模块远端，子模块预检自 0.7.0 起）、`--json` |
| `verify` | `projjan verify MANIFEST`（Rust 0.2.5 及更新版本） | 只校验归档 SHA-256；Git 项目默认只检查 manifest 元数据；支持 `--project NAME`、`--json`；已包含于已发布 `0.2.5`。`--check-remote`（仓库源码，未发布）对 Git-only 记录联网确认提交是否仍被远端引用指向（含记录的子模块远端） |
| `clean` | `projjan clean ROOT...` | `--rules NAME[,NAME]`、`--list-rules`、`--apply`、`--yes`、`--json`；默认只预览 |

安装当前已发布 crate：

```bash
cargo install --locked proj-janitor --version 0.2.6
```

从仓库 main 编译安装后续修订：

```bash
cargo install --locked --path .
```

若要使用 main 的后续开发版本，可在仓库根目录运行 `cargo install --locked --path .`，或直接运行 `cargo install --locked --git https://github.com/TangCan/proj-janitor.git --branch main proj-janitor`。开发分支行为可能随后续提交变化；确认具体源码时应检查 Git commit。稳定安装使用已发布的 crates.io 版本 `0.2.6`。

### 16.2 `verify` 子命令

```bash
projjan verify MANIFEST
projjan verify MANIFEST --project NAME --json
projjan verify MANIFEST --check-remote --json
```

归档记录会定位归档文件并比较 manifest 中的 SHA-256。Git 记录默认只验证 URL 格式和 commit 格式，结果为 `metadata-only`，不联网。命令不写入文件，也不检查恢复目标是否存在。该命令自 crates.io `0.2.5` 起提供。

`--check-remote` 让 Git-only 记录改用第 10.4 节的 `remote-*` 状态：对每个远端执行一次 `git ls-remote`（单条最长等待 30 秒，`GIT_TERMINAL_PROMPT=0`，参数列表调用不经 shell），只读取引用列表，不下载对象。manifest 中的 URL 含用户名、口令、query 或 fragment 时状态为 `remote-skipped`，该 URL 不会传给 Git。任一记录 `remote-unreachable` 时仍输出完整报告，并以退出码 `1` 结束。含子模块的 Git-only 记录还会对每个子模块的 URL+固定提交执行一次相同预检，JSON 增加 `submodules` 数组（每项 `{path,url,branch,reason,refs,status}`），任一子模块远端不可达时项目级 `status` 同样为 `remote-unreachable`。

该选项已随 Rust `0.5.0` 发布到 crates.io（子模块预检随 Rust `0.7.0` 发布）；Python 发行包已发布至 PyPI（`proj-janitor 0.1.0`，tag `python-v0.1.0`）。不带该选项时，文本与 JSON 输出结构和之前完全一致。

## 16.3 当前安全与文件接口

- 新备份根目录和项目目录（Unix）为 `0700`，归档与 manifest 为 `0600`；已存在的目录权限不改。Windows ACL 继承自目标父目录。
- Git 与归档恢复都在执行前拒绝 Windows 不可移植项目名；包括 `CONIN$` 和 `CONOUT$`。
- Python 与 Rust 都可恢复仅含目录成员的非空归档；Python↔Rust 双向兼容由 `tests/cross_version_compat.py` 验证。
- schema v2 仍是文件接口；没有 HTTP/REST 服务。SHA-256 检查保证文件与 manifest 中摘要一致，不提供签名或来源认证。

## 17. `prune` 备份保留

```text
projjan prune BACKUP_ROOT [--keep N] [--config PATH] [--apply] [--yes] [--json]
```

`--keep` 按 **命令行 > 配置文件 `[prune].keep`** 的优先级解析；两者均未提供时报错 `set --keep or configure [prune].keep`。

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `BACKUP_ROOT` | 是 | 由 `backup` 生成的备份根目录。 |
| `--keep N` | 条件必填 | 每个项目目录至少保留 N 份最新清单；N 必须大于等于 1。未传时使用配置文件 `[prune].keep`。 |
| `--config PATH` | 否 | 显式指定配置文件；未指定时从当前目录向上逐级查找 `.proj-janitor.toml`。 |
| `--apply` | 否 | 实际删除预览中的过期清单和它们引用的 artifact；不指定时始终只预览。 |
| `--yes` | 否 | 跳过交互确认；非交互执行和 JSON apply 必须显式指定。 |
| `--json` | 否 | 输出纯 JSON 计划或删除结果。 |

处理边界：

- 只扫描备份根目录下的直接项目目录和符合当前 `manifest-YYYYmmdd-HHMMSS[-N].json` 格式的清单；旧版 `backup-YYYYmmdd-HHMMSS/` 运行目录不处理。
- 清单必须是 schema v2 且每份只含一条记录；无法解析的清单会使计划失败，不删除任何文件。
- 只删除过期清单直接引用、与清单位于同一项目目录的 `.tar.gz`/`.bundle` 文件；保留清单引用的共享 artifact。孤立 artifact、未知文件和符号链接不会删除。
- 计划完成后会复核文件身份；备份期间若同权限进程替换目标，工具报告错误并跳过。多个文件删除不是事务，执行中断时可能只删掉部分旧清单/artifact，留下超过 N 份备份；工具不会为凑齐 N 而继续删除。
- 操作前检查退出码，并保留至少一份清单及其引用文件；备份目录应有独立副本。
- `backup --keep N` 复用同一保留逻辑，在成功备份后对备份根目录自动执行；非交互执行需加 `--yes`，否则只打印计划不删除（详见 6.1）。

## 18. Rust 0.5.0 发布状态（2026-11-05）

Rust `0.5.0` 已发布至 crates.io 和 GitHub Release，tag `v0.5.0` 指向 `d2c329e16170649724a2ac702ef5b3bf03ee3070`。本版本新增 `verify --check-remote` 与 `restore --check-remote`，并包含 Python 打包就绪与多处健壮性修复；CLI 和 manifest schema v2 的其他行为不变；发布验证见 `docs/发布记录/0.5.0-Rust版.md`。

### Rust 0.4.0 发布状态（2026-11-03）

Rust `0.4.0` 已发布至 crates.io 和 GitHub Release，tag `v0.4.0` 指向 `c18fab1ebf7b29a198be3e64845dc374d12fcafc`。本版本新增 `prune` 命令，CLI 和 manifest schema v2 的其他行为不变；发布验证见 `docs/发布记录/0.4.0-Rust版.md`。

最新接口行为以 `src/main.rs`、`src/restore.rs`、Python `proj_janitor/cli.py` 和 CI 为准。内部函数、未发布 main 提交和格式化文本输出均不构成长期稳定 API 承诺。

## Rust 0.2.4 发布时状态（2026-10-27）

以下记录 Rust `0.2.4` 发布时的交付基线；最新版本和维护状态见文末“发布后 main 维护状态”。

发布标签 `v0.2.4` 指向提交 `4ac7f788992eca87ed6111cd45ac75df0a52c95b`；功能源码始于 `98d43abd77f2bf7948d64f3569dda0d3bb4c4070`。Python 123 项测试、Rust 26 项单元测试和 3 项 CLI 集成测试通过；`cargo fmt --check`、Clippy、打包、crates.io 发布及安装冒烟均通过。提交 CI run [40466700148](https://github.com/TangCan/proj-janitor/actions/runs/40466700148) 和 tag CI run [40467153200](https://github.com/TangCan/proj-janitor/actions/runs/40467153200) 均 11/11 个 job 成功。发布细节见 `docs/发布记录/0.2.4-Rust版.md`。

正式验收和签字仍由实际负责人/接收人完成；开源发布、CI 通过和交付材料齐备不代替接收人确认。

### 发布后 main 维护状态（2026-10-31）

- 已发布 crate：Rust `0.2.5`（tag `v0.2.5`）；main 当前是 `0.2.6` 候选，选项实现提交 `7bd6f317637ce1daace91e896cfd57b07f601818`，候选版本准备提交 `ad2ef13222b3bffaefa4958d150b18100ec04d96`，候选行为修正与回归测试提交 `dcc116e7d7243751a3683741d9507be0803f4f50`，尚未创建 tag 或发布；Python `0.1.2` 已发布至 PyPI。
- Rust `0.2.5` 已包含只读 `verify`：archive 校验 manifest 中的 SHA-256；Git 策略只校验 manifest 元数据，不联网确认远程。
- `0.2.6` 候选包含 `--exclude-git-metadata`：归档时省略 `.git`，manifest 记录实际行为；恢复后没有 Git 历史。该版本尚未发布，crates.io `0.2.5` 不含此选项。
- Windows Rust CLI 在 8 MiB 栈线程中执行，修复备份/verify 子进程默认栈溢出；Windows CLI 回归通过。
- Python 160 项测试（Linux 本地 159 项通过、1 项 Windows 专用测试跳过）、Rust 42 项单元测试和 15 项 CLI 集成测试通过；Python/Rust 对 archive 与 Git metadata-only manifest 的 verify JSON 结果一致。CI run [41183260006](https://github.com/TangCan/proj-janitor/actions/runs/41183260006) 的 18/18 jobs 与 CodeQL run [41183259488](https://github.com/TangCan/proj-janitor/actions/runs/41183259488) 的三项分析均通过。
- GitHub Actions run [41159972991](https://github.com/TangCan/proj-janitor/actions/runs/41159972991) 的 18/18 个 job 成功，包含 Windows Rust CLI 和跨语言 verify 检查。
- CodeQL run [41159973417](https://github.com/TangCan/proj-janitor/actions/runs/41159973417) 的 Actions、Python、Rust 三个 job 成功；当前无开放 CodeQL 告警。Dependabot 当前无开放依赖告警。
- 正式结项仍待实际负责人/接收人验收与签字；verify 不验证归档成员结构或 Git 远程可用性。

## Rust 0.2.6 发布时状态（2026-11-02）

- Rust `0.2.6` 已发布至 crates.io，tag 为 `v0.2.6`；Python `0.1.2` 已发布至 PyPI。
- `--exclude-git-metadata` 随 Rust 0.2.6 提供，Python API/CLI 尚未提供该选项。
- 发布验证、tag CI 和安装命令见 `docs/发布记录/0.2.6-Rust版.md`。

## Rust 0.2.7 发布状态（2026-11-02）

Rust `0.2.7` 已发布至 crates.io 与 GitHub Release，tag 为 `v0.2.7`。该版本让 `backup --help` 只显示 `BACKUP_DEST` 环境变量名，不显示实际路径；Python 备份清理改动属于仓库源码，不包含在 Rust crate 中。发布验证见 `docs/发布记录/0.2.7-Rust版.md`。

## Rust 0.2.8 发布状态（2026-11-02）

Rust `0.2.8` 已发布至 crates.io 与 GitHub Release，tag 为 `v0.2.8`。此版只刷新 crate 内 README，不改变 CLI 或 schema；发布记录见 `docs/发布记录/0.2.8-Rust版.md`。

## Rust 0.2.9 发布状态（2026-11-02）

Rust `0.2.9` 已发布至 crates.io 与 GitHub Release，tag 为 `v0.2.9`。此版只修正 crate README 的安全策略链接，不改变 CLI 或 schema；发布记录见 `docs/发布记录/0.2.9-Rust版.md`。

## Rust 0.3.0 发布状态（2026-11-02）

Rust `0.3.0` 已发布至 crates.io 与 GitHub Release，tag 为 `v0.3.0`。新增 `git_bundle` 备份策略，manifest schema 仍为 v2；兼容条件、字段、恢复和验证行为见第 6、8、10、11 章及 `docs/发布记录/0.3.0-Rust版.md`。

## 19. Rust 0.6.0 发布状态（2026-11-06）

Rust `0.6.0` 已发布至 crates.io 和 GitHub Release，tag `v0.6.0`（注释对象 `269c0f83ae074eec2ef9aa25eae5d79fbc958dff`）指向 `096f08ad4310ae329948bf9c6c3f06884463a3b6`。本版本新增 `.proj-janitor.toml` 配置文件默认值（CR-090）、`backup --keep` 自动保留（CR-091）、`[backup].exclude` 排除规则（CR-092）与 `backup --incremental` 增量备份（CR-093）；manifest schema v2 行为不变（新增可选字段 `inventory` 与 `incremental`，向后兼容）。发布验证见 `docs/发布记录/0.6.0-Rust版.md`。

## 20. Rust 0.7.0 发布状态（2026-11-06）

Rust `0.7.0` 已发布至 crates.io 和 GitHub Release，tag `v0.7.0`（注释对象 `a2ab6dbb356552c978de720f82201fe3d949fa90`）指向 `d5eadb764d30b7b260516348b6030e11ec56252e`。本版本把 `verify`/`restore --check-remote` 远端预检扩展到子模块并记录 LFS 指针 OID（CR-094）；manifest schema v2 行为不变（新增可选字段 `submodules` 与 `lfs_objects`，向后兼容）。发布验证见 `docs/发布记录/0.7.0-Rust版.md`。

## 21. Rust 0.7.1 发布状态（2026-11-07）

Rust `0.7.1` 已发布至 crates.io 和 GitHub Release，tag `v0.7.1`（注释对象 `26072ce95c2add6255cec8bc69fe82c733cb61f8`）指向 `ee3b136bf8bcd4dbb2c77faf939c917c8c03aab6`。本版本修复 `git_bundle` 恢复在默认分支名冲突与非 git cwd 下失败（CR-095）；manifest schema v2 不变。发布验证见 `docs/发布记录/0.7.1-Rust版.md`。

## 22. Rust 0.7.2 / Python 0.1.1 发布状态（2026-11-07）

Rust `0.7.2`（tag `v0.7.2` → `7fc4c02e20c60c93e637a4af2e5086cd4113858f`）与 Python `0.1.1`（tag `python-v0.1.1`）已发布。本版本恢复失败时透出脱敏 git stderr（CR-096）。详见 `docs/发布记录/0.7.2-Rust版.md` 与 `docs/发布记录/0.1.1-Python版.md`。

## 23. Rust 0.7.3 / Python 0.1.3 发布状态（2026-11-07）

Rust `0.7.3`（tag `v0.7.3`）与 Python `0.1.3`（tag `python-v0.1.3`）已发布。本版本把 CR-097 LFS 对象可用性探测纳入 `--check-remote`（本地对象库 / HTTPS batch，不下载）。详见 `docs/发布记录/0.7.3-Rust版.md` 与 `docs/发布记录/0.1.3-Python版.md`。

## 24. Rust 0.7.4 / Python 0.1.4 发布状态（2026-11-07）

Rust `0.7.4`（tag `v0.7.4`）与 Python `0.1.4`（tag `python-v0.1.4`）已发布。本版本加入 CR-098：`[verify]`/`[restore].check_remote` 配置默认值与 `--no-check-remote`。详见 `docs/发布记录/0.7.4-Rust版.md` 与 `docs/发布记录/0.1.4-Python版.md`。

## 25. Rust 0.7.5 / Python 0.1.5 发布状态（2026-11-07）

Rust `0.7.5`（tag `v0.7.5`）与 Python `0.1.5`（tag `python-v0.1.5`）已发布。本版本加入 CR-099：archive 备份对常见敏感路径的 stderr 告警。详见 `docs/发布记录/0.7.5-Rust版.md` 与 `docs/发布记录/0.1.5-Python版.md`。

## 26. Rust 0.7.6 / Python 0.1.6 发布状态（2026-11-07）

Rust `0.7.6`（tag `v0.7.6`）与 Python `0.1.6`（tag `python-v0.1.6`）已发布。本版本加入 CR-100：`--fail-on-secrets` / `[backup].fail_on_secrets`。详见 `docs/发布记录/0.7.6-Rust版.md` 与 `docs/发布记录/0.1.6-Python版.md`。

## 27. Rust 0.7.7 / Python 0.1.7 发布状态（2026-11-07）

Rust `0.7.7`（tag `v0.7.7`）与 Python `0.1.7`（tag `python-v0.1.7`）已发布。本版本加入 CR-101：`--exclude-secrets` / `[backup].exclude_secrets`。详见 `docs/发布记录/0.7.7-Rust版.md` 与 `docs/发布记录/0.1.7-Python版.md`。

## 28. Rust 0.7.8 / Python 0.1.8 发布状态（2026-11-07）

Rust `0.7.8`（tag `v0.7.8`）与 Python `0.1.8`（tag `python-v0.1.8`）已发布。本版本加入 CR-102：`--require-dest` / `[restore].require_dest`。详见 `docs/发布记录/0.7.8-Rust版.md` 与 `docs/发布记录/0.1.8-Python版.md`。

## 29. Rust 0.7.9 / Python 0.1.9 发布状态（2026-11-07）

Rust `0.7.9`（tag `v0.7.9`）与 Python `0.1.9`（tag `python-v0.1.9`）已发布。本版本加入 CR-103：`--progress` / `[backup].progress`。详见 `docs/发布记录/0.7.9-Rust版.md` 与 `docs/发布记录/0.1.9-Python版.md`。

## 30. Rust 0.7.10 / Python 0.1.10 发布状态（2026-11-07）

Rust `0.7.10`（tag `v0.7.10`）与 Python `0.1.10`（tag `python-v0.1.10`）已发布。本版本加入 CR-104：`--check-space` / `[backup].check_space`。详见 `docs/发布记录/0.7.10-Rust版.md` 与 `docs/发布记录/0.1.10-Python版.md`。

## 31. Rust 0.7.11 / Python 0.1.11 发布状态（2026-11-07）

Rust `0.7.11`（tag `v0.7.11`）与 Python `0.1.11`（tag `python-v0.1.11`）已发布。本版本加入 CR-105：`--fail-on-insecure-dest` / `[backup].fail_on_insecure_dest`。详见 `docs/发布记录/0.7.11-Rust版.md` 与 `docs/发布记录/0.1.11-Python版.md`。

## 32. Rust 0.7.12 / Python 0.1.12 发布状态（2026-11-07）

Rust `0.7.12`（tag `v0.7.12`）与 Python `0.1.12`（tag `python-v0.1.12`）已发布。本版本加入 CR-106：可选 openssl 归档加密。详见 `docs/发布记录/0.7.12-Rust版.md` 与 `docs/发布记录/0.1.12-Python版.md`。

## 33. Rust 0.7.13 / Python 0.1.13 发布状态（2026-11-07）

Rust `0.7.13`（tag `v0.7.13`）与 Python `0.1.13`（tag `python-v0.1.13`）已发布。本版本加入 CR-108：`scan` 对 Git STATE=`unknown` 输出诊断。详见 `docs/发布记录/0.7.13-Rust版.md` 与 `docs/发布记录/0.1.13-Python版.md`。
