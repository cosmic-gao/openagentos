# OpenAgentOS 自审(Review)与质量保证 — 设计记录

> 记录于 2026-07-16。汇总关于 `RubricMiddleware` 自审机制的事实核实、质量保证/提升策略,以及两个设计方向(满意度驱动按需触发、RAG 优质样例增强)的讨论与结论,供后续工作参考。
>
> 相关:[性能分析](performance-analysis.md)。标注约定:✅ 已定论/已落地 · 🔬 待验证 · 🧭 设计方向(未实现)。deepagents 源码引用形如 `rubric.py:NNN`(位于 `.venv/.../deepagents/middleware/`),项目文件为相对链接。

---

## 1. 目标与一个关键区分

核心目标:**给最终结果做质量保证 + 质量提升**。必须先分清两者(需要不同机制):

- **质量保证(gate)**:拦住不合格产出、确认达到底线。
- **质量提升(improvement)**:让产出比原本更好。

`RubricMiddleware` 本质是"外部评审员 + 自动返工循环",**主要服务"保证"**,对"提升"只有有限且非单调的帮助;质量的**提升**主要靠上游(见 §4)。

---

## 2. RubricMiddleware 机制(已核实事实)

基于 deepagents 源码与项目 `agentos/` 逐处核对。

### 2.1 启用条件与配置 ✅
- 仅当 assistant `config.configurable.review.rubric`(非空)时装配;否则 [build_review](../agentos/middleware.py) 返回 `[]`,完全不触发。
- 配置项:`review.rubric`(开关+标准)、`review.max_iterations`(默认 3,硬上限 [1,20])、`review.model`(grader 模型,本会话新增,见 §2.5)。

### 2.2 grader 是独立 LLM 判定,非自评 ✅
- verdict 由**独立 grader 子 agent**(`create_agent` + `response_format=GraderResponse`)产生,不是主 agent 自评。
- grader 模型 = `review.model`,缺省复用主模型;网关(base_url/api_key)复用主模型那套。
- grader **默认无工具**,只凭 transcript 判定(官方把工具设计成可选例外);**不继承**主 agent 的 tools。

### 2.3 触发时机与频率 ✅
- 时机:agent **自然停止**时(`after_agent`,回复交付前),非对话中途。
- 频率:配了 rubric 后,**同一会话每条消息(每个 run)都触发一次**新审查——上一 run 以终态结束,下一 run 的 `before_agent` 检测到终态即重置、开新 grading run(`rubric.py:409` + `_TERMINAL_RESULTS`)。
- 单次内部迭代:`needs_revision` → 注入反馈 → 跳回模型重做 → 再审,直到 `satisfied`/`failed`/`max_iterations`。

### 2.4 verdict 语义 ✅
- `satisfied`(全过)/ `needs_revision`(有条目不达标,循环)/ `failed`(rubric 无法评估)。
- `max_iterations_reached`、`grader_error`:**终态但不改回复**——会"没过审却收尾",需可观测(见 §3.6)。

### 2.5 本会话已落地的代码改动 ✅
为支持 grader 独立模型,已改 4 处(git 已确认落盘):
- [config.py](../agentos/config.py):`ReviewConfig` 加 `model` 字段、`ResolvedConfig` 加 `review_model`、`resolve()` 映射;
- [builder.py](../agentos/builder.py):按 `review_model` 构建 grader(缺省复用主模型 `llm`)。
- 用法:`"review": {"rubric": "...", "model": "gpt-4o-mini", "max_iterations": 3}`;grader 网关复用主网关(同 `fallback_model` 套路)。

---

## 3. 关键结论与最佳实践

### 3.1 薄评审员原则 ✅
**执行归主 agent,判定归 grader,评审员不下场干活。** 不给 grader 工具(官方默认、也最鲁棒):给工具 = 变成第二个执行 agent,徒增失败面(`grader_error`)、成本、不确定性。项目现状(不传 tools)正确,勿改。

### 3.2 rubric 写法 ✅
- **写成可客观验证的逐条清单**。grader 系统提示是保守原则:凡不能正面确认的判 fail(`rubric.py:146`)——主观标准("专业""全面")必然反复空转。
- **用 rubric 逼证据进 transcript**:与其给 grader 取证工具,不如把 rubric 写成"要求主 agent 自证"(附测试输出、来源),让证据留在对话里供 grader 直接判。
- rubric 是给 grader 的**验收标准**,不是给主 agent 的任务指令。

### 3.3 max_iterations 与效果非单调 ✅
它是**上限/安全阀**,不是质量旋钮。一轮过审时增大它无影响;多轮修订收益递减,且可能过度修改 / 上下文漂移 / grader 噪声累积 / 震荡而**变差**;与成本才正相关。设 **2–4**。提质靠 rubric 和模型,不靠调它。

### 3.4 LLM-as-judge 准确率是条件性的 ✅
- 评**可客观验证**的 → 准(退化成"文本里有没有 X");评**主观质量** → 不准、不稳。
- 准确率主要由 rubric 写法决定,非 grader 天生固定低。官方降噪设计:保守原则、逐条 criterion+gap、一致性校验(`rubric.py:277`)、结构化输出——降噪但消不掉根本不确定性。
- 定位:**廉价初筛,非权威裁决**。

### 3.5 能用代码判的,别用 grader ✅
编译 / 测试 / schema / 正则等可机械判定的,用**确定性检查**远比 LLM grader 便宜、稳定、100% 准。grader 只留给需要判断力的部分。

### 3.6 可观测性 🔬
`failed`/`max_iterations_reached`/`grader_error` 不改回复——会"没过审却收尾"且无声。官方提供 `on_evaluation` 回调 / `rubric_evaluation_end` 事件 / `_rubric_status`。**项目当前未接 `on_evaluation`,建议补**(纯观测、无副作用,不违背薄评审员)。

---

## 4. 质量的分层策略 ✅

质量主要在**上游**决定(预防 > 检验)。按 ROI 从高到低:

| 层 | 手段 | 服务目标 |
|---|---|---|
| 1 预防 | 强化系统提示"完成前自证" + `write_todos` 分解 | 提升(主力) |
| 2 主 agent 自我校验 | 收尾前对 checklist 用自己的工具核验并就地改 | 提升(主引擎) |
| 3 确定性闸门 | 代码检查(编译 / 测试 / schema) | 保证 |
| 4 rubric 自审 | 独立 grader 按客观 rubric 判定 + 返工 | 保证 |
| 5 人工闸门 | `interrupt_on` HITL | 保证(高风险) |

**独立评审 vs 自我校验**:提升靠"有工具、有上下文、能就地改"的一方(主 agent 自校验);保证靠独立性(外部 grader)。两者目标相反、互补。

---

## 5. 🧭 设计方向 A:满意度驱动的按需触发

### 5.1 时序陷阱(关键)
审查在**回复交付前**,满意度是**交付后**信号 → **不能用本条满意度审本条**。可行的只是:满意度 → 决定"重做本条"或"下一轮"是否审查。

### 5.2 openagentos 天然契合
rebuild 模式下 `review.rubric` 是 **per-run config**:
- 默认 run 不传 rubric → 不装 middleware → 不审、零成本;
- 用户 👎/重试 → 对重做 run 传 rubric → 触发审查 + 修订;
- **纯 config/客户端层,middleware 不用改**;装不装由该 run config 决定,与 state 残留无关(没 middleware 就没人读 `state.rubric`)。

### 5.3 落地建议
- 二值满意度(👍/👎):👎 → 重做 run 传 rubric。**最小改动,推荐。**
- 带具体原因的反馈:直接把原因作为修订指令喂回,**不必走 grader**(用户即最权威 judge)。
- 服务端全自动:改 `RubricSeed` 为条件注入(读 state 满意度标志)——较重。

### 5.4 边界
`RubricSeed` 只在 state 无 rubric 时注入([middleware.py:78](../agentos/middleware.py)),**跨 run 换不同 rubric 内容时旧的会 stick**;同一质量 rubric 按需开关则无影响。

---

## 6. 🧭 设计方向 B:RAG + 优质样例增强(reference-guided)

原始设想:用户不满意 → RAG 检索优质类似对话 → 动态生成 rubric → 触发反思修订。

### 6.1 范式映射与风险
| 环节 | 对应成熟范式 | 成熟度 |
|---|---|---|
| 不满意 → 触发 | 满意度驱动(§5) | ✅ |
| RAG 检索优质对话 | RAG / dynamic few-shot | ✅ |
| **优质对话 → 动态生成 rubric** | rubric induction | ⚠️ 最险 |
| 反思修订 | Reflexion / reference-guided grading | ✅(受 judge 准确率限制) |

### 6.2 结论:"生成 rubric"是多余损耗
已检索到具体优质样例,再压缩成抽象 rubric 会掉信息、加噪声,且生成的 rubric 多为主观描述式(grader 判不准那类)+ 多一次 LLM 不确定性。**优质对话应更直接地用:**
- **路 A(推荐先做)**:优质样例当 **few-shot** 让主 agent 重做——最短、最可靠,直接抬升生成质量,无需 grader 打回。
- **路 B(要保留审查)**:**reference-guided grading**——优质对话当 grader 的参考答案做对比评判(准确率高于凭空判 / 主观 rubric),而非先压成 rubric。

### 6.3 地基风险(必须先解决)
整个想法押在"优质对话库"上:冷启动(初期无积累)、"优质"如何界定("用户满意"≠"客观优质")、"相似"如何定义 + 检索噪声(错检索会污染全链路)。

### 6.4 复杂度 / 时机
链路长(检索 → 评判 → 修订)、失败面多,且触发在"用户已不满意、正在等"的敏感时刻——每环加超时/降级,检索无果即跳过。

### 6.5 务实演进路径
1. 🔬 **最小版验证核心假设**:👎 → RAG 检索优质样例 → few-shot 重做(路 A)。先证明"喂优质样例真能提升满意度"。
2. 需自动把关再加 reference-guided grading(路 B)。
3. "生成 rubric" 留到最后,且仅当要**沉淀高频任务的可复用结构化标准**时才做,并人工审可判定性。

结合项目:优质库可复用现有 `StoreBackend`/memory 做向量检索;触发接 §5 满意度信号。

---

## 7. 🔬 待验证 / 待决策清单

- [ ] `on_evaluation` 可观测性是否接入(§3.6)。
- [ ] 满意度信号的采集与传递方式(二值 vs 具体反馈)(§5)。
- [ ] 是否需要服务端自动触发(改 `RubricSeed` 条件注入)(§5.3)。
- [ ] 优质对话库:来源、"优质"标注、冷启动方案(§6.3)。
- [ ] 路 A 最小版的数据流与 openagentos 落点(触发点 / 检索存储 / 注入重做)。
- [ ] 性能相关项(graph 缓存、MCP 复用等)见 [性能分析](performance-analysis.md)。

## 8. ✅ 已定论(不再反复)

- grader 是独立 LLM 判定,非自评;默认无工具、保持薄。
- rubric 必须可客观验证;主观标准不可用。
- `max_iterations` 是安全阀非质量旋钮,设 2–4。
- 能代码判的用确定性检查,不用 grader。
- 提升靠上游(预防 + 主 agent 自校验),保证靠 grader / 确定性 / 人工。
- 满意度不能审本条(时序),只能驱动重做 / 下一轮。
- 优质样例直接当 few-shot / 参考用,不绕道生成 rubric。
- `review.model` 独立 grader 模型已落地(§2.5)。
