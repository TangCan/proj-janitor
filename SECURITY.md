# 安全策略

## 支持的版本

安全修复优先针对：

- crates.io 最新 Rust 发布版（当前 `0.7.x`）；
- PyPI 最新 Python 发布版（当前 `0.1.x`）；
- `main` 分支。

旧版本不保证回补。具体修复与披露时间取决于问题严重性和复现情况。

## 报告安全问题

请不要在公开 GitHub issue 中披露疑似漏洞。优先使用仓库的 **Security → Advisories → Report a vulnerability** 私密入口；若不可用，发送邮件至 [tang_can@qq.com](mailto:tang_can@qq.com)。

请说明：受影响版本、操作系统、复现步骤、可能影响；尽量使用最小化示例，不要附真实备份、访问令牌或其他用户数据。

## 威胁模型

`proj-janitor` 主要防范**不可信归档/清单/仓库元数据**在备份、校验、恢复过程中造成的破坏，以及误操作导致的删除/覆盖。它**不是**操作系统沙箱，也不试图防御：

- 已取得本机账户权限的攻击者；
- 恶意 Git 服务端、Git 客户端或 openssl 自身的漏洞；
- 用户显式关闭校验（如 `--no-verify`）或使用不可信配置；
- 备份介质本身的物理/加密层失守。

## 已实现的安全属性

### 归档路径与解压

- 拒绝绝对路径、`..` 穿越、反斜杠、Windows 保留名（含 Unicode/NFKC 变体）、尾随点/空格、C0/C1 控制字符、Unicode `Cf` 不可见格式字符（保留 ZWNJ/ZWJ）、Tag 字符、`U+2028`/`U+2029`；
- 拒绝硬链接、设备/FIFO 等特殊成员，符号链接目标必须留在项目内；
- 成员路径经大小写/Unicode 归一化冲突检查；恶意归档在写入前失败；
- 恢复先解压到目标父目录下的私有暂存目录，成功后再发布为最终目标；失败不留下半成品。

### 完整性与覆盖控制

- `verify` 与 `restore` 默认校验 archive/bundle 的 SHA-256；
- 默认不覆盖已有目标；`--force` 才替换，`--keep-replaced` 可旁侧保留旧目录；
- `--skip-existing`、`--continue-on-error`、`--rollback-on-error` 提供显式续跑/回滚语义；
- 恢复目标/父目录在 Unix 上以 `0700` 创建；可启用 `--fail-on-insecure-dest` 拒绝 group/other 可访问目录。

### 磁盘与资源

- `backup --check-space` 在归档前估算空间；
- `restore --check-space`（可选）在写入前按 inventory/`source_bytes` 估算解压总量，同一父目录合计，稀疏/超大成员提前停止扫描；
- 恢复失败时暂存目录会清理；不自动删除用户原有目标（除非显式 `--force`）。

### 凭据与远程

- 敏感路径（`.env`、私钥等）默认告警，可 `--fail-on-secrets` 或 `--exclude-secrets`；
- 含凭据/query 的 remote URL 不写入归档；Git-only 备份只记录脱敏 URL；
- `--check-remote` 可在恢复前探测提交/子模块/LFS 可用性；不可达时整批不开始。

### 发布与供应链

- GitHub Actions 使用 SHA pin 的第三方 action；
- 发布前要求目标提交 CI 全绿，并校验版本、`Cargo.lock`、README 与发布记录一致性；
- PyPI 使用 Trusted Publisher；crates.io 使用仓库 secret；
- 依赖安全告警由 `pip-audit` / `cargo audit` 在 CI 检查。

## 明确的非保证

- **不是沙箱**：恢复 Git 仓库时，仓库自带的 filter/配置可能被 Git 执行；不要恢复不可信仓库到敏感环境。
- **校验可选**：`--no-verify` 会跳过 SHA-256，只应用于完全可信的归档。
- **空间预检是估算**：默认关闭；inventory/`source_bytes` 可能高估或低估，文件系统元数据与并发写入也会影响结果。
- **无 SLA**：容量与性能有回归基线，但不承诺固定耗时/内存上限。
- **覆盖率不是安全证明**：CI 覆盖与恶意归档矩阵只是证据，不能保证不存在未知绕过。
- **加密可选**：archive/bundle 默认不加密；需要时使用 `--encrypt-passphrase-file`，并妥善保管 passphrase。

## 加固建议

- 对不可信归档始终保留校验，不使用 `--no-verify`；
- 需要恢复前空间检查时配置 `[restore].check_space = true`；
- 重要项目使用 `--always-archive`，并保留完整增量链；
- 对 Git-only 备份启用 `--check-remote`；
- 在隔离副本上验证原位恢复，不要直接用备份覆盖唯一副本；
- 定期轮换 crates.io token，仓库 secret 仅授予最小 scope。
