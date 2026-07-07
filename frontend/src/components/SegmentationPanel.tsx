import { useState } from "react";
import { useAppStore } from "../store/appStore";
import { api } from "../api/client";
import { Swatch, materialById } from "./common";
import { segmentationClassColor } from "../types/api";
import type { MaskSource, Prim } from "../types/api";

// Multi-material building split: preview a per-face material segmentation of one
// prim's textured mesh, review it (2D overlay + table + 3D region tint), then
// bake it into the visual GLB as per-material sub-prims. Lives on the selected
// prim's inspector card, only for prims that carry a base-color texture (the
// mask sources read the texture atlas / render tiles from it).

const MASK_SOURCES: { value: MaskSource; label: string; hint: string }[] = [
  {
    value: "color_heuristic",
    label: "Color heuristic",
    hint: "Instant: classify each texel by color (concrete/glass/metal/ground).",
  },
  {
    value: "vlm_tile_vote",
    label: "VLM tile vote",
    hint: "Slower: a local vision model votes on texture tiles (runs as a job).",
  },
  {
    value: "user_png",
    label: "Upload mask PNG",
    hint: "Bring your own id-mask (SAM2/DINOv2 grade): one class id per pixel.",
  },
];

const TILE_COUNTS = [16, 36, 64];

/** Mesh-splitting tools for the selected prim (rendered inside PrimCard):
 *  "Split by material…" (needs a texture atlas) and "Split into connected
 *  parts…" (any mesh — breaks a merged multi-building blob into per-part
 *  prims that inherit the source RF binding). */
export default function SegmentationPanel({ prim }: { prim: Prim }) {
  const materials = useAppStore((s) => s.materials);
  const busy = useAppStore((s) => s.busy);
  const projectId = useAppStore((s) => s.projectId);
  const segPreview = useAppStore((s) => s.segPreview);
  const segJobProgress = useAppStore((s) => s.segJobProgress);
  const lastSegApply = useAppStore((s) => s.lastSegApply);
  const runPreview = useAppStore((s) => s.runSegmentationPreview);
  const applySeg = useAppStore((s) => s.applySegmentation);
  const doSplitParts = useAppStore((s) => s.splitParts);
  const undoSeg = useAppStore((s) => s.undoSegmentation);
  const clearSeg = useAppStore((s) => s.clearSegPreview);

  const hasTexture = Boolean(prim.visual?.base_color_texture);
  const [open, setOpen] = useState(false);
  const [partsOpen, setPartsOpen] = useState(false);
  const [minFaces, setMinFaces] = useState(200);
  const [maskSource, setMaskSource] = useState<MaskSource>("color_heuristic");
  const [flipV, setFlipV] = useState(true);
  const [tileCount, setTileCount] = useState(64);
  const [model, setModel] = useState("");
  const [maskAssetPath, setMaskAssetPath] = useState<string | null>(null);
  const [uploadInfo, setUploadInfo] = useState<{ width: number; height: number } | null>(null);
  const [uploadErr, setUploadErr] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);

  const disabled = busy !== null;
  // A preview belongs to a specific prim; only surface it on that prim's card.
  const previewHere = segPreview !== null && segPreview.primId === prim.id;
  const applyHere = lastSegApply !== null && lastSegApply.primId === prim.id;

  const onUpload = async (file: File) => {
    if (!projectId) return;
    setUploading(true);
    setUploadErr(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const resp = await api.uploadSegmentationMask(projectId, form);
      setMaskAssetPath(resp.mask_asset_path);
      setUploadInfo({ width: resp.width, height: resp.height });
    } catch (err) {
      setUploadErr(err instanceof Error ? err.message : String(err));
      setMaskAssetPath(null);
      setUploadInfo(null);
    } finally {
      setUploading(false);
    }
  };

  const runDisabled =
    disabled ||
    !projectId ||
    (maskSource === "user_png" && !maskAssetPath);

  const doRun = () => {
    void runPreview({
      prim_id: prim.id,
      mask_source: maskSource,
      flip_v: flipV,
      ...(maskSource === "vlm_tile_vote"
        ? { max_tiles: tileCount, model: model.trim() || null }
        : {}),
      ...(maskSource === "user_png" ? { mask_asset_path: maskAssetPath } : {}),
    });
  };

  const totalPreviewFaces =
    previewHere && segPreview
      ? segPreview.manifest.reduce((n, r) => n + r.face_count, 0)
      : 0;

  return (
    <div className="seg-section" style={{ marginTop: 12 }}>
      {hasTexture && (
        <button
          className={"seg-expander" + (open ? " open" : "")}
          onClick={() => setOpen((o) => !o)}
          title="Split this textured mesh into per-material sub-prims"
        >
          {open ? "▾" : "▸"} Split by material…
        </button>
      )}

      {hasTexture && open && (
        <div className="seg-body">
          {/* Mask source */}
          <div className="seg-sources">
            {MASK_SOURCES.map((s) => (
              <label key={s.value} className="seg-radio" title={s.hint}>
                <input
                  type="radio"
                  name={`seg-src-${prim.id}`}
                  checked={maskSource === s.value}
                  disabled={disabled}
                  onChange={() => setMaskSource(s.value)}
                />
                <span>{s.label}</span>
              </label>
            ))}
          </div>
          <p className="hint seg-src-hint">
            {MASK_SOURCES.find((s) => s.value === maskSource)?.hint}
          </p>

          {/* flip_v (default on) */}
          <label className="solver-check" title="Mask images are top-left origin; GLB UVs are bottom-left">
            <input
              type="checkbox"
              checked={flipV}
              disabled={disabled}
              onChange={(e) => setFlipV(e.target.checked)}
            />
            Flip V
            <span className="hint" style={{ marginLeft: 6 }}>
              on unless the atlas was authored with an already-flipped V
            </span>
          </label>

          {/* VLM-only controls */}
          {maskSource === "vlm_tile_vote" && (
            <div className="seg-vlm">
              <label className="solver-field">
                <span className="solver-field-label">Tiles</span>
                <select
                  value={tileCount}
                  disabled={disabled}
                  onChange={(e) => setTileCount(Number(e.target.value))}
                >
                  {TILE_COUNTS.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
              <label className="solver-field">
                <span className="solver-field-label">Model</span>
                <input
                  type="text"
                  value={model}
                  disabled={disabled}
                  placeholder="provider default"
                  onChange={(e) => setModel(e.target.value)}
                />
              </label>
            </div>
          )}

          {/* user_png upload */}
          {maskSource === "user_png" && (
            <div className="seg-upload">
              <input
                type="file"
                accept="image/png,image/*"
                disabled={disabled || uploading}
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) void onUpload(file);
                }}
              />
              {uploading && <span className="hint">Uploading…</span>}
              {maskAssetPath && uploadInfo && (
                <p className="hint">
                  Uploaded {uploadInfo.width}×{uploadInfo.height} —{" "}
                  <span className="mono">{maskAssetPath}</span>
                </p>
              )}
              {uploadErr && <p className="field-error">{uploadErr}</p>}
            </div>
          )}

          {/* Run */}
          <div className="panel-actions">
            <button className="primary" disabled={runDisabled} onClick={doRun}>
              {disabled && !previewHere ? "Running…" : "Run Preview"}
            </button>
          </div>

          {/* VLM job progress */}
          {segJobProgress && !previewHere && (
            <div className="seg-progress">
              <div className="seg-progress-label">
                {segJobProgress.total > 0
                  ? `${segJobProgress.progress}/${segJobProgress.total} tiles`
                  : "Working…"}
                {segJobProgress.detail ? ` · ${segJobProgress.detail}` : ""}
              </div>
              <div className="seg-progress-bar">
                <div
                  style={{
                    width:
                      segJobProgress.total > 0
                        ? `${Math.round((segJobProgress.progress / segJobProgress.total) * 100)}%`
                        : "100%",
                  }}
                />
              </div>
            </div>
          )}

          {/* Preview result */}
          {previewHere && segPreview && (
            <div className="seg-preview">
              {projectId && (
                <a
                  href={api.assetUrl(projectId, segPreview.overlayAssetPath)}
                  target="_blank"
                  rel="noreferrer"
                  title="Open the full-size overlay in a new tab"
                >
                  <img
                    className="seg-overlay-img"
                    src={api.assetUrl(projectId, segPreview.overlayAssetPath)}
                    alt="Segmentation overlay"
                  />
                </a>
              )}

              <table className="seg-table">
                <thead>
                  <tr>
                    <th />
                    <th>material</th>
                    <th>RF material</th>
                    <th>faces</th>
                    <th>%</th>
                  </tr>
                </thead>
                <tbody>
                  {segPreview.manifest.map((r) => {
                    const rfMat = materialById(materials, r.rf_material_id);
                    const pct =
                      totalPreviewFaces > 0
                        ? ((r.face_count / totalPreviewFaces) * 100).toFixed(1)
                        : "0.0";
                    return (
                      <tr key={r.material_id}>
                        <td>
                          <Swatch color={segmentationClassColor(r.material_id)} />
                        </td>
                        <td>{r.name}</td>
                        <td>
                          <span className="mono" title={rfMat?.display_name ?? ""}>
                            {r.rf_material_id}
                          </span>
                        </td>
                        <td>{r.face_count.toLocaleString()}</td>
                        <td>{pct}%</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>

              <div className="panel-actions">
                <button className="primary" disabled={disabled} onClick={() => void applySeg()}>
                  Apply split
                </button>
                <button disabled={disabled} onClick={() => clearSeg()}>
                  Cancel
                </button>
              </div>
              <p className="hint">
                Applying bakes the split into the visual GLB (backup kept for undo).
              </p>
            </div>
          )}

          {/* Applied result + undo (survives panel re-render via lastSegApply) */}
          {applyHere && lastSegApply && !previewHere && (
            <div className="seg-applied">
              <div className="seg-applied-note">
                ✓ Split into {lastSegApply.addedPrimIds.length} prim(s):{" "}
                {lastSegApply.addedPrimIds.map((id) => (
                  <span key={id} className="mono seg-applied-prim">
                    {id}
                  </span>
                ))}
              </div>
              <button
                className="on-reject"
                disabled={disabled}
                onClick={() => void undoSeg(lastSegApply.batchId)}
              >
                Undo split
              </button>
            </div>
          )}
        </div>
      )}

      {/* Connected-parts split: any mesh, no texture needed. Breaks a merged
          multi-building blob into per-part prims (RF binding inherited). */}
      <button
        className={"seg-expander" + (partsOpen ? " open" : "")}
        onClick={() => setPartsOpen((o) => !o)}
        title="Split a merged mesh into its connected components (one prim per part)"
      >
        {partsOpen ? "▾" : "▸"} Split into connected parts…
      </button>
      {partsOpen && (
        <div className="seg-body">
          <p className="hint">
            City exports often merge many buildings into one mesh; this breaks
            it into disconnected pieces. Parts smaller than the face threshold
            pool into a single &quot;rest&quot; prim. New prims inherit this
            prim&apos;s RF material and texture; undo is available after.
          </p>
          <label className="solver-field">
            <span className="solver-field-label">Min faces / part</span>
            <input
              type="number"
              min={1}
              step={50}
              value={minFaces}
              disabled={disabled}
              onChange={(e) => setMinFaces(Math.max(1, Number(e.target.value) || 1))}
            />
          </label>
          <div className="panel-actions">
            <button
              className="primary"
              disabled={disabled || !projectId}
              onClick={() => void doSplitParts(prim.id, minFaces)}
            >
              Split into parts
            </button>
          </div>
          {applyHere && lastSegApply && (
            <div className="seg-applied">
              <div className="seg-applied-note">
                ✓ Last split made {lastSegApply.addedPrimIds.length} prim(s)
              </div>
              <button
                className="on-reject"
                disabled={disabled}
                onClick={() => void undoSeg(lastSegApply.batchId)}
              >
                Undo split
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
