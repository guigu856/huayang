"""bgm_analyze 核心服务：调用 AudioAnalysisService 并构建结构化摘要。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from components.audio_analysis.service import AudioAnalysisService


class BgmAnalysisService:
    """对音频文件执行 DSP 分析，返回面向阶段二 BgmPackage 的结构化摘要。"""

    def __init__(self, *, top_n_grids: int = 2) -> None:
        self._top_n_grids = top_n_grids

    def analyze(self, source: Path, output_dir: Path) -> dict[str, Any]:
        """分析音频文件，返回结构化摘要。

        返回字段：
        - status: "succeeded" | "no_tempo_candidate"
        - duration_us: 总时长（微秒）
        - tempo_candidates: [{bpm, score, period_samples}...]
        - beat_grids: {"tempo_001": [timestamp_us...], "tempo_002": [...]}
        - transient_count: 瞬态候选数
        - energy_summary: [{start_us, end_us, mean_normalized_energy}...]
        - sections: [{start_us, end_us, mean_energy}...]（算法建议的粗段落）
        - analysis_dir: 完整分析产物目录路径
        """
        audio_service = AudioAnalysisService()
        result = audio_service.analyze(source, output_dir)

        # 读取 tempo 候选
        tempo_data = json.loads(result.tempo_candidates_path.read_text(encoding="utf-8"))
        candidates_raw = tempo_data.get("candidates", [])
        tempo_candidates = [
            {
                "bpm": c["bpm"],
                "score": c["score"],
                "period_samples": c["period_samples"],
            }
            for c in candidates_raw
        ]

        # 为 top N 候选各生成一份 beat grid
        beat_grids: dict[str, list[int]] = {}
        if tempo_candidates:
            beat_grid_data = json.loads(result.beat_grid_path.read_text(encoding="utf-8"))
            # 第一份网格直接从 beat_grid.json 取（算法已用 tempos[0] 生成）
            beat_grids["tempo_001"] = [
                beat["timestamp_us"] for beat in beat_grid_data.get("beats", [])
            ]
            # 第二份网格用 tempos[1] 的 period + 相位锁定生成
            if len(tempo_candidates) >= 2:
                second = tempo_candidates[1]
                second_raw = candidates_raw[1]
                period = second["period_samples"]
                lag = second_raw["lag_frames"]
                # 读取 spectral flux 包络做相位锁定
                flux_data = json.loads(
                    result.spectral_flux_path.read_text(encoding="utf-8")
                )
                envelope = [f["spectral_flux"] for f in flux_data.get("frames", [])]
                # 相位锁定：找使每隔 lag 取值之和最大的起始帧
                search_range = min(lag, len(envelope))
                phase = max(
                    range(search_range),
                    key=lambda p: (sum(envelope[p::lag]), -p),
                )
                hop_size = flux_data.get("hop_size", 256)
                first_sample = phase * hop_size
                beats_2: list[int] = []
                sample_index = first_sample
                while sample_index < result.sample_count:
                    ts = round(sample_index / result.sample_rate * 1_000_000)
                    beats_2.append(ts)
                    sample_index += period
                beat_grids["tempo_002"] = beats_2

        # 能量摘要：按 ~2 秒桶做平均
        energy_data = json.loads(result.energy_curve_path.read_text(encoding="utf-8"))
        frames = energy_data.get("frames", [])
        energy_summary = self._build_energy_summary(frames, result.duration_us)

        # 段落（算法建议，纯数值）
        section_data = json.loads(result.section_candidates_path.read_text(encoding="utf-8"))
        sections = [
            {
                "start_us": s["start_timestamp_us"],
                "end_us": s["end_timestamp_us"],
                "mean_energy": s.get("mean_normalized_energy", 0.0),
            }
            for s in section_data.get("sections", [])
        ]

        status = "succeeded" if tempo_candidates else "no_tempo_candidate"

        return {
            "status": status,
            "duration_us": result.duration_us,
            "tempo_candidates": tempo_candidates,
            "beat_grids": beat_grids,
            "transient_count": result.transient_count,
            "energy_summary": energy_summary,
            "sections": sections,
            "analysis_dir": str(result.output_dir),
        }

    @staticmethod
    def _build_energy_summary(
        frames: list[dict[str, Any]], duration_us: int, bucket_us: int = 2_000_000
    ) -> list[dict[str, Any]]:
        """将逐帧能量按固定时长桶聚合为摘要。"""
        if not frames:
            return []
        summary: list[dict[str, Any]] = []
        bucket_start = 0
        while bucket_start < duration_us:
            bucket_end = min(bucket_start + bucket_us, duration_us)
            contained = [
                f["normalized_rms"]
                for f in frames
                if bucket_start <= f.get("timestamp_us", 0) < bucket_end
            ]
            mean_energy = sum(contained) / len(contained) if contained else 0.0
            summary.append(
                {
                    "start_us": bucket_start,
                    "end_us": bucket_end,
                    "mean_normalized_energy": round(mean_energy, 6),
                }
            )
            bucket_start = bucket_end
        return summary
