from __future__ import annotations

import argparse
import os
import threading
import webbrowser
from collections.abc import Sequence

import uvicorn

from video_create_plugin.installation import install_codex_plugin, print_doctor, run_doctor

from .api import create_app


def main(argv: Sequence[str] | None = None) -> None:
    arguments = list(argv) if argv is not None else None
    if arguments is None:
        import sys

        arguments = sys.argv[1:]
    if not arguments:
        _serve_admin()
        return

    parser = _build_parser()
    options = parser.parse_args(arguments)
    if options.command == "doctor":
        checks = run_doctor(launch_browser=not options.skip_browser)
        print_doctor(checks, json_output=options.json)
        if not all(check.ok for check in checks):
            raise SystemExit(1)
        return
    if options.command == "plugin" and options.plugin_command == "install":
        if options.host != "codex":
            parser.error(f"暂不支持的插件宿主：{options.host}")
        marketplace = install_codex_plugin(build_only=options.build_only)
        action = "已构建" if options.build_only else "已安装"
        print(f"Huayang Codex Plugin {action}：{marketplace}")
        return
    parser.error("缺少命令")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="huayang")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="检查安装后的运行环境")
    doctor.add_argument("--json", action="store_true", help="输出单行 JSON")
    doctor.add_argument(
        "--skip-browser",
        action="store_true",
        help="只检查 Chromium 文件，不实际启动浏览器",
    )

    plugin = commands.add_parser("plugin", help="管理 Agent 宿主插件")
    plugin_commands = plugin.add_subparsers(dest="plugin_command", required=True)
    plugin_install = plugin_commands.add_parser("install", help="构建并安装宿主插件")
    plugin_install.add_argument("host", choices=("codex",))
    plugin_install.add_argument(
        "--build-only",
        action="store_true",
        help="只构建本地 Marketplace，不修改 Codex 配置",
    )
    return parser


def _serve_admin() -> None:
    port = _admin_port()
    url = f"http://127.0.0.1:{port}"
    opener = threading.Timer(0.8, lambda: webbrowser.open(url))
    opener.daemon = True
    opener.start()
    print(f"Huayang 后台管理已启动：{url}")
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="info")


def _admin_port() -> int:
    raw = os.environ.get("HUAYANG_ADMIN_PORT", "8788")
    try:
        port = int(raw)
    except ValueError as error:
        raise SystemExit("HUAYANG_ADMIN_PORT 必须是数字") from error
    if port < 1 or port > 65535:
        raise SystemExit("HUAYANG_ADMIN_PORT 必须在 1 到 65535 之间")
    return port


if __name__ == "__main__":
    main()
