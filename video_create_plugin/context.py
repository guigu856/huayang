from __future__ import annotations

import os
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from .errors import PluginError
from .models import StagePolicy, TaskType


@dataclass(frozen=True, slots=True)
class ContextResource:
    resource_id: str
    kind: str
    uri: str
    title: str
    description: str
    content: str
    relative_path: str
    builtin: bool


@dataclass(frozen=True, slots=True)
class StageBundle:
    task_type: TaskType
    stage: str
    rule_ids: tuple[str, ...]
    skill_ids: tuple[str, ...]
    schema_ids: tuple[str, ...]
    tool_ids: tuple[str, ...]
    confirmation_required: bool


@dataclass(frozen=True, slots=True)
class _ResourceRegistration:
    resource_id: str
    kind: str
    relative_path: str
    title: str
    description: str
    builtin: bool = True


_RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


_REGISTRATIONS = (
    _ResourceRegistration(
        "main-agent",
        "rule",
        "rules/main-agent.md",
        "主 Agent 规则",
        "任务分类、阶段路由、确认与职责边界",
    ),
    _ResourceRegistration(
        "evidence-boundary",
        "rule",
        "rules/common/evidence-boundary.md",
        "证据边界",
        "事实、候选、推断和重建建议的分级",
    ),
    _ResourceRegistration(
        "stage-boundary",
        "rule",
        "rules/common/stage-boundary.md",
        "阶段边界",
        "冻结产物和单向具体化约束",
    ),
    _ResourceRegistration(
        "reference-study-agent",
        "rule",
        "rules/reference-study/orchestrator.md",
        "参考学习角色",
        "参考源、证据、语义分析、报告和发布编排",
    ),
    _ResourceRegistration(
        "reference-visual-agent",
        "rule",
        "rules/reference-study/visual-agent.md",
        "画面分析角色",
        "镜头、图层、运动和效果作用域判断",
    ),
    _ResourceRegistration(
        "reference-bgm-agent",
        "rule",
        "rules/reference-study/bgm-agent.md",
        "BGM 分析角色",
        "音乐结构、节奏层、能量与声音事件判断",
    ),
    _ResourceRegistration(
        "audiovisual-agent",
        "rule",
        "rules/reference-study/audiovisual-agent.md",
        "音画关系角色",
        "画面动作与音乐事件时序关系判断",
    ),
    _ResourceRegistration(
        "editing-grammar-agent",
        "rule",
        "rules/reference-study/editing-grammar-agent.md",
        "剪辑规律角色",
        "剪辑句子与可迁移机制归纳",
    ),
    _ResourceRegistration(
        "creative-direction-agent",
        "rule",
        "rules/creation/creative-direction-agent.md",
        "总体方案角色",
        "创作方向、制作方法与观看体验设计",
    ),
    _ResourceRegistration(
        "resource-preparation-agent",
        "rule",
        "rules/creation/resource-preparation-agent.md",
        "素材与 BGM 角色",
        "冻结资源包及溯源信息",
    ),
    _ResourceRegistration(
        "editing-spec-agent",
        "rule",
        "rules/creation/editing-spec-agent.md",
        "镜头规划角色",
        "人类表格与 ActionSpec 的精确设计",
    ),
    _ResourceRegistration(
        "execution-agent",
        "rule",
        "rules/creation/execution-agent.md",
        "执行工程角色",
        "能力预检、确定性编译、渲染和检查",
    ),
    _ResourceRegistration(
        "video-task-router",
        "skill",
        "skills/video-task-router/SKILL.md",
        "视频任务路由",
        "从自然语言识别三类视频任务",
    ),
    _ResourceRegistration(
        "reference-study",
        "skill",
        "skills/reference-study/SKILL.md",
        "参考视频学习",
        "参考视频学习闭环及工作单元顺序",
    ),
    _ResourceRegistration(
        "reference-visual-analysis",
        "skill",
        "skills/reference-visual-analysis/SKILL.md",
        "参考画面分析",
        "按真实时间线分析主镜头、镜内事件和图层",
    ),
    _ResourceRegistration(
        "reference-bgm-analysis",
        "skill",
        "skills/reference-bgm-analysis/SKILL.md",
        "参考 BGM 分析",
        "分析音乐段落、节拍、声音层和能量",
    ),
    _ResourceRegistration(
        "audiovisual-relation-analysis",
        "skill",
        "skills/audiovisual-relation-analysis/SKILL.md",
        "音画关系分析",
        "分析同步、提前、延后和非节拍绑定",
    ),
    _ResourceRegistration(
        "editing-grammar-synthesis",
        "skill",
        "skills/editing-grammar-synthesis/SKILL.md",
        "剪辑规律归纳",
        "从镜头组归纳可迁移剪辑句子",
    ),
    _ResourceRegistration(
        "creative-direction",
        "skill",
        "skills/creative-direction/SKILL.md",
        "视频总体方案",
        "阶段一总体方案的七个语义板块",
    ),
    _ResourceRegistration(
        "material-preparation",
        "skill",
        "skills/material-preparation/SKILL.md",
        "素材筹备",
        "素材发现、选择、预处理和溯源",
    ),
    _ResourceRegistration(
        "bgm-preparation",
        "skill",
        "skills/bgm-preparation/SKILL.md",
        "BGM 筹备",
        "短视频 BGM 选择、授权和结构分析",
    ),
    _ResourceRegistration(
        "editing-specification",
        "skill",
        "skills/editing-specification/SKILL.md",
        "可执行剪辑规格",
        "冻结资源范围内的逐镜表和 ActionSpec",
    ),
    _ResourceRegistration(
        "execution-project-authoring",
        "skill",
        "skills/execution-project-authoring/SKILL.md",
        "执行工程编译",
        "能力预检、工程编译与 TraceMap",
    ),
    _ResourceRegistration(
        "render-inspection",
        "skill",
        "skills/render-inspection/SKILL.md",
        "成片检查",
        "画面、声音、规划覆盖和观看体验检查",
    ),
    _ResourceRegistration(
        "reference-study",
        "schema",
        "schemas/reference-study.schema.json",
        "参考学习产物 Schema",
        "参考学习主 Manifest 的结构合同",
    ),
    _ResourceRegistration(
        "creative-direction",
        "schema",
        "schemas/creative-direction.schema.json",
        "总体方案 Schema",
        "阶段一七板块 Manifest 合同",
    ),
    _ResourceRegistration(
        "resource-preparation",
        "schema",
        "schemas/resource-preparation.schema.json",
        "资源包 Schema",
        "素材与 BGM 汇总 Manifest 合同",
    ),
    _ResourceRegistration(
        "editing-specification",
        "schema",
        "schemas/editing-specification.schema.json",
        "剪辑规格 Schema",
        "逐镜表和 ActionSpec 合同",
    ),
    _ResourceRegistration(
        "execution",
        "schema",
        "schemas/execution.schema.json",
        "执行产物 Schema",
        "能力评估、工程、渲染和检查合同",
    ),
    _ResourceRegistration(
        "knowledge-publication",
        "schema",
        "schemas/knowledge-publication.schema.json",
        "知识发布 Schema",
        "知识发布版本与索引元数据合同",
    ),
)

_STAGE_RESOURCES: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    "reference_study": (
        ("evidence-boundary", "stage-boundary", "reference-study-agent"),
        ("reference-study",),
        "reference-study",
    ),
    "knowledge_publication": (
        ("evidence-boundary", "stage-boundary", "reference-study-agent"),
        ("editing-grammar-synthesis",),
        "knowledge-publication",
    ),
    "creative_direction": (
        ("stage-boundary", "creative-direction-agent"),
        ("creative-direction",),
        "creative-direction",
    ),
    "resource_preparation": (
        ("stage-boundary", "resource-preparation-agent"),
        ("material-preparation", "bgm-preparation"),
        "resource-preparation",
    ),
    "editing_specification": (
        ("stage-boundary", "editing-spec-agent"),
        ("editing-specification",),
        "editing-specification",
    ),
    "execution": (
        ("stage-boundary", "execution-agent"),
        ("execution-project-authoring", "render-inspection"),
        "execution",
    ),
}

_STAGE_TOOLS: dict[str, tuple[str, ...]] = {
    "reference_study": (
        "workflow_submit_artifact",
        "workflow_record_approval",
        "workflow_reopen_stage",
        "video_download",
        "reference_resolve_source",
        "media_probe",
        "analysis_start",
        "analysis_get_job",
        "analysis_refine_intervals",
        "analysis_validate_artifact",
        "report_generate",
    ),
    "knowledge_publication": (
        "workflow_submit_artifact",
        "workflow_reopen_stage",
        "knowledge_preview_publication",
        "knowledge_publish",
        "knowledge_search",
    ),
    "creative_direction": (
        "workflow_submit_artifact",
        "workflow_record_approval",
        "workflow_reopen_stage",
        "reference_get_creation_context",
        "knowledge_search",
    ),
    "resource_preparation": (
        "workflow_submit_artifact",
        "workflow_record_approval",
        "workflow_reopen_stage",
        "reference_get_creation_context",
        "knowledge_search",
        "materials_list_sources",
        "materials_search",
        "materials_acquire",
        "images_list_sources",
        "images_search",
        "images_acquire",
        "media_preprocess",
        "bgm_list_sources",
        "bgm_search",
        "bgm_acquire",
    ),
    "editing_specification": (
        "workflow_submit_artifact",
        "workflow_record_approval",
        "workflow_reopen_stage",
        "reference_get_creation_context",
        "knowledge_search",
    ),
    "execution": (
        "workflow_submit_artifact",
        "workflow_reopen_stage",
        "editor_create_project",
        "editor_import_asset",
        "editor_apply_commands",
        "editor_preflight_spec",
        "editor_compile_spec",
        "editor_validate_execution_project",
        "editor_submit_render",
        "editor_get_render",
        "editor_inspect_render",
    ),
}


class ContextCatalog:
    """读取内置及后台新增的版本化 rule、skill 与 Schema。"""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = (
            Path(root).expanduser().resolve() if root is not None else _default_resource_root()
        )

    def catalog(self) -> list[ContextResource]:
        resources: list[ContextResource] = []
        for uri, registration in sorted(self._registrations().items()):
            try:
                resources.append(self._read_registration(uri, registration))
            except PluginError as error:
                if not registration.builtin and error.code == "context_resource_unavailable":
                    continue
                raise
        return resources

    def read(self, uri: str) -> ContextResource:
        registration = self._registrations().get(uri)
        if registration is None:
            raise PluginError("context_resource_not_found", "上下文资源未注册")
        return self._read_registration(uri, registration)

    def _read_registration(
        self,
        uri: str,
        registration: _ResourceRegistration,
    ) -> ContextResource:
        path = self._path_for(registration)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as error:
            raise PluginError("context_resource_unavailable", "上下文资源读取失败") from error
        return ContextResource(
            resource_id=registration.resource_id,
            kind=registration.kind,
            uri=uri,
            title=registration.title,
            description=registration.description,
            content=content,
            relative_path=registration.relative_path,
            builtin=registration.builtin,
        )

    def resource_path(self, kind: str, resource_id: str) -> Path:
        registration = self._registrations().get(self._uri(kind, resource_id))
        if registration is None:
            raise PluginError("context_resource_not_found", "上下文资源未注册")
        return self._path_for(registration)

    def _registrations(self) -> dict[str, _ResourceRegistration]:
        registrations = {
            self._uri(registration.kind, registration.resource_id): registration
            for registration in _REGISTRATIONS
        }
        registered_paths = {registration.relative_path for registration in _REGISTRATIONS}
        candidates = [
            *(self.root / "rules").glob("**/*.md"),
            *(self.root / "skills").glob("*/SKILL.md"),
        ]
        for path in sorted(candidates):
            relative_path = path.relative_to(self.root).as_posix()
            if relative_path in registered_paths:
                continue
            kind, resource_id = _discovered_identity(path, self.root)
            if resource_id is None:
                continue
            uri = self._uri(kind, resource_id)
            if uri in registrations:
                continue
            title, description = _markdown_metadata(path, resource_id)
            registrations[uri] = _ResourceRegistration(
                resource_id=resource_id,
                kind=kind,
                relative_path=relative_path,
                title=title,
                description=description,
                builtin=False,
            )
        return registrations

    def _path_for(self, registration: _ResourceRegistration) -> Path:
        path = (self.root / registration.relative_path).resolve()
        if not path.is_relative_to(self.root):
            raise PluginError("context_resource_not_found", "上下文资源路径无效")
        return path

    def rule(self, resource_id: str) -> ContextResource:
        return self.read(self._uri("rule", resource_id))

    def skill(self, resource_id: str) -> ContextResource:
        return self.read(self._uri("skill", resource_id))

    def schema(self, resource_id: str) -> ContextResource:
        return self.read(self._uri("schema", resource_id))

    def bootstrap_bundle(self) -> tuple[ContextResource, ContextResource]:
        return self.rule("main-agent"), self.skill("video-task-router")

    def stage_bundle(self, task_type: TaskType, stage: str) -> StageBundle:
        if stage not in _STAGE_RESOURCES:
            raise PluginError("stage_not_allowed", "阶段上下文未注册")
        if stage not in _stages_for_task(task_type):
            raise PluginError("stage_not_allowed", "阶段不属于当前任务类型")
        rule_ids, skill_ids, schema_id = _STAGE_RESOURCES[stage]
        tool_ids = _STAGE_TOOLS[stage]
        if task_type != "reference_guided_creation":
            tool_ids = tuple(
                tool_id for tool_id in tool_ids if tool_id != "reference_get_creation_context"
            )
        return StageBundle(
            task_type=task_type,
            stage=stage,
            rule_ids=rule_ids,
            skill_ids=skill_ids,
            schema_ids=(schema_id,),
            tool_ids=tool_ids,
            confirmation_required=stage
            in {
                "reference_study",
                "creative_direction",
                "resource_preparation",
                "editing_specification",
            },
        )

    def policy(self, task_type: TaskType, stage: str) -> StagePolicy:
        bundle = self.stage_bundle(task_type, stage)
        role_id = bundle.rule_ids[-1]
        return StagePolicy(
            role_resource=self._uri("rule", role_id),
            skill_resources=[self._uri("skill", item) for item in bundle.skill_ids],
            allowed_tools=list(bundle.tool_ids),
            output_contract=self._uri("schema", bundle.schema_ids[0]),
            confirmation_required=bundle.confirmation_required,
        )

    @staticmethod
    def _uri(kind: str, resource_id: str) -> str:
        namespace = {"rule": "rules", "skill": "skills", "schema": "schemas"}[kind]
        return f"huayang://{namespace}/{resource_id}"


def _discovered_identity(path: Path, root: Path) -> tuple[str, str | None]:
    relative = path.relative_to(root)
    if relative.parts[0] == "skills":
        resource_id = path.parent.name
        return "skill", resource_id if _RESOURCE_ID.fullmatch(resource_id) else None
    rule_parts = relative.with_suffix("").parts[1:]
    if rule_parts and rule_parts[0] == "custom":
        rule_parts = rule_parts[1:]
    resource_id = "-".join(rule_parts)
    return "rule", resource_id if _RESOURCE_ID.fullmatch(resource_id) else None


def _markdown_metadata(path: Path, resource_id: str) -> tuple[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return resource_id, "后台新增上下文资源"
    title = next(
        (line.lstrip("#").strip() for line in lines if line.startswith("# ")),
        resource_id,
    )
    description = next(
        (
            line.strip()
            for line in lines
            if line.strip()
            and not line.startswith("#")
            and line.strip() != "---"
            and not re.match(r"^(name|description):", line.strip())
        ),
        "后台新增上下文资源",
    )
    return title, description[:180]


def _stages_for_task(task_type: TaskType) -> tuple[str, ...]:
    if task_type == "reference_study":
        return ("reference_study", "knowledge_publication")
    if task_type == "original_creation":
        return (
            "creative_direction",
            "resource_preparation",
            "editing_specification",
            "execution",
        )
    return (
        "reference_study",
        "creative_direction",
        "resource_preparation",
        "editing_specification",
        "execution",
    )


def _default_resource_root() -> Path:
    configured_root = os.environ.get("HUAYANG_RESOURCE_ROOT")
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    source_root = Path(__file__).resolve().parents[1]
    if (source_root / "rules/main-agent.md").is_file():
        return source_root

    try:
        installed_distribution = distribution("huayang")
    except PackageNotFoundError:
        return source_root

    marker_suffix = "share/huayang/rules/main-agent.md"
    for entry in installed_distribution.files or ():
        if entry.as_posix().endswith(marker_suffix):
            marker = Path(str(installed_distribution.locate_file(entry))).resolve()
            return marker.parents[1]
    return source_root
