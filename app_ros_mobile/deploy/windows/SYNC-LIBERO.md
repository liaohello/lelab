> Public copy: connection addresses, account names and machine labels below are anonymized examples. Configure your own local deployment settings.

# 同步远程 LIBERO 开发文件

服务器：`trainer@192.0.2.10:22`。
远程目录：`/home/trainer/Desktop/lelab_server/libero`。

在本地 LeLab 项目根目录执行：

```powershell
# 查看文件差异，不修改本地文件。
.venv/Scripts/python.exe deploy/windows/sync-libero.py

# 拉取服务器上的脚本、配置记录和评估结果。
.venv/Scripts/python.exe deploy/windows/sync-libero.py --apply
```

该脚本使用已配置的 SSH 密钥，只读取服务器文件，不修改远程环境或服务。
这是按需执行的同步命令，没有设置后台自动同步。

## 本地位置

| 远程文件 | 本地文件 |
| --- | --- |
| `scripts/*` | `deploy/train/libero/*` |
| `DEPLOYMENT*.md` | `deploy/train/libero/DEPLOYMENT*.md` |
| `deployment.json`、`libero.env`、`.lelab-libero.json` | `runs/libero/192.0.2.10/` |
| `results/*` | `runs/libero/192.0.2.10/results/*` |
| 同步清单、SHA-256、时间 | `runs/libero/192.0.2.10/sync-manifest.json` |

`runs/` 是正常的本地目录，已被项目 `.gitignore` 排除。
它仍可在文件管理器中查看；IDE 若启用了隐藏 Git 忽略文件，需要关闭该过滤选项。
代码与部署说明保留在可提交的 `deploy/` 中。

## 校验与保留修改

先下载到时间戳暂存目录，对所有文件核对 SHA-256，再写入目标位置。
源码仅有 CRLF/LF 差异时保留本地文件。需要覆盖的本地文件先保存到：

```text
runs/libero/192.0.2.10/.sync-backups/<UTC时间戳>/
```

不同内容的脚本会以服务器版本更新，本地旧版本保留在上述备份目录；不自动合并源码。
本地独有文件不会删除。`sync-manifest.json` 记录更新和备份路径。

不复制模型权重、虚拟环境、tokenizer/编译缓存和容器文件系统。
默认允许最多 512 MiB 的选定文件；评估结果增多后可显式调整 `--max-mb`。
旧的 `.smoke/libero-20260928/` 副本保留，后续按此脚本同步到 `runs/`。

2026-09-28 首次同步：校验 28 个远程文件；创建 23 个配置/结果文件；
5 个本地脚本与说明文件已经一致，无需覆盖。
