# 模型路由策略

## 1. 决策目标

先满足任务的质量与风险下限，再在满足下限的候选中选择更省额度、反馈更快的模型。不要把“一个项目”永久绑定到一个模型；每次按“项目上下文 × 当前任务 × 所处阶段 × 错误代价”判断。

路由只决定计算资源，不决定项目是否值得做，也不替代事实核验、内容门禁、用户验收或生产审批。

## 2. 输入字段

把当前任务归一化为以下字段。除非某个未知项会改变风险下限，否则先根据现有证据推断，不要求用户填写问卷。

| 字段 | 取值 | 判断重点 |
|---|---|---|
| `stage` | `investigate` / `plan` / `execute` / `verify` / `deploy` | 当前是调查、规划、执行、验证还是上线 |
| `action_scope` | `read_only` / `local_write` / `external_effect` | 是否会写本地、触达外部或改变现实状态 |
| `task_kind` | `coding` / `research` / `content` / `media` / `business` / `governance` / `operations` | 当前任务类型，不是项目名称 |
| 七项评分 | 1—5 整数 | `complexity`、`ambiguity`、`context_load`、`tool_load`、`error_cost`、`latency_need`、`repeatability` |
| 三个布尔值 | `true` / `false` | `parallelizable`、`rapid_coding_iteration`、`text_only` |
| `preference` | `balanced` / `quota` / `quality` | 平衡、优先额度或优先质量 |
| `risk_flags` | 字符串数组 | `production`、`public_release`、`payment`、`legal`、`security`、`privacy`、`client_delivery`、`irreversible` |

评分锚点：1 表示范围清晰、上下文少、错误易回滚；3 表示日常多步骤工作；5 表示开放性强、跨系统或错误会造成生产、金钱、合规、客户或声誉影响。

## 3. 风险下限

按最高命中项确定 `risk_floor`：

| 下限 | 条件 | 最低路由 |
|---|---|---|
| `critical` | 命中任一 `risk_flags`，或进入 `deploy` | `gpt-5.6-sol` + `high` |
| `high` | `external_effect`，或 `error_cost >= 4` | `gpt-5.6-sol` + `high` |
| `medium` | `error_cost == 3`，或复杂度、歧义、上下文、工具负荷任一 `>= 4` | 至少 `gpt-5.6-terra` + `medium` |
| `low` | 其余任务 | 可在 Spark、Luna、Terra 中选择 |

`quota` 偏好不能突破风险下限。额度不足而任务又是高风险时，保持原路由，并建议缩小范围、分阶段、等待重置或使用额外 credits；不要静默降档。

## 4. 模型选择顺序

从上到下命中第一条适用规则：

1. **Ultra 并行任务**：任务可拆成彼此独立的部分，且不是上线或外部动作，并满足 `complexity == 5`、`context_load == 5`、`tool_load >= 4`，选 `gpt-5.6-sol` + `ultra`。Ultra 是多 Agent 并行，不是普通“更深思考”；生产变更不得因可并行而自动走 Ultra。
2. **关键或高风险任务**：`risk_floor` 为 `critical` 或 `high`，选 `gpt-5.6-sol`。默认 `high`；只有处于生产部署或 `error_cost == 5` 时自动升到 `xhigh`。其他任务即使负荷较高，也先以 `high` 获取代表性结果，再按实测质量决定是否升档。
3. **极速小型编码迭代**：同时满足 `task_kind == coding`、`rapid_coding_iteration == true`、`text_only == true`、无风险标记、非外部动作、`complexity <= 2`、`ambiguity <= 2`、`context_load <= 3`、`tool_load <= 3`、`error_cost <= 2`，选 `gpt-5.3-codex-spark` + `high`。
4. **清晰且可重复的批处理**：`ambiguity <= 2`、`repeatability >= 4`、`complexity <= 3`、`tool_load <= 3`、`error_cost <= 2`、非外部动作，选 `gpt-5.6-luna` + `medium`。若范围极小且优先速度或额度，可用 `low`。
5. **复杂开放任务**：`complexity >= 4`，并且歧义或上下文至少一项 `>= 4`，选 `gpt-5.6-sol` + `high`。先用代表性结果判断 `xhigh` 是否带来可测质量提升，不因评分较高自动升级。
6. **日常多步骤工作**：其余任务选 `gpt-5.6-terra` + `medium`；任一主要负荷 `>= 4` 或 `error_cost == 3` 时用 `high`。

不要自动推荐 GPT-5.5。它只用于已经用代表性样本证明 5.5 明显更合适的遗留流程或 A/B 对照，且必须说明证据。

## 5. 推理档位调整

- `low`：范围小、完成定义明确、错误易回滚。
- `medium`：日常多步骤任务的默认平衡档。
- `high`：存在多步骤、多个来源、工具调用或权衡。
- `xhigh`：复杂、开放、高价值或高错误代价任务。
- `max`：仅用于难以拆分、最困难、深度明显比速度和额度更重要的单体问题；不要由普通评分自动触发。
- `ultra`：仅用于可以真实拆分的复杂任务；不要把顺序依赖强的任务硬拆并行。

`quality` 偏好可以将 `medium` 升到 `high`；只有代表性结果显示质量不足或用户明确要求时，才将 `high` 升到 `xhigh`。不得自动升到 `max` 或 `ultra`。`quota` 偏好只在不突破风险下限时将 `high` 降到 `medium` 或将 `medium` 降到 `low`。

## 6. 额度与重新路由

只使用“低 / 中 / 高 / 很高”相对标签。实际消耗还受上下文长度、输出长度、推理档位、工具、图片和缓存影响，不承诺精确节省比例。

以下情况重新生成路由卡：

- 新项目首次进入实质工作；
- 项目从调查转为执行、从本地验证转为上线等阶段变化；
- 动作范围、风险、上下文或交付对象显著变化；
- 恢复一项已经中断的重要工作；
- 用户明确要求改为省额度或保质量。

同一阶段内的连续执行、普通追问和微小修改沿用已确认路由，不重复打断用户。
