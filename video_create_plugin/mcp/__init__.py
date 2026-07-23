"""视频创作 Plugin 的 stdio MCP 服务。"""

from .application import PluginApplication
from .server import build_server

__all__ = ["PluginApplication", "build_server"]
