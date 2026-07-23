---
name: editing-specification
description: 使用冻结素材、BGM 和阶段三共享知识生成逐镜表与严格 ActionSpec。
---

# 可执行剪辑规格

1. 读取冻结总体方案、MaterialPackage 和 BgmPackage。
2. 参考驱动任务先调用 `reference_get_creation_context` 取得冻结报告与 `editing_specification` 投影；原创任务跳过此调用。
3. 检索阶段三共享知识，选择适合本任务的剪辑句子、PIP、密度和音画机制，并把 `retrieval_id` 及参考驱动任务的 `ReferenceContextBinding` 写入规格。
4. 先设计音乐段落与视觉密度，再划分主镜头和节奏单元。
5. 为每个镜头确定素材源区间、绝对时间、图层、空间、进入、保持、退出、效果、声音和连接。
6. 为每个执行动作分配 `action_id`，填写类型、整数时间范围、素材引用、参数、作用域和所需能力。
7. 检查主镜覆盖、合法叠层、音画关系、素材差异性和观看呼吸。
8. 检查表格动作与 ActionSpec 双向引用完整性，展示给用户确认。
