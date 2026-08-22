# V0.1 真实任务试运行协议

> 收口状态（2026-08-23）：用户明确授权在原定样本与效率指标达标前升级到 `MRA_GLOBAL_GATE_V1`，因此 V0.1 已提前结束。该决定不是指标通过；最终有效数据为 5 个自然日、3 个路由决策、0 个已闭环任务、接受推荐率 66.7%、3 个实际配置待核实项和 0 个效率改善信号。`trial-state.json` 中的 `global_gate_enabled: false` 只描述历史 V0.1 的边界，当前 V1 安装状态以独立验收证据为准。

## 1. 目的与边界

在不启用全局强制门的前提下，连续观察 7 个自然日或至少 10 个真实闭环任务，验证模型路由建议是否安全、稳定、可接受，并留下可复核证据。本阶段只提示、确认和记录，不自动切换模型，不以推测值代替真实观测，也不据此承诺固定额度节省比例。

- 状态文件：`evidence/trial-state.json`
- 事件日志：`evidence/trial-events.jsonl`
- 日志格式：UTF-8 JSONL，每行一个 JSON 对象。
- 写入纪律：只追加，不改写、不删除既有事件。录入有误时，追加同类型更正事件，并用 `supersedes_event_id` 指向被更正事件。
- Git 锚点：首条启动事件必须先提交；以后每次有效追加在汇总校验通过后单独提交。提交前检查该文件相对上一个锚点只有尾部新增行，没有删除或改写历史行。汇总器负责结构与语义校验，Git 历史负责提供不可静默改写的外部锚点。
- 未知值：写 `null` 或省略可选字段；不得用 `0`、空字符串或主观估计冒充观测值。
- 汇总截止时间：`as_of` 只能是当前实际时间或过去时间；不得传入未来日期提前满足自然日门槛。

## 2. 通用事件字段

每个事件都包含：

| 字段 | 必填 | 约束 |
|---|---:|---|
| `schema_version` | 是 | 固定为 `1` |
| `event_id` | 是 | 日志内唯一字符串 |
| `event_type` | 是 | `trial_started`、`route_decision` 或 `task_outcome` |
| `trial_id` | 是 | 必须与状态文件一致 |
| `occurred_at` | 是 | 事件写入时间，使用 Asia/Shanghai 的 ISO 8601 时间，例如 `2026-08-19T10:12:22+08:00`；日志必须按此时间非递减追加 |
| `task_id` | 后两类必填 | 同一阶段的一次真实任务及其结果必须使用同一个标识；阶段变化重新路由时创建新标识；`trial_started` 为 `null` |
| `supersedes_event_id` | 否 | 仅更正既有记录时填写；必须指向当前有效叶节点，不能从同一旧事件分叉；`trial_started` 不允许更正 |
| `evidence_note` | 否 | 简短说明事实来源；未知时为 `null` |

## 3. 三类事件

### `trial_started`

必须作为日志第一条，包含：

| 字段 | 类型 | 含义 |
|---|---|---|
| `source_commit` | 字符串 | 本轮规则基线，固定为 `db98b44` |
| `global_gate_enabled` | 布尔值 | 固定为 `false` |
| `targets` | 对象 | 与 `trial-state.json.targets` 完全一致 |

### `route_decision`

只在路由卡已展示并得到用户明确确认后写入；接受推荐和改选都必须记录。

| 字段 | 类型 | 含义 |
|---|---|---|
| `project` | 字符串或 `null` | 项目名；无法确认时不猜测 |
| `task_summary` | 字符串 | 当前阶段的具体工作，一句话 |
| `task_class` | 字符串 | 用稳定分类表达同类任务；建议由 `task_kind/stage/action_scope/risk_floor/preference` 组成 |
| `router_version` | 字符串 | 必须与本轮锁定路由器输出的 `router_version` 一致 |
| `stage` | 字符串 | `investigate`、`plan`、`execute`、`verify`、`deploy` |
| `action_scope` | 字符串 | `read_only`、`local_write`、`external_effect` |
| `task_kind` | 字符串 | 路由策略定义的任务类型 |
| `preference` | 字符串 | `balanced`、`quota` 或 `quality` |
| `complexity`、`ambiguity`、`context_load`、`tool_load`、`error_cost`、`latency_need`、`repeatability` | 整数 | 路由时使用的原始 1—5 分，必须如实复制，不得从推荐结果反推 |
| `parallelizable`、`rapid_coding_iteration`、`text_only` | 布尔值 | 路由时使用的原始布尔输入 |
| `risk_flags` | 字符串数组 | 路由时使用的风险标记；允许值与路由器一致 |
| `risk_floor` | 字符串 | `low`、`medium`、`high`、`critical` |
| `route_trigger` | 字符串 | `new_project`、`stage_change`、`risk_change`、`scope_change`、`resume_significant_work`、`user_requested` |
| `recommended_model` | 字符串 | 路由卡推荐模型 |
| `recommended_effort` | 字符串 | 路由卡推荐档位 |
| `relative_quota` | 字符串 | 必须与路由器针对推荐模型和档位生成的相对额度标签一致；只表示相对量级，不表示固定消耗百分比 |
| `reason_codes` | 字符串数组 | 生成推荐的稳定原因码 |
| `user_choice` | 字符串 | `accept_recommended`、`prefer_quota`、`prefer_quality` 或 `custom`；不得写模糊值 |
| `selected_model` | 字符串 | 用户确认后实际选择的模型配置 |
| `selected_effort` | 字符串 | 用户确认后实际选择的档位 |
| `choice_reason` | 字符串或 `null` | 改选原因；接受推荐时可为 `null` |
| `duplicate_prompt` | 布尔值 | 同一任务、同一阶段且无路由触发条件变化时是否被重复打断 |

`task_class` 固定写成 `task_kind/stage/action_scope/risk_floor/preference`。汇总器还会用全部原始路由输入建立更严格的可比签名：任务分类相同但评分、风险标记或布尔输入不同，不算同一可比样本。`router_version`、`risk_floor`、推荐模型、推荐档位、相对额度和原因码必须逐字段匹配本轮锁定路由器对原始输入的输出；不能把外部部署手写成 Ultra，也不能用任意合法模型冒充路由器推荐。若 `user_choice == "accept_recommended"`，所选模型和档位必须与推荐完全一致；改选则必须填写非空的 `choice_reason`。

更正事件的 `occurred_at` 仍表示追加更正的时间，因此决定更正在何时生效；但路由先于结果、基线先于当前任务、同类决策排序等逻辑位置，继承同一更正链根事件的原始位置。这样，事后纠正一条早期事实不会把它错误移动到后续任务之后。

### `task_outcome`

任务结束、暂停或失败时写入。`completed` 才是闭环任务；其他状态保留证据但不计返工改善。

| 字段 | 类型 | 含义 |
|---|---|---|
| `status` | 字符串 | `completed`、`blocked`、`cancelled` 或 `incomplete` |
| `actual_model` | 字符串或 `null` | 任务实际使用的模型；未知时为 `null` |
| `actual_effort` | 字符串或 `null` | 任务实际使用的档位；未知时为 `null` |
| `quality_score` | 数字或 `null` | 用户或明确验收者给出的 1—5 分；没有真实评分时为 `null` |
| `rework_count` | 非负整数或 `null` | 为达到验收而发生的真实返工轮数；未知时为 `null` |
| `elapsed_minutes` | 非负数字或 `null` | 有实际起止记录才填写 |
| `quota_observation` | 对象或 `null` | 有观测时必须含 `pool`、`window_id`、`before_remaining_percent`、`after_remaining_percent`、`source`、`reset_observed`、`note`；只填界面或日志中实际观察到的值 |
| `baseline` | 对象或 `null` | 可比基线引用，必须含更早闭环任务的 `task_id`、`task_class`、`rework_count`；汇总器会与日志中的原事件交叉核对 |
| `acceptance_evidence` | 字符串或 `null` | 验收、测试或用户反馈的简短事实 |

`completed` 事件必须填写 `actual_model` 和 `actual_effort`；无法确认实际配置时先记为 `incomplete`，后续确认后再追加更正事件。

基线不能引用当前任务、未来任务、未闭环任务或日志外任务。基线与当前任务必须具有相同 `task_class` 和相同完整可比签名，且 `baseline.rework_count` 必须与被引用任务的有效结果一致。额度对比还要求两个任务的 `pool`、`window_id` 和 `source` 相同，二者均没有观察到重置；后续任务开始前的剩余额度不得高于更早任务结束后的剩余额度。

## 4. 统计与判定公式

### 进入复核

满足任一条件即可进入阶段复核，但不会自动启用全局门：

```text
elapsed_calendar_days >= 7 OR completed_task_count >= 10
```

### 路由接受率

```text
明确选择集合 D = 所有 user_choice 属于四个合法枚举值的 route_decision
accepted_count = D 中 user_choice == "accept_recommended" 的数量
acceptance_rate = accepted_count / |D|
```

分母只包含有明确 `user_choice` 的 `route_decision`；没有明确选择的记录不进入分母。目标为 `acceptance_rate >= 0.80`。当 `|D| == 0` 时结果为“证据不足”，不得记为 100%。

### 高风险低配错误

把 `gpt-5.6-sol` + `high` 视为 `high` / `critical` 风险下限；`xhigh`、`max`、`ultra` 均不低于该档位。对每个高风险任务：

```text
recommended_below_floor = recommended_model != "gpt-5.6-sol"
                          OR recommended_effort NOT IN {"high", "xhigh", "max", "ultra"}

actual_below_floor = actual_model 已知且 actual_model != "gpt-5.6-sol"
                     OR actual_effort 已知且 actual_effort NOT IN {"high", "xhigh", "max", "ultra"}

high_risk_under_routing = risk_floor IN {"high", "critical"}
                          AND (recommended_below_floor OR actual_below_floor)
```

目标为 `high_risk_under_routing_count == 0`。实际配置未知时一律标记“待核实”，即使任务状态是取消也不能推定为合规；也不得为了补齐统计而虚构实际模型或档位。

### 同类稳定性与重复打断

只有 `task_class` 相同且全部原始路由输入、偏好和风险未变化的相邻决策才构成可比对。可比对中推荐配置变化即记一次待解释的不稳定：

```text
unexplained_route_change_count = 可比对中 recommended_model/effort 改变且无输入变化证据的次数
duplicate_prompt_count = duplicate_prompt == true 的 route_decision 数量
```

两项目标都为 `0`。阶段、风险、范围、偏好或模型目录变化导致的重新路由不是不稳定，也不是重复打断。

### 返工与额度改善

返工只比较满足全部条件的样本：`status == completed`、当前 `rework_count` 为实测整数、基线引用指向日志中更早的同类闭环任务、引用值与原记录一致，且双方至少有一个非空的 `acceptance_evidence` 或 `evidence_note` 指向实际测试、验收或用户反馈。未闭环任务、跨类基线、日志外基线、缺少实测值或缺少证据说明的任务不计入改善，也不按 0 处理。

```text
rework_delta = baseline.rework_count - rework_count
mean_rework_delta = sum(rework_delta) / comparable_closed_loop_count
```

`rework_delta > 0` 表示返工下降。额度消耗以 `before_remaining_percent - after_remaining_percent` 计算；只有同额度池、同重置窗口、同来源、双方均无重置、前后时间序列不倒增、两个额度观测均有非空说明且两个结果均有证据说明的基线对才可比较。一个任务即使同时出现返工与额度改善，也只计 1 个效率信号。跨额度池、跨窗口、观察到重置、时间序列矛盾或只有主观感受时仅作备注。阶段通过至少需要 1 个可核验的返工下降或额度改善信号；没有可比样本时结论为“证据不足”，不能宣称节省额度。

## 5. 阶段结论

只有状态仍为 `active` 且同时满足以下条件，才可建议用户裁决是否进入全局接入：达到复核门槛、接受率不低于 80%、推荐与实际配置的高风险低配任务数不超过目标、同类无无解释漂移、无重复打断，并出现至少 1 个可核验效率改善信号。用户主动改选低于建议下限会单列为警告，但不冒充路由器错误；实际执行低配仍会阻断通过。任何指标缺少有效分母或真实观测时，结论必须是“继续试运行/证据不足”，而不是“通过”。`rolled_back` 或 `completed` 状态不能再次返回待批准结论；全局接入仍需用户单独确认。
