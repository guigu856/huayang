import { ApiError, editorApi } from "./api.js";
import { createPreview } from "./preview.js";
import {
  allClips,
  createEditorState,
  findAsset,
  findClip,
  formatTimecode,
  projectDuration,
} from "./state.js";
import { createTimeline } from "./timeline.js";

const DEFAULT_TRACKS = [
  { type: "track.add", media_domain: "visual", name: "视觉 1", index: 0 },
  { type: "track.add", media_domain: "audio", name: "音频 1" },
];
const LAST_PROJECT_KEY = "video-create:last-project-id";

const elements = {
  projectName: document.querySelector("#project-name"),
  saveState: document.querySelector("#save-state"),
  newProject: document.querySelector("#new-project"),
  renderProject: document.querySelector("#render-project"),
  assetUpload: document.querySelector("#asset-upload"),
  assetList: document.querySelector("#asset-list"),
  assetEmpty: document.querySelector("#asset-empty"),
  addText: document.querySelector("#add-text"),
  canvasLabel: document.querySelector("#canvas-label"),
  revisionLabel: document.querySelector("#revision-label"),
  previewCanvas: document.querySelector("#preview-canvas"),
  previewLayers: document.querySelector("#preview-layers"),
  previewEmpty: document.querySelector("#preview-empty"),
  jumpStart: document.querySelector("#jump-start"),
  playToggle: document.querySelector("#play-toggle"),
  currentTime: document.querySelector("#current-time"),
  duration: document.querySelector("#duration"),
  previewScale: document.querySelector("#preview-scale"),
  selectionKind: document.querySelector("#selection-kind"),
  inspectorEmpty: document.querySelector("#inspector-empty"),
  inspector: document.querySelector("#clip-inspector"),
  textFields: document.querySelector("#text-fields"),
  volumeValue: document.querySelector("#volume-value"),
  deleteClip: document.querySelector("#delete-clip"),
  splitClip: document.querySelector("#split-clip"),
  deleteSelected: document.querySelector("#delete-selected"),
  addVisualTrack: document.querySelector("#add-visual-track"),
  addAudioTrack: document.querySelector("#add-audio-track"),
  clipCount: document.querySelector("#clip-count"),
  timelineZoom: document.querySelector("#timeline-zoom"),
  trackLabels: document.querySelector("#track-labels"),
  timelineContent: document.querySelector("#timeline-content"),
  timeRuler: document.querySelector("#time-ruler"),
  trackList: document.querySelector("#track-list"),
  playhead: document.querySelector("#playhead"),
  toastRegion: document.querySelector("#toast-region"),
  renderDialog: document.querySelector("#render-dialog"),
  renderIdle: document.querySelector("#render-idle"),
  startRender: document.querySelector("#start-render"),
  renderProgress: document.querySelector("#render-progress"),
  renderProgressBar: document.querySelector("#render-progress-bar"),
  renderStatus: document.querySelector("#render-status"),
  renderMessage: document.querySelector("#render-message"),
  cancelRender: document.querySelector("#cancel-render"),
};

const state = createEditorState();
const preview = createPreview(elements.previewLayers, {
  onSelect(clipId) {
    state.selectClip(clipId);
  },
  onTransformStart() {
    stopPlayback();
  },
  onTransform(clip, transform) {
    const selected = findClip(state.value.project, clip.id);
    if (!selected) return;
    void applyCommands([
      {
        type: "clip.update",
        track_id: selected.track.id,
        clip_id: clip.id,
        changes: { transform },
      },
    ]);
  },
});
let commandQueue = Promise.resolve();
let animationFrame = 0;
let previousFrameTime = 0;
let activeRenderId = null;

function toast(message, isError = false) {
  const item = document.createElement("div");
  item.className = `toast${isError ? " is-error" : ""}`;
  item.textContent = message;
  elements.toastRegion.append(item);
  window.setTimeout(() => item.remove(), 3600);
}

function errorMessage(error) {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "操作失败";
}

function handleCommandError(error) {
  if (error instanceof ApiError && error.code === "revision_conflict") {
    void reloadProject();
    toast("工程已被其他编辑者更新，已重新加载", true);
    return;
  }
  elements.saveState.textContent = "保存失败";
  toast(errorMessage(error), true);
}

function enqueueMutation(callback) {
  const operation = commandQueue.catch(() => null).then(callback);
  commandQueue = operation.then(
    () => null,
    () => null,
  );
  operation.catch(handleCommandError);
  return operation;
}

function applyCommands(commands) {
  return enqueueMutation(async () => {
    const project = state.value.project;
    if (!project) return null;
    elements.saveState.textContent = "保存中…";
    const updated = await editorApi.applyCommands(project.id, project.revision, commands);
    state.setProject(updated);
    elements.saveState.textContent = "已保存";
    return updated;
  });
}

async function reloadProject() {
  const project = state.value.project;
  if (!project) return;
  state.setProject(await editorApi.getProject(project.id));
  elements.saveState.textContent = "已保存";
}

function trackForAsset(project, asset) {
  const mediaDomain = asset.kind === "audio" ? "audio" : "visual";
  return project.tracks.find((track) => track.media_domain === mediaDomain) ?? null;
}

function fitTransform(project, asset) {
  const { width: canvasWidth, height: canvasHeight } = project.canvas;
  const sourceWidth = asset.metadata.width;
  const sourceHeight = asset.metadata.height;
  if (!sourceWidth || !sourceHeight) {
    return { x: 0, y: 0, width: canvasWidth, height: canvasHeight, rotation: 0, opacity: 1 };
  }
  const scale = Math.min(canvasWidth / sourceWidth, canvasHeight / sourceHeight);
  const width = sourceWidth * scale;
  const height = sourceHeight * scale;
  return {
    x: (canvasWidth - width) / 2,
    y: (canvasHeight - height) / 2,
    width,
    height,
    rotation: 0,
    opacity: 1,
  };
}

function addAssetToTrack(track, assetId, timelineStart) {
  const project = state.value.project;
  const asset = findAsset(project, assetId);
  if (!project || !asset) return;
  const accepted = track.media_domain === "audio" ? ["audio"] : ["video", "image"];
  if (!accepted.includes(asset.kind)) {
    toast("素材类型与目标轨道不匹配", true);
    return;
  }
  const duration = asset.kind === "image" ? 5 : asset.metadata.duration;
  if (!duration) {
    toast("素材缺少可用时长", true);
    return;
  }
  void applyCommands([
    {
      type: "clip.add",
      track_id: track.id,
      clip: {
        kind: "media",
        timeline_start: timelineStart,
        duration,
        source_in: 0,
        asset_id: asset.id,
        transform: fitTransform(project, asset),
        volume: 1,
      },
    },
  ]);
}

function appendAsset(asset) {
  const project = state.value.project;
  if (!project) return;
  const track = trackForAsset(project, asset);
  if (!track) {
    toast("工程中缺少匹配的轨道", true);
    return;
  }
  addAssetToTrack(track, asset.id, projectDuration(project));
}

function renderAssets(project) {
  elements.assetList.replaceChildren();
  elements.assetEmpty.hidden = project.assets.length > 0;
  for (const asset of project.assets) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "asset-card";
    card.draggable = true;
    card.title = "拖到时间线，或双击追加";
    const thumbnail = document.createElement("div");
    thumbnail.className = "asset-thumb";
    thumbnail.textContent = { video: "▶", image: "▧", audio: "♫" }[asset.kind];
    const name = document.createElement("strong");
    name.textContent = asset.name;
    const detail = document.createElement("span");
    const duration = asset.metadata.duration;
    detail.textContent = duration ? `${duration.toFixed(2)} 秒` : "静态图片";
    card.append(thumbnail, name, detail);
    card.addEventListener("dragstart", (event) => {
      event.dataTransfer?.setData("application/x-video-create-asset", asset.id);
    });
    card.addEventListener("dblclick", () => appendAsset(asset));
    elements.assetList.append(card);
  }
}

function renderInspector(project) {
  const selected = findClip(project, state.value.selectedClipId);
  elements.inspector.hidden = !selected;
  elements.inspectorEmpty.hidden = Boolean(selected);
  if (!selected) {
    elements.selectionKind.textContent = "未选择片段";
    return;
  }
  const { track, clip } = selected;
  elements.selectionKind.textContent = `${track.name} · ${clip.kind === "text" ? "文字" : "媒体"}`;
  elements.textFields.hidden = clip.kind !== "text";
  const values = {
    start: clip.timeline_start,
    duration: clip.duration,
    source_in: clip.source_in,
    x: clip.transform.x,
    y: clip.transform.y,
    width: clip.transform.width,
    height: clip.transform.height,
    rotation: clip.transform.rotation,
    opacity: clip.transform.opacity,
    text: clip.text ?? "",
    volume: clip.volume,
  };
  for (const [name, value] of Object.entries(values)) {
    const input = elements.inspector.elements.namedItem(name);
    if (input) input.value = String(value);
  }
  elements.volumeValue.textContent = `${Math.round(clip.volume * 100)}%`;
}

function renderUi() {
  const project = state.value.project;
  if (!project) return;
  const duration = projectDuration(project);
  elements.projectName.value = project.name;
  elements.canvasLabel.textContent = `${project.canvas.width} × ${project.canvas.height}`;
  elements.previewCanvas.style.aspectRatio = `${project.canvas.width} / ${project.canvas.height}`;
  elements.revisionLabel.textContent = `版本 ${project.revision}`;
  elements.currentTime.textContent = formatTimecode(state.value.playheadSeconds);
  elements.duration.textContent = formatTimecode(duration);
  elements.clipCount.textContent = `${allClips(project).length} 个片段`;
  elements.renderProject.disabled = allClips(project).length === 0;
  elements.playToggle.textContent = state.value.isPlaying ? "Ⅱ" : "▶";
  elements.previewEmpty.hidden = allClips(project).length > 0;
  renderAssets(project);
  renderInspector(project);
  timeline.render(project, state.value.pixelsPerSecond, state.value.selectedClipId);
  timeline.updatePlayhead(state.value.playheadSeconds);
  preview.render(
    project,
    state.value.playheadSeconds,
    state.value.isPlaying,
    state.value.selectedClipId,
  );
}

const timeline = createTimeline(
  {
    labels: elements.trackLabels,
    content: elements.timelineContent,
    ruler: elements.timeRuler,
    tracks: elements.trackList,
    playhead: elements.playhead,
  },
  {
    onMove(clip, timelineStart) {
      const selected = findClip(state.value.project, clip.id);
      if (!selected) return;
      void applyCommands([
        {
          type: "clip.update",
          track_id: selected.track.id,
          clip_id: clip.id,
          changes: { timeline_start: timelineStart },
        },
      ]);
    },
    onTrim(clip, changes) {
      const selected = findClip(state.value.project, clip.id);
      if (!selected) return;
      void applyCommands([
        {
          type: "clip.update",
          track_id: selected.track.id,
          clip_id: clip.id,
          changes,
        },
      ]);
    },
    onSelect(clipId) {
      state.selectClip(clipId);
    },
    onSeek(seconds) {
      state.setPlayhead(seconds);
    },
    onScrubStart() {
      stopPlayback();
    },
    onAssetDrop(track, assetId, seconds) {
      addAssetToTrack(track, assetId, seconds);
    },
    onTrackRename(track, name) {
      const resolved = name.trim();
      if (!resolved || resolved === track.name) return;
      void applyCommands([
        { type: "track.update", track_id: track.id, changes: { name: resolved } },
      ]);
    },
    onTrackMove(track, toIndex) {
      void applyCommands([{ type: "track.move", track_id: track.id, to_index: toIndex }]);
    },
    onTrackDelete(track) {
      if (track.clips.some((clip) => clip.id === state.value.selectedClipId)) {
        state.selectClip(null);
      }
      void applyCommands([{ type: "track.delete", track_id: track.id }]);
    },
  },
);

function deleteSelectedClip() {
  const selected = findClip(state.value.project, state.value.selectedClipId);
  if (!selected) return;
  state.selectClip(null);
  void applyCommands([
    { type: "clip.delete", track_id: selected.track.id, clip_id: selected.clip.id },
  ]);
}

function splitSelectedClip() {
  const selected = findClip(state.value.project, state.value.selectedClipId);
  if (!selected) return;
  const at = state.value.playheadSeconds;
  const start = selected.clip.timeline_start;
  if (at <= start || at >= start + selected.clip.duration) {
    toast("播放头需要位于所选片段内部", true);
    return;
  }
  void applyCommands([
    {
      type: "clip.split",
      track_id: selected.track.id,
      clip_id: selected.clip.id,
      at,
    },
  ]);
}

function updateInspectorField(event) {
  const input = event.target;
  if (!(input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement)) return;
  const selected = findClip(state.value.project, state.value.selectedClipId);
  if (!selected) return;
  if (input.name === "text" && selected.clip.kind !== "text") return;
  let changes;
  if (["x", "y", "width", "height", "rotation", "opacity"].includes(input.name)) {
    changes = {
      transform: { ...selected.clip.transform, [input.name]: Number(input.value) },
    };
  } else if (input.name === "start") {
    changes = { timeline_start: Number(input.value) };
  } else if (input.name === "text") {
    changes = { text: input.value };
  } else {
    changes = { [input.name]: Number(input.value) };
  }
  void applyCommands([
    {
      type: "clip.update",
      track_id: selected.track.id,
      clip_id: selected.clip.id,
      changes,
    },
  ]);
}

function playbackFrame(timestamp) {
  if (!state.value.isPlaying) return;
  if (!previousFrameTime) previousFrameTime = timestamp;
  const elapsed = (timestamp - previousFrameTime) / 1000;
  previousFrameTime = timestamp;
  const duration = projectDuration(state.value.project);
  const next = state.value.playheadSeconds + elapsed;
  if (next >= duration) {
    state.setPlayhead(duration);
    state.setPlaying(false);
    previousFrameTime = 0;
    return;
  }
  state.setPlayhead(next);
  animationFrame = window.requestAnimationFrame(playbackFrame);
}

function togglePlayback() {
  const next = !state.value.isPlaying;
  if (next && state.value.playheadSeconds >= projectDuration(state.value.project)) {
    state.setPlayhead(0);
  }
  state.setPlaying(next);
  previousFrameTime = 0;
  window.cancelAnimationFrame(animationFrame);
  if (next) animationFrame = window.requestAnimationFrame(playbackFrame);
}

function stopPlayback() {
  if (!state.value.isPlaying) return;
  state.setPlaying(false);
  previousFrameTime = 0;
  window.cancelAnimationFrame(animationFrame);
  animationFrame = 0;
}

async function createProject() {
  const project = await editorApi.createProject(`视频工程 ${new Date().toLocaleString("zh-CN")}`);
  state.setProject(project);
  const initialized = await editorApi.applyCommands(project.id, project.revision, DEFAULT_TRACKS);
  state.setProject(initialized);
  window.localStorage.setItem(LAST_PROJECT_KEY, initialized.id);
  elements.saveState.textContent = "已保存";
}

async function loadInitialProject() {
  const projects = await editorApi.listProjects();
  if (projects.length === 0) {
    await createProject();
    return;
  }
  const lastProjectId = window.localStorage.getItem(LAST_PROJECT_KEY);
  const selected = projects.find((project) => project.id === lastProjectId) ?? projects[0];
  const project = await editorApi.getProject(selected.id);
  state.setProject(project);
  window.localStorage.setItem(LAST_PROJECT_KEY, project.id);
  if (project.tracks.length === 0) {
    state.setProject(await editorApi.applyCommands(project.id, project.revision, DEFAULT_TRACKS));
  }
}

async function uploadFiles(files) {
  for (const file of files) {
    try {
      await enqueueMutation(async () => {
        const project = state.value.project;
        if (!project) return;
        elements.saveState.textContent = `正在导入 ${file.name}`;
        const result = await editorApi.uploadAsset(project.id, project.revision, file);
        state.setProject(result.project);
      });
      toast(`已导入 ${file.name}`);
    } catch (error) {
      // enqueueMutation 已按统一错误契约更新界面。
    }
  }
  elements.assetUpload.value = "";
  elements.saveState.textContent = "已保存";
}

function addTrack(mediaDomain) {
  const project = state.value.project;
  if (!project) return;
  const prefix = mediaDomain === "visual" ? "视觉" : "音频";
  const names = new Set(project.tracks.map((track) => track.name));
  let number = 1;
  while (names.has(`${prefix} ${number}`)) number += 1;
  const name = `${prefix} ${number}`;
  const command = { type: "track.add", media_domain: mediaDomain, name };
  if (mediaDomain === "visual") command.index = 0;
  void applyCommands([command]);
}

async function addTextClip() {
  let project = state.value.project;
  if (!project) return;
  let track = project.tracks.find((item) => item.media_domain === "visual");
  if (!track) {
    project = await applyCommands([
      { type: "track.add", media_domain: "visual", name: "视觉 1", index: 0 },
    ]);
    track = project?.tracks.find((item) => item.media_domain === "visual");
  }
  if (!project || !track) return;
  await applyCommands([
    {
      type: "clip.add",
      track_id: track.id,
      clip: {
        kind: "text",
        timeline_start: state.value.playheadSeconds,
        duration: 5,
        source_in: 0,
        text: "双击属性面板编辑文字",
        transform: {
          x: 0,
          y: project.canvas.height / 2 - 90,
          width: project.canvas.width,
          height: 180,
          rotation: 0,
          opacity: 1,
        },
        volume: 1,
      },
    },
  ]);
}

async function pollRender(jobId) {
  const job = await editorApi.getRender(jobId);
  const progress = Math.round((job.progress ?? 0) * 100);
  elements.renderProgressBar.style.width = `${Math.max(8, progress)}%`;
  elements.renderStatus.textContent = {
    queued: "等待渲染",
    running: "正在渲染",
    succeeded: "导出完成",
    failed: "导出失败",
    cancelled: "已取消",
    interrupted: "任务已中断",
  }[job.status] ?? job.status;
  elements.renderMessage.textContent =
    job.error?.message ?? job.message ?? `进度 ${progress}%`;
  if (["queued", "running"].includes(job.status)) {
    window.setTimeout(() => void pollRender(jobId).catch(handleCommandError), 700);
    return;
  }
  activeRenderId = null;
  elements.cancelRender.hidden = true;
  if (job.status === "succeeded") {
    const link = document.createElement("a");
    link.className = "button button-primary render-download";
    link.href = editorApi.renderDownloadUrl(jobId);
    link.textContent = "下载 MP4";
    elements.renderProgress.append(link);
  }
}

async function startRender() {
  await commandQueue;
  const project = state.value.project;
  if (!project) return;
  elements.renderIdle.hidden = true;
  elements.renderProgress.hidden = false;
  const job = await editorApi.createRender(project.id, project.revision);
  activeRenderId = job.id;
  elements.cancelRender.hidden = false;
  elements.cancelRender.disabled = false;
  await pollRender(job.id);
}

state.subscribe((current, reason) => {
  if (reason === "playhead" || reason === "playback") {
    elements.currentTime.textContent = formatTimecode(current.playheadSeconds);
    elements.playToggle.textContent = current.isPlaying ? "Ⅱ" : "▶";
    timeline.updatePlayhead(current.playheadSeconds);
    preview.render(
      current.project,
      current.playheadSeconds,
      current.isPlaying,
      current.selectedClipId,
    );
    return;
  }
  renderUi();
});

elements.newProject.addEventListener("click", () => void enqueueMutation(createProject));
elements.projectName.addEventListener("change", () => {
  const name = elements.projectName.value.trim();
  if (name) void applyCommands([{ type: "project.update", changes: { name } }]);
});
elements.assetUpload.addEventListener("change", () => void uploadFiles(elements.assetUpload.files));
elements.assetList.parentElement.addEventListener("dragover", (event) => event.preventDefault());
elements.assetList.parentElement.addEventListener("drop", (event) => {
  event.preventDefault();
  if (event.dataTransfer?.files.length) void uploadFiles(event.dataTransfer.files);
});
elements.addText.addEventListener("click", () => void addTextClip().catch(() => {}));
elements.addVisualTrack.addEventListener("click", () => addTrack("visual"));
elements.addAudioTrack.addEventListener("click", () => addTrack("audio"));
elements.jumpStart.addEventListener("click", () => state.setPlayhead(0));
elements.playToggle.addEventListener("click", togglePlayback);
elements.timelineZoom.addEventListener("input", () => state.setPixelsPerSecond(elements.timelineZoom.value));
elements.previewScale.addEventListener("change", () => {
  const project = state.value.project;
  if (!project) return;
  if (elements.previewScale.value === "fit") {
    elements.previewCanvas.style.width = "";
  } else {
    elements.previewCanvas.style.width = `${project.canvas.width * Number(elements.previewScale.value)}px`;
  }
});
elements.inspector.addEventListener("change", updateInspectorField);
elements.inspector.elements.namedItem("volume").addEventListener("input", (event) => {
  elements.volumeValue.textContent = `${Math.round(Number(event.target.value) * 100)}%`;
});
elements.deleteClip.addEventListener("click", deleteSelectedClip);
elements.deleteSelected.addEventListener("click", deleteSelectedClip);
elements.splitClip.addEventListener("click", splitSelectedClip);
elements.renderProject.addEventListener("click", () => {
  elements.renderProgress.querySelector(".render-download")?.remove();
  elements.renderIdle.hidden = false;
  elements.renderProgress.hidden = true;
  elements.renderDialog.showModal();
});
elements.startRender.addEventListener("click", () => void startRender().catch(handleCommandError));
elements.cancelRender.addEventListener("click", () => {
  if (!activeRenderId) return;
  elements.cancelRender.disabled = true;
  void editorApi
    .cancelRender(activeRenderId)
    .catch(handleCommandError)
    .finally(() => {
      if (activeRenderId) elements.cancelRender.disabled = false;
    });
});
document.addEventListener("keydown", (event) => {
  const target = event.target;
  const editing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement;
  if (editing) return;
  if (event.code === "Space") {
    event.preventDefault();
    togglePlayback();
  } else if (event.key === "Delete") {
    deleteSelectedClip();
  } else if (event.key.toLowerCase() === "s") {
    splitSelectedClip();
  }
});

void loadInitialProject().catch((error) => {
  elements.saveState.textContent = "连接失败";
  toast(errorMessage(error), true);
});
