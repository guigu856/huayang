import { editorApi } from "./api.js";
import { findAsset, visibleClips } from "./state.js";

export function createPreview(container, callbacks) {
  const elements = new Map();
  const audioGraphs = new Map();
  const selection = document.createElement("div");
  selection.className = "preview-selection";
  selection.hidden = true;
  for (const handle of ["nw", "ne", "sw", "se"]) {
    const control = document.createElement("span");
    control.className = "preview-resize-handle";
    control.dataset.handle = handle;
    control.addEventListener("pointerdown", (event) => beginTransform(event, handle));
    selection.append(control);
  }
  selection.addEventListener("pointerdown", (event) => beginTransform(event, null));
  container.append(selection);
  let audioContext;
  let activeSelection = null;

  function ensureAudioGraph(clipId, media) {
    let graph = audioGraphs.get(clipId);
    if (graph) return graph;
    audioContext ??= new AudioContext();
    const source = audioContext.createMediaElementSource(media);
    const gain = audioContext.createGain();
    source.connect(gain).connect(audioContext.destination);
    graph = { source, gain };
    audioGraphs.set(clipId, graph);
    return graph;
  }

  function removeUnused(activeIds) {
    for (const [clipId, element] of elements) {
      if (!activeIds.has(clipId)) {
        const media = element.firstElementChild;
        if (media instanceof HTMLMediaElement) media.pause();
        const graph = audioGraphs.get(clipId);
        if (graph) {
          graph.source.disconnect();
          graph.gain.disconnect();
          audioGraphs.delete(clipId);
        }
        element.remove();
        elements.delete(clipId);
      }
    }
  }

  function mediaElement(project, clip, asset) {
    const wrapper = document.createElement("div");
    wrapper.className = "preview-layer";
    wrapper.dataset.clipId = clip.id;
    let media;
    if (asset.kind === "image") {
      media = document.createElement("img");
      media.alt = asset.name;
    } else if (asset.kind === "audio") {
      media = document.createElement("audio");
      wrapper.hidden = true;
    } else {
      media = document.createElement("video");
      media.playsInline = true;
    }
    media.preload = "metadata";
    media.src = editorApi.assetUrl(project.id, asset.id);
    wrapper.append(media);
    wrapper.addEventListener("pointerdown", (event) => selectClip(event, clip.id));
    return wrapper;
  }

  function textElement(clip) {
    const element = document.createElement("div");
    element.className = "preview-layer preview-text-layer";
    element.dataset.clipId = clip.id;
    element.addEventListener("pointerdown", (event) => selectClip(event, clip.id));
    return element;
  }

  function selectClip(event, clipId) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    callbacks.onSelect(clipId);
  }

  function setGeometry(element, transform, canvas) {
    element.style.left = `${(transform.x / canvas.width) * 100}%`;
    element.style.top = `${(transform.y / canvas.height) * 100}%`;
    element.style.width = `${(transform.width / canvas.width) * 100}%`;
    element.style.height = `${(transform.height / canvas.height) * 100}%`;
    element.style.transform = `rotate(${transform.rotation}deg)`;
  }

  function positionElement(element, clip, canvas) {
    const transform = clip.transform;
    setGeometry(element, transform, canvas);
    element.style.opacity = String(transform.opacity);
  }

  function positionSelection(clip, canvas) {
    selection.dataset.clipId = clip.id;
    setGeometry(selection, clip.transform, canvas);
    selection.hidden = false;
  }

  function applyDraft(clipId, transform, canvas) {
    const element = elements.get(clipId);
    if (element) positionElement(element, { transform }, canvas);
    positionSelection({ id: clipId, transform }, canvas);
  }

  function rounded(value) {
    return Math.round(value * 1000) / 1000;
  }

  function resizedTransform(start, handle, dx, dy) {
    const angle = (start.rotation * Math.PI) / 180;
    const cosine = Math.cos(angle);
    const sine = Math.sin(angle);
    const localDx = dx * cosine + dy * sine;
    const localDy = -dx * sine + dy * cosine;
    const directionX = handle.includes("e") ? 1 : -1;
    const directionY = handle.includes("s") ? 1 : -1;
    const denominator = start.width ** 2 + start.height ** 2;
    const scale = Math.max(
      Math.max(16 / start.width, 16 / start.height),
      1 +
        (localDx * directionX * start.width + localDy * directionY * start.height) /
          denominator,
    );
    const width = rounded(start.width * scale);
    const height = rounded(start.height * scale);
    const centerX = start.x + start.width / 2;
    const centerY = start.y + start.height / 2;
    const anchorLocalX = (-directionX * start.width) / 2;
    const anchorLocalY = (-directionY * start.height) / 2;
    const anchorX = centerX + anchorLocalX * cosine - anchorLocalY * sine;
    const anchorY = centerY + anchorLocalX * sine + anchorLocalY * cosine;
    const nextAnchorLocalX = (-directionX * width) / 2;
    const nextAnchorLocalY = (-directionY * height) / 2;
    const nextCenterX = anchorX - (nextAnchorLocalX * cosine - nextAnchorLocalY * sine);
    const nextCenterY = anchorY - (nextAnchorLocalX * sine + nextAnchorLocalY * cosine);
    return {
      ...start,
      x: rounded(nextCenterX - width / 2),
      y: rounded(nextCenterY - height / 2),
      width,
      height,
    };
  }

  function beginTransform(event, handle) {
    if (event.button !== 0 || !activeSelection) return;
    event.preventDefault();
    event.stopPropagation();
    callbacks.onTransformStart?.();

    const target = event.currentTarget;
    const pointerId = event.pointerId;
    const { clip, canvas } = activeSelection;
    const start = { ...clip.transform };
    const bounds = container.getBoundingClientRect();
    const scaleX = canvas.width / bounds.width;
    const scaleY = canvas.height / bounds.height;
    const startX = event.clientX;
    const startY = event.clientY;
    let draft = start;
    let changed = false;
    let cleanedUp = false;

    function update(nextEvent) {
      if (nextEvent.pointerId !== pointerId) return;
      const dx = (nextEvent.clientX - startX) * scaleX;
      const dy = (nextEvent.clientY - startY) * scaleY;
      draft = handle
        ? resizedTransform(start, handle, dx, dy)
        : { ...start, x: rounded(start.x + dx), y: rounded(start.y + dy) };
      changed = true;
      applyDraft(clip.id, draft, canvas);
    }

    function cleanup() {
      if (cleanedUp) return;
      cleanedUp = true;
      target.removeEventListener("pointermove", update);
      target.removeEventListener("pointerup", commit);
      target.removeEventListener("pointercancel", cancel);
      target.removeEventListener("lostpointercapture", cancel);
      selection.classList.remove("is-transforming");
    }

    function commit(nextEvent) {
      if (nextEvent.pointerId !== pointerId) return;
      update(nextEvent);
      cleanup();
      if (changed) callbacks.onTransform(clip, draft);
    }

    function cancel(nextEvent) {
      if (nextEvent.pointerId !== pointerId) return;
      cleanup();
      applyDraft(clip.id, start, canvas);
    }

    selection.classList.add("is-transforming");
    target.addEventListener("pointermove", update);
    target.addEventListener("pointerup", commit);
    target.addEventListener("pointercancel", cancel);
    target.addEventListener("lostpointercapture", cancel);
    target.setPointerCapture(pointerId);
  }

  function render(project, seconds, isPlaying, selectedClipId) {
    if (!project) {
      removeUnused(new Set());
      activeSelection = null;
      selection.hidden = true;
      return;
    }
    const active = visibleClips(project, seconds);
    const activeIds = new Set(active.map(({ clip }) => clip.id));
    removeUnused(activeIds);

    active.forEach(({ track, clip }) => {
      const asset = findAsset(project, clip.asset_id);
      let element = elements.get(clip.id);
      if (!element) {
        if (clip.kind === "media" && !asset) return;
        element = clip.kind === "text" ? textElement(clip) : mediaElement(project, clip, asset);
        elements.set(clip.id, element);
        container.append(element);
      }
      positionElement(element, clip, project.canvas);
      const trackIndex = project.tracks.indexOf(track);
      const clipIndex = track.clips.indexOf(clip);
      element.style.zIndex = String((project.tracks.length - trackIndex) * 1000 + clipIndex);

      if (clip.kind === "text") {
        element.textContent = clip.text ?? "";
        element.style.color = "#ffffff";
        const previewHeight = container.clientHeight || project.canvas.height;
        element.style.fontSize = `${(48 / project.canvas.height) * previewHeight}px`;
        return;
      }

      const media = element.firstElementChild;
      if (!(media instanceof HTMLMediaElement)) return;
      if (isPlaying) {
        const graph = ensureAudioGraph(clip.id, media);
        graph.gain.gain.value = clip.volume;
      }
      const targetTime = clip.source_in + seconds - clip.timeline_start;
      if (Number.isFinite(media.duration)) {
        const clamped = Math.max(0, Math.min(targetTime, media.duration));
        if (Math.abs(media.currentTime - clamped) > 0.12) media.currentTime = clamped;
      }
      if (isPlaying && media.paused) {
        if (audioContext?.state === "suspended") void audioContext.resume().catch(() => {});
        void media.play().catch(() => {});
      } else if (!isPlaying && !media.paused) {
        media.pause();
      }
    });

    const selected = active.find(
      ({ track, clip }) => track.media_domain === "visual" && clip.id === selectedClipId,
    );
    activeSelection = selected ? { clip: selected.clip, canvas: project.canvas } : null;
    if (activeSelection) {
      positionSelection(activeSelection.clip, project.canvas);
    } else {
      selection.hidden = true;
      delete selection.dataset.clipId;
    }
  }

  return { render };
}
