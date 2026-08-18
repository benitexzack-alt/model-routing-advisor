# Model Routing Advisor

为 Codex 项目和重要任务生成“模型 + 推理档位 + 相对额度 + 升降档条件”的确认卡，帮助在质量下限之上合理使用额度。

当前状态：**V0.1 离线验证阶段，尚未接入全局强制启动流程。** Skill 只给建议并等待用户确认，不会自动切换当前模型。

## 核心原则

- 按当前任务阶段和风险路由，不把一个项目永久绑定到一个模型。
- 先守住生产、公开发布、付款、客户交付、隐私和安全等风险下限，再优化额度。
- 只给“低 / 中 / 高 / 很高”相对额度，不承诺精确消耗或节省比例。
- GPT-5.5 不自动首选；Spark 只用于符合条件的文本编码迭代。
- Max 用于最难的单体问题；Ultra 用于可真实拆分的并行任务。

## 目录

Skill 位于 `skills/model-routing-advisor/`：

- `SKILL.md`：触发说明与执行流程；
- `references/`：当前模型目录、路由策略、路由卡格式和 12 个评估场景；
- `scripts/route_model.py`：确定性路由基线；
- `scripts/test_model_route.py`：离线回归测试。

## 验证

```bash
python3 skills/model-routing-advisor/scripts/test_model_route.py
python3 /Users/pc/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/model-routing-advisor
```

离线测试通过后，还需完成 7 天或至少 10 个真实任务试运行。只有达到“高风险零降错、用户接受率至少 80%、同类任务路由稳定、没有重复打断”的门槛，才进入全局接入阶段。
