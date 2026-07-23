# Huayang 视频创作插件

本项目提供可由 Codex、Claude、Cursor 等 Agent 直接调用的视频创作插件与确定性组件。插件公共名称和 Python 分发名均为 `huayang`，内部 Python 包保持为 `video_create_plugin`。

安装 Huayang 命令并启动 stdio MCP Server：

```powershell
uv sync --extra dev
uv run playwright install chromium
uv tool install --force --editable .
huayang-mcp
```

将最小插件包注册并安装到 Codex：

```powershell
uv run python -c "from pathlib import Path; from video_create_plugin.codex_install import build_codex_marketplace; print(build_codex_marketplace(Path.cwd()))"
codex plugin marketplace add "$env:LOCALAPPDATA\huayang\marketplace"
codex plugin add huayang@huayang-local
```

终端只输入以下命令即可启动本地后台并自动打开浏览器：

```powershell
huayang
```

后台默认监听 `http://127.0.0.1:8788`，用于查看和维护 Rules、Skills，查看创作产物、学习产物、工作流任务与 Artifact。内置 Rule/Skill 可编辑且保留删除保护；自定义 Rule/Skill 支持新增、编辑和删除。保存后 MCP Context Catalog 立即读取新版本；Codex 原生 Skills 使用安装快照，重建 Marketplace 并新建 Codex 任务后刷新。

Huayang 通过 `.codex-plugin/`、`.claude-plugin/` 和 `.mcp.json` 暴露参考视频学习、原创创作、参考驱动创作、共享知识检索、素材与 BGM 筹备、剪辑规格编译、渲染和成片检查。原创任务的总体方案、素材与 BGM、剪辑规格三个阶段都会检索按阶段过滤的共享知识，并将检索审计写入阶段产物。完整边界见
[`docs/组件/剪辑创作插件.md`](docs/组件/剪辑创作插件.md)，架构合同见
[`docs/架构/剪辑创作插件架构设计.md`](docs/架构/剪辑创作插件架构设计.md)。

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
