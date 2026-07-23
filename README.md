# Huayang 视频创作插件

本项目提供可由 Codex、Claude、Cursor 等 Agent 直接调用的视频创作插件与确定性组件。

> **当前状态**：项目处于基础架构已跑通的早期阶段——各组件可安装、可启动、主流程可运行，但功能细节、稳定性与体验仍在持续完善和优化中，接口与行为可能随时调整。

## 核心功能

- **剪辑创作插件**：以 MCP 方式向 Agent 暴露从学习到成片的完整创作链路，内置 Rules/Skills 知识体系与分阶段共享知识检索。
- **本地管理后台**：维护 Rules/Skills，查看创作产物、学习产物、工作流任务与 Artifact。
- **本地视频剪辑器**：浏览器内多轨道剪辑（裁剪、分割、文字、画中画、变换、导出），同一工程模型同时暴露 Python API、CLI 与 HTTP API。
- **视频下载**：输入视频链接或平台分享文本，下载到本地并输出 JSON 结果。
- **素材获取**：检索并下载可用素材，自动记录来源与授权。

### 支持的 3 类任务

插件按用户意图自动路由到三类任务：

| 任务类型 | 说明 |
| --- | --- |
| 参考视频学习 `reference_study` | 只学习参考片，拆解其画面、BGM 与剪辑手法 |
| 原创创作 `original_creation` | 从一个想法出发，全新制作视频 |
| 参考驱动创作 `reference_guided_creation` | 以参考片为蓝本，用新素材制作视频 |

## 使用方式

### 一键安装

脚本会检查并按需安装 git、uv、FFmpeg，验证或克隆官方仓库，同步 Python 依赖、安装 Playwright Chromium、安装 `huayang` 命令，并执行安装自检。检测到 Codex CLI 时还会自动构建并安装 Huayang Plugin。

Windows（PowerShell）：

```powershell
iwr -useb https://raw.githubusercontent.com/guigu856/huayang/main/install.ps1 | iex
```

macOS / Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/guigu856/huayang/main/install.sh | bash
```

安装脚本拒绝覆盖来源不匹配、损坏或包含未提交修改的托管仓库目录，更新失败时也不会继续使用旧版本。uv 命令目录通过 `uv tool dir --bin` 动态获取，支持自定义 `UV_TOOL_BIN_DIR`。

### 手动安装（开发者）

```powershell
uv sync --extra dev
uv run playwright install chromium
uv tool install --force --editable .
huayang doctor
```

### 安装或刷新 Codex Plugin

该命令从已安装包自动定位 `.mcp.json`、Rules、Skills 与 Schemas，不依赖当前工作目录：

```powershell
huayang plugin install codex
```

只构建本地 Marketplace、不修改 Codex 配置：

```powershell
huayang plugin install codex --build-only
```

### 安装自检

```powershell
huayang doctor
huayang doctor --json
```

自检会检查 Python、插件资源、FFmpeg、ffprobe、MCP Server 构建以及 Chromium 实际启动。仅检查 Chromium 文件、不启动浏览器时使用 `--skip-browser`。

### 启动本地管理后台

终端只输入以下命令即可启动本地后台并自动打开浏览器：

```powershell
huayang
```

后台默认监听 `http://127.0.0.1:8788`。

### 启动 MCP Server

```powershell
huayang-mcp
```

### 本地视频剪辑器

```powershell
uv run video-editor serve --host 127.0.0.1 --port 8765
```

浏览器打开 `http://127.0.0.1:8765` 即可使用。

### 视频下载

```powershell
uv run video-download "<视频链接或平台分享文本>"
```

命令成功时向标准输出写一行 JSON，视频保存在 `output/download/`。

### 素材获取

```powershell
uv run material-acquisition sources
uv run material-acquisition search "vintage cereal commercial" --source archive_org --limit 1
uv run material-acquisition acquire "<candidate_ref>"
```

素材保存在 `output/materials/downloads/`，来源和授权记录保存在 `output/materials/provenance/`。
