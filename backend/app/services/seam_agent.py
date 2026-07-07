"""SEAM-Agent: retrieval-augmented local LLM/VLM RF material authoring.

Turns ONE building-level prim into segment-level RF material candidates:

    multi-view mesh renders (RGB + triangle-id buffers, captured by the FE)
    + optional user site/building hint
    + optional web/image evidence (DuckDuckGo, local-first & opt-in)
    + local VLM region analysis (LM Studio / OpenAI-compatible)
    -> per-view region boxes -> triangle-id back-projection -> face votes
    -> connected face groups -> RF material candidates with confidence,
       evidence cards and an observable activity trace
    -> user review -> physical split via the segmentation bake machinery.

Design principles (SEAM_Agent_Material_Assignment_Handoff.md):
- the LLM/VLM plans and interprets EVIDENCE; deterministic code does all
  mesh inspection, back-projection, grouping and export;
- bounded loop with explicit budgets (searches, VLM calls, runtime);
- an activity trace instead of raw chain-of-thought;
- web images are WEAK priors with provenance, never truth;
- everything degrades: no web -> renders only; no VLM -> error with a
  clear trace step, nothing half-applied.
"""

from __future__ import annotations

import base64
import io
import json
import re
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

# Semantic label -> (primary rf material, alternatives). Deliberately the
# handoff's coarse RF-relevant vocabulary; anything else the VLM invents is
# folded into "unknown".
SEMANTIC_TO_RF: dict[str, tuple[str, list[str]]] = {
    "exterior_wall": ("itu_concrete", ["itu_brick"]),
    "glass_window": ("itu_glass", []),
    "curtain_wall_glass": ("itu_glass", []),
    "roof": ("itu_concrete", ["metal"]),
    "metal_frame": ("metal", []),
    "door": ("itu_wood", ["itu_glass"]),
    "vegetation": ("vegetation_custom", []),
    "ground": ("ground_28ghz", []),
    "unknown": ("unknown_rf", []),
}
_LABELS = list(SEMANTIC_TO_RF)


@dataclass
class AgentBudget:
    max_web_searches: int = 6
    max_image_searches: int = 4
    max_images: int = 6
    max_vlm_calls: int = 40
    max_runtime_sec: int = 600


@dataclass
class AgentJob:
    job_id: str
    status: str = "running"  # running | needs_review | done | error
    detail: str = ""
    steps: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    segments: Optional[list[dict]] = None
    # face labels backing the segments, for apply (segment_id -> face indices)
    face_groups: dict[str, list[int]] = field(default_factory=dict)
    prim_id: str = ""
    started_at: float = field(default_factory=time.time)


_jobs: dict[str, AgentJob] = {}
_lock = threading.Lock()


def get_job(job_id: str) -> Optional[AgentJob]:
    with _lock:
        return _jobs.get(job_id)


def _step(job: AgentJob, step_id: str, summary: str, **extra) -> dict:
    """Append a running trace step (the user-visible 'thinking')."""
    entry = {"step_id": step_id, "status": "running", "summary": summary, **extra}
    with _lock:
        job.steps.append(entry)
    return entry


def _finish(entry: dict, summary: Optional[str] = None, status: str = "done") -> None:
    entry["status"] = status
    if summary is not None:
        entry["summary"] = summary


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

    from app.core.config import get_settings

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

_REGION_PROMPT = """You are labeling ONE rendered view of a single building mesh for RF (radio)
simulation. {hint_line}{evidence_line}
Identify the RF-relevant regions VISIBLE in the render and output ONLY JSON:
{{"regions": [{{"label": "<one of: exterior_wall, glass_window, curtain_wall_glass, roof, metal_frame, door, vegetation, ground, unknown>",
  "bbox": [x0, y0, x1, y1],  // fractions of image width/height, 0.0-1.0
  "confidence": 0.0-1.0,
  "reason": "short phrase"}}]}}
Rules: boxes may overlap; cover the large facade/roof areas first; use
"unknown" when unsure; 2-8 regions; no prose outside the JSON."""

_EVIDENCE_PROMPT = """These photos were retrieved from the web for the site/building: "{hint}".
They are WEAK evidence (may show a different building). Summarize, as JSON only:
{{"claims": [{{"claim": "short factual statement about visible construction materials
 (e.g. 'facade is mostly blue glass curtain wall', 'roof appears metal')",
  "confidence": 0.0-1.0}}], "dominant_materials": ["concrete"|"glass"|"metal"|"brick"|...]}}"""


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
    """Spawn the bounded agent loop in a thread; returns the job id."""
    job_id = uuid.uuid4().hex[:12]
    job = AgentJob(job_id=job_id, prim_id=prim_id)
    with _lock:
        _jobs[job_id] = job

    def run() -> None:
        try:
            _run_pipeline(
                job, project_dir, scene, prim_id, views, user_hint, allow_web,
                model, budget,
            )
        except Exception as exc:  # surface, never crash the server
            with _lock:
                job.status = "error"
                job.detail = str(exc)[:500]

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
    deadline = job.started_at + budget.max_runtime_sec
    vlm_calls = 0

    # ---- step 1: inspect --------------------------------------------------
    st = _step(job, "inspect_mesh", "Inspecting mesh…")
    prim, geom = seg._resolve_prim_geometry(project_dir, scene, prim_id)
    n_faces = int(len(geom.faces))
    has_tex = bool(prim.visual and prim.visual.base_color_texture)
    _finish(
        st,
        f"{prim.name}: {n_faces:,} faces, texture {'present' if has_tex else 'missing'}, "
        f"{len(views)} rendered views received",
    )

    # ---- step 2: decode triangle-id buffers -------------------------------
    st = _step(job, "decode_views", "Decoding triangle-id buffers…")
    decoded: list[dict] = []
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
        decoded.append({"view_id": v["view_id"], "ids": ids, "rgb": v["rgb_data_url"]})
    _finish(st, f"{len(decoded)} views decoded against {n_faces:,} faces")

    # ---- step 3: optional retrieval ---------------------------------------
    evidence_urls: list[str] = []  # data URLs fed to the VLM
    web_claims: list[str] = []
    if allow_web and user_hint:
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
                _finish(st, "; ".join(web_claims[:3]) or "no clear material claims")
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
    for d in decoded:
        if time.time() > deadline or vlm_calls >= budget.max_vlm_calls:
            break
        stv = _step(job, f"view_{d['view_id']}", f"View {d['view_id']}: asking the VLM…")
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
        ids = d["ids"]
        h, w = ids.shape
        applied = 0
        for r in regions:
            if not isinstance(r, dict):
                continue
            label = str(r.get("label", "unknown"))
            if label not in SEMANTIC_TO_RF:
                label = "unknown"
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

    # ---- step 5: deterministic priors + face labels ------------------------
    st = _step(job, "aggregate", "Aggregating votes into face labels…")
    # Orientation prior: upward faces in the UPPER half of the prim are roofs
    # (the height gate keeps terrain/plaza prims from turning into "roof").
    # Weight is modest - a strong VLM vote can override it.
    normals = np.asarray(geom.face_normals)
    centers_z = np.asarray(geom.triangles_center)[:, 2]
    z_mid = (float(geom.bounds[0][2]) + float(geom.bounds[1][2])) / 2.0
    up = (normals[:, 2] > 0.75) & (centers_z > z_mid)
    votes[up, _LABELS.index("roof")] += votes.max(axis=1)[up] * 0.5 + 1.0
    labels_idx = votes.argmax(axis=1)
    voted = votes.max(axis=1) > 0
    labels_idx[~voted] = _LABELS.index("unknown")
    # Confidence per face: winner share of that face's total vote mass.
    totals = votes.sum(axis=1)
    face_conf = np.where(totals > 0, votes.max(axis=1) / np.maximum(totals, 1e-6), 0.0)
    counts = {
        _LABELS[i]: int((labels_idx == i).sum())
        for i in range(len(_LABELS))
        if int((labels_idx == i).sum())
    }
    _finish(st, ", ".join(f"{k}: {v:,}" for k, v in counts.items()))

    # ---- step 6: segments -------------------------------------------------
    st = _step(job, "segments", "Building segments…")
    segments: list[dict] = []
    for li, label in enumerate(_LABELS):
        faces = np.nonzero(labels_idx == li)[0]
        if faces.size == 0:
            continue
        rf_primary, rf_alts = SEMANTIC_TO_RF[label]
        conf = float(face_conf[faces].mean()) if faces.size else 0.0
        seg_id = f"{prim.name}_{label}"
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
    (out_dir / "trace.json").write_text(
        json.dumps(
            {
                "job_id": job.job_id,
                "prim_id": prim_id,
                "user_hint": user_hint,
                "allow_web": allow_web,
                "model": model,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "steps": job.steps,
                "evidence": job.evidence,
                "segments": segments,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


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
                assignment_status="rule_suggested",
                assignment_sources=[f"seam_agent:{job.job_id}"],
                confidence=meta["confidence"],
            ),
        )

    scene, info = seg._bake_submeshes(
        project_dir, scene, prim, submeshes, batch=batch, make_prim=make_prim,
        undo_extra={"seam_agent_job": job.job_id, "segments": chosen},
    )
    return scene, info
