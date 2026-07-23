from __future__ import annotations

import argparse
from pathlib import Path

from validation.reference_publication import REFERENCE_FIXTURE_SLUGS

from .runner import (
    CreationScenarioRunner,
    ReferenceFixture,
    default_run_directory,
    dump_result,
)
from .scenario import bundled_scenario_path, load_scenario


def main() -> None:
    parser = argparse.ArgumentParser(description="运行真实视频创作 E2E 场景")
    parser.add_argument(
        "scenario",
        help="场景 JSON 路径，或 fast_cut_pip / calm_layered",
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--plugin-output-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--reference-fixture",
        choices=REFERENCE_FIXTURE_SLUGS,
        help="提供时执行 reference_guided_creation",
    )
    parser.add_argument(
        "--reference-source-media-root",
        type=Path,
        help="参考视频源文件目录；参考驱动真实验收时必填",
    )
    args = parser.parse_args()
    if (args.reference_fixture is None) != (args.reference_source_media_root is None):
        parser.error("--reference-fixture 与 --reference-source-media-root 必须同时提供")

    project_root = args.project_root.resolve()
    scenario_argument = Path(args.scenario)
    scenario_path = (
        scenario_argument if scenario_argument.is_file() else bundled_scenario_path(args.scenario)
    )
    scenario = load_scenario(scenario_path)
    plugin_output = (
        args.plugin_output_root.resolve()
        if args.plugin_output_root is not None
        else project_root / "output" / "plugin"
    )
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else default_run_directory(plugin_output, scenario.scenario_id)
    )
    result = CreationScenarioRunner(
        project_root=project_root,
        plugin_output_root=plugin_output,
    ).run(
        scenario,
        output_dir,
        reference_fixture=(
            ReferenceFixture(
                slug=args.reference_fixture,
                source_media_root=args.reference_source_media_root.resolve(),
            )
            if args.reference_fixture is not None
            else None
        ),
    )
    print(dump_result(result))


if __name__ == "__main__":
    main()
