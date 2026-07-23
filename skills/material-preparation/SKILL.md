---
name: material-preparation
description: 在冻结总体方案下完成素材发现、筛选、预处理和逐素材溯源。
---

# 素材筹备

1. 从总体方案提取素材内容、构图、动作方向、画幅和技术要求。
2. 参考驱动任务先调用 `reference_get_creation_context` 取得冻结报告与 `resource_preparation` 投影；原创任务跳过此调用。
3. 检索阶段二共享知识，补充素材特征和预处理需求，并把 `retrieval_id` 及参考驱动任务的 `ReferenceContextBinding` 写入阶段产物。
4. 组合用户素材、受约束素材来源和派生帧；先发现，再筛选，再获取。
5. 对每个候选检查内容相关性、分辨率、时长、可用区间、运动方向和画面协调性。
6. 保留来源、作者、授权、查询词、文件哈希和预处理 provenance。
7. 输出 MaterialPackage，不设计最终时间线。
