# 视频创作组件

本项目提供可由 Codex、Claude、Cursor 等 Agent 直接调用的视频创作组件。

本地视频剪辑器：

```powershell
uv sync --extra dev
uv run video-editor serve --host 127.0.0.1 --port 8765
```

浏览器打开 `http://127.0.0.1:8765`，可导入视频、图片和音频，动态增删、改名和重排 visual/audio 轨道，并完成移动、裁剪、分割、文字、画中画、变换与导出。Agent 通过同一 `schema_version: "2.0"` 工程模型调用 Python API、`video-editor` CLI 或 `/api/v1` HTTP API。完整契约见
[`docs/组件/本地视频剪辑器.md`](docs/组件/本地视频剪辑器.md)，产品与架构设计见
[`docs/产品/本地视频剪辑器PRD.md`](docs/产品/本地视频剪辑器PRD.md) 和
[`docs/架构/本地视频剪辑器架构.md`](docs/架构/本地视频剪辑器架构.md)。

视频下载：

```powershell
uv sync --extra dev
uv run playwright install chromium
uv run video-download "<视频链接或平台分享文本>"
```

命令成功时向标准输出写一行 JSON，视频保存在 `output/download/`。完整接口与配置见
[`docs/组件/视频下载组件.md`](docs/组件/视频下载组件.md)，常用命令见
[`docs/命令速查.md`](docs/命令速查.md)。

素材获取：

```powershell
uv run material-acquisition sources
uv run material-acquisition search "vintage cereal commercial" --source archive_org --limit 1
uv run material-acquisition acquire "<candidate_ref>"
```

素材保存在 `output/materials/downloads/`，来源和授权记录保存在
`output/materials/provenance/`。完整接口见
[`docs/组件/素材获取组件.md`](docs/组件/素材获取组件.md)。
