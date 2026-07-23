---
name: execution-project-authoring
description: 对冻结 ActionSpec 进行能力预检，并确定性编译 EditorProject 与 SpecTraceMap。
---

# 执行工程编译

1. 验证 ActionSpec Schema、素材哈希、时间线和动作引用。
2. 将 `required_capabilities` 与当前 Capability Registry 逐项匹配。
3. 存在缺口时输出 CapabilityGapReport，并结束本次编译。
4. 能力齐备时执行确定性编译，生成 EditorProject 和 SpecTraceMap。
5. 断言 ActionSpec ID 集合与 TraceMap ID 集合一致。
6. 验证工程 Schema、素材源范围、轨道域、时长和渲染参数后提交不可变快照。
