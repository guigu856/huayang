# 证据边界

所有参考视频结论必须标记证据等级：

- `measured`：工具直接测量的媒体、时间、像素或音频数据。
- `algorithm_candidate`：算法提出的切点、节拍、段落或视觉事件候选。
- `agent_inference`：Agent 依据可定位证据作出的语义解释。
- `reconstruction_suggestion`：原工程缺失时给出的重建方法。

扁平视频中的原始素材入点、独立图层、Alpha、关键帧曲线、独立声音轨和插件参数属于推断或重建建议。每个重要结论引用真实 `evidence_id`、时间范围和置信度。
