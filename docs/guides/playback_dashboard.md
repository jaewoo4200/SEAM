# Multimodal playback: measured reality vs. digital twin

> **English** · [한국어](playback_dashboard.ko.md)

If you have a real drive/flight recording — camera frames, LiDAR sweeps, a
measured beam-power profile, a tracked UE pose — SEAM Studio can replay it
frame by frame **against the digital twin**: for every recorded frame the RX
is moved to the measured pose, a ray-tracing solve plus a codebook beam sweep
runs, and the result is stored as one *playback pack* you can scrub like a
video. The Results-mode playback panel shows ground truth and the twin side
by side (beam curves on a shared world-azimuth axis, camera, LiDAR, KPIs)
while the 3D viewport plays the frame's rays and a **beam lobe whose shape is
the measured sweep itself** — a concentrated sweep draws a narrow spike, a
spread sweep a wide lobe. Nothing about the lobe is a canned shape.

Everything here works with the **mock backend** too (values come from the
mock solver, but the whole pipeline — manifest, build, playback, lobe —
runs end to end without Sionna RT).

## 1. Register the recording: `sensor_data/manifest.json`

Put the recording *inside the project folder* and describe it with one
manifest (files are served by the normal asset route, so project-relative
paths are mandatory):

```
my_project.seam/
  sensor_data/
    manifest.json
    camera/2764.jpg        # any image the browser renders
    lidar/2764.pcd         # PCD v0.7, ascii or binary, xyz [+ packed rgb]
    gt_beam/2764.json      # {"azimuth_deg": [...], "power_dbm": [...]}
```

`manifest.json` (see `backend/seam_studio/schemas/sensors.py` for the full
schema):

```json
{
  "version": 1,
  "entity_id": "ue",
  "channels": [
    {"key": "camera",  "kind": "image",        "label": "Front camera"},
    {"key": "lidar",   "kind": "pointcloud",   "label": "Roof LiDAR"},
    {"key": "gt_beam", "kind": "beam_profile", "label": "Measured beam power"}
  ],
  "frames": [
    {
      "index": 2764,
      "time_s": 276.4,
      "files": {"camera": "sensor_data/camera/2764.jpg",
                "lidar": "sensor_data/lidar/2764.pcd",
                "gt_beam": "sensor_data/gt_beam/2764.json"},
      "pose": {"position": [-44.7, 94.3, 1.68], "orientation_deg": [0, 0, 0]},
      "gt": {"rss_dbm": -93.2, "rss_coherent_dbm": -95.1,
             "tau_rms_ns": 12.4, "n_paths": 18, "best_beam_deg": -51.5}
    }
  ]
}
```

- `index` is the dataset's own frame key (DeepVerse `scene_N`, a rosbag
  sequence number, …). `time_s` matters: drive datasets are usually several
  segments concatenated, and the server splits playback **sequences** at
  frame-time jumps — without times everything is one sequence.
- `pose` is the measured UE pose in scene coordinates (Z-up ENU meters).
  Frames without a pose are skipped by the pack builder (with a warning).
- `gt` scalars and the `beam_profile` curve are optional — the panel shows
  an em-dash where ground truth is absent. Beam-profile azimuths are **world
  azimuth degrees** (atan2(y, x) about +Z).

`GET /projects/{id}/sensors` returns the manifest plus detected segments;
opening the project loads it automatically and the playback panel appears in
Results mode.

## 2. Build the playback pack

In the playback panel press **Build playback pack** (or POST
`/projects/{id}/simulate/playback`). Per frame the builder moves the RX to
the recorded pose *in memory* (the stored scene is never edited), runs the
paths solve and a codebook sweep, and keeps: the beam curve reprojected to
world azimuth, RSS (noncoherent + coherent), τ_rms, path count, and the
strongest rays for the viewport. Progress and Cancel work like any solve.

Two knobs matter for correctness:

- **`use_device_orientation` (default true here).** A fixed-bearing base
  station sweeps relative to its array broadside; set the TX device's
  `orientation_deg` yaw to the array bearing. With the default look-at
  behavior the sweep axis re-aims at the UE every frame and a beam
  *trajectory* is physically meaningless.
- **Absorption / frequency** come from the simulation config — at 60 GHz
  enable `atmospheric_absorption` (see the accuracy doc) or the GT
  comparison inherits a distance-proportional bias.

The pack persists as a normal result set (`kind: "playback"`, run history,
labels, pruning all apply). It is a *historical* record of solves against
recorded poses — editing the scene does not mark it stale.

## 3. Read the dashboard

- **Beam chart** — GT (teal) and SEAM (amber) on one world-azimuth axis with
  best-beam markers: beam-tracking agreement is visible as the two peaks
  moving together while you scrub.
- **LiDAR panel** — top-down with user toggles: framing (full scene / zoom on
  the UE), color (height shading with a widened low band so half-meter
  objects survive, or semantic colors when the PCD carries them).
- **KPIs** — GT vs SEAM: RSS, coherent RSS, τ_rms, path count, best beam,
  plus deltas where both sides exist.
- **Viewport** — the frame's rays, the moving RX marker, and the dynamic TX
  beam lobe. The lobe also works outside playback: any codebook-sweep
  beamforming result draws it (Beam lobe toggle in the overlay row).
