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


class MaterialSuggestionResponse(StrictModel):
    suggestions: list[MaterialSuggestion] = Field(default_factory=list)
    # Provider that actually produced the result: "rule_based",
    # "ollama_text", "ollama_vision", "disabled".
    provider: str
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


class SuggestMaterialsRequest(StrictModel):
    # None = every prim whose RF binding is unassigned.
    prim_ids: Optional[list[str]] = None
    # Force a specific provider ("rule_based", "ollama_text"); None = best available.
    provider: Optional[str] = None
    # Optional viewport capture (data:image/jpeg;base64,...) passed to
    # vision-capable providers as visual evidence. Never RF truth.
    screenshot_data_url: Optional[str] = None


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
