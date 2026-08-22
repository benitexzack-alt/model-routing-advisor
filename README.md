# Model Routing Advisor

为 Codex 项目和重要任务生成“模型 + 推理档位 + 相对额度 + 升降档条件”的确认卡，帮助在质量下限之上合理使用额度。

当前状态：**V1.1 全局 fail-open 提醒入口已安装并受信任；Codex CLI 与 app-server 金丝雀验证通过，桌面端下一条真实新任务仍是最终人机界面验收点。** 它不是程序级工具硬锁，也不会替用户自动切换当前模型或推理档位。

完整验收证据见 [`evidence/2026-08-23-global-gate-validation.md`](evidence/2026-08-23-global-gate-validation.md)，机器可读快照见 [`evidence/global-gate-state.json`](evidence/global-gate-state.json)。

## 用户会看到什么

- 每个新 Codex 对话/任务在第一次进入实质工作前，只收到一张模型路由卡；
- 第一次处方按该对话/项目可预见完整生命周期的最高复杂度和风险下限选型，避免后续阶段再加问；
- 用户确认后，该对话内的执行、验证、部署、风险变化和归档恢复都静默沿用，不再弹卡；
- 只有用户明确要求重新选模型、改变额度偏好，或已选模型/档位经核验不可用时才重新处方；
- 普通寒暄、简单解释和微小追问不会提前消耗这一次处方机会。

路由卡只给建议和确认状态。实际模型与档位是否已经切换，必须以宿主运行时证据为准。

## 工作方式

V1.1 采用两层入口：

1. 全局 `~/.codex/AGENTS.md` 受管块规定执行者在新对话首次实质工作前读取本 Skill、只展示一张路由卡并等待确认；
2. 已信任的 `UserPromptSubmit` Hook 只在该会话首次实质请求或用户明确要求改选时注入短提醒，后续阶段和归档恢复静默跳过，并在故障时显式告警。

Hook 使用 `continue: true`，避免基础设施故障吞掉用户消息。因此它是提醒与传输层，不是 `PreToolUse` 或等价的程序级阻断器；高风险停止仍由全局规则和主执行者遵守。

## 核心原则

- 首次处方按该对话/任务可预见完整生命周期的最高复杂度和风险下限路由；确认后在同一对话内保持稳定，不因阶段、范围、风险或归档恢复自动重问；
- 先守住生产、公开发布、付款、客户交付、隐私和安全等风险下限，再优化额度；
- 只给“低 / 中 / 高 / 很高”相对额度，不承诺精确消耗或节省比例；
- GPT-5.5 不自动首选；Spark 只用于符合条件的文本编码迭代；
- Max 用于最难的单体问题；Ultra 用于可真实拆分的并行任务。

## 目录

- `skills/model-routing-advisor/SKILL.md`：触发说明与执行流程；
- `skills/model-routing-advisor/references/`：全局门禁契约、当前模型目录、路由策略、卡片格式和试运行协议；
- `skills/model-routing-advisor/scripts/route_model.py`：确定性路由基线；
- `skills/model-routing-advisor/scripts/global_gate.py`：fail-open `UserPromptSubmit` Hook；
- `scripts/manage_global_gate.py`：可逆安装、检查、卸载、回滚与 app-server 信任管理；
- `tests/`：Hook 和管理器回归；
- `evidence/`：V0.1 历史试运行与 V1.1 验收证据。

## 安装与运维

管理器只接管一个带标记的全局规则块、一个目标 Hook 组、一个 Hook 文件和一条精确的信任项；其他已有配置保持不动。

```bash
/usr/bin/python3 scripts/manage_global_gate.py install --trust --json
/usr/bin/python3 scripts/manage_global_gate.py check --json
/usr/bin/python3 scripts/manage_global_gate.py uninstall --dry-run --json
/usr/bin/python3 scripts/manage_global_gate.py rollback --backup-id <备份编号> --dry-run --json
```

先用 `--dry-run` 检查卸载或回滚目标。真实卸载、回滚会改变全局 Codex 配置，应单独确认后再执行。

## 验证

```bash
/usr/bin/python3 -m unittest discover -s tests -p 'test_*.py' -v
/usr/bin/python3 skills/model-routing-advisor/scripts/test_model_route.py
/usr/bin/python3 skills/model-routing-advisor/scripts/test_trial_summary.py
/usr/bin/python3 skills/model-routing-advisor/scripts/summarize_trial.py
python3 /Users/pc/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/model-routing-advisor
/usr/bin/python3 scripts/manage_global_gate.py check --json
```

当前基线分别为：Hook/管理器 61 项、路由器 16 项、试运行汇总器 27 项。Hook/管理器、路由器和汇总器均在系统 Python 3.9 与当前 Python 下通过；Skill Creator 使用带 PyYAML 的当前 Python。数字只是回归基线，CLI/app-server 真实场景和桌面端人机验收不能由单元测试替代。

## 已知边界

- Hook 只能注入上下文，不能保证执行模型绝不违背提醒；
- 当前运行时状态只证明该会话已经出现过一次处方提醒或观察到选择短语，不保存推荐模型与实际切换证据；
- 本版以一个 Codex 对话/任务为处方作用域；新建独立对话会在其首个实质请求再问一次，不自动跨对话继承；
- Hook 使用启发式语义判断，仍可能出现误报或漏报；
- 为防止旧归档任务因状态淘汰而重新弹卡，本版不自动删除会话状态；状态文件和事件日志会随任务数增长，后续只能迁移到无损精确账本，不能用 TTL 或固定容量牺牲一次性语义；
- 运行日志不保存原始提示词，只保存元数据与 SHA-256，但目前没有自动轮转，哈希也不是匿名化承诺；
- 安装前已有三个指向缺失脚本的旧 Hook 配置，本项目为避免越权而原样保留，详见 V1.1 验收证据。

## V0.1 历史试运行

V0.1 于 2026-08-23 提前收口。用户明确选择优先保证质量并授权进入 V1，但原定 7 天/10 个真实闭环任务、80% 接受率和效率改善信号没有达成；这不是“试运行指标通过”。历史状态继续保留 `global_gate_enabled: false`，因为该字段只描述当时的 V0.1 边界。

## 官方机制参考

- [Skills：通过描述进行隐式匹配](https://learn.chatgpt.com/docs/build-skills)
- [AGENTS.md：为 Codex 提供持久项目与全局指令](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Hooks：在生命周期事件中运行命令](https://learn.chatgpt.com/docs/hooks)
