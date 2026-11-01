# 2026-11-01 推送内容敏感信息审计

审计对象：`codex/ros2-mobile-collection`（`bab4580`）与 `feat/local-bimanual`（`805357d`）。
这两条分支此前不存在于远端，因此**分支上的全部内容都是本次新增公开面**。

结论先说：**没有私钥、没有公钥/私钥内容、没有任何 API token、没有公网 IP。**
带出去的是若干本地标识信息，均为低风险，明细在下面。本轮按用户要求**不做脱敏、不重写历史**，
本文作为已知暴露面的记录。为避免二次扩散，本文不逐字复写被暴露的字符串。

## 1. 审计范围与方法

| 项 | 值 |
| --- | --- |
| 对比基线 | `origin/main` = `07216e0`（审计前已在公网） |
| 新增文件 | 小车分支 409 个（29 个提交），双臂分支 105 个（1 个提交） |
| 覆盖方式 | 逐个读取新增/修改的文本文件；对每个新增提交再做一次全树 `git grep`，防止"提交后又被删掉"的痕迹漏检 |
| 二进制 | 5 张 JPEG 用 Pillow 读 EXIF（全部 `exif_keys=0`、无 GPS）；STL/MP4/so 等跳过正文扫描 |
| 未覆盖 | 未做熵值全量建模；未审计 `main` 既有历史；未审计 GitHub 侧 fork/缓存 |

## 2. 未发现的项（0 命中）

| 类别 | 结果 |
| --- | --- |
| `-----BEGIN … PRIVATE KEY-----` 私钥块 | 0（含 30 个新增提交的完整历史） |
| GitHub / HuggingFace / OpenAI / AWS / Slack / Google token 形态 | 0 |
| `password=` / `api_key=` 等硬编码赋值 | 唯一命中是 `sync_space.yml` 的 `password=$HF_TOKEN`，引用 Actions secret，不是明文 |
| `.env`、`.pem`、`.key`、真实 `servers.toml` | 0（只有 `servers.example.toml`，地址为 `192.0.2.x` 文档网段） |
| 公网 IPv4 | 0。正则先命中 110 处，逐条核对后**全部**是 `uv.lock` 里的版本号（如 `3.54.1.0`）与 dist 里的版本串 |
| 训练服务器地址与 SSH 端口 | 0（未出现在任何一个新增文件里） |
| 模型权重 / 数据集 / bag / mcap | 0 新增。仓库里 41 个 `.safetensors`/`.bag` 是 LeRobot 上游测试样本，`main` 上本来就有 |
| `work/` 与 `plans/` 内容 | 0（`.gitignore` 生效，未被强制添加） |
| JPEG 地理位置 | 0（5 张图无 EXIF） |

## 3. 已暴露的本地标识（低风险，均为本次新引入）

`main` 上原本都没有这些，可用第 5 节命令复现计数。

| 类别 | 新增文件数 | 说明与影响 |
| --- | --- | --- |
| 本机 SSH 私钥**文件名** | 小车 1 处、双臂 2 个文件共 7 处 | 只有文件名与"BatchMode 下全部 Permission denied"的结论，没有任何密钥内容。影响：暴露了本机存在哪几个命名习惯的密钥文件 |
| 本机网卡与树莓派网卡 MAC | 2 个文件 | 二层标识。影响：同网段内可据此定位设备；跨网段无意义 |
| 私网 IP `192.168.x` | 小车 15 个、双臂 6 个文件 | 树莓派车端地址、宿主无线网段等。影响：暴露实验室/家庭网段布局，需先进入同一网络才有用 |
| 本机绝对路径 `/home/liaozhengyan/...` | 小车 10 个、双臂 24 个文件 | 多数来自 pytest 输出与 evidence JSON。影响：暴露用户名与目录结构；用户名在提交作者名里本来就公开 |
| 四台舵机总线序列号 | 双臂 20 个文件 | `/dev/serial/by-id/...` 与 `four-arm-inventory.json`。影响：设备身份可被识别，不构成访问凭据 |
| 本机 UID/组（含 `27(sudo)`） | 双臂 1 个文件（`env-local.txt`） | 影响：说明该账号在 sudo 组，属于权限画像信息 |

判断依据：以上都不是凭据，不能直接用于认证；但它们降低了针对本机/本网段的侦察成本，
所以记录在案。真正的门禁类信息（口令、私钥、token、公网入口）一条都没有出去。

## 4. 后续若要清理

| 方案 | 效果 | 代价 |
| --- | --- | --- |
| 新增脱敏提交 | 当前 HEAD 干净 | 旧 blob 仍在分支历史里，GitHub 可翻到 |
| 脱敏 + 重写历史 + force push | 旧 blob 从分支消失 | 两个分支哈希变化，已发出的 compare/PR 链接失效 |
| 换密钥（若坚持认为文件名暴露不可接受） | 一次性消除关联 | 需要重新分发公钥到各设备 |

本轮未执行以上任何一项。

## 5. 复现命令

```bash
cd /home/liaozhengyan/Desktop/lelab

# 1) 私钥与 token 形态（含历史）
for c in $(git rev-list origin/main..codex/ros2-mobile-collection); do
  git grep -I -n -E "BEGIN [A-Z ]{0,30}PRIVATE KEY|ghp_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{25,}" $c -- 2>/dev/null
done

# 2) 新增文件里的私钥文件名 / MAC / 绝对路径
git diff --name-only origin/main...codex/ros2-mobile-collection \
  | xargs grep -InE "\.ssh/[A-Za-z0-9._-]+|([0-9a-f]{2}:){5}[0-9a-f]{2}|/home/liaozhengyan" 2>/dev/null

# 3) 非私有 IPv4（应只剩 uv.lock 的版本号）
git diff --name-only origin/main...codex/ros2-mobile-collection | xargs grep -InE "\b([0-9]{1,3}\.){3}[0-9]{1,3}\b" 2>/dev/null | grep -v "192.168\.\|127\.0\.0\.1\|192\.0\.2\."
```

## 6. 边界

- 本审计只看**仓库内容**，不判断 GitHub 仓库可见性设置（public/private）、fork 数量或搜索索引状态。
- "未命中"只代表没匹配到本文列出的模式，不等于对所有可能的敏感信息形式都做过证明。
- 未审计 `main` 既有的 1296 个跟踪文件；如需要，可另开一轮。
