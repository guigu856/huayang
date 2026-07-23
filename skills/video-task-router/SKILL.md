---
name: video-task-router
description: 从自然语言识别参考学习、原创创作或参考驱动创作，并进入首个阶段。
---

# 视频任务路由

1. 判断用户是否提供或指向一个参考视频。
2. 判断用户目标是理解该视频，还是使用新素材制作视频。
3. 映射任务类型：
   - 只学习参考片：`reference_study`。
   - 从想法创作：`original_creation`。
   - 参考片驱动新创作：`reference_guided_creation`。
4. 只补充会改变创作结果的业务信息，例如主题、发布场景、时长或已有素材。
5. 创建任务并进入 StageEnvelope 指定阶段。

用户需求保持自然语言表达。工具选择、角色加载和阶段顺序由 Plugin 与 Agent 共同完成。
