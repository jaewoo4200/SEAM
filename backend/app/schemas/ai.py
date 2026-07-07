"""AI material-suggestion contracts.

Rules (HANDOFF.md section 9):
- suggestions are structured JSON validated against these models; free-form
  AI output never mutates the scene;
- every suggestion carries confidence, evidence and provider provenance;
- applying a suggestion is an explicit user decision recorded in
  ai/suggestions.jsonl.
"""

from typing import Literal, Optional

from pydantic import Field

from .common import StrictModel, UnitFloat


class MaterialAlternative(StrictModel):
    rf_material_id: str
    confidence: UnitFloat


class MaterialSuggestion(StrictModel):
    prim_id: str
    recommended_rf_material_id: str
    confidence: UnitFloat
    evidence: list[str] = Field(default_factory=list)
    alternatives: list[MaterialAlternative] = Field(default_factory=list)
    needs_user_confirmation: bool = True


class EvidenceImage(StrictModel):
    """A persisted copy of an image the provider actually saw (reproducibility).

    ``asset_path`` is project-relative (ai/evidence/<batch>/<prim>.jpg) and
    servable via GET /projects/{id}/assets/{asset_path}.
    """

    prim_id: str
    asset_path: str


class MaterialSuggestionResponse(StrictModel):
    suggestions: list[MaterialSuggestion] = Field(default_factory=list)
    # Provider that actually produced the result: "rule_based",
    # "ollama_text", "ollama_vision", "disabled".
    provider: str
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    # Texture crops attached to this batch, persisted under ai/evidence/ so a
    # researcher can audit exactly what the VLM saw. None when no crops were
    # attached (text-only providers, feature off, or persistence failed).
    evidence_images: Optional[list[EvidenceImage]] = None


class SuggestMaterialsRequest(StrictModel):
    # None = every prim whose RF binding is unassigned.
    prim_ids: Optional[list[str]] = None
    # Force a specific provider ("rule_based", "ollama_text"); None = best available.
    provider: Optional[str] = None
    # Optional viewport capture (data:image/jpeg;base64,...) passed to
    # vision-capable providers as visual evidence. Never RF truth.
    screenshot_data_url: Optional[str] = None
    # Multiple camera angles of the SAME scene (Qualcomm multi-view). Capped at
    # 6. Back-compat: when only screenshot_data_url is set it is treated as a
    # one-item list.
    screenshot_data_urls: Optional[list[str]] = Field(default=None, max_length=6)
    # When true and the provider is multimodal-capable, attach per-prim texture
    # crops (extracted from the visual GLB) as extra evidence images.
    attach_texture_crops: bool = False


class SuggestionDecision(StrictModel):
    prim_id: str
    action: Literal["approve", "reject", "edit"]
    # For approve: optional (defaults to the suggested material).
    # For edit: required - the material the user chose instead.
    rf_material_id: Optional[str] = None


class ApplySuggestionsRequest(StrictModel):
    """User decisions on a previously returned suggestion batch."""

    decisions: list[SuggestionDecision] = Field(min_length=1)
    # Echo of the suggestion batch being decided on, for provenance logging.
    suggestions: list[MaterialSuggestion] = Field(default_factory=list)
    provider: str = "unknown"
    model: Optional[str] = None


class AIProviderStatus(StrictModel):
    name: str
    available: bool
    model: Optional[str] = None
    detail: str = ""


class AssignmentRule(StrictModel):
    """One name-match -> RF material rule (SEAM spec style).

    A rule fires when any of ``match_name_contains`` appears (case-insensitively)
    in a prim's evidence (name / mesh_name / semantic tags / visual material
    name). ``rf_material_id`` must exist in the project library - unknown ids
    are dropped at generation time, same anti-hallucination stance as the
    suggestion parser.
    """

    id: str = Field(pattern=r"^[a-z0-9_\-]+$")
    match_name_contains: list[str] = Field(min_length=1)
    rf_material_id: str
    note: Optional[str] = None


class RuleGenerationRequest(StrictModel):
    instruction: str = Field(min_length=1)


class RuleGenerationResponse(StrictModel):
    rules: list[AssignmentRule] = Field(default_factory=list)
    provider: str
    model: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


class ApplyRulesRequest(StrictModel):
    rules: list[AssignmentRule] = Field(min_length=1)


class ExplainValidationResponse(StrictModel):
    explanation: str
    provider: str
    model: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
