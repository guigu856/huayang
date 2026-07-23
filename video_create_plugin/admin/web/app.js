const state = {
  view: "overview",
  resourceKind: null,
  resources: [],
  currentResource: null,
  isCreating: false,
  generation: 0,
  selectionGeneration: 0,
};

const content = document.querySelector("#content");
const pageTitle = document.querySelector("#page-title");
const refreshButton = document.querySelector("#refresh-button");
const previewDialog = document.querySelector("#preview-dialog");
const previewTitle = document.querySelector("#preview-title");
const previewBody = document.querySelector("#preview-body");
const toastElement = document.querySelector("#toast");

const titles = {
  overview: "创作系统概览",
  rules: "Rules 管理",
  skills: "Skills 管理",
  creation: "创作产物",
  learning: "学习产物",
  artifacts: "工作流 Artifacts",
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 204) return null;
  const payload = await response.json();
  if (!response.ok || !payload.ok) {
    throw new Error(payload?.error?.message || `请求失败：${response.status}`);
  }
  return payload.data;
}

async function pagedApi(path, view, generation) {
  const items = [];
  const separator = path.includes("?") ? "&" : "?";
  for (let offset = 0; offset <= 100000; offset += 1000) {
    const page = await api(`${path}${separator}limit=1000&offset=${offset}`);
    if (!isCurrent(generation, view)) return null;
    items.push(...page);
    if (page.length < 1000) return items;
  }
  return items;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function formatDate(value) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

let toastTimer;
function toast(message, isError = false) {
  clearTimeout(toastTimer);
  toastElement.textContent = message;
  toastElement.className = `toast is-visible${isError ? " is-error" : ""}`;
  toastTimer = setTimeout(() => { toastElement.className = "toast"; }, 2400);
}

async function renderCurrentView() {
  const generation = ++state.generation;
  const view = state.view;
  content.innerHTML = '<div class="loading">正在读取 Huayang 数据…</div>';
  pageTitle.textContent = titles[view];
  try {
    if (view === "overview") await renderOverview(generation);
    if (view === "rules") await renderResources("rule", generation);
    if (view === "skills") await renderResources("skill", generation);
    if (view === "creation") await renderOutputs("creation", generation);
    if (view === "learning") await renderOutputs("learning", generation);
    if (view === "artifacts") await renderArtifacts(generation);
  } catch (error) {
    if (!isCurrent(generation, view)) return;
    content.innerHTML = `<div class="empty-state"><div><h2>读取失败</h2><p>${escapeHtml(error.message)}</p></div></div>`;
    toast(error.message, true);
  }
}

function isCurrent(generation, view) {
  return state.generation === generation && state.view === view;
}

async function renderOverview(generation) {
  const data = await api("/api/v1/overview");
  if (!isCurrent(generation, "overview")) return;
  document.querySelector("#service-status").textContent = "运行正常";
  const metrics = [
    [data.rule_count, "Rules"],
    [data.skill_count, "Skills"],
    [data.task_count, "任务"],
    [data.artifact_count, "Artifacts"],
    [data.creation_output_count, "创作文件"],
    [data.learning_output_count, "学习文件"],
  ];
  content.innerHTML = `
    <div class="hero">
      <article class="hero-card">
        <p class="eyebrow">LOCAL CREATIVE INTELLIGENCE</p>
        <h2>从参考学习到剪辑成片，所有上下文与产物集中在这里。</h2>
        <p>编辑 Agent 使用的 Rules 与 Skills，检查工作流 Artifact，并直接预览创作和学习阶段生成的视频、图片、报告与证据。</p>
      </article>
      <article class="notice-card">
        <p class="eyebrow">ACTIVE STORAGE</p>
        <h3>本地数据边界</h3>
        <p>后台仅监听本机地址。资源写入采用 SHA 版本检查，产物预览限定在 Huayang 输出根目录。</p>
        <p class="sync-note">MCP 上下文在保存后读取新版本；Codex 原生 Skills 快照通过重建 Marketplace 并新建任务刷新。</p>
        <span class="path-line" title="${escapeHtml(data.output_root)}">${escapeHtml(data.output_root)}</span>
      </article>
    </div>
    <div class="metrics">
      ${metrics.map(([value, label]) => `<article class="metric"><strong>${value}</strong><span>${label}</span></article>`).join("")}
    </div>`;
}

async function renderResources(kind, generation = state.generation) {
  const view = kind === "rule" ? "rules" : "skills";
  const resources = await api(`/api/v1/resources?kind=${kind}`);
  if (!isCurrent(generation, view)) return;
  state.resourceKind = kind;
  state.resources = resources;
  state.currentResource = null;
  state.isCreating = false;
  content.innerHTML = `
    <div class="resource-layout">
      <section class="panel">
        <div class="panel-header">
          <h2>${kind === "rule" ? "Rules" : "Skills"} · ${state.resources.length}</h2>
          <button id="new-resource" class="small-button" type="button">新建</button>
        </div>
        <div id="resource-list" class="resource-list"></div>
      </section>
      <section id="editor-panel" class="panel editor-panel">
        <div class="empty-state"><div><h2>选择一个资源</h2><p>查看内容、编辑并保存版本。</p></div></div>
      </section>
    </div>`;
  drawResourceList();
  document.querySelector("#new-resource").addEventListener("click", beginCreateResource);
  if (state.resources.length) {
    await selectResource(state.resources[0].resource_id, generation);
  }
}

function drawResourceList() {
  const list = document.querySelector("#resource-list");
  list.innerHTML = state.resources.map((item) => `
    <button class="resource-item${state.currentResource?.resource_id === item.resource_id ? " is-active" : ""}" data-resource-id="${escapeHtml(item.resource_id)}">
      <strong>${escapeHtml(item.title)} ${item.builtin ? '<span class="badge">内置</span>' : ""}</strong>
      <p>${escapeHtml(item.description)}</p>
    </button>`).join("");
  list.querySelectorAll("[data-resource-id]").forEach((button) => {
    button.addEventListener("click", () => selectResource(button.dataset.resourceId));
  });
}

async function selectResource(resourceId, generation = state.generation) {
  const selectionGeneration = ++state.selectionGeneration;
  const kind = state.resourceKind;
  const view = kind === "rule" ? "rules" : "skills";
  const resource = await api(`/api/v1/resources/${kind}/${resourceId}`);
  if (
    !isCurrent(generation, view)
    || state.resourceKind !== kind
    || state.selectionGeneration !== selectionGeneration
  ) return;
  state.isCreating = false;
  state.currentResource = resource;
  drawResourceList();
  drawEditor();
}

function beginCreateResource() {
  state.selectionGeneration += 1;
  state.isCreating = true;
  state.currentResource = null;
  drawResourceList();
  drawEditor();
}

function drawEditor() {
  const panel = document.querySelector("#editor-panel");
  const item = state.currentResource;
  panel.innerHTML = `
    <div class="panel-header">
      <h2>${state.isCreating ? `新建 ${state.resourceKind === "rule" ? "Rule" : "Skill"}` : escapeHtml(item.title)}</h2>
      ${item ? `<span class="badge">${escapeHtml(item.uri)}</span>` : ""}
    </div>
    ${state.isCreating ? `
      <div class="editor-meta">
        <label>资源 ID<input id="resource-id" placeholder="my-resource" /></label>
        <label>标题<input id="resource-title" placeholder="资源标题" /></label>
        <label style="grid-column: 1 / -1">说明<input id="resource-description" placeholder="说明这个资源何时使用" /></label>
      </div>` : ""}
    <div class="editor-content"><textarea id="resource-content" spellcheck="false" placeholder="${state.isCreating ? "留空会生成基础模板" : ""}">${escapeHtml(item?.content || "")}</textarea></div>
    <div class="editor-actions">
      <span class="meta-note">${item ? `${escapeHtml(item.relative_path)} · ${formatBytes(item.size_bytes)} · ${item.sha256.slice(0, 10)}` : "新资源会进入 MCP Context Catalog"}</span>
      <span class="spacer"></span>
      ${item ? `<button id="delete-resource" class="danger-button" ${item.builtin ? "disabled title=\"内置阶段资源带有删除保护\"" : ""}>删除</button>` : ""}
      <button id="save-resource" class="primary-button">${state.isCreating ? "创建" : "保存"}</button>
    </div>`;
  document.querySelector("#save-resource").addEventListener("click", saveResource);
  document.querySelector("#delete-resource")?.addEventListener("click", deleteResource);
}

async function saveResource() {
  const generation = state.generation;
  const kind = state.resourceKind;
  const selectionGeneration = state.selectionGeneration;
  try {
    if (state.isCreating) {
      const resourceId = document.querySelector("#resource-id").value.trim();
      const created = await api(`/api/v1/resources/${kind}`, {
        method: "POST",
        body: JSON.stringify({
          resource_id: resourceId,
          title: document.querySelector("#resource-title").value.trim(),
          description: document.querySelector("#resource-description").value.trim(),
          content: document.querySelector("#resource-content").value,
        }),
      });
      toast("资源已创建");
      if (
        !isCurrent(generation, kind === "rule" ? "rules" : "skills")
        || state.selectionGeneration !== selectionGeneration
      ) return;
      await renderResources(kind, generation);
      await selectResource(created.resource_id, generation);
      return;
    }
    const resourceId = state.currentResource.resource_id;
    const updated = await api(
      `/api/v1/resources/${kind}/${resourceId}`,
      {
        method: "PUT",
        body: JSON.stringify({
          expected_sha256: state.currentResource.sha256,
          content: document.querySelector("#resource-content").value,
        }),
      },
    );
    toast("资源已保存");
    if (
      !isCurrent(generation, kind === "rule" ? "rules" : "skills")
      || state.resourceKind !== kind
      || state.selectionGeneration !== selectionGeneration
      || state.currentResource?.resource_id !== resourceId
    ) return;
    state.currentResource = updated;
    state.resources = await api(`/api/v1/resources?kind=${kind}`);
    if (!isCurrent(generation, kind === "rule" ? "rules" : "skills")) return;
    drawResourceList();
    drawEditor();
  } catch (error) { toast(error.message, true); }
}

async function deleteResource() {
  const generation = state.generation;
  const kind = state.resourceKind;
  const selectionGeneration = state.selectionGeneration;
  const resource = state.currentResource;
  if (!window.confirm(`确认删除 ${resource.title}？`)) return;
  try {
    await api(
      `/api/v1/resources/${kind}/${resource.resource_id}?expected_sha256=${resource.sha256}`,
      { method: "DELETE" },
    );
    toast("资源已删除");
    if (
      !isCurrent(generation, kind === "rule" ? "rules" : "skills")
      || state.selectionGeneration !== selectionGeneration
    ) return;
    await renderResources(kind, generation);
  } catch (error) { toast(error.message, true); }
}

async function renderOutputs(scope, generation) {
  const outputs = await pagedApi(`/api/v1/outputs?scope=${scope}`, scope, generation);
  if (outputs === null) return;
  const label = scope === "creation" ? "创作" : "学习";
  content.innerHTML = `
    <div class="section-heading">
      <div><h2>${label}产物 · ${outputs.length}</h2><p>${scope === "creation" ? "总体方案、资源包、剪辑规格、成片与检查报告" : "分析证据、参考报告、知识发布与检索数据"}</p></div>
    </div>
    <div class="output-grid">
      ${outputs.map((item) => `
        <button class="output-card" data-output-id="${item.output_id}">
          <div class="output-kind"><span>${escapeHtml(item.kind)}</span><span>${item.previewable ? "可预览" : "下载"}</span></div>
          <h3>${escapeHtml(item.name)}</h3>
          <p>${escapeHtml(item.relative_path)}</p>
          <footer><span>${formatBytes(item.size_bytes)}</span><span>${formatDate(item.modified_at)}</span></footer>
        </button>`).join("") || '<div class="empty-state"><div><h2>暂无产物</h2><p>完成对应工作流后会显示在这里。</p></div></div>'}
    </div>`;
  content.querySelectorAll("[data-output-id]").forEach((button) => {
    const item = outputs.find((entry) => entry.output_id === button.dataset.outputId);
    button.addEventListener("click", () => openOutput(item));
  });
}

async function renderArtifacts(generation) {
  const sources = await api("/api/v1/workflow/sources");
  if (!isCurrent(generation, "artifacts")) return;
  const pages = await Promise.all(
    sources.map((sourceId) => pagedApi(
      `/api/v1/workflow/artifacts?source_id=${encodeURIComponent(sourceId)}`,
      "artifacts",
      generation,
    )),
  );
  if (pages.some((page) => page === null)) return;
  const artifacts = pages.flat().sort((left, right) => (
    String(right.created_at).localeCompare(String(left.created_at))
  ));
  content.innerHTML = `
    <div class="section-heading"><div><h2>Artifacts · ${artifacts.length}</h2><p>主工作流与参考学习验证工作流的不可变产物。</p></div></div>
    <section class="panel table-panel"><table>
      <thead><tr><th>类型</th><th>来源</th><th>任务</th><th>状态</th><th>时间</th><th></th></tr></thead>
      <tbody>${artifacts.map((item) => `
        <tr><td>${escapeHtml(item.artifact_type)}</td><td>${escapeHtml(item.source_id)}</td><td><code>${escapeHtml(item.task_id)}</code></td><td>${escapeHtml(item.status)}</td><td>${formatDate(item.created_at)}</td><td><button class="table-link" data-artifact-id="${item.artifact_id}" data-source-id="${item.source_id}" data-artifact-type="${escapeHtml(item.artifact_type)}">查看</button></td></tr>`).join("")}</tbody>
    </table></section>`;
  content.querySelectorAll("[data-artifact-id]").forEach((button) => {
    button.addEventListener("click", () => openArtifact(button.dataset));
  });
}

async function openOutput(item) {
  previewTitle.textContent = item.name;
  const url = item.content_url;
  if (item.kind === "image") previewBody.innerHTML = `<img src="${url}" alt="${escapeHtml(item.name)}" />`;
  else if (item.kind === "video") previewBody.innerHTML = `<video controls autoplay src="${url}"></video>`;
  else if (item.kind === "audio") previewBody.innerHTML = `<audio controls autoplay src="${url}"></audio>`;
  else if (item.kind === "json" || item.kind === "text") {
    previewBody.innerHTML = '<div class="loading">读取内容…</div>';
    previewDialog.showModal();
    const response = await fetch(url);
    const text = await response.text();
    let display = text;
    if (item.kind === "json") {
      try { display = JSON.stringify(JSON.parse(text), null, 2); } catch { display = text; }
    }
    previewBody.innerHTML = `<pre>${escapeHtml(display)}</pre>`;
    return;
  } else previewBody.innerHTML = `<a class="download-link" href="${url}" download>下载 ${escapeHtml(item.name)}</a>`;
  previewDialog.showModal();
}

async function openArtifact(dataset) {
  const url = `/api/v1/workflow/${dataset.sourceId}/artifacts/${dataset.artifactId}/content`;
  previewTitle.textContent = dataset.artifactType;
  previewBody.innerHTML = '<div class="loading">读取 Artifact…</div>';
  previewDialog.showModal();
  try {
    const response = await fetch(url);
    const text = await response.text();
    let display = text;
    try { display = JSON.stringify(JSON.parse(text), null, 2); } catch { display = text; }
    previewBody.innerHTML = `<pre>${escapeHtml(display)}</pre>`;
  } catch (error) {
    previewBody.innerHTML = `<pre>${escapeHtml(error.message)}</pre>`;
  }
}

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("is-active"));
    button.classList.add("is-active");
    state.view = button.dataset.view;
    renderCurrentView();
  });
});
refreshButton.addEventListener("click", renderCurrentView);
document.querySelector("#preview-close").addEventListener("click", () => previewDialog.close());
previewDialog.addEventListener("close", () => { previewBody.innerHTML = ""; });

renderCurrentView();
