# proj-janitor Python CLI

扫描、备份、恢复和清理本地项目的命令行工具。Python 版要求 Python `>=3.10`，没有第三方运行时依赖。

## 安装

```bash
python -m pip install proj-janitor
```

安装后运行 `projjan --help`。也可以运行 `python -m proj_janitor --help`。

## 功能

- `scan`：盘点项目目录与 Git 状态。
- `backup`：按项目情况选择 Git 元数据、Git bundle 或归档；备份目录可使用 `BACKUP_DEST` 或 `--backup-dest` 指定。
- `restore`：校验 manifest 和归档摘要后恢复，不覆盖已存在的目标目录。
- `verify`：验证归档或 Git bundle 的 SHA-256。
- `prune`：默认预览过期备份；删除需要 `--apply` 和确认。
- `clean`：默认预览可清理的构建产物；删除需要 `--apply` 和确认。

完整说明、示例与安全边界见 [GitHub README](https://github.com/TangCan/proj-janitor/blob/main/README.md)。如需免 Python 的独立 CLI，可用 Rust 安装：`cargo install --locked proj-janitor`。
