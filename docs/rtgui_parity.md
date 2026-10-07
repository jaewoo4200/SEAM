# Sionna RT GUI Feature Parity Matrix

> **English** · [한국어](rtgui_parity.ko.md)

Baseline: full source analysis of NVlabs/sionna-rt-gui **v0.1.1** (2026-07-03, commit
`6d37a26`). **RT GUI's engine is pinned exactly to `sionna-rt==1.2.2`**
(requirements.txt:5, pyproject.toml:29) — via its engine switcher, this tool can **run the
same sionna-rt 1.2.2 as-is** (builtin is 2.2.x), so physical-level
equivalence is guaranteed by version selection ([engines.md](engines.md)).

## Parity Table (RT GUI Feature → This Tool's Status)

| RT GUI Feature | This Tool | Notes |
|---|---|---|
| Scene load (builtin/XML) | ✅ | Project model + toolbar **Import** (Mitsuba XML or zip bundle via a file picker); drag-and-drop onto the viewport is not supported |
| Z-up coordinates / camera orbit | ✅ | Same convention |
| Camera reset (R) / fit scene (F) | ✅ | Shortcuts R/F |
| Add TX/RX at cursor (K/L, surface +1.5m) | ✅ | Shortcuts K/L, surface-normal snap, same convention |
| Device select / color / delete / delete all | ✅ | + numeric position/orientation input (not in RT GUI) |
| Antenna array: pattern/polarization/rows-cols/spacing(λ) | ✅ | Spacing exposed; each device stores its own antenna, but a Sionna solve uses one scene-level TX and RX array (the first selected TX's/RX's, as RT GUI) and warns when the selected devices differ |
| All PathSolver parameters (depth/samples/synthetic/6 mechanisms + lit-region) | ✅ | Seed also exposed (RT GUI hardcodes 12345) |
| Auto-update | ✅ | paths/radio map + beamforming/channel — 4 kinds (RT GUI has 2) |
| Path visualization (color by type) | ✅ | + filter / by-strength / selection inspection (not supported by RT GUI) |
| Radio map: cell size/samples/mechanisms | ✅ | Same |
| Radio map colormap/vmin/vmax/colorbar | ✅ | jet/viridis/plasma/turbo + manual range |
| Slice plane (S toggle) | ✅ | Clips scene mesh only, Z slider |
| Trajectory animation / playback speed / loop mode | ✅ | once/loop/pingpong + per-frame ray recomputation (RT GUI is display-only) |
| Doppler (velocity vector) | ✅ | Actor and device velocities from trajectories (and explicit `velocity_m_s`) give per-path Doppler on the Sionna backend, in scenario frames and plain solves; Doppler spread / spectrograms in the channel and trajectory views |
| Photorealistic ray-trace view (Mitsuba) | ✅ | Texture overlay backdrop + lighting panel live; the viewport's **Render** button writes a server-side Mitsuba path-traced image (`POST /render`) |
| Live reload / config YAML | ✅ equivalent | vite HMR + presets/localStorage/project save |
| Help / shortcut table | ✅ | TUTORIAL.md + tooltips |
| Move gizmo | ✅ | X/Y/Z translate gizmo on the selected device or actor (three.js TransformControls) + numeric input |

## Features Where This Tool Exceeds RT GUI (excerpt)

CIR/CFR **display & export** (RT GUI only computes and does not display), channel analysis (K-factor,
delay spread, 38.901 comparison), codebook beam sweep, material editor / ITU picker / per-primitive assignment (not
supported by RT GUI), AI material suggestion (+VLM), measurement calibration, actor/V2X scenarios,
live sync, mesh radio maps, **radar sensing / ISAC** (RCS targets, multistatic fusion,
EKF tracking, coverage maps), **ML dataset pipeline**, RFData and AODT parquet
import/export, multi sionna-rt engines, plugin system, project/provenance.

## Remaining Gaps (an honest list)

1. **UI drag-and-drop import**: scenes come in through the toolbar's Import dialog
   (file picker) or `examples/scripts/import_bundle_scene.py`; dropping a file onto
   the viewport is not supported.
