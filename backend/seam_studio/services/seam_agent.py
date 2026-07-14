"""SEAM-Agent: retrieval-augmented local LLM/VLM RF material authoring.

Turns ONE building-level prim into segment-level RF material candidates:

    multi-view mesh renders (RGB + triangle-id buffers, captured by the FE)
    + optional user site/building hint
    + optional web/image evidence (DuckDuckGo, local-first & opt-in)
    + local VLM region analysis (LM Studio / OpenAI-compatible)
    -> per-view region boxes -> triangle-id back-projection -> face votes
    -> low-confidence refinement (zoomed re-queries)
    -> connected face groups -> RF material candidates with confidence,
       per-segment render previews, evidence cards and an activity trace
    -> user review -> physical split via the segmentation bake machinery.

Design principles (SEAM_Agent_Material_Assignment_Handoff.md):
- the LLM/VLM plans and interprets EVIDENCE; deterministic code does all
  mesh inspection, back-projection, grouping and export;
- bounded loop with explicit budgets (searches, VLM calls, refinement calls,
  runtime) and cooperative cancellation between tool calls;
- an activity trace instead of raw chain-of-thought, persisted to disk after
  every step so it survives a backend restart;
- web images are WEAK priors with provenance, never truth;
- everything degrades: no web -> renders only; no VLM -> error with a
  clear trace step, nothing half-applied.
"""

from __future__ import annotations

import base64
import io
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..schemas.scene import MeshRef, Prim, RFBinding, Scene, VisualBinding
from . import material_segmentation as seg

# ------------------------------------------------------------------ labels

# Canonical semantic label -> (primary rf material, alternatives). Wall/roof
# variants are split by material family so brick/metal evidence can win the
# primary slot instead of hiding in "alternatives"; vegetation is split into
# canopy/trunk/grass so trees are first-class citizens of the vocabulary.
SEMANTIC_TO_RF: dict[str, tuple[str, list[str]]] = {
    "concrete_wall": ("itu_concrete", ["itu_brick"]),
    "brick_wall": ("itu_brick", ["itu_concrete"]),
    "glass_window": ("itu_glass", []),
    "curtain_wall_glass": ("itu_glass", []),
    "metal_panel": ("metal", []),
    "metal_frame": ("metal", []),
    "roof_concrete": ("itu_concrete", ["metal"]),
    "roof_metal": ("metal", ["itu_concrete"]),
    "door": ("itu_wood", ["itu_glass"]),
    "tree_canopy": ("vegetation_custom", []),
    "tree_trunk": ("itu_wood", []),
    "grass": ("vegetation_custom", ["ground_28ghz"]),
    "ground": ("ground_28ghz", ["asphalt_custom"]),
    "unknown": ("unknown_rf", []),
}
_LABELS = list(SEMANTIC_TO_RF)

# Legacy / free-form labels the VLM may still emit -> canonical vocabulary.
_LABEL_ALIASES: dict[str, str] = {
    "exterior_wall": "concrete_wall",
    "wall": "concrete_wall",
    "concrete": "concrete_wall",
    "brick": "brick_wall",
    "roof": "roof_concrete",
    "window": "glass_window",
    "glass": "glass_window",
    "metal": "metal_panel",
    "vegetation": "tree_canopy",
    "tree": "tree_canopy",
    "foliage": "tree_canopy",
    "bush": "tree_canopy",
    "lawn": "grass",
    "road": "ground",
    "asphalt": "ground",
}

# Web-evidence "dominant material" strings -> labels whose votes get a mild
# multiplicative boost. Weak prior by design: it breaks ties, never overrides
# a clear visual majority.
_EVIDENCE_BOOST: dict[str, list[str]] = {
    "concrete": ["concrete_wall", "roof_concrete"],
    "brick": ["brick_wall"],
    "glass": ["glass_window", "curtain_wall_glass"],
    "metal": ["metal_panel", "metal_frame", "roof_metal"],
    "steel": ["metal_panel", "metal_frame", "roof_metal"],
    "wood": ["door", "tree_trunk"],
    "vegetation": ["tree_canopy", "grass"],
}
_EVIDENCE_BOOST_FACTOR = 1.15

# Per-label tint for the segment preview crops (RGB 0-255).
_LABEL_COLORS: dict[str, tuple[int, int, int]] = {
    "concrete_wall": (156, 163, 175),
    "brick_wall": (192, 98, 65),
    "glass_window": (56, 152, 255),
    "curtain_wall_glass": (56, 152, 255),
    "metal_panel": (217, 119, 6),
    "metal_frame": (217, 119, 6),
    "roof_concrete": (120, 130, 150),
    "roof_metal": (234, 155, 40),
    "door": (146, 104, 62),
    "tree_canopy": (52, 168, 83),
    "tree_trunk": (110, 84, 50),
    "grass": (130, 190, 90),
    "ground": (176, 131, 62),
    "unknown": (120, 120, 130),
}


def _canonical_label(raw: str) -> str:
    label = str(raw).strip().lower().replace(" ", "_")
    if label in SEMANTIC_TO_RF:
        return label
    return _LABEL_ALIASES.get(label, "unknown")


@dataclass
class AgentBudget:
    max_web_searches: int = 6
    max_image_searches: int = 4
    max_images: int = 6
    max_vlm_calls: int = 40
    max_refine_calls: int = 3
    max_runtime_sec: int = 600


# Canonical pipeline stages for the progress readout. Stages that do not
# apply to a run (e.g. retrieve with allow_web off) are skipped.
_STAGES = ["inspect", "decode", "retrieve", "views", "refine", "aggregate", "segments"]


@dataclass
class AgentJob:
    job_id: str
    status: str = "running"  # running | needs_review | done | error | cancelled
    detail: str = ""
    steps: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    segments: Optional[list[dict]] = None
    # face labels backing the segments, for apply (segment_id -> face indices)
    face_groups: dict[str, list[int]] = field(default_factory=dict)
    prim_id: str = ""
    started_at: float = field(default_factory=time.time)
    # Live progress for the FE bar: stage, counters, elapsed/ETA seconds.
    progress: dict = field(default_factory=dict)
    # Cooperative cancellation: checked between tool calls, never mid-write.
    cancel_requested: bool = False
    # Saved per-view renders (view_id -> project-relative asset path).
    view_assets: list[dict] = field(default_factory=list)
    project_dir: Optional[Path] = None
    out_dir: Optional[Path] = None
    from_disk: bool = False


class AgentBusyError(RuntimeError):
    """Another agent job is already running (per-project or global cap)."""


class AgentCancelled(Exception):
    """Raised inside the pipeline when the user requested cancellation."""


_jobs: dict[str, AgentJob] = {}
_lock = threading.Lock()
# Heavy VLM/web jobs: one per project, at most two total across projects.
_MAX_CONCURRENT_JOBS = 2


def get_job(job_id: str) -> Optional[AgentJob]:
    with _lock:
        return _jobs.get(job_id)


def cancel_job(job_id: str) -> bool:
    """Request cooperative cancellation; returns False for unknown/settled."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.status not in ("running",):
            return False
        job.cancel_requested = True
        return True


def load_job_from_disk(project_dir: Path, job_id: str) -> Optional[AgentJob]:
    """Rehydrate a finished job (trace + face labels) after a backend restart.

    The rebuilt job is read-only review state: trace/evidence/segments come
    from trace.json and face_groups are rebuilt from face_labels.npz so that
    apply still works. Running jobs are memory-only by design.
    """
    out_dir = project_dir / "ai" / "agent" / job_id
    trace_path = out_dir / "trace.json"
    if not trace_path.exists():
        return None
    try:
        data = json.loads(trace_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    segments = data.get("segments")
    job = AgentJob(
        job_id=job_id,
        # v1 traces have no status field; a trace with segments was reviewable.
        status=data.get("status") or ("needs_review" if segments else "error"),
        detail=data.get("detail", ""),
        steps=data.get("steps", []),
        evidence=data.get("evidence", []),
        segments=segments,
        prim_id=data.get("prim_id", ""),
        progress=data.get("progress", {}),
        view_assets=data.get("views", []),
        project_dir=project_dir,
        out_dir=out_dir,
        from_disk=True,
    )
    # Rebuild segment -> face indices from the persisted per-face labels.
    npz_path = out_dir / "face_labels.npz"
    if segments and npz_path.exists():
        try:
            import numpy as np

            with np.load(npz_path, allow_pickle=False) as npz:
                labels = npz["labels"]
                classes = [str(c) for c in npz["classes"]]
            by_label = {s.get("semantic_label"): s.get("segment_id") for s in segments}
            for li, cls in enumerate(classes):
                sid = by_label.get(cls)
                if sid is None:
                    continue
                faces = np.nonzero(labels == li)[0]
                if faces.size:
                    job.face_groups[sid] = [int(f) for f in faces]
        except Exception:
            pass  # trace stays viewable even if apply data is unavailable
    return job


def resolve_job(project_dir: Path, job_id: str) -> Optional[AgentJob]:
    """Memory first (live jobs), then disk (finished jobs after a restart)."""
    job = get_job(job_id)
    if job is not None:
        return job
    job = load_job_from_disk(project_dir, job_id)
    if job is not None:
        with _lock:
            # Cache so repeated trace polls / apply reuse the same object.
            _jobs.setdefault(job_id, job)
    return job


def _step(job: AgentJob, step_id: str, summary: str, **extra) -> dict:
    """Append a running trace step (the user-visible 'thinking')."""
    entry = {"step_id": step_id, "status": "running", "summary": summary, **extra}
    with _lock:
        job.steps.append(entry)
    _persist_trace(job)
    return entry


def _finish(entry: dict, summary: Optional[str] = None, status: str = "done") -> None:
    entry["status"] = status
    if summary is not None:
        entry["summary"] = summary


def _check_cancel(job: AgentJob) -> None:
    if job.cancel_requested:
        raise AgentCancelled()


def _set_progress(job: AgentJob, stage: str, **counters) -> None:
    entry = {
        "stage": stage,
        "stage_index": _STAGES.index(stage) + 1 if stage in _STAGES else 0,
        "total_stages": len(_STAGES),
        "elapsed_sec": round(time.time() - job.started_at, 1),
    }
    entry.update(counters)
    with _lock:
        job.progress = entry


def _persist_trace(job: AgentJob) -> None:
    """Atomically write the current trace snapshot (crash/restart safe).

    Called after every step append and at settle time; the file is small, so
    rewriting it keeps the on-disk trace equal to what the FE last saw.
    """
    out_dir = job.out_dir
    if out_dir is None:
        return
    payload = {
        "job_id": job.job_id,
        "prim_id": job.prim_id,
        "status": job.status,
        "detail": job.detail,
        "progress": job.progress,
        "steps": job.steps,
        "evidence": job.evidence,
        "segments": job.segments,
        "views": job.view_assets,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        tmp = out_dir / "trace.json.tmp"
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, out_dir / "trace.json")
    except OSError:
        pass  # persistence is best-effort; the live job keeps running


# ------------------------------------------------------------------ tools


def _tool_web_search(query: str, max_results: int = 5) -> list[dict]:
    from ddgs import DDGS

    with DDGS() as d:
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": (r.get("body") or "")[:300],
            }
            for r in d.text(query, max_results=max_results)
        ]


def _tool_image_search(query: str, max_results: int = 6) -> list[dict]:
    from ddgs import DDGS

    with DDGS() as d:
        return [
            {
                "title": r.get("title", ""),
                "image_url": r.get("image", ""),
                "page_url": r.get("url", ""),
            }
            for r in d.images(query, max_results=max_results)
        ]


def _download_thumb(url: str, max_px: int = 384) -> Optional[bytes]:
    """Fetch an image and return a JPEG thumbnail, or None on any failure."""
    import httpx
    from PIL import Image

    try:
        resp = httpx.get(
            url,
            timeout=10,
            follow_redirects=True,
            headers={"User-Agent": "SEAM-Studio/0.1 (research; local tool)"},
        )
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=80)
        return buf.getvalue()
    except Exception:
        return None


def _vlm_chat(
    prompt: str,
    images_data_urls: list[str],
    model: Optional[str],
    max_tokens: int = 1500,
) -> str:
    """One LM Studio chat round-trip with images; raises on transport errors."""
    import httpx

    from seam_studio.core.config import get_settings

    settings = get_settings().ai
    content: list[dict] = [{"type": "text", "text": prompt}]
    for url in images_data_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    resp = httpx.post(
        f"{settings.openai_url}/chat/completions",
        json={
            "model": model or settings.openai_model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
        },
        timeout=settings.vision_timeout_s,
    )
    resp.raise_for_status()
    msg = resp.json()["choices"][0]["message"]
    return msg.get("content") or msg.get("reasoning_content") or ""


def _extract_json(text: str) -> Optional[object]:
    """Best-effort first JSON object/array in a model answer."""
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == opener:
                    depth += 1
                elif text[i] == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start : i + 1])
                        except json.JSONDecodeError:
                            break
            start = text.find(opener, start + 1)
    return None


# ------------------------------------------------------------------ prompts

_LABEL_LIST = ", ".join(l for l in _LABELS if l != "unknown")

_REGION_PROMPT = """You are labeling ONE rendered view of a single building mesh for RF (radio)
simulation. {hint_line}{evidence_line}
Identify the RF-relevant regions VISIBLE in the render and output ONLY JSON:
{{"regions": [{{"label": "<one of: """ + _LABEL_LIST + """, unknown>",
  "bbox": [x0, y0, x1, y1],  // fractions of image width/height, 0.0-1.0
  "confidence": 0.0-1.0,
  "reason": "short phrase"}}]}}
Rules: boxes may overlap; cover the large facade/roof areas first; pick the
wall/roof variant by APPEARANCE (brick_wall for visible brick, roof_metal for
metallic sheeting); use tree_canopy / tree_trunk / grass for vegetation; use
"unknown" when unsure; 2-8 regions; no prose outside the JSON."""

_EVIDENCE_PROMPT = """These photos were retrieved from the web for the site/building: "{hint}".
They are WEAK evidence (may show a different building). Summarize, as JSON only:
{{"claims": [{{"claim": "short factual statement about visible construction materials
 (e.g. 'facade is mostly blue glass curtain wall', 'roof appears metal')",
  "confidence": 0.0-1.0}}], "dominant_materials": ["concrete"|"glass"|"metal"|"brick"|"wood"|"vegetation"|...]}}"""

_REFINE_PROMPT = """This is a ZOOMED CROP of one region of a rendered building mesh. The region
was provisionally labeled "{label}" with LOW confidence. {hint_line}
Look closely at the crop and pick the single best label. Output ONLY JSON:
{{"label": "<one of: """ + _LABEL_LIST + """, unknown>",
  "confidence": 0.0-1.0, "reason": "short phrase"}}"""


# ------------------------------------------------------------------ pipeline


def start_job(
    project_dir: Path,
    scene: Scene,
    prim_id: str,
    views: list[dict],
    user_hint: Optional[str],
    allow_web: bool,
    model: Optional[str],
    budget: AgentBudget,
) -> str:
    """Spawn the bounded agent loop in a thread; returns the job id.

    Raises AgentBusyError when this project already has a live job or the
    global concurrency cap is reached — multi-building runs are sequential
    by design (the FE queues buildings one after another).
    """
    job_id = uuid.uuid4().hex[:12]
    job = AgentJob(job_id=job_id, prim_id=prim_id, project_dir=project_dir)
    with _lock:
        for other in _jobs.values():
            if other.status == "running" and other.project_dir == project_dir:
                raise AgentBusyError(
                    f"agent job {other.job_id} is already running for this project; "
                    "cancel it or wait for it to finish"
                )
        if sum(1 for j in _jobs.values() if j.status == "running") >= _MAX_CONCURRENT_JOBS:
            raise AgentBusyError(
                f"agent concurrency limit ({_MAX_CONCURRENT_JOBS}) reached; retry shortly"
            )
        _jobs[job_id] = job

    def run() -> None:
        try:
            _run_pipeline(
                job, project_dir, scene, prim_id, views, user_hint, allow_web,
                model, budget,
            )
        except AgentCancelled:
            with _lock:
                job.status = "cancelled"
                job.detail = "cancelled by user"
            _step(job, "cancelled", "Job cancelled by user")["status"] = "done"
            _persist_trace(job)
        except Exception as exc:  # surface, never crash the server
            with _lock:
                job.status = "error"
                job.detail = str(exc)[:500]
            _persist_trace(job)

    threading.Thread(target=run, daemon=True).start()
    return job_id


def _run_pipeline(
    job: AgentJob,
    project_dir: Path,
    scene: Scene,
    prim_id: str,
    views: list[dict],
    user_hint: Optional[str],
    allow_web: bool,
    model: Optional[str],
    budget: AgentBudget,
) -> None:
    import numpy as np
    from PIL import Image

    out_dir = project_dir / "ai" / "agent" / job.job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    job.out_dir = out_dir
    deadline = job.started_at + budget.max_runtime_sec
    vlm_calls = 0

    # ---- step 1: inspect --------------------------------------------------
    _set_progress(job, "inspect", views_total=len(views), vlm_calls=0)
    st = _step(job, "inspect_mesh", "Inspecting mesh…")
    prim, geom = seg._resolve_prim_geometry(project_dir, scene, prim_id)
    n_faces = int(len(geom.faces))
    has_tex = bool(prim.visual and prim.visual.base_color_texture)
    _finish(
        st,
        f"{prim.name}: {n_faces:,} faces, texture {'present' if has_tex else 'missing'}, "
        f"{len(views)} rendered views received",
    )
    _check_cancel(job)

    # ---- step 2: decode triangle-id buffers + save view renders ------------
    _set_progress(job, "decode", views_total=len(views), vlm_calls=vlm_calls)
    st = _step(job, "decode_views", "Decoding triangle-id buffers…")
    decoded: list[dict] = []
    visibility = np.zeros(n_faces, dtype=np.int32)  # views a face is seen in
    for v in views:
        png_b64 = v["tri_id_png_data_url"].split("base64,", 1)[-1]
        img = Image.open(io.BytesIO(base64.b64decode(png_b64))).convert("RGB")
        arr = np.asarray(img, dtype=np.uint32)
        ids = (arr[..., 0] << 16) | (arr[..., 1] << 8) | arr[..., 2]
        ids[ids == 0xFFFFFF] = 0xFFFFFFFF  # background sentinel
        bad_mask = (ids != 0xFFFFFFFF) & (ids >= n_faces)
        n_bad = int(bad_mask.sum())
        if n_bad:
            # Antialiased/blended edge pixels decode to garbage ids; they must
            # be dropped BEFORE voting or they index past the vote table.
            ids[bad_mask] = 0xFFFFFFFF
            _step(
                job, f"warn_{v['view_id']}",
                f"{v['view_id']}: {n_bad} px decode out of range (dropped)",
            )["status"] = "done"
        seen = np.unique(ids[ids != 0xFFFFFFFF]).astype(np.int64)
        if seen.size:
            visibility[seen] += 1
        # Persist the RGB render so proposals can show WHERE a segment lives
        # (and so the trace stays reviewable after a restart).
        rgb_b64 = v["rgb_data_url"].split("base64,", 1)[-1]
        view_name = f"view_{v['view_id']}.jpg"
        try:
            (out_dir / view_name).write_bytes(base64.b64decode(rgb_b64))
            with _lock:
                job.view_assets.append(
                    {
                        "view_id": v["view_id"],
                        "asset_path": f"ai/agent/{job.job_id}/{view_name}",
                    }
                )
        except OSError:
            pass
        decoded.append({"view_id": v["view_id"], "ids": ids, "rgb": v["rgb_data_url"]})
    _finish(st, f"{len(decoded)} views decoded against {n_faces:,} faces")
    _check_cancel(job)

    # ---- step 3: optional retrieval ---------------------------------------
    evidence_urls: list[str] = []  # data URLs fed to the VLM
    web_claims: list[str] = []
    dominant_materials: list[str] = []
    if allow_web and user_hint:
        _set_progress(job, "retrieve", views_total=len(views), vlm_calls=vlm_calls)
        st = _step(job, "plan_retrieval", "Planning web evidence retrieval…")
        queries = [
            f"{user_hint} building exterior",
            f"{user_hint} facade",
        ]
        _finish(st, f"{len(queries)} queries planned", )
        st = _step(job, "web_search", "Searching the web…", queries=queries)
        found_pages = []
        for q in queries[: budget.max_web_searches]:
            if time.time() > deadline:
                break
            _check_cancel(job)
            try:
                found_pages += _tool_web_search(q, max_results=4)
            except Exception as exc:
                _step(job, "web_search_error", f"web search failed: {exc}")["status"] = "done"
                break
        _finish(st, f"{len(found_pages)} web results")
        for p in found_pages[:6]:
            with _lock:
                job.evidence.append(
                    {
                        "evidence_id": f"ev_page_{len(job.evidence):02d}",
                        "type": "web_page",
                        "claim": p["title"][:120],
                        "source_url": p["url"],
                        "query": user_hint,
                    }
                )

        st = _step(job, "image_search", "Searching for exterior photos…",
                   queries=[f"{user_hint} building"])
        images_meta: list[dict] = []
        try:
            images_meta = _tool_image_search(f"{user_hint} building", max_results=10)
        except Exception as exc:
            _finish(st, f"image search failed: {exc}", status="error")
        kept = 0
        for m in images_meta:
            if kept >= budget.max_images or time.time() > deadline:
                break
            _check_cancel(job)
            thumb = _download_thumb(m["image_url"])
            if thumb is None:
                continue
            kept += 1
            name = f"evidence_{kept:02d}.jpg"
            (out_dir / name).write_bytes(thumb)
            data_url = "data:image/jpeg;base64," + base64.b64encode(thumb).decode()
            evidence_urls.append(data_url)
            with _lock:
                job.evidence.append(
                    {
                        "evidence_id": f"ev_img_{kept:02d}",
                        "type": "web_image",
                        "claim": (m["title"] or "retrieved exterior photo")[:120],
                        "source_url": m["image_url"],
                        "page_url": m["page_url"],
                        "thumb_asset_path": f"ai/agent/{job.job_id}/{name}",
                        "query": user_hint,
                    }
                )
        if st["status"] == "running":
            _finish(st, f"{kept} usable photos retrieved (of {len(images_meta)} candidates)")

        # VLM summarizes the retrieved photos into material claims.
        if evidence_urls and vlm_calls < budget.max_vlm_calls:
            _check_cancel(job)
            st = _step(job, "analyze_evidence", "Reading retrieved photos with the VLM…")
            try:
                vlm_calls += 1
                answer = _vlm_chat(
                    _EVIDENCE_PROMPT.format(hint=user_hint),
                    evidence_urls[:4],
                    model,
                )
                parsed = _extract_json(answer) or {}
                claims = parsed.get("claims", []) if isinstance(parsed, dict) else []
                for c in claims[:6]:
                    if isinstance(c, dict) and c.get("claim"):
                        web_claims.append(str(c["claim"])[:160])
                if isinstance(parsed, dict):
                    dominant_materials = [
                        str(m).strip().lower()
                        for m in (parsed.get("dominant_materials") or [])
                        if isinstance(m, str)
                    ][:6]
                summary = "; ".join(web_claims[:3]) or "no clear material claims"
                if dominant_materials:
                    summary += f" · dominant: {', '.join(dominant_materials)}"
                _finish(st, summary)
                for i, c in enumerate(web_claims):
                    with _lock:
                        job.evidence.append(
                            {
                                "evidence_id": f"ev_claim_{i:02d}",
                                "type": "vlm_claim",
                                "claim": c,
                            }
                        )
            except Exception as exc:
                _finish(st, f"evidence analysis failed: {exc}", status="error")

    # ---- step 4: per-view VLM region analysis ------------------------------
    st_all = _step(job, "analyze_views", "Analyzing rendered views with the VLM…")
    hint_line = f'The user says this is: "{user_hint}". ' if user_hint else ""
    evidence_line = (
        "Web evidence suggests: " + "; ".join(web_claims[:3]) + ". "
        if web_claims
        else ""
    )
    votes = np.zeros((n_faces, len(_LABELS)), dtype=np.float32)
    views_ok = 0
    view_durations: list[float] = []
    for vi, d in enumerate(decoded):
        if time.time() > deadline or vlm_calls >= budget.max_vlm_calls:
            break
        _check_cancel(job)
        # ETA: measured per-view VLM time so far × views left (+ small tail
        # for refine/aggregate). Before the first sample, assume ~9 s/view.
        avg = (sum(view_durations) / len(view_durations)) if view_durations else 9.0
        _set_progress(
            job, "views",
            views_done=vi, views_total=len(decoded), vlm_calls=vlm_calls,
            eta_sec=round(avg * (len(decoded) - vi) + 8.0, 1),
        )
        stv = _step(job, f"view_{d['view_id']}", f"View {d['view_id']}: asking the VLM…")
        t0 = time.time()
        try:
            vlm_calls += 1
            answer = _vlm_chat(
                _REGION_PROMPT.format(hint_line=hint_line, evidence_line=evidence_line),
                [d["rgb"]],
                model,
            )
            parsed = _extract_json(answer)
            regions = parsed.get("regions", []) if isinstance(parsed, dict) else []
        except Exception as exc:
            _finish(stv, f"view {d['view_id']}: VLM failed ({exc})", status="error")
            continue
        view_durations.append(time.time() - t0)
        ids = d["ids"]
        h, w = ids.shape
        applied = 0
        for r in regions:
            if not isinstance(r, dict):
                continue
            label = _canonical_label(r.get("label", "unknown"))
            box = r.get("bbox")
            conf = float(r.get("confidence", 0.5) or 0.5)
            if not (isinstance(box, list) and len(box) == 4):
                continue
            x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in box)
            if x1 <= x0 or y1 <= y0:
                continue
            sub = ids[int(y0 * h) : max(int(y1 * h), int(y0 * h) + 1),
                      int(x0 * w) : max(int(x1 * w), int(x0 * w) + 1)]
            face_px = sub[sub != 0xFFFFFFFF]
            if face_px.size == 0:
                continue
            # Per-face pixel counts weighted by the VLM's confidence: a face
            # mostly covered by a region gets a strong vote for its label.
            uniq, counts = np.unique(face_px, return_counts=True)
            votes[uniq, _LABELS.index(label)] += counts.astype(np.float32) * conf
            applied += 1
        views_ok += 1
        _finish(stv, f"view {d['view_id']}: {applied} regions back-projected")
    _finish(st_all, f"{views_ok}/{len(decoded)} views analyzed, {vlm_calls} VLM calls")
    if views_ok == 0:
        raise RuntimeError("no view could be analyzed (VLM unreachable or over budget)")
    _check_cancel(job)

    # ---- helpers shared by refine + final aggregation ----------------------
    def _label_faces(v: "np.ndarray") -> tuple["np.ndarray", "np.ndarray"]:
        """argmax labels + per-face confidence (share × coverage × visibility)."""
        li = v.argmax(axis=1)
        voted = v.max(axis=1) > 0
        li = np.where(voted, li, _LABELS.index("unknown"))
        totals = v.sum(axis=1)
        share = np.where(totals > 0, v.max(axis=1) / np.maximum(totals, 1e-6), 0.0)
        pos = totals[totals > 0]
        norm = float(np.percentile(pos, 75)) if pos.size else 1.0
        coverage = np.clip(totals / max(norm, 1e-6), 0.0, 1.0)
        # Faces confirmed by ≥2 views count fully; single-view faces are
        # discounted; unseen faces cannot be confident at all.
        vis_factor = np.where(visibility >= 2, 1.0, np.where(visibility == 1, 0.7, 0.0))
        conf = share * np.sqrt(coverage) * vis_factor
        return li, conf.astype(np.float32)

    def _rgb_image(d: dict) -> "Image.Image":
        cache = d.get("_pil")
        if cache is None:
            b64 = d["rgb"].split("base64,", 1)[-1]
            cache = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
            d["_pil"] = cache
        return cache

    # ---- step 5: low-confidence refinement (zoomed re-queries) -------------
    labels_idx, face_conf = _label_faces(votes)
    refine_done = 0
    if budget.max_refine_calls > 0:
        # Candidate labels: enough faces to matter, weak mean confidence.
        cands: list[tuple[str, "np.ndarray", float]] = []
        for li, label in enumerate(_LABELS):
            faces = np.nonzero(labels_idx == li)[0]
            if faces.size < 40:
                continue
            mean_conf = float(face_conf[faces].mean())
            if mean_conf < 0.55:
                cands.append((label, faces, mean_conf))
        cands.sort(key=lambda c: -c[1].size)
        if cands:
            _set_progress(job, "refine", views_done=len(decoded), views_total=len(decoded),
                          vlm_calls=vlm_calls)
            st = _step(job, "refine", f"Refining {len(cands[:budget.max_refine_calls])} "
                       "low-confidence region(s) with zoomed crops…")
            for label, faces, mean_conf in cands[: budget.max_refine_calls]:
                if time.time() > deadline or vlm_calls >= budget.max_vlm_calls:
                    break
                _check_cancel(job)
                # The view where this label's faces cover the most pixels.
                best, best_mask, best_count = None, None, 0
                for d in decoded:
                    mask = np.isin(d["ids"], faces)
                    cnt = int(mask.sum())
                    if cnt > best_count:
                        best, best_mask, best_count = d, mask, cnt
                if best is None or best_count < 64:
                    continue
                ys, xs = np.nonzero(best_mask)
                hh, ww = best_mask.shape
                pad_y = max(4, int((ys.max() - ys.min()) * 0.1))
                pad_x = max(4, int((xs.max() - xs.min()) * 0.1))
                y0, y1 = max(0, ys.min() - pad_y), min(hh, ys.max() + pad_y)
                x0, x1 = max(0, xs.min() - pad_x), min(ww, xs.max() + pad_x)
                if (y1 - y0) < 24 or (x1 - x0) < 24:
                    continue
                crop = _rgb_image(best).crop((int(x0), int(y0), int(x1), int(y1)))
                buf = io.BytesIO()
                crop.save(buf, format="JPEG", quality=85)
                crop_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
                try:
                    vlm_calls += 1
                    answer = _vlm_chat(
                        _REFINE_PROMPT.format(label=label, hint_line=hint_line),
                        [crop_url], model, max_tokens=400,
                    )
                    parsed = _extract_json(answer) or {}
                except Exception as exc:
                    _step(job, f"refine_{label}_error", f"refine {label}: VLM failed ({exc})")[
                        "status"
                    ] = "error"
                    continue
                new_label = _canonical_label(parsed.get("label", label)) if isinstance(parsed, dict) else label
                conf = float(parsed.get("confidence", 0.5) or 0.5) if isinstance(parsed, dict) else 0.5
                # Focused-crop verdicts outweigh the coarse pass (×1.5): they
                # either flip the label or consolidate the existing one.
                sub_ids = best["ids"][best_mask]
                uniq, counts = np.unique(sub_ids, return_counts=True)
                keep = np.isin(uniq, faces)
                votes[uniq[keep], _LABELS.index(new_label)] += (
                    counts[keep].astype(np.float32) * conf * 1.5
                )
                refine_done += 1
                _step(
                    job, f"refine_{label}",
                    f"{label} (conf {mean_conf:.2f}) → zoomed verdict: {new_label} ({conf:.2f})",
                )["status"] = "done"
            _finish(st, f"{refine_done} region(s) re-examined")
            labels_idx, face_conf = _label_faces(votes)

    # ---- step 6: deterministic priors + final face labels ------------------
    _set_progress(job, "aggregate", views_done=len(decoded), views_total=len(decoded),
                  vlm_calls=vlm_calls)
    st = _step(job, "aggregate", "Aggregating votes into face labels…")
    # Weak web-evidence prior: nudge the labels matching claimed dominant
    # materials. Multiplicative so it only breaks near-ties.
    boosted: list[str] = []
    for mat in dominant_materials:
        for label in _EVIDENCE_BOOST.get(mat, []):
            votes[:, _LABELS.index(label)] *= _EVIDENCE_BOOST_FACTOR
            boosted.append(label)
    # Orientation prior: upward faces in the UPPER half of the prim are roofs
    # (the height gate keeps terrain/plaza prims from turning into "roof").
    # Weight is modest - a strong VLM vote can override it. The prior feeds
    # whichever roof variant already leads on that face (default concrete).
    normals = np.asarray(geom.face_normals)
    centers_z = np.asarray(geom.triangles_center)[:, 2]
    z_mid = (float(geom.bounds[0][2]) + float(geom.bounds[1][2])) / 2.0
    up = np.nonzero((normals[:, 2] > 0.75) & (centers_z > z_mid))[0]
    if up.size:
        rc, rm = _LABELS.index("roof_concrete"), _LABELS.index("roof_metal")
        base = votes[up].max(axis=1) * 0.5 + 1.0
        target = np.where(votes[up, rm] > votes[up, rc], rm, rc)
        np.add.at(votes, (up, target), base)
    labels_idx, face_conf = _label_faces(votes)
    counts = {
        _LABELS[i]: int((labels_idx == i).sum())
        for i in range(len(_LABELS))
        if int((labels_idx == i).sum())
    }
    unknown_rate = counts.get("unknown", 0) / max(n_faces, 1)
    low_conf_rate = float((face_conf < 0.5).sum()) / max(n_faces, 1)
    summary = ", ".join(f"{k}: {v:,}" for k, v in counts.items())
    summary += f" · unknown {unknown_rate:.0%}, low-confidence {low_conf_rate:.0%}"
    if boosted:
        summary += f" · web prior on {', '.join(sorted(set(boosted)))}"
    _finish(st, summary)
    _check_cancel(job)

    # ---- step 7: segments + per-segment render previews ---------------------
    _set_progress(job, "segments", views_done=len(decoded), views_total=len(decoded),
                  vlm_calls=vlm_calls)
    st = _step(job, "segments", "Building segments…")
    segments: list[dict] = []
    for li, label in enumerate(_LABELS):
        faces = np.nonzero(labels_idx == li)[0]
        if faces.size == 0:
            continue
        rf_primary, rf_alts = SEMANTIC_TO_RF[label]
        conf = float(face_conf[faces].mean()) if faces.size else 0.0
        seg_id = f"{prim.name}_{label}"

        # Preview: the view where this segment covers the most pixels, cropped
        # to the segment and tinted — "show me the faces you mean".
        preview_path: Optional[str] = None
        best, best_mask, best_count = None, None, 0
        for d in decoded:
            mask = np.isin(d["ids"], faces)
            cnt = int(mask.sum())
            if cnt > best_count:
                best, best_mask, best_count = d, mask, cnt
        if best is not None and best_count >= 32:
            try:
                ys, xs = np.nonzero(best_mask)
                hh, ww = best_mask.shape
                pad = max(6, int(max(ys.max() - ys.min(), xs.max() - xs.min()) * 0.08))
                y0, y1 = max(0, ys.min() - pad), min(hh, ys.max() + pad)
                x0, x1 = max(0, xs.min() - pad), min(ww, xs.max() + pad)
                rgb = np.asarray(_rgb_image(best), dtype=np.float32).copy()
                tint = np.array(_LABEL_COLORS.get(label, (255, 80, 80)), dtype=np.float32)
                m3 = best_mask[..., None]
                rgb = np.where(m3, rgb * 0.55 + tint * 0.45, rgb)
                crop = Image.fromarray(rgb[int(y0):int(y1), int(x0):int(x1)].astype(np.uint8))
                crop.thumbnail((320, 320))
                name = f"segment_{label}.jpg"
                crop.save(out_dir / name, format="JPEG", quality=85)
                preview_path = f"ai/agent/{job.job_id}/{name}"
            except Exception:
                preview_path = None

        segments.append(
            {
                "segment_id": seg_id,
                "semantic_label": label,
                "face_count": int(faces.size),
                "rf_material_id": rf_primary,
                "confidence": round(conf, 3),
                "alternatives": [
                    {"rf_material_id": a, "confidence": round(conf * 0.5, 3)}
                    for a in rf_alts
                ],
                "evidence_ids": [e["evidence_id"] for e in job.evidence][:6],
                "preview_asset_path": preview_path,
            }
        )
        job.face_groups[seg_id] = [int(f) for f in faces]
    _finish(st, f"{len(segments)} segments proposed")

    # Benchmark/repro artifact: the raw per-face labels backing the segments.
    np.savez_compressed(
        out_dir / "face_labels.npz",
        labels=labels_idx.astype(np.int8),
        classes=np.array(_LABELS),
        confidence=face_conf.astype(np.float32),
    )

    # ---- persist + finish ---------------------------------------------------
    with _lock:
        job.segments = segments
        job.status = "needs_review"
        job.progress = {**job.progress, "stage": "segments", "eta_sec": 0.0,
                        "elapsed_sec": round(time.time() - job.started_at, 1)}
    _persist_trace(job)


# ------------------------------------------------------------------ apply


def apply_segments(
    project_dir: Path,
    scene: Scene,
    job: AgentJob,
    segment_ids: list[str],
) -> tuple[Scene, dict]:
    """Bake the accepted segments as a physical split (unaccepted faces pool
    into an 'unknown' remainder so the partition stays strict)."""
    import numpy as np

    if job.segments is None:
        raise seg.SegmentationError("job has no segments yet")
    prim, geom = seg._resolve_prim_geometry(project_dir, scene, job.prim_id)
    by_id = {s["segment_id"]: s for s in job.segments}
    chosen = [sid for sid in segment_ids if sid in by_id and sid in job.face_groups]
    if not chosen:
        raise seg.SegmentationError("no valid segment ids to apply")

    n_faces = len(geom.faces)
    taken = np.zeros(n_faces, dtype=bool)
    submeshes: dict[str, object] = {}
    seg_meta: dict[str, dict] = {}
    for sid in chosen:
        faces = np.asarray(job.face_groups[sid], dtype=np.int64)
        faces = faces[(faces >= 0) & (faces < n_faces) & ~taken[faces]]
        if faces.size == 0:
            continue
        taken[faces] = True
        label = by_id[sid]["semantic_label"]
        submeshes[label] = seg._subset_mesh(geom, faces)
        seg_meta[label] = by_id[sid]
    rest = np.nonzero(~taken)[0]
    if rest.size:
        submeshes["unassigned"] = seg._subset_mesh(geom, rest)
    if len(submeshes) < 2:
        raise seg.SegmentationError("accepted segments cover the whole mesh or nothing; nothing to split")

    batch, out_dir = seg._batch_dir(project_dir)

    def make_prim(cls_name: str, node: str) -> Prim:
        meta = seg_meta.get(cls_name)
        if meta is None:  # the unassigned remainder keeps the source binding
            visual = prim.visual.model_copy(deep=True) if prim.visual else None
            return Prim(
                id=f"{prim.id}_{cls_name}",
                name=f"{prim.name}_{cls_name}",
                type="mesh_primitive",
                semantic_tags=list(prim.semantic_tags),
                mesh_ref=MeshRef(asset_uri=prim.mesh_ref.asset_uri, mesh_name=node, face_group=None),
                visual=visual,
                rf=prim.rf.model_copy(deep=True),
            )
        return Prim(
            id=f"{prim.id}_{cls_name}",
            name=f"{prim.name}_{cls_name}",
            type="mesh_primitive",
            semantic_tags=[cls_name],
            mesh_ref=MeshRef(asset_uri=prim.mesh_ref.asset_uri, mesh_name=node, face_group=None),
            visual=VisualBinding(
                material_name=f"seam_agent:{cls_name}",
                base_color_texture=(prim.visual.base_color_texture if prim.visual else None),
            ),
            rf=RFBinding(
                material_id=meta["rf_material_id"],
                # SEAM-Agent proposals are AI provenance, not rule provenance:
                # validation keeps flagging them until the user confirms.
                assignment_status="ai_suggested",
                assignment_sources=[f"seam_agent:{job.job_id}"],
                confidence=meta["confidence"],
            ),
        )

    scene, info = seg._bake_submeshes(
        project_dir, scene, prim, submeshes, batch=batch, make_prim=make_prim,
        undo_extra={"seam_agent_job": job.job_id, "segments": chosen},
    )
    return scene, info
