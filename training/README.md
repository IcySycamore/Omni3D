# training —— 模型训练与评测

**训练 / 评测侧**的东西都在这里。使用侧（`server/` 推理核心 + `panel/` 服务）对这里**零引用**。

| 目录 | 内容 |
|---|---|
| `configs/` | Hydra 配置树：`train.yaml` / `eval.yaml` / `trainer/` / `callbacks/` / `eval/` … |
| `notebooks/` | 数据探索与论文配图（保留上游作者的 notebook 原文） |
| `scripts/` | 评测脚本：`run_benchmark.py`、`fast3r_re10k_pose_eval.py`、`robustmvd_eval.py`、出图 |

## 训练

```bash
pip install -r requirements.txt    # 研究栈；根目录的 requirements-app.txt 是服务侧用的
python fast3r/train.py             # Hydra 入口，配置树在本目录的 configs/
python fast3r/resume_train.py --run_dir <上一次的 run 目录>
```

## 评测

```bash
python training/scripts/run_benchmark.py --config training/configs/eval/benchmark_re10k.yaml
```

## 为什么模型代码不在这里

`fast3r/` 是 **vendored 的上游仓库**，训练与推理**共用**其中的
`models/`、`croco/models/`、`dust3r/{heads,utils,model}`，所以它留在仓库根。

服务侧只消费这几个推理入口（`grep` 可验证）：

```
fast3r.models.fast3r.Fast3R                       <- panel/server.py
fast3r.dust3r.utils.image.load_images             <- server/core/pipeline.py
fast3r.dust3r.inference_multiview.inference       <- server/core/pipeline.py
```
