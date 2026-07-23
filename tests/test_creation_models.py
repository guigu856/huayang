from __future__ import annotations

from video_create_plugin.creation import CreativeDirection


def test_creative_direction_keeps_stage_one_semantic_only() -> None:
    direction = CreativeDirection(
        title="城市速度",
        user_intent="制作有推进感的短视频",
        video_type="节奏型短视频",
        core_mechanism="以稳定节奏单元交替主画面与辅助层",
        production_method="先建立视觉母题，再按音乐能量组织段落",
        visual_language="清晰主体、局部叠层、强弱交替",
        rhythm_and_sound="重拍承担切换，新音色承担辅助层进入",
        transition_principles="主变化使用硬切，镜内辅助变化保持连续",
        asset_and_music_traits="方向一致的动态素材和清晰瞬态音乐",
        viewing_experience="紧凑但有呼吸，信息丰富而不混乱",
        retrieval_ids=["retrieval_0123456789abcdef"],
    )
    payload = direction.model_dump(mode="json")
    assert "assets" not in payload
    assert "shots" not in payload
    assert payload["retrieval_ids"]
