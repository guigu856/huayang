from __future__ import annotations

import os
import threading
import webbrowser

import uvicorn

from .api import create_app


def main() -> None:
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
