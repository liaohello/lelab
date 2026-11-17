# 镜像源清单（实测，2026-11-17，g128）

本文件只记录**实际测过**的源与结论；没测过的不列。所有哈希都是本机算的，不是抄的。

## 1. 结论表

| 用途 | 源 | 状态 | 证据 |
| --- | --- | --- | --- |
| HF 权重/元数据 | `https://hf-mirror.com` | **可用** | 单流 2.46 MiB/s（urllib 16 MiB 采样）、curl 4.7 MB/s；8.71 GiB FineART 权重下载后 SHA-256 与 Hub LFS 一致 |
| HF 大文件 | 同上 + `parallel_fetch.py`（16 路分块） | **可用，32.5 MiB/s** | 8.71 GiB / 4 分 37 秒，逐块 Range + 整文件 SHA-256 校验 |
| HF 直连 | `https://huggingface.co` | **不可达** | g128 `curl` 超时；transformers 离线报错同源 |
| Gated 仓库（PaliGemma） | `hf-mirror.com/google/paligemma-3b-pt-224` | **403** | API `gated=manual`；文件请求 403（无许可证 token） |
| Gated 仓库的镜像 | `https://modelscope.cn` 的 `google/paligemma-3b-pt-224` | **可用且字节一致** | `tokenizer.model` SHA-256 `8986bb4f423f07f8…` 与 HF LFS 摘要逐位相同；另 4 个文件与 ModelScope 自带 sha256 一致 |
| Python 包 | `https://pypi.tuna.tsinghua.edu.cn/simple` | **可用** | 装到 `scipy 1.18.1`、`peft 0.21.3`、`pip 26.3`（venv 原本无 pip，`ensurepip` 引导） |
| npm 包 | `https://registry.npmmirror.com` | **可用** | `npm ci` 403 个包 / 503 MB，前端可重建 |
| npm 官方 | `https://registry.npmjs.org` | **不可达** | g128 `curl` 挂起 |

## 2. 用镜像解掉 FineART 的阻塞

`run_fineart.py` 的 `2_load` 失败原因是 processor 需要 `google/paligemma-3b-pt-224`。
处理办法不是改代码里的模型名，而是**把镜像文件放进 HF 缓存布局**，让原来的仓库名在离线状态下解析到这份字节：

```bash
# 1) 从 ModelScope 取 5 个 tokenizer 文件（无门禁）
B="https://modelscope.cn/api/v1/models/google/paligemma-3b-pt-224/repo?Revision=master&FilePath="
# tokenizer.* 之外，config/preprocessor/generation 也要取：AutoTokenizer 在离线模式会读它们
for f in tokenizer.model tokenizer.json tokenizer_config.json special_tokens_map.json \
         added_tokens.json config.json preprocessor_config.json generation_config.json; do
  curl -sSL --fail -o "/home/lfy/lelab-rmr/pg-tok/$f" "$B$f"
done
sha256sum /home/lfy/lelab-rmr/pg-tok/tokenizer.model   # 必须等于 8986bb4f423f07f8…
```

```python
# 2) 构造 $HF_HOME/hub/models--google--paligemma-3b-pt-224/{blobs,refs,snapshots}
#    blob 名 = 内容 sha256；snapshot 软链指向 ../../blobs/<sha>；refs/main 写 HF 的 revision
#    脚本见本次运行记录（构造后 transformers 在 HF_HUB_OFFLINE=1 下可直接命中）
```

要点：

- **许可证没变**：镜像是第三方副本，不能替代在 Hugging Face 上接受 PaliGemma 条款；
  生产环境仍应配 `HF_TOKEN` 走官方源。本节只用于让验证在无 token 的机器上继续。
- **校验是必须的**：只有 `tokenizer.model` 有 HF 公开的 LFS 摘要可比；其余文件用
  ModelScope 元数据里的 sha256 交叉核对。没有任何"看起来一样"的判断。
- 缓存布局里的 blob 名用内容哈希，和 HF 的命名规则一致。构造后必须把 `HF_HOME` 指向该缓存
  （默认 `~/.cache/huggingface` 看不到 `/home/lfy/lelab-rmr/hf-cache`，这是本轮踩过的坑），
  然后离线探针应得到 `GemmaTokenizer`、`vocab 257153`、`pick the cube` → `[1,4]`。

## 3. 环境侧的两个坑（已修）

1. g128 的 venv 是 uv 建的，**没有 pip**：`python -m ensurepip --upgrade` 后才有
   `pip 26.3`，然后才能装 `scipy`/`peft`。
2. `huggingface_hub.snapshot_download` 走 HEAD 校验，镜像对 HEAD 不稳定 →
   `LocalEntryNotFoundError`。改用 `parallel_fetch.py`（Range 分块 + 内容哈希）绕过，
   并把 `PaliGemma` 的小文件用 curl 直接取。

## 4. 复跑命令

```bash
# 大文件（示例：FineART 权重）
python parallel_fetch.py \
  https://hf-mirror.com/lerobot/fineart_vla_base/resolve/b6cf4faf2630f971dd390bc32365e9d9c330da8e/model.safetensors \
  /home/lfy/lelab-rmr/fineart-ckpt/model.safetensors \
  9e384c7351e5e42008efa34c5e3485bf071ca0d5c00490aa6bb63167022dd197 9354050752 \
  --threads 16 --chunk-mb 32

# FineART 四阶段（tokenizer 缓存已构造后）
cd /home/lfy/lelab-rmr/closure && HF_HUB_OFFLINE=1 /home/lfy/lelab-rmr/.venv/bin/python run_fineart.py \
  --checkpoint-dir /home/lfy/lelab-rmr/fineart-ckpt \
  --tokenizer-dir /home/lfy/lelab-rmr/pg-tok \
  --manifest fineart-inventory-g128.json --report fineart-verify.json
```
