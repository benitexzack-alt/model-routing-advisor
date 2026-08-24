# MRA V1.2 定时任务免重复询问验收记录

> 验收时间：2026-08-25 01:14（Asia/Shanghai）
>
> 当前结论：V1.2 已安装并受信任。独立 cron 通过 Codex 自动化 transcript 核验后不再生成模型路由卡；heartbeat 沿用目标任务已经确认的一次性路线。已安装 Hook 的真实历史任务探针通过，但下一次由宿主实际调度的运行仍是最终现场验收点。

## 一、问题与根因

归档不是本次重复询问的根因。直接证据显示：

- `automation-2` 在 2026-08-24 的独立运行任务中只读取了模型路由 Skill，生成路由卡后停在等待确认，没有开始知识治理业务流程；
- `ai-8` 与 `skill-agent` 也出现了相同的“先给模型路由卡、等待人工确认”行为；
- 每日 heartbeat 复用固定目标任务，但旧全局规则仍把每次唤醒文字当成新的实质任务判断。

根因是原 V1.1 把“每个新 session”近似成“每个新人工任务”。独立 cron 每次运行都会创建新 session，因此每天都会被误判成新的处方作用域；heartbeat 虽然复用原任务，也缺少明确的自动化边界说明。

## 二、V1.2 契约

### 2.1 独立 cron

- 只有位于 Codex 受信 transcript 目录、所有者和文件名正确、会话标识一致，且首条 `session_meta.thread_source=automation` 的运行，才取得自动化线程来源；
- 初始自动触发轮还要完整匹配 cron 宿主信封与本机当前用户拥有、非组/全局可写的 `ACTIVE` 配置，并核对 ID、名称、完整正文、模型、推理档位和工作目录；
- 命中后返回 `routing-not-required / scheduled_automation`，路由卡计数保持 0，并继续业务流程；
- 同一自动化运行任务中的普通人工跟进标记为 `automation_thread_followup`，不会冒充新的定时触发；人工明确要求重选时仍只允许一张替代卡。

### 2.2 heartbeat

- heartbeat 信封本身不能创建豁免，也不能伪造确认；
- heartbeat 复用 `target_thread_id` 对应任务原有的一次性路由状态；
- 目标任务已经确认时，后续每日唤醒走 `route_already_set`；尚未确认时只继续等待原有一张卡，不生成第二张；
- 当前每日 heartbeat `7` 的目标任务已经观察到明确选择，下一次唤醒应静默沿用。

### 2.3 不变边界

自动化处理只取消重复的模型路由等待，不批准发布、付款、部署、删除、隐私、安全、客户交付或其他现实行动。自动化时间、正文、启停状态和业务权限没有因本次安装而改变。

## 三、红队复验

第二轮独立红队结论：P0 为 0，P1 为 0；第一轮发现的两个 P1 均已关闭。

| 反例 | 修复后结果 |
|---|---|
| 人工 session 精确复制本机真实 `automation-2` cron 信封 | `new_task`、`automation_exempt=false`、`route_prompt_count=1` |
| V1.1 旧 pending 自动化任务第一次人工发送“重新选模型” | 首次即 `user_requested / inject`，不再吞掉 |
| heartbeat 已确认目标任务 | `route_already_set`，计数仍为 1 |
| heartbeat 未确认目标任务 | `route_prompt_pending`，继续原卡 |
| 自动化任务普通人工跟进 | `automation_thread_followup`，没有 scheduled 执行上下文 |
| 自动化配置权限为 `0660` 或 `0666` | 拒绝；`0644` 接受 |

剩余两个非阻断 P2：自动化 transcript 来源状态与“本轮配置完全核验”仍共用一个持久字段；自动化配置只检查文件本身，尚未逐级检查所有父目录是否可被组或其他用户写入。本版在配置不完整时不会注入“直接执行本次自动化”上下文，后续可再拆分状态语义并加强目录链检查。

## 四、回归与安装

| 验证项 | 系统 Python 3.9 | 当前 Python | 结果 |
|---|---:|---:|---|
| Hook + 管理器 unittest | 73/73 | 73/73 | 通过 |
| 路由器回归 | 16/16 | 16/16 | 通过 |
| 试运行汇总器回归 | 27/27 | 27/27 | 通过 |
| `py_compile` | 通过 | 通过 | 通过 |
| Skill Creator `quick_validate.py` | 当前 Python 执行 | 源 Skill 与已安装 Skill | 通过 |
| `git diff --check` | — | — | 通过 |

安装与信任结果：

- 功能提交：`7fb73ac`，已推送到 `origin/main`；
- 全局安装备份：`20260824T171138984647Z-install-9355a7b5`；
- 管理器检查：`ok=true`、`issues=[]`、`trust_status=trusted`；
- app-server Hook 当前哈希：`sha256:4d8291bddcbd0e36f6c0a408c4b9af633fea2fab02c8e649b31027b5f0bb1273`；
- 仓库 Hook、全局 Hook、已安装 Skill Hook 文件 SHA-256：`e54c2532d2ab276a3d2ec0af3fab233c78df2b030c529263e1e83759afff6f59`；
- 全局规则受管块 SHA-256：`626db0853608d1902a1ac4264058440ece577fd26bf39d1f47362a2fd9373330`；
- `install --trust --dry-run` 返回 `changed=false`；
- 本次备份的 `rollback --dry-run` 与当前安装的 `uninstall --dry-run` 均可解析；
- 目标 Hook 为 `0700`，运行状态目录为 `0700`，状态和事件文件为 `0600`。

## 五、已安装 Hook 的真实任务探针

探针直接加载当前 `/Users/pc/.codex/hooks/model-routing-gate.py`，使用真实历史 transcript、真实当前自动化配置和隔离临时状态目录，没有执行自动化业务正文，也没有改动生产路由状态。

| 探针 | 结果 |
|---|---|
| 真实 `automation-2` 自动化 transcript + 当前配置 | `scheduled_automation`、计数 0、`automation_exempt=true` |
| 把同一真实 cron 信封放入非 automation transcript | `new_task`、计数 1、`automation_exempt=false` |
| 当前每日 heartbeat `7` 的已确认目标任务 | 无注入上下文、`route_already_set`、计数 1、选择状态为 true |

这证明安装后的识别逻辑能够区分真实独立 cron、人工复制信封和已确认 heartbeat。它仍不能替代 2026-08-25 下一次由 Codex 宿主实际触发的完整运行。

## 六、现有自动化保持情况

安装后只读检查仍有 6 个 `ACTIVE` 自动化；本轮没有调用自动化更新、暂停或删除接口，也没有改写 `automation.toml`。

| ID | 类型 | 频率 | 模型路线 |
|---|---|---|---|
| `7` | heartbeat | 每日 08:30 | 沿用目标任务已确认路线 |
| `ai-8` | cron | 每日配置 | `gpt-5.6-sol / low` |
| `automation-2` | cron | 每日 02:00 | `gpt-5.6-sol / low` |
| `automation` | heartbeat | 每周日 20:30 | 沿用目标任务路线；当前仍待原卡确认 |
| `automation-3` | cron | 每周日 02:30 | `gpt-5.6-sol / low` |
| `skill-agent` | cron | 每周一 09:00 | `gpt-5.6-sol / low` |

另有一个既存且与本次路由修复无关的观察：`ai-8` 名称与 rrule 表达为每日 08:00，但本地数据库在验收时显示下一次运行约为 16:01。为避免扩大授权范围，本轮没有修改它的时区或计划。

## 七、验收边界

可以确认：代码回归、两轮红队、真实历史 transcript 探针、受信安装、哈希一致性、幂等安装与可逆预演均通过；已知的“独立 cron 每天新建 session 后被路由卡卡住”路径已经被修正，每日 heartbeat `7` 也会沿用其目标任务已经确认的路线。

仍不能提前确认：下一次真实调度是否完整执行了业务任务、宿主未来格式是否变化、或任何业务流程本身已经成功。下一次真实定时运行应作为现场验收；若再次出现模型路由卡，应保留该运行任务与 `gate-events.jsonl` 证据后单独诊断，不得伪称已通过。
