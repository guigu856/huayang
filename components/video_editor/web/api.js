export class ApiError extends Error {
  constructor(code, message, details = null, status = 0) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.details = details;
    this.status = status;
  }
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new ApiError("invalid_response", "服务返回了无效响应", null, response.status);
  }
  if (!response.ok || payload.ok === false) {
    const error = payload.error ?? {};
    throw new ApiError(
      error.code ?? "request_failed",
      error.message ?? `请求失败（${response.status}）`,
      error.details ?? null,
      response.status,
    );
  }
  return payload.data;
}

function jsonOptions(method, body) {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export const editorApi = {
  health() {
    return request("/api/v1/health");
  },

  listProjects() {
    return request("/api/v1/projects");
  },

  createProject(name) {
    return request("/api/v1/projects", jsonOptions("POST", { name }));
  },

  getProject(projectId) {
    return request(`/api/v1/projects/${encodeURIComponent(projectId)}`);
  },

  applyCommands(projectId, expectedRevision, commands) {
    return request(
      `/api/v1/projects/${encodeURIComponent(projectId)}/commands`,
      jsonOptions("POST", {
        expected_revision: expectedRevision,
        commands,
      }),
    );
  },

  async uploadAsset(projectId, expectedRevision, file) {
    const data = new FormData();
    data.append("expected_revision", String(expectedRevision));
    data.append("file", file);
    return request(`/api/v1/projects/${encodeURIComponent(projectId)}/assets`, {
      method: "POST",
      body: data,
    });
  },

  assetUrl(projectId, assetId) {
    return `/api/v1/projects/${encodeURIComponent(projectId)}/assets/${encodeURIComponent(assetId)}/content`;
  },

  createRender(projectId, expectedRevision) {
    return request(
      `/api/v1/projects/${encodeURIComponent(projectId)}/renders`,
      jsonOptions("POST", { expected_revision: expectedRevision }),
    );
  },

  getRender(renderId) {
    return request(`/api/v1/render-jobs/${encodeURIComponent(renderId)}`);
  },

  cancelRender(renderId) {
    return request(`/api/v1/render-jobs/${encodeURIComponent(renderId)}`, {
      method: "DELETE",
    });
  },

  renderDownloadUrl(renderId) {
    return `/api/v1/render-jobs/${encodeURIComponent(renderId)}/output`;
  },
};
