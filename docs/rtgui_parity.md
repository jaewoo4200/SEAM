# Sionna RT GUI Feature Parity Matrix

> **English** · [한국어](rtgui_parity.ko.md)

Baseline: full source analysis of NVlabs/sionna-rt-gui **v0.1.1** (2026-07-03, commit
`6d37a26`). **RT GUI's engine is pinned exactly to `sionna-rt==1.2.2`**
(requirements.txt:5, pyproject.toml:29) — via its engine switcher, this tool can **run the
same sionna-rt 1.2.2 as-is** (builtin is 2.0.1), so physical-level
equivalence is guaranteed by version selection ([engines.md](engines.md)).

## Parity Table (RT GUI Feature → This Tool's Status)

| RT GUI Feature | This Tool | Notes |
|---|---|---|
| Scene load (builtin/XML) | ✅ | Project model + import script; UI drag-and-drop is on the roadmap |
| Z-up coordinates / camera orbit | ✅ | Same convention |
| Camera reset (R) / fit scene (F) | ✅ | Shortcuts R/F |
| Add TX/RX at cursor (K/L, surface +1.5m) | ✅ | Shortcuts K/L, surface-normal snap, same convention |
| Device select / color / delete / delete all | ✅ | + numeric position/orientation input (not in RT GUI) |
| Antenna array: pattern/polarization/rows-cols/spacing(λ) | ✅ | Spacing exposed; **per-device** arrays (RT GUI is scene-wide) |
| All PathSolver parameters (depth/samples/synthetic/6 mechanisms + lit-region) | ✅ | Seed also exposed (RT GUI hardcodes 12345) |
| Auto-update | ✅ | paths/radio map + beamforming/channel — 4 kinds (RT GUI has 2) |
| Path visualization (color by type) | ✅ | + filter / by-strength / selection inspection (not supported by RT GUI) |
| Radio map: cell size/samples/mechanisms | ✅ | Same |
| Radio map colormap/vmin/vmax/colorbar | ✅ | jet/viridis/plasma/turbo + manual range |
| Slice plane (S toggle) | ✅ | Clips scene mesh only, Z slider |
| Trajectory animation / playback speed / loop mode | ✅ | once/loop/pingpong + per-frame ray recomputation (RT GUI is display-only) |
| Doppler (velocity vector) | ⚠ roadmap | RT GUI sets velocity during animation; this tool's trajectory is position-only |
| Photorealistic ray-trace view (Mitsuba) | ⚠ alternative | Texture overlay backdrop + lighting panel; server-side Mitsuba render-to-file is on the roadmap |
| Live reload / config YAML | ✅ equivalent | vite HMR + presets/localStorage/project save |
| Help / shortcut table | ✅ | TUTORIAL.md + tooltips |

## Features Where This Tool Exceeds RT GUI (excerpt)

CIR/CFR **display & export** (RT GUI only computes and does not display), channel analysis (K-factor,
delay spread, 38.901 comparison), codebook beam sweep, material editor / ITU picker / per-primitive assignment (not
supported by RT GUI), AI material suggestion (+VLM), measurement calibration, actor/V2X scenarios,
live sync, **ML dataset pipeline**, RFData export, multi sionna-rt
engines, plugin system, project/provenance.

## Remaining Gaps (an honest list)

1. **Doppler/velocity**: object velocity during trajectory playback → per-path Doppler. Planned via the
   sionna-rt 2.x velocity API.
2. **UI drag-and-drop XML import**: currently `examples/scripts/import_bundle_scene.py`.
3. **Mitsuba path-trace render-to-file**: planned to add a camera-pose render
   endpoint via backend mitsuba (RT GUI also has no file save — implementing it would exceed RT GUI).
4. **Gizmo move** (RT GUI style): numeric input exists; three.js TransformControls
   planned (cf. the X/Y/Z arrows in the AODT client).
