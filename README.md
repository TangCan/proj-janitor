# proj-janitor

扫描、备份、恢复、保留和安全清理旧项目的轻量命令行工具。适合在多年积累的项目目录（如 `~/projects`）中盘点、备份、恢复，并按需清理构建产物或过期备份。

## 安全原则

- `scan` 只读：报告项目大小、Git 分支和工作区状态；若 Git STATE 为 `unknown`，stderr 给出诊断原因，JSON 含 `git.detail`（CR-108）。可用 `--progress` / `[scan].progress` 在 stderr 输出逐项目扫描进度与耗时（CR-123）。
- `backup` 优先记录可恢复信息：可从 GitHub 恢复的干净项目只写入远程 URL 和提交 SHA；干净、无子模块/LFS 的本地 Git 仓库会生成 `.bundle`，保留本地分支和提交；其他项目回退为 `tar.gz` 并记录 SHA-256。可用 `--progress` / `[backup].progress` 输出逐项目与打包进度；对 `git_bundle` 额外报告创建起止并转发 `git bundle create --progress`（CR-124）。
- 备份目录可通过 `BACKUP_DEST` 环境变量指定，也可用 `--backup-dest` 指定，或从 `.proj-janitor.toml` 的 `[backup].dest` 读取；命令行参数优先，其次环境变量，再次配置文件。空的 `BACKUP_DEST` 按未设置处理。`.proj-janitor.toml` 的 `[backup].exclude` 可为 archive 策略配置排除模式（glob）；`[backup].incremental` 或 `--incremental` 可生成差异快照（archive 策略）。恢复仍使用 `--dest`。
- `restore` 默认恢复到 manifest 记录的原始位置，指定 `--dest` 时恢复到新目录；dry-run 规划时校验 archive/bundle SHA-256；实际恢复在确认后、写入前预检整批摘要，且默认不覆盖已有目录；可用 `--force` / `[restore].force` 在暂存成功后替换已存在目录（需 `--yes`；仍拒绝对称链接与非目录；配合 `--keep-replaced` / `[restore].keep_replaced` 将被替换目录重命名旁侧而非删除）；可用 `--continue-on-error` / `[restore].continue_on_error` 在单个项目失败后继续恢复其余项目（仍非零退出；不自动回滚已成功项；失败时立即 stderr 报错，配合 `--progress` 输出 `failed i/n`）；可用 `--rollback-on-error` / `[restore].rollback_on_error` 在失败后撤销本批已恢复目标（`--keep-replaced` 的旁侧目录会还原）；可用 `--skip-existing` / `[restore].skip_existing` 跳过已存在的目标目录以便重跑续传（与 `--force` 互斥）；可用 `--progress` / `[restore].progress` 在 stderr 输出逐项目恢复进度，并对 archive 策略额外输出解压成员计数进度；对 git 策略额外报告 fetch / LFS pull / submodule update 起止并转发相关进度输出。
- `verify` 命令校验 archive/bundle SHA-256；Git-only 记录默认只验证 manifest 中的 URL 和提交格式，不联网检查远程，加 `--check-remote` 才会确认提交是否仍被远端引用指向。`restore` 同样支持 `--check-remote`，把同一远端预检集成进恢复流程，写入前发现远端不可达即整批不开始。该命令从 Rust `0.2.5` 开始包含在 crates.io 发布版中；`--check-remote`（`verify` 与 `restore`）从 Rust `0.5.0` 起随 crates.io 发布，子模块预检扩展（CR-094）自 Rust `0.7.0` 起发布；LFS 对象可用性探测（CR-097）自 Rust `0.7.3` / Python `0.1.3` 起发布；`--check-remote` 配置默认值（CR-098）自 Rust `0.7.4` / Python `0.1.4` 起发布；敏感路径告警（CR-099）自 Rust `0.7.5` / Python `0.1.5` 起发布；`--fail-on-secrets`（CR-100）自 Rust `0.7.6` / Python `0.1.6` 起发布；`--exclude-secrets`（CR-101）自 Rust `0.7.7` / Python `0.1.7` 起发布；`--require-dest`（CR-102）自 Rust `0.7.8` / Python `0.1.8` 起发布；`--progress`（CR-103）自 Rust `0.7.9` / Python `0.1.9` 起发布；`--check-space`（CR-104）自 Rust `0.7.10` / Python `0.1.10` 起发布；`--fail-on-insecure-dest`（CR-105）自 Rust `0.7.11` / Python `0.1.11` 起发布；openssl 归档加密（CR-106）自 Rust `0.7.12` / Python `0.1.12` 起发布；`scan` Git unknown 诊断（CR-108）自 Rust `0.7.13` / Python `0.1.13` 起发布；`restore --force`（CR-109）自 Rust `0.7.14` / Python `0.1.14` 起发布；不可移植路径预检（CR-110）自 Rust `0.7.15` / Python `0.1.15` 起发布；`restore --continue-on-error`（CR-111）自 Rust `0.7.16` / Python `0.1.16` 起发布；`restore --skip-existing`（CR-112）自 Rust `0.7.17` / Python `0.1.17` 起发布；`restore --progress`（CR-113）自 Rust `0.7.18` / Python `0.1.18` 起发布；archive 解压文件级进度（CR-114）自 Rust `0.7.19` / Python `0.1.19` 起发布；超长 symlink 目标（CR-115）自 Rust `0.7.20` / Python `0.1.20` 起发布；GNU sparse 解压（CR-116）自 Rust `0.7.21` / Python `0.1.21` 起发布；CLI help 文案（CR-117）自 Rust `0.7.22` / Python `0.1.22` 起发布；`--progress` 耗时（CR-118）自 Rust `0.7.23` / Python `0.1.23` 起发布；`projjan init` 与 `backup --progress` 逐项目进度（CR-119）自 Rust `0.7.24` / Python `0.1.24` 起提供；`restore --progress` 的 git fetch 进度（CR-120）自 Rust `0.7.25` / Python `0.1.25` 起提供；LFS/submodule 进度（CR-121）自 Rust `0.7.26` / Python `0.1.26` 起提供；`verify --progress` 与 restore 远端探测进度（CR-122）自 Rust `0.7.27` / Python `0.1.27` 起提供；`scan --progress`（CR-123）自 Rust `0.7.28` / Python `0.1.28` 起提供；`backup --progress` 的 git bundle 创建进度（CR-124）自 Rust `0.7.29` / Python `0.1.29` 起提供；openssl 加解密进度（CR-125）自 Rust `0.7.30` / Python `0.1.30` 起提供；`clean`/`prune --progress`（CR-126）自 Rust `0.7.31` / Python `0.1.31` 起提供；`verify`/`restore --progress` 的 SHA-256 校验进度（CR-127）自 Rust `0.7.32` / Python `0.1.32` 起提供；`restore --continue-on-error` 即时失败报告与 `--progress` 失败行（CR-132）自 Rust `0.7.33` / Python `0.1.33` 起提供；`--force --keep-replaced` 旁侧保留被替换目录（CR-133）自 Rust `0.7.34` / Python `0.1.34` 起提供；`--rollback-on-error`（CR-134）自 Rust `0.7.35` / Python `0.1.35` 起提供；Unix 上 passphrase 文件拒绝 group/other 可读权限（CR-135）自 Rust `0.7.36` / Python `0.1.36` 起提供；`backup`/`restore` dry-run 亦预检 passphrase 权限（CR-136）自 Rust `0.7.37` / Python `0.1.37` 起提供；`--fail-on-remote-moved`（CR-137）自 Rust `0.7.38` / Python `0.1.38` 起提供；`--fail-on-remote-skipped`（CR-138）自 Rust `0.7.39` / Python `0.1.39` 起提供；`restore --fail-on-insecure-dest`（CR-139）自 Rust `0.7.40` / Python `0.1.40` 起提供；`restore --allowed-root`（CR-140）自 Rust `0.7.41` / Python `0.1.41` 起提供；新建恢复目标父目录 Unix `0700`（CR-141）自 Rust `0.7.42` / Python `0.1.42` 起提供；V-03 恶意归档矩阵扩充（CR-142）自 Rust `0.7.43` / Python `0.1.43` 起提供；Git-only 未开 `--check-remote` 时 stderr 提示（CR-143）自 Rust `0.7.44` / Python `0.1.44` 起提供；原位恢复未传 `--dest` 时 stderr 提示（CR-144）自 Rust `0.7.45` / Python `0.1.45` 起提供；未开 `--fail-on-insecure-dest` 时对 group/other 可访问目录 stderr 提示（CR-145）自 Rust `0.7.46` / Python `0.1.46` 起提供；V-03 恶意归档矩阵扩充 trailing-space/COM1/LPT1（CR-146）自 Rust `0.7.47` / Python `0.1.47` 起提供；`projjan init` 安全推荐注释（CR-147）自 Rust `0.7.48` / Python `0.1.48` 起提供（Rust crates.io 已发布至 `0.7.43`；`0.7.44`–`0.7.48` 待下一批）。Python 源码元数据为 `0.1.48`；最新已发布 PyPI 仍可能滞后至上一 tag。
- `clean` 默认 dry-run：只匹配白名单内的构建产物目录/文件。
- `prune` 默认 dry-run：每个项目保留指定数量的最新备份，只删除过期 manifest 明确引用的归档/bundle；孤立文件不动。
- Unix 上新建的备份根目录和项目目录权限为 `0700`，归档和 manifest 为 `0600`；已有目录权限不变。Windows 使用备份目录继承的 ACL。
- 归档拒绝 Windows 不可移植名称和非 UTF-8 路径组件，避免恢复时文件名被替换或发生跨平台冲突；`backup`/`backup --dry-run` 会在写入前预检并一次列出全部问题路径（如误生成的 `file.rs:line:col`），避免打到一半才失败（CR-110）。
- 永远不删除项目根目录，永远跳过 `.git`，不跟随符号链接。
- 真正删除需要同时提供 `--apply` 和交互确认（或 `--yes`）。

## 安装

### Rust 命令行版（推荐免 Python 环境）

需要 Rust 1.88 或更新版本。安装 Rust 工具链后，在仓库根目录运行：

```bash
cargo install --locked proj-janitor
```

该命令安装 crates.io 最新发布版。需要固定版本时，可使用 `cargo install --locked --version VERSION proj-janitor`；版本与发布记录见 [GitHub Releases](https://github.com/TangCan/proj-janitor/releases)。

从源码开发安装可运行 `cargo install --locked --path .`。安装后可直接使用 `projjan`。Rust 版与 Python 版共用 CLI 名称，若同时安装，后安装的版本会覆盖同名入口；需要并存时可分别安装到不同 `--root` 并使用对应路径。

如需安装 main 的最新开发版本，可直接从 Git 安装：

```bash
cargo install --locked --git https://github.com/TangCan/proj-janitor.git --branch main proj-janitor
```

这会安装 main 当前源码，随后的提交可能改变功能；日常使用建议从 crates.io 安装已发布稳定版。

Rust 版会生成和读取 schema v2 manifest，支持 `scan`、`backup`、`restore`、`verify`、`prune`、`clean`、`init`。默认备份策略与 Python 版一致：优先选择 GitHub Git-only；不适用时，符合条件的干净本地仓库使用 Git bundle；其余项目创建 `tar.gz`。bundle 保留本地分支、tag 和这些引用可达的提交历史，不保存工作区以外的 Git 配置、reflog、stash、悬空对象、子模块对象或 LFS 对象。bundle 恢复需本机安装 Git。归档备份会把硬链接按普通文件内容保存，不保留链接关系；FIFO、设备和 socket 不归档。归档恢复拒绝跨平台越界路径、重复成员、硬链接、未知成员类型、大小写或 Unicode 规范化冲突路径和特殊文件；Unix 可恢复项目内符号链接。Windows 也支持文件与目录符号链接恢复，但运行账户必须有创建符号链接的权限。GitHub Git-only 恢复需要 Git；使用子模块或 Git LFS 时，还需要可访问对应远端，LFS 项目须将 `git-lfs` 安装到 PATH，安装方法见 [GitHub 文档](https://docs.github.com/en/repositories/working-with-files/managing-large-files/installing-git-large-file-storage)。

Rust 版首次从源码安装需要 Cargo 下载依赖；安装完成后的程序不需要 Python。`backup` 仍可用 `BACKUP_DEST` 或 `--backup-dest` 指定目录。

### Python 版

Python 实现支持 Python `>=3.10`；除 Python 3.10 依赖 `tomli`（3.11+ 用标准库 `tomllib`）外无第三方运行时依赖。源码元数据版本为 `0.1.48`；发布说明见 [Python 0.1.48 发布记录](docs/发布记录/0.1.48-Python版.md)。推荐安装：

```bash
pipx install proj-janitor
# 或
pip install proj-janitor==0.1.48
```

从 Git 安装最新 main，或开发时需要可编辑源码安装：

```bash
pipx install git+https://github.com/TangCan/proj-janitor.git
# 或
git clone https://github.com/TangCan/proj-janitor.git
cd proj-janitor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
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
# 归档时排除 .git 元数据，恢复后不含 Git 历史
projjan backup ~/richard_plus/bos_dynamic --backup-dest ~/backups/projects --always-archive --exclude-git-metadata
# 成功备份后每个项目仅保留最近 3 份快照（非交互执行需加 --yes）
projjan backup ~/richard_plus --backup-dest ~/backups/projects --keep 3 --yes
# 对 archive 策略生成差异快照：仅归档相对上次快照变更/新增的文件（恢复时按链重建）
projjan backup ~/richard_plus --backup-dest ~/backups/projects --incremental
```

也可设置默认备份目录，之后省略选项：

```bash
export BACKUP_DEST=~/backups/projects
projjan backup ~/richard_plus --dry-run
projjan backup ~/richard_plus
```

备份策略按项目自动选择：

- `git`：工作区干净（包含子模块）、无 stash，所有本地分支和本地 tag 均已推送，且远程及子模块远程受支持时，仅记录 Git 远程、分支、上游和提交 SHA，不生成归档。未初始化（目录内无额外文件）或已初始化且干净的子模块均支持；恢复时按主仓库记录的 gitlink 提交递归克隆子模块。含 Git LFS 属性的仓库也支持，但备份与恢复都要求安装 `git-lfs`，并使用默认 GitHub LFS endpoint。
- `git_bundle`：Git-only 不适用时，对工作区干净、无 stash、非浅克隆、无子模块且无 LFS 的仓库创建 `.bundle`，保存本地分支、tag 和这些引用可达的历史；校验 SHA-256，恢复时在隔离临时目录校验 bundle 并重建本地分支/tag，不写入 remote URL。bundle 不包括 Git 配置、reflog、stash 或悬空对象；提交历史中已删除的敏感信息仍会保留。使用 `--exclude-git-metadata`、`--always-archive` 或 `--include-artifacts` 时不会选择 bundle。
- `archive`：Git-only 与 bundle 都不适用时回退为 `tar.gz`，并在清单和 Rust/Python 命令输出中说明原因（例如 `working tree is not clean`、`Git LFS is not installed`、`submodule URL is not a supported GitHub URL`、`remote is not a supported GitHub URL`、`local tag is not pushed: v1`）。修改过或提交位置不匹配的子模块会回退归档；若需归档时 remote/submodule/LFS URL 含 userinfo、query 或 fragment，或本地 Git 配置含认证 header、密码、内联 helper、认证参数、自定义 SSH command 或项目内 credential 文件，自动备份会失败且不写归档或 manifest。
- 备份会在写入前拒绝 Windows 保留名或其他不可移植的项目目录名，避免生成无法由 restore 接受的 manifest。
- 存在本地 tag 时，URL-only 判定会执行 `git ls-remote --tags --refs <remote>`，逐个比较远程 tag 对象 ID；远程不可达、tag 缺失或指向不一致都会回退归档。没有本地 tag 时不会发起这次网络查询。
- 受支持的 GitHub HTTPS 主 remote URL 可以内嵌 PAT（如 `https://user:token@github.com/...` 或 `https://token@github.com/...`）；只有采用 Git-only 时才会安全使用，记录和命令输出会去掉用户名/令牌，只保留 `https://github.com/...`。如果需归档，则不论 fetch/push URL 是否受支持，只要其中含凭据、query 或 fragment，或本地 Git 配置含认证 header、credential password、helper 中无法识别的参数、敏感 service URL 或指向项目内的 credential 文件，自动备份都会报错并且不写 archive/manifest。helper 仅允许标准 `cache --timeout` 和 `store --file` 参数形式；Git helper 由 shell 执行；`store --file` 路径仅接受字面参数，shell 引号、变量、命令替换、named-home、通配符和其它展开语法会被拒绝。独立 `--file ~/path` 形式会按 home 目录展开并检查。其它未识别参数也按可能包含不透明编码凭据处理，避免其随 `.git/config` 落入归档。
- 子模块 URL 支持无凭据的 GitHub URL，以及相对于 GitHub 主仓库的 `./` / `../` URL。含凭据、query 或 fragment 的 URL 会让自动备份失败，不生成归档/manifest，避免敏感值被二次保存或写入恢复仓库的 `.git/config`。Git-only 备份只记录主仓库提交，不镜像子模块对象；恢复需要子模块远端仍可访问且固定提交仍存在。恢复会先在临时目录取回全部子模块，成功后才发布目标目录。Git bundle 暂不支持带子模块的仓库。
- Git LFS 内容与 Git 提交中的指针分开存放。恢复会在临时仓库配置本地 LFS filter 并执行 `git lfs pull`；LFS 对象缺失或不可访问时恢复失败，不创建目标目录。`.lfsconfig` 和自定义 LFS endpoint 暂不支持。
- 归档会包含 `.git/config` 和项目跟踪文件（包括 `.gitmodules`）。如果其中内嵌凭据，归档也会包含该凭据。显式使用 `--always-archive` 或 `--include-artifacts` 时仍会按要求打包；执行前应清理凭据并限制归档访问。

默认归档会排除构建产物与依赖缓存，避免把 `node_modules`、`target`、虚拟环境打进压缩包；`--include-artifacts` 强制完整归档，`--always-archive` 强制所有项目都归档。

Rust `0.2.6` 支持 `--exclude-git-metadata`：使用 archive 策略时跳过项目中的 `.git` 目录/文件，可显著缩小含大型 Git 历史的项目备份。该选项不修改源项目，也不强制 archive 策略；从这种归档恢复后只得到工作树，没有分支、tag、提交历史或 remote 配置。Python 源码与 PyPI `0.1.0` 均支持该选项。需要保留 Git 历史时不要使用。

每个项目使用一个稳定的项目目录，不再创建 `backup-.../` 运行目录。纯 `git` 备份只在该目录下写清单：

```text
backups/
└── xtask_todo/
    ├── manifest-20261023-122828.json
    └── manifest-20261023-122832.json
```

bundle 或需要归档的项目也写入同一个项目目录，文件名带运行时间戳：

```text
backups/
├── xtask_todo/
│   ├── manifest-20261023-103117.json
│   └── xtask_todo-20261023-103117.tar.gz
└── another_project/
    ├── manifest-20261023-103117.json
    └── another_project-20261023-103117.tar.gz
```

Git bundle 示例：

```text
backups/
└── local_history/
    ├── manifest-20261102-120000.json
    └── local_history-20261102-120000.bundle
```

使用 Git bundle 时，项目目录中保存 `.bundle` 文件和对应 manifest；manifest 中 `strategy` 为 `git_bundle`，`archive` 字段指向 bundle 文件。

- 一次运行包含多个项目时，每个项目目录各写一份只包含该项目记录的清单。
- 同一项目不同时间的清单和归档共用项目目录，文件名带时间戳。
- 不同根目录下存在同名、大小写不同或 Unicode 规范化等价的项目名时，项目目录会追加 8 位路径哈希，例如 `app-1a2b3c4d/`。
- 同一秒内重复备份同一项目时，文件名会追加 `-2`、`-3`，不会覆盖已有文件。
- 旧版本生成的 `backup-YYYYmmdd-HHMMSS/` 目录不会被自动迁移或删除。

清单使用 `schema_version: 2`，顶层记录 `include_artifacts`、`always_archive`、`exclude_git_metadata` 和 `records`。`exclude_git_metadata` 在项目使用 archive 策略且启用了排除选项时为 `true`；它表示所选策略，不表示源项目一定含有 `.git`。`git` 记录包含 `remote_url`、`branch`、`commit`、`upstream`；`git_bundle` 记录在 `archive` 字段保存 bundle 路径，并记录 `branch`、`commit` 和 SHA-256；`archive` 记录包含归档路径、大小、源大小、SHA-256 和可选的 `fallback_reason`。增量快照（archive 策略）另记录 `inventory`（本快照的完整文件清单：`path`/`sha256`/`size`）与 `incremental`（差异快照才有：`base` 指向上一个快照的清单文件名、`deleted` 为相对上次快照删除的路径列表）；完整快照不写 `incremental`，差异快照通过 `base` 串成链。

### 2.4 备份保留

按每个项目目录保留最近 N 份 manifest，默认只预览：

```bash
projjan prune ~/backups/projects --keep 3
```

实际删除需显式指定 `--apply`；交互终端会列出所有待删文件并要求确认。非交互或 JSON 执行还要加 `--yes`：

```bash
projjan prune ~/backups/projects --keep 3 --apply --yes
projjan prune ~/backups/projects --keep 3 --json
```

prune 只删除超出保留数量的 manifest，以及这些 manifest 在同一项目目录中明确引用的归档或 bundle。孤立归档、bundle、格式异常的清单和旧版 `backup-YYYYmmdd-HHMMSS/` 目录不会删除。`--keep` 必须至少为 1。

### 2.5 配置文件

可用 `projjan init`（或 `projjan init --dir PATH`）生成注释齐全的 starter `.proj-janitor.toml`；已存在时需加 `--force` 才会覆盖。

`.proj-janitor.toml` 提供常用选项的默认值，减少重复传参。`backup` 与 `prune` 都接受 `--config <PATH>` 显式指定；否则从当前目录向上逐级查找 `.proj-janitor.toml`（找不到则使用内置默认）。当前支持：

```toml
[backup]
dest = "/data/backups"   # 备份目标：当未传 --backup-dest 且未设 BACKUP_DEST 时使用
keep = 3                 # 成功备份后保留的最近快照数：当未传 --keep 时使用
exclude = ["node_modules", "*.log"]   # 排除模式：归档时跳过匹配的文件/目录（仅 archive 策略）
incremental = true          # 备份时记录差异快照（仅 archive 策略）：当未传 --incremental 时使用

[prune]
keep = 3                 # 保留数量：当未传 --keep 时使用
```

取值优先级为 **命令行选项 > 环境变量 > 配置文件 > 内置默认/报错**。例如未传 `--backup-dest` 且未设 `BACKUP_DEST` 时会读取 `[backup].dest`；仍未提供则报错并提示 `set BACKUP_DEST, pass --backup-dest, or configure [backup].dest`。未知键或非法值（如 `keep` 为 0 或负数）会在启动时被拒绝。`[backup].exclude` 支持 glob 排除模式（`*`、`?`、`[seq]`、`**`），按项目根相对路径逐段匹配；匹配到目录会跳过其整个子树。排除仅作用于 archive（tar.gz）策略的文件收集；`git` 与 `git_bundle` 策略不受影响（`git` 只记录远程信息、`git_bundle` 打包整个仓库历史）。不支持花括号展开（`{a,b}`），空模式会被拒绝。`[backup].incremental = true` 让 `backup` 默认生成差异快照（archive 策略）；`--incremental` 命令行选项可单独开启（优先级高于配置）。Python 版解析配置用标准库 `tomllib`（Python 3.11+；Python 3.10 自动安装 `tomli` 回退）；Rust 版内置 `toml` 解析。

### 3. 清理

```bash
projjan clean ~/richard_plus
projjan clean ~/richard_plus --apply
projjan clean ~/richard_plus --apply --yes
projjan clean ~/richard_plus --rules node,python-cache
projjan clean . --list-rules
```

默认展示将被删除的目录/文件及可回收空间，不会删除任何内容。确认后加 `--apply`；非交互环境还需 `--yes`。POSIX Python 使用目录文件描述符相对删除，并核对计划时记录的项目根身份及打开后的目标目录身份；Rust 记录并核对打开的目录 capability 根身份，再限定相对删除范围。Python Windows 暂使用根目录身份与路径校验回退。所有实现都会核对清理计划记录的目标身份，若目标已被替换则拒绝删除；极窄的检查与 unlink 间竞态仍需注意。

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

# 恢复前确认每个 Git-only 远端仍可访问；不可达则整批不开始（仓库源码）
projjan restore <manifest> --dest ~/restored --check-remote

# Rust 0.2.5 及更新版本：校验归档 SHA-256，并检查 Git 备份元数据（不联网）
projjan verify <manifest>

# 只校验指定项目，并输出 JSON
projjan verify <manifest> --project xtask_todo --json

# 额外联网确认 Git-only 记录的提交、子模块与 LFS 对象仍可用
projjan verify <manifest> --check-remote --json

# 也可在 .proj-janitor.toml 中默认开启，再用 --no-check-remote 临时关闭
# [verify]
# check_remote = true
# [restore]
# check_remote = true
```

`verify` 的 Git-only 结果默认标记为 `metadata-only`：不联网。若清单含 Git-only 记录且未启用 `--check-remote`（含配置默认值关闭），stderr 会提示 `warning: N git-only record(s) ... not remote-checked`（CR-143）。`git_bundle` 会校验 bundle 文件的 SHA-256。

加 `--check-remote`（或配置 `[verify].check_remote = true`）时，Git-only 记录改用下列状态；子模块与 LFS 也会探测，且**不下载对象**：

- `remote-verified`：远端至少有一个引用指向记录的提交。
- `remote-moved`：远端可达，但没有引用指向该提交。恢复按提交 SHA 抓取，因此提交仍可能从其它引用可达。
- `remote-unreachable`：`git ls-remote` 失败或超时，无法确认远端。
- `remote-skipped`：manifest 中的远端 URL 含用户名、口令、查询或 fragment；此时不会把该 URL 传给 Git，也不联网。
- `lfs-unavailable`：记录的 LFS 对象缺失或 LFS batch/本地对象库不可达。

任一记录为 `remote-unreachable` 或 `lfs-unavailable` 时，命令仍会输出完整报告，并以退出码 1 结束。`--json` 可含脱敏 `remote`、`submodules`、`lfs` 字段。`--no-check-remote` 可覆盖配置默认值。

恢复规则：

- 省略 `--dest` 时，目标取自每条记录的 `source`；`source` 必须是绝对路径、文件名等于项目名，且不能包含 `..` 或 NUL。伪造或损坏的 manifest 可能指向任意可写路径，因此实际恢复到原始位置前必须确认 manifest 来源可信。未传 `--dest` 时 stderr 会提示原位恢复风险（CR-144）；可用 `--require-dest` 直接拒绝。
- 原始位置模式默认要求交互确认；标准输入不是 TTY 时必须显式加 `--yes`，`--json` 的实际恢复也必须加 `--yes`。`--dest` 模式不需要该确认。
- `git` 策略：初始化仓库，将脱敏 URL 写入 `origin`，再直接从远程 fetch 分支和 tags（不写 `FETCH_HEAD`），最后 `git checkout --detach <commit>`。恢复后是 detached HEAD，不会自动创建或切换原分支；远程必须仍包含该提交。
- `git_bundle` 策略：校验 SHA-256 和 bundle 结构，从 bundle 取回本地分支与 tags，恢复 manifest 记录的分支和提交；不配置远程。
- Git 恢复的 init、remote add、fetch、checkout 和 HEAD 校验单步最长等待 10 分钟；超时会尝试终止 Git 进程树并报告失败，目标项目不会发布到最终路径。POSIX 上对进程组发送 `SIGKILL`；已 `setsid`/脱离进程组的后代可能逃逸（CR-131 有回归证明该已知限制），严格隔离需依赖 OS 级监督（cgroup/job）。
- 远程 URL 带凭据或查询参数时，Git 恢复失败会隐藏原始错误文本，避免错误输出回显敏感 URL。
- 带认证信息、查询或 fragment 的 URL 通过 Git 的临时配置环境传递；若本机 Git 不支持此方式，工具会拒绝恢复，并提示改用 credential helper/SSH 或升级 Git。
- 该方式避免凭据写入仓库配置或命令行，但同权限进程仍可能检查 Git 进程环境；共享机器应优先使用 credential helper/SSH。
- `archive` 策略：默认先按 manifest 校验 SHA-256；执行恢复时会在同一打开的归档句柄上重新校验，再从该句柄解包，避免计划后归档路径被替换。`--no-verify` 可显式跳过校验。
- 归档会先解压到临时目录，确认顶层目录为项目名后再移动到目标位置；拒绝绝对路径、`..` 路径、Windows 保留设备名/非法字符/尾随点空格、含冒号的成员路径/链接目标（避免 ADS）和越界链接。
- 目标目录已存在或是符号链接时拒绝覆盖；需要恢复副本时请指定 `--dest`，或先自行移走旧目录。
- 多项目恢复按顺序执行；默认中途失败即停止，错误信息会列出此前已恢复的项目和目标；`--continue-on-error` 可继续后续项目，JSON 含 `failures`；`--skip-existing` 可跳过已存在目标以便与续跑组合使用。
- manifest 中的归档绝对路径失效时，会尝试 manifest 同目录及 `<项目名>/` 下的同名归档，便于整体移动备份目录。
- 原始位置路径与备份时的机器和目录布局绑定；在另一台机器或不同用户下恢复时，应使用 `--dest`。
- 恢复后如需继续开发 Git 项目，可自行创建分支：

  ```bash
  git -C ~/restored/xtask_todo switch -c main
  ```

`git` 恢复依赖远程仓库和本机凭据配置；私有仓库可使用 `gh auth setup-git`、SSH 或 credential helper。恢复后，仓库的 `origin` 会移除 URL 中的认证信息、查询和 fragment，保留 SSH 登录名；使用查询参数定位仓库的特殊远程需在恢复后手动重设 `origin`。后续访问私有仓库需使用本机凭据配置。默认只校验 manifest 中的 URL/提交格式，并在存在 Git-only 记录且未启用远端预检时向 stderr 发出提示（CR-143）；加 `--check-remote`（或 `[restore].check_remote = true`）会在写入前预检远端引用、子模块与 LFS 对象可用性。

## 项目结构

```text
.
├── proj_janitor/       # Python 实现
│   ├── config.py       # 项目标记与清理规则
│   ├── models.py       # 数据模型与大小格式化
│   ├── scanner.py      # 项目发现、大小与 Git 状态
│   ├── backup.py       # URL-only 记录、tar.gz 归档与清单
│   ├── restore.py      # 清单校验、Git/归档恢复与安全解压
│   ├── retention.py    # 按项目预览/清理过期备份
│   ├── cleaner.py      # 清理计划与执行
│   └── cli.py          # argparse 命令行入口
├── src/                # Rust CLI
│   ├── main.rs         # CLI 入口与命令分发
│   ├── project.rs      # 项目发现与 Git 可恢复性
│   ├── backup.rs       # 备份计划执行与 manifest
│   ├── archive.rs      # 归档、校验与安全解压
│   ├── restore.rs      # manifest 预检与恢复
│   ├── retention.rs    # 备份保留计划与执行
│   ├── clean.rs        # 清理计划与执行
│   └── tests.rs        # Rust 单元测试
└── tests/
    ├── test_*.py      # Python unittest 测试
    └── rust_cli.rs    # Rust CLI 集成测试
```

## 开发

```bash
python3 -m unittest discover -s tests -v
```

验证快照（2026-11-07，Rust 0.7.14）：Python 258 项（257 通过、1 项 Windows 专属测试跳过）；Rust 84 项单元测试及 45 项 CLI 集成测试通过；`cargo fmt --check`、Clippy 和 Python/Rust archive、bundle、retention、远端预检及增量备份跨语言检查通过。完整 CI 状态见 GitHub Actions。版本化发布说明见 `docs/发布记录/`。Python 版除 Python 3.10 自动安装 `tomli` 外无第三方运行时依赖，要求 Python 3.10+。

Rust 版开发检查：

```bash
cargo fmt --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked
cargo package --locked
cargo audit
actionlint
```

Python 版回归检查：

```bash
python3 -m unittest discover -s tests -v
```

可选行覆盖率基线（不设门禁；需本机安装 `coverage` 与 `cargo-llvm-cov`）：

```bash
./scripts/measure_coverage.sh
```

2026-11-09 本地快照约：Python `proj_janitor` 83% 行；Rust `src` 约 84% 行（详见 `docs/结项文档/06-测试报告与缺陷清单.md` 第 12.42 节）。

## 许可证

本项目采用 [MIT License](LICENSE)。

安全漏洞报告方式见 [`SECURITY.md`](https://github.com/TangCan/proj-janitor/blob/main/SECURITY.md)。

## 后续计划

- ~~增量备份与自动/定时保留~~：自动保留已实现——`backup --keep N`（或 `[backup].keep`）在成功备份后按项目仅保留 N 份最新快照；增量备份已实现——`backup --incremental`（或 `[backup].incremental = true`）对 archive 策略生成差异快照（仅归档相对上次快照变更/新增的文件与符号链接，并记录完整文件清单 `inventory` 与删除列表 `deleted`），保留时按链整体保留（Scheme A），恢复时先解出最近完整快照再逐级应用差异。
- ~~配置文件（排除规则、备份目标、保留策略）~~：已实现 `.proj-janitor.toml` 提供 `[backup].dest`、`[backup].keep`、`[backup].exclude` 与 `[prune].keep` 默认值。`[backup].exclude` 为 archive 策略提供用户自定义排除模式（glob，按项目根相对路径逐段匹配，目录匹配则跳过其子树）；`git`/`git_bundle` 策略不受影响。
- ~~子模块/LFS 远端预检~~：已实现——Git-only 记录备份时记录 `submodules`（`[{path,url,commit}]`）与 `lfs_objects`（`[{oid,size}]`，经 `git lfs ls-files --long` + pointer blob；旧清单中的裸 OID 字符串仍可读）；`verify`/`restore --check-remote` 对子模块做 `git ls-remote` 预检，并对 LFS 对象做可用性探测（本地/`file://`/`url.*.insteadOf` 查 `lfs/objects`，HTTPS 走 LFS batch API，**不下载对象**）。缺失或不可达时 verify 置 `lfs-unavailable`，restore 在创建目标前整批不开始。
- ~~`--check-remote` 配置默认值~~：已实现
- ~~敏感路径告警（R-05）~~：已实现——archive 策略备份/dry-run 时对 `.env`、`id_rsa`、`*.pem`、`.aws/credentials` 等常见敏感路径输出 stderr 警告（默认不中断；归档仍不加密）。可用 `--fail-on-secrets` 或 `[backup].fail_on_secrets = true` 改为硬失败（不写归档）；可用 `--exclude-secrets` 或 `[backup].exclude_secrets = true` 从归档中省略这些路径。——`.proj-janitor.toml` 支持 `[verify].check_remote` / `[restore].check_remote`；CLI `--check-remote` / `--no-check-remote` 可覆盖。可用 `--require-dest` 或 `[restore].require_dest = true` 禁止按 manifest `source` 原位恢复（R-14）；可用 `--allowed-root` / `[restore].allowed_roots` 限制恢复目标根目录（CR-140）。可用 `--progress` 或 `[backup].progress = true` 输出逐项目与打包进度（含耗时），并在 `--dry-run` 时估算排除后的文件数/体积；可用 `projjan init` 生成 starter 配置；可用 `--check-space` 或 `[backup].check_space = true` 在目标盘可用空间低于估算时拒绝 archive；海量小文件与多项目扫描有 CI 容量回归（CR-107，`PROJ_JANITOR_STRESS_SCALE` 可放大；R-08，无 SLA）。可用 `--fail-on-insecure-dest` 或 `[backup].fail_on_insecure_dest = true` 拒绝 group/other 可访问的既有备份目录（R-05；Unix）；`restore` 亦支持同名选项/`[restore].fail_on_insecure_dest`（CR-139）；新建恢复父目录默认 `0700`（CR-141）。可用 `--encrypt-passphrase-file` / `[backup].encrypt_passphrase_file` 经 openssl 加密 archive/bundle，恢复时用 `--decrypt-passphrase-file`（R-05；需 PATH 中有 openssl；Unix 上 passphrase 文件不得含 group/other 权限位，建议 `chmod 600`，CR-135；dry-run 同样预检，CR-136）。

