# 新仓库 ADR 索引

> ADR（Architecture Decision Record）：**一篇一个决策**，写清「结论 / 为什么 / 代价 / 未决」。
> 状态：`Accepted`（已定，照做）· `Proposed`（有推荐，待评审）· `Superseded`（被取代）。
> 模板在文末。新决策**新增文件**，不改历史。

| ADR                                      | 标题                                           | 状态     |
| ---------------------------------------- | ---------------------------------------------- | -------- |
| [0001](0001-backend-language.md)         | 后端语言与运行时：C# / .NET 8+                 | Accepted |
| [0002](0002-service-topology.md)         | 服务拓扑：Portal / Api / 静态面板 三服务       | Accepted |
| [0003](0003-inference-boundary.md)       | 推理层边界：Python 无状态 GPU worker           | Accepted |
| [0004](0004-control-data-plane.md)       | 控制面 / 数据面分层与账务归属                  | Accepted |
| [0005](0005-quota-and-payment-path.md)   | 热路径与配额：后付费 + 护栏 + 扣押产物         | Accepted |
| [0006](0006-job-protocol.md)             | 对外作业协议：复用 OGC API-Processes + NodeODM | Accepted |
| [0007](0007-result-transport-and-lod.md) | 结果与产物传输 + 点云 LOD                      | Proposed |
| [0008](0008-frontend-stack.md)           | 前端技术栈：Vite + TS + Vue 3 + three.js       | Accepted |
| [0009](0009-model-code-handover.md)      | 模型代码交接方式                               | Proposed |

**这两篇必须先看**：0002（几个服务）与 0007（结果怎么传），它们决定阶段一/二的接线。
**这篇最容易被忽略**：0005 —— "提交时不查配额"是**已定的产品取舍**，不是漏了。

---

## ADR 模板

```markdown
# ADR-XXXX：<一句话决策>

- 状态：Proposed | Accepted | Superseded by ADR-YYYY
- 日期：YYYY-MM-DD
- 关联：ADR-… / issue #… / docs/handover/…

## 背景

（现在是什么样、为什么会疼；只写与这个决策有关的事实）

## 决策

（我们决定做什么；必要时代码/接口片段）

## 代价与后果

（放弃了什么、引入了什么新约束；哪一步以后会后悔）

## 未决

（这个决策还没关掉的分支，及各自的触发条件）
```
