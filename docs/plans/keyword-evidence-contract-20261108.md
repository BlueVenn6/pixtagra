# 有证据的关键词概念与关系约束：一次有限实验

日期：2026-11-08。依据：[一手资料与现有失败分析](../reports/keyword-method-research-20261108.md)。

## 不变的产品标准

- 最终清洗、事实复核、语义去重后至少 **20** 个，目标 **25–30** 个，上限 **35** 个。
- 主体及关键辨识细节进入前十；不以同义词、排列组合或无依据推断填充数量。
- 中文、英文各自检查最终数量和表达，不能把翻译前的概念数当作两种语言都已达标。
- 生成默认模型保留用户选择的 GPT-5.6 Terra。模型名称是中转标识，不能据此证明实际后端模型身份。
- 不通过降低原标准、关闭去重、忽略未完成复核或把失败改为警告来通过。
- 本方案不修改 UI、导出、密钥、图片导入或打包行为。质量通过前不构建新的 EXE。

## 本次方法为什么不同

前次独立观察流程已经失败。新的实验不再增加一个独立观察请求，也不新增第二套补词后备路径。它改变两处已证实的问题：

1. **候选覆盖**：一次生成同时给出候选概念与证据。内部尝试收集 32–40 个有据候选，给复核留出余量；这是内部候选池，不是最终标签数量。没有足够依据就返回不足，禁止为凑满候选池分配机械配额。不得只靠加大数量宣称有效。
2. **删词权限**：复核返回逐概念事实判断、逐组关系类型，而不是直接给出可任意执行的删除列表。主体相同、共享单词、上下位类别或不同属性，均不能自动成为同义删除的理由。结构校验不等于事实判断；模型未知或有争议的关系仍须复核，不能为保住 20 个直接放行。

## 图库索引规则

将候选分成：主体、可见属性/部件、支持对象、场景/构图、上位类别、直接相关概念。每个词有实体与证据锚点。

| 关系 | 处理 |
| --- | --- |
| 完全相同、大小写/标点变体 | 规则合并 |
| 单复数、明确的同义或等价改写 | 保留一个自然表达；已确认的别名进入小型等价词表 |
| 具体对象与有用的上位类别 | 独立索引候选，不能仅因层级关系合并；也不把整条分类树机械展开 |
| 同一对象的颜色、表面、形状等不同属性 | 证据分别成立时可以同时保留 |
| 泛泛的画面占位词、同一属性的弱修饰排列 | 删除，不能算独立搜索概念 |
| 味觉、确切材质、品种、地点、事件阶段等缺证据推断 | 删除或标记不确定；不能计入合格数 |
| 主题/用途概念 | 需要普通、直接且可解释的关联；最多两个，不允许由共现频率推断本图事实 |

类别扩展采用小型、可审阅的词表，来源于普通词义与官方索引规则，不复制他人图片的整组关键词。词表区分等价关系和上下位关系。不能简单把“牛”“牲畜”设为同义，也不能把所有动物祖先类别全加上来补足数量。

普通摄影优先主体、姿态、可见细节、支持对象及场景。商业背景优先真实纹理/图案、颜色、光影、渐变、重复结构、方向和可见构图；没有诊断证据时，不因外观相似断言石材、布料或工艺。两种模式共享质量标准，但候选覆盖重点不同。

## 内部数据契约

以下结构仅用于实验及内部判断，不展示给普通用户，不更换现有导出格式：

```json
{
  "concepts": [
    {
      "id": "c01",
      "term": "natural search phrase in the selected language",
      "entity": "e01",
      "kind": "subject|attribute|object|setting|composition|category|concept",
      "facet": "what independent aspect this phrase describes",
      "evidence_kind": "visible|category_entailment|direct_context",
      "evidence": "specific image evidence or a defensible category derivation",
      "priority": 1
    }
  ],
  "relations": [
    {"a": "c01", "b": "c02", "type": "equivalent|broader|different_facet|unrelated", "reason": "..."}
  ],
  "title": "...",
  "description": "..."
}
```

独立复核必须覆盖全部候选 ID，分别返回 supported / uncertain / unsupported 及理由，并确认关系、标题和描述。ID 漏项、重复、越界、关系矛盾、未完成响应，都算复核失败。

生成者给两个词不同 ID，并不能证明它们独立。程序可以合并确定的等价词；未知语义关系要由复核按相同规则判断。有争议的候选不能悄悄留在“可交付”集合里以保住数量。语义合并也不能反过来把原本真实的标题事实标成虚假。

排除已完整复核且明确标记为不确定的候选后，如果仍有至少 20 个确定的独立词，可以继续验收最终列表；不能仅因原始候选池曾出现不确定词就判定最终列表不合格。漏项、超时、非法响应不属于这种完整复核，仍然直接失败。数量与事实门槛始终按排除后的最终结果计算。

先在实验模块实施这个契约；只有实图实验通过才接到生产调用。现有生产层的一次纠正若需要保留，应改为修正这一组证据与概念，不能额外叠加第二次补词。小样本实验先不启用内容纠正，以免掩盖一次输出的缺陷。

## 提示词草案的职责

生成提示词：

> Act as a stock-image indexer. Inspect the complete attached image. Build evidence-backed search concepts, not a caption split into phrases. Include the main subject, distinctive visible details, supporting objects, useful categories, setting and composition where established. General and specific categories are not automatically synonyms. Collect 32–40 defensible internal candidates when the image supports them; never invent, repeat or assign facet quotas to reach that range. Each candidate must name one useful search concept and identify its entity, facet and evidence. Do not turn clothing, color or count permutations into separate concepts with the same complete meaning. Exclude uncertain materials, ingredients, breeds, locations and event phases. At most two ordinary, directly grounded thematic concepts are allowed. Order the subject and image-defining details first. Return the specified JSON in the selected language. The final acceptance after independent review remains 20–35, targeting 25–30; an insufficient image or response must fail honestly.

复核提示词：

> Independently inspect the image and every candidate. Judge visible evidence, ordinary category entailment and limited direct thematic relevance separately. A keyword is not a literal caption claim, but it must not imply unsupported factual detail. Return one factual decision for every candidate ID. Classify proposed pair relationships as equivalent, broader/narrower, different facet, or unrelated. Equivalence requires interchangeable complete meaning; sharing a subject, one attribute or a category ancestor is insufficient. A distinct ID or plausible explanation from the generator is not evidence. Flag vague filler, weak permutations and genuine synonym chains. Check the title and description more strictly for factual assertions. Do not optimize for a keyword count. Report uncertainty explicitly; do not silently delete, invent or replace words.

这两段必须与 JSON 模式、关系校验、示例校准一起实施。单独把英文段落追加到现有提示词上不算实施本方案。

## 先校准规则，再付费实图测试

1. 固定参考文件：[pilot-reference-v1.json](../quality/keyword-pilot-reference-v1.json)。它记录五张原图的主体、必含细节、禁止推断，以及允许共存/应合并的关系。观察来自本代理直接查看原图，**不是独立人工专家标注**。文件不输入生成模型，避免把答案给模型再评分。
2. 实施实验解析器、等价/层级区分及离线边界用例。关系判断必须通过参考用例，且没有漏项被默认放行。发现口径错误，先修口径，不发模型请求。
3. 冻结实验提示词、代码、图片 SHA-256、生产基线、参考规则及验收标准。五张图 × 中英，共 10 份；每份一次 Terra 生成、一次 Sol 图像与关系复核，最多 20 次模型请求，无自动内容重试、无补词。两者在同一中转，不能称为独立供应商的交叉证明。
4. 使用已授权的 Windows 已存凭据，进程内部复用，不再次弹窗索要同一个 API，不把密钥写到实验文件或日志。
5. 同时记录生成数、去重数、每次删词理由、最终数、核心细节覆盖、明显事实错误、待定项、响应失败、耗时、用量和源码哈希。与已有相同图片结果逐图比较；不能仅报告平均词数。

## 同时满足才算小样本通过

- 10/10 份均完成完整复核；超时、格式错误、不确定关系不得按通过处理。
- 10/10 份最终各有 20–35 个独立、有据词；目标仍为 25–30，逐图报告目标达成情况。
- 10/10 份主体与固定关键细节进入前十；无明显编造、无未解决材质/事件判断错误、无明显同义堆叠。
- 对照原图逐词检查最终列表及标题描述，公开失败项。另一模型同意不替代这一步。
- 同图中英文的事实一致；词数按各自的最终去重结果计算。
- 不能通过只保护类别词而保留错误内容来获得数量改善；必须同时优于已知失败状态。

若其中任一项失败，停止本变体，不在这五张图上连改若干版直到过关。输出阶段归因，区分识别漏项、搜索概念覆盖不足、错误合并、翻译合并、协议失败和网络失败；质量失败不以改阈值或跳过复核补救。

若全通过，才将同一方案最小接入生产，冻结源码并用真实生产路径完成完整 40 份复测与相关回归。40 份仍须各自满足数量、事实、前十排序和完整复核要求。只有该门槛通过后，才继续原有构建流程、记录唯一 EXE 的哈希、启动这个确切文件并验收生成/取消/恢复/导出。实验成功本身仍不等于最终 EXE 通过验收。

## 当前执行状态

已取得一手资料、核对现有丢词阶段、直接查看五张图、建立参考规则并写出本方案。**尚未实现新实验解析器、尚未执行这个新变体的付费实验、尚未改变生产行为。** 该限制不能被写成“关键词已修复”。
