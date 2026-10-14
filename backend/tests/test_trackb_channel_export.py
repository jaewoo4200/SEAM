"""Track B #22: channel tensors (include_channel -> channel.npz) and
background rows (include_background_paths) of POST /export/sensing-dataset,
checked against independent numpy synthesis from the exported rows (the
reference side never imports the module under test). Mock backend."""

import hashlib
import io
import json
import math
import zipfile

import numpy as np
import pytest

from seam_studio.services import channel_synthesis

from .test_sensing_track import MOCK_CFG, _trp_scene

PID = "trackb_export"
C = 299_792_458.0
F_C = MOCK_CFG["frequency_hz"]
GRID_S = {
    "scs_hz": 15_625.0, "n_fft": 2048, "n_active_sc": 1280, "cp": "normal",
    "symbol_duration_s": 1.0 / 14_400,
    "pilot": {"pattern": "comb", "subcarrier_step": 5, "symbol_step": 9},
}
CFAR = {"mode": "cfar", "map": {"window": "hann", "clutter_removal": "mean_subtract"}}
V0115_MANIFEST_KEYS = [
    "schema", "schema_version", "created_at", "seam_version", "project_id", "formats",
    "tables", "split", "rows_per_split", "label_balance", "results", "columns", "conventions",
]
V0115_ECHO_COLUMNS = [
    "result_id", "split", "frame_index", "time_s", "path_id", "tx_id", "rx_id", "target_id",
    "path_type", "delay_ns", "bistatic_range_m", "power_dbm", "path_gain_db", "phase_rad",
    "doppler_hz", "aod_az_deg", "aod_el_deg", "aoa_az_deg", "aoa_el_deg", "num_interactions",
    "direct", "reported",
]
CHANNEL_COLUMNS = ["a_re", "a_im", "tx_power_dbm", "interaction_types", "rf_material_ids",
                   "prim_ids"]


@pytest.fixture()
def client(api_client):
    resp = api_client.post("/api/projects", json={"name": "Track B", "project_id": PID})
    assert resp.status_code == 201, resp.text
    scene = _trp_scene(scene_id=PID)
    put = api_client.put(f"/api/projects/{PID}/scene", json=scene.model_dump(mode="json"))
    assert put.status_code == 200, put.text
    return api_client


def _run(client, num_frames=4, **sensing) -> dict:
    payload = {
        "config": MOCK_CFG, "num_frames": num_frames, "dt_s": 0.5, "include_paths": False,
        "sensing": {"enabled": True, **sensing},
    }
    resp = client.post(f"/api/projects/{PID}/simulate/scenario", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _trackb_run(client, background=None, **extra) -> dict:
    return _run(
        client, waveform=GRID_S, background=background or {"mode": "both", "store": "both"},
        detection=CFAR, **extra,
    )


def _export(client, **body):
    return client.post(f"/api/projects/{PID}/export/sensing-dataset", json=body)


def _zip(client, body) -> zipfile.ZipFile:
    resp = client.get(body["download_url"])
    assert resp.status_code == 200, resp.text
    return zipfile.ZipFile(io.BytesIO(resp.content))


def _npz(zf, name):
    return np.load(io.BytesIO(zf.read(name)), allow_pickle=False)


def _export_dir():
    from seam_studio.api import deps

    return deps.get_store().resolve(PID) / "export" / "sensing_dataset"


def ref_synth(a, delay_ns, doppler_hz, f, t) -> np.ndarray:
    H = np.zeros((len(t), len(f)), dtype=complex)
    for ai, d, fd in zip(a, delay_ns, doppler_hz):
        H += ai * np.exp(-2j * np.pi * f[None, :] * d * 1e-9) * np.exp(
            2j * np.pi * fd * t[:, None]
        )
    return H


def _rel(a, b) -> float:
    den = np.linalg.norm(b)
    return float(np.linalg.norm(a - b) / den) if den > 0 else float(np.linalg.norm(a))


def _wrap(x):
    return (np.asarray(x) + np.pi) % (2 * np.pi) - np.pi


# ------------------------------------------------------------ default export


def test_default_export_is_the_v0115_one(client):
    plain = _run(client, num_frames=2, cpi_pulses=4096)
    body = _export(client, include_echo_paths=True).json()
    assert body["files"] == ["manifest.json", "README.txt", "links.npz", "echoes.npz"]
    assert body["num_background_rows"] == 0 and body["num_channel_rows"] == 0
    assert body["channel_elements"] == 0
    zf = _zip(client, body)
    manifest = json.loads(zf.read("manifest.json"))
    assert list(manifest) == V0115_MANIFEST_KEYS and manifest["schema_version"] == 1
    assert [c["name"] for c in manifest["columns"]["echoes"]] == V0115_ECHO_COLUMNS
    assert list(_npz(zf, "echoes.npz").files) == V0115_ECHO_COLUMNS
    assert set(manifest["conventions"]) == {
        "frame", "angles", "azimuth", "elevation", "arrival", "doppler", "snr_db", "missing",
        "association",
    }
    reason = next(c for c in manifest["columns"]["links"] if c["name"] == "reason")
    assert reason["description"] == "detected / no_echo / below_threshold / mti_rejected"
    assert "Track B" not in zf.read("README.txt").decode()
    assert "background_h0" not in json.dumps(plain["frames"][0]["sensing"])

    # A cfar run: still schema 1, the link descriptions say what cfar means.
    _trackb_run(client)
    body = _export(client).json()
    manifest = json.loads(_zip(client, body).read("manifest.json"))
    assert list(manifest) == V0115_MANIFEST_KEYS and manifest["schema_version"] == 1
    assert "oracle gate" in manifest["conventions"]["detection"]
    reason = next(c for c in manifest["columns"]["links"] if c["name"] == "reason")
    assert reason["description"].endswith("/ cfar_miss")


# ------------------------------------------------------------ channel tensors


def test_channel_tensors_match_numpy_synthesis_of_the_rows(client):
    result = _trackb_run(client)
    rid = result["result_id"]
    body = _export(
        client, include_echo_paths=True, include_background_paths=True,
        include_channel={"components": ["echo", "background_h0", "background_h1", "total"]},
    ).json()
    assert body["files"][-1] == "channel.npz" and "background.npz" in body["files"]
    zf = _zip(client, body)
    assert zf.getinfo("channel.npz").compress_type == zipfile.ZIP_STORED
    ch = _npz(zf, "channel.npz")
    echoes = _npz(zf, "echoes.npz")
    background = _npz(zf, "background.npz")
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["schema_version"] == 2
    assert list(echoes.files) == V0115_ECHO_COLUMNS + CHANNEL_COLUMNS

    f, t = ch["f_hz"], ch["t_s"]
    assert len(f) == 256 and len(t) == 16
    assert np.array_equal(f, (np.arange(256) * 5 - 640) * 15_625.0)
    assert np.allclose(t, np.arange(16) * 9 / 14_400, rtol=1e-15, atol=0)
    n_rows = 4 * 16
    assert body["num_channel_rows"] == n_rows and body["channel_elements"] == n_rows * 4096 * 4
    assert ch["H_echo"].shape == (n_rows, 16, 256) and ch["H_echo"].dtype == np.complex64
    meta = manifest["channel"]
    assert meta["tensor_layout"] == "H[row, symbol, subcarrier]"
    assert meta["waveform_source"] == {rid: "run"} and meta["numerology"]["grid_k"] == 256
    assert meta["arrays"] == ["H_echo", "H_background_h0", "H_background_h1", "H_total"]

    # a_re / a_im: 10^(gain/20) e^{j phase}.
    a = echoes["a_re"] + 1j * echoes["a_im"]
    assert np.allclose(
        a, 10 ** (echoes["path_gain_db"] / 20) * np.exp(1j * echoes["phase_rad"]), rtol=1e-12
    )
    assert np.all(echoes["tx_power_dbm"] == 43.0)
    assert set(echoes["interaction_types"].tolist()) == {"sensing"}
    assert set(echoes["prim_ids"].tolist()) == {"uav_01"}

    # H0 is static (solved on frame 0, reused); H1 follows the moving target.
    index = {i: (h0, h1) for i, h0, h1 in manifest["background_index"][rid]}
    assert index == {i: (0, i) for i in range(4)}
    seen_bg = 0
    for row in range(n_rows):
        i, tx, rx = int(ch["frame_index"][row]), str(ch["tx_id"][row]), str(ch["rx_id"][row])
        assert ch["result_id"][row] == rid and ch["row"][row] == row
        e = (echoes["frame_index"] == i) & (echoes["tx_id"] == tx) & (echoes["rx_id"] == rx)
        e &= echoes["target_id"] != ""
        ref_echo = ref_synth(a[e], echoes["delay_ns"][e], echoes["doppler_hz"][e], f, t)
        assert _rel(ch["H_echo"][row], ref_echo) < 1e-4
        refs = {}
        for n, hyp in enumerate(("h0", "h1")):
            b = (background["hypothesis"] == hyp) & (background["frame_index"] == index[i][n])
            b &= (background["tx_id"] == tx) & (background["rx_id"] == rx)
            ab = background["a_re"][b] + 1j * background["a_im"][b]
            refs[hyp] = ref_synth(ab, background["delay_ns"][b], background["doppler_hz"][b], f, t)
            assert _rel(ch[f"H_background_{hyp}"][row], refs[hyp]) < 1e-4
            seen_bg += int(b.sum())
        assert _rel(ch["H_total"][row], ref_echo + refs["h1"]) < 1e-4
    assert seen_bg > 0


def test_single_los_phase_and_doppler_sign(client):
    _trackb_run(client, background={"mode": "h0_removed", "max_depth": 0})
    body = _export(
        client, include_echo_paths=True, include_background_paths=True,
        include_channel={"components": ["echo", "background_h0"]},
    ).json()
    zf = _zip(client, body)
    ch = _npz(zf, "channel.npz")
    background = _npz(zf, "background.npz")
    echoes = _npz(zf, "echoes.npz")
    f, t = ch["f_hz"], ch["t_s"]
    # Bistatic link, max_depth 0: H0 is the single LoS path.
    row = int(np.flatnonzero((ch["tx_id"] == "trp1") & (ch["rx_id"] == "trp3_rx"))[0])
    b = (background["hypothesis"] == "h0") & (background["tx_id"] == "trp1")
    b &= (background["rx_id"] == "trp3_rx") & (background["frame_index"] == 0)
    assert b.sum() == 1 and background["path_type"][b][0] == "los"
    tau = background["delay_ns"][b][0] * 1e-9
    expected = _wrap(-2 * np.pi * (F_C + f) * tau)
    assert np.max(np.abs(_wrap(np.angle(ch["H_background_h0"][row, 0]) - expected))) < 1e-5

    # Closing echo (f_D > 0): the phase advances by 2 pi f_D T_p per pilot symbol.
    k = int(np.argmax(echoes["doppler_hz"]))
    fd = echoes["doppler_hz"][k]
    assert fd > 0
    row = int(np.flatnonzero(
        (ch["frame_index"] == echoes["frame_index"][k]) & (ch["tx_id"] == echoes["tx_id"][k])
        & (ch["rx_id"] == echoes["rx_id"][k])
    )[0])
    H = ch["H_echo"][row].astype(np.complex128)
    tp = t[1] - t[0]
    step = np.angle(H[1] / H[0])
    assert np.max(np.abs(_wrap(step - 2 * np.pi * fd * tp))) < 1e-4
    Z = np.fft.fftshift(np.fft.fft(np.fft.ifft(H, axis=1), axis=0), axes=0)
    r, _ = np.unravel_index(np.argmax(np.abs(Z)), Z.shape)
    assert r - len(t) // 2 > 0


# ------------------------------------------------------------ noise


def _ref_noise(seed, frame, tx, rx, shape, sigma2):
    def id32(s):
        return int.from_bytes(hashlib.sha256(s.encode("utf-8")).digest()[:4], "little")

    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence(
        entropy=[seed, 0x5EA024], spawn_key=(0, frame, id32(tx), id32(rx))
    )))
    re = rng.standard_normal(shape)
    im = rng.standard_normal(shape)
    return (re + 1j * im) * math.sqrt(sigma2 / 2)


def test_noise_level_seed_and_in_run_equality(client):
    result = _trackb_run(client, noise_seed=17)
    det = result["metadata"]["sensing"]["detection"]
    body = _export(
        client, include_channel={"components": ["total"], "noise": {"enabled": True},
                                 "pilot_ls": True},
    ).json()
    zf = _zip(client, body)
    ch = _npz(zf, "channel.npz")
    noise, sigma2 = ch["noise"], ch["sigma_h2"]
    assert noise.dtype == np.complex64 and noise.shape == (64, 16, 256)
    assert noise.size >= 100_000
    ratio = float(np.mean(np.abs(noise.astype(np.complex128)) ** 2 / sigma2[:, None, None]))
    assert 0.98 <= ratio <= 1.02
    # sigma_h2 = N0 / Es = N_floor(B_occ) / P_tx; the run's own value.
    n_floor = -174 + 10 * math.log10(20e6) + 7.0
    assert np.allclose(10 * np.log10(sigma2), n_floor - 43.0, atol=1e-9)
    assert det["noise"]["sigma_h2_db"]["trp1"] == pytest.approx(n_floor - 43.0, abs=1e-9)
    manifest = json.loads(zf.read("manifest.json"))["channel"]
    assert manifest["noise"]["seed"] == {result["result_id"]: 17}
    assert manifest["noise"]["n0_dbm_per_re"][result["result_id"]] == pytest.approx(
        -174 + 10 * math.log10(15_625.0) + 7.0
    )
    # The pilot-grid noise of the run's waveform is the CFAR map's noise, bit for bit.
    for row in (0, 21, 63):
        i, tx, rx = int(ch["frame_index"][row]), str(ch["tx_id"][row]), str(ch["rx_id"][row])
        run_noise = channel_synthesis.noise_frame(17, i, tx, rx, (16, 256), float(sigma2[row]))
        assert np.array_equal(noise[row], run_noise.astype(np.complex64))
        assert np.array_equal(
            noise[row], _ref_noise(17, i, tx, rx, (16, 256), float(sigma2[row])).astype(np.complex64)
        )
    ls = ch["H_ls"].astype(np.complex128)
    assert np.allclose(ls, ch["H_total"] + noise.astype(np.complex128), rtol=1e-6, atol=1e-12)

    again = _npz(_zip(client, _export(
        client, include_channel={"components": ["total"], "noise": {"enabled": True}},
    ).json()), "channel.npz")
    assert np.array_equal(again["noise"], noise)
    assert np.array_equal(again["H_total"], ch["H_total"])
    other = _npz(_zip(client, _export(
        client, include_channel={"components": ["total"], "noise": {"enabled": True, "seed": 18}},
    ).json()), "channel.npz")
    assert not np.array_equal(other["noise"], noise)
    level = _npz(_zip(client, _export(
        client, include_channel={"components": ["echo"],
                                 "noise": {"enabled": True, "n0_dbm_per_re": -120.0}},
    ).json()), "channel.npz")
    es = 43.0 - 10 * math.log10(1280)
    assert np.allclose(level["sigma_h2"], 10 ** ((-120.0 - es) / 10), rtol=1e-12)


def test_max_elements_and_grid_checks(client):
    _trackb_run(client)
    before = set(_export_dir().glob("*.zip")) if _export_dir().exists() else set()
    resp = _export(client, include_channel={"components": ["echo", "total"], "max_elements": 1000})
    assert resp.status_code == 400
    assert "524288 complex64 elements" in resp.json()["detail"]
    assert "max_elements = 1000" in resp.json()["detail"]
    after = set(_export_dir().glob("*.zip")) if _export_dir().exists() else set()
    assert after == before
    # A run without a waveform resolves to Grid S at its own cpi_s; a 50 ms
    # run has another grid -> 400.
    _run(client, num_frames=2, cpi_pulses=4096)
    _run(client, num_frames=2, cpi_pulses=4096, cpi_s=0.05)
    resp = _export(client, include_channel={})
    assert resp.status_code == 400 and "different channel grids" in resp.json()["detail"]
    resp = _export(client, include_channel={"active": 1})
    assert resp.status_code == 422


def test_active_grid_and_default_waveform(client):
    plain = _run(client, num_frames=1, cpi_pulses=4096)
    body = _export(client, include_channel={"subcarriers": "active", "components": ["echo"]})
    assert body.status_code == 200, body.text
    zf = _zip(client, body.json())
    ch = _npz(zf, "channel.npz")
    assert ch["H_echo"].shape == (16, 144, 1280)
    meta = json.loads(zf.read("manifest.json"))["channel"]
    assert meta["waveform_source"] == {plain["result_id"]: "grid_s_default"}
    assert meta["subcarriers"] == "active"


# ------------------------------------------------------------ background rows


def test_background_rows_only_on_solved_frames(client):
    result = _trackb_run(client)
    rid = result["result_id"]
    body = _export(client, include_background_paths=True)
    assert body.status_code == 200, body.text
    body = body.json()
    assert body["files"] == ["manifest.json", "README.txt", "links.npz", "background.npz"]
    zf = _zip(client, body)
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["schema_version"] == 2
    # H0 solved once (static nodes), H1 every frame (the target moves).
    assert manifest["background_index"] == {rid: [[i, 0, i] for i in range(4)]}
    bg = _npz(zf, "background.npz")
    h0 = bg["hypothesis"] == "h0"
    assert set(bg["frame_index"][h0].tolist()) == {0}
    assert set(bg["frame_index"][~h0].tolist()) == {0, 1, 2, 3}
    frames = [f["sensing"] for f in result["frames"]]
    assert all(f.get("background_h0") is None for f in frames[1:])
    n0 = len(frames[0]["background_h0"])
    n1 = sum(len(f["background_h1"]) for f in frames)
    assert body["num_background_rows"] == n0 + n1 == len(bg["path_id"]) > 0
    assert int(h0.sum()) == n0
    frame0 = frames[0]
    for k, p in enumerate(frame0["background_h0"]):
        assert bg["path_id"][k] == p["path_id"] and bg["delay_ns"][k] == p["delay_ns"]
        assert bg["interaction_types"][k] == ";".join(i["type"] for i in p["interactions"])
        assert bg["a_re"][k] + 1j * bg["a_im"][k] == pytest.approx(
            10 ** (p["path_gain_db"] / 20) * np.exp(1j * p["phase_rad"])
        )
    assert "Track B" in zf.read("README.txt").decode()
    assert "background" in manifest["columns"] and "background" in manifest["tables"]


def test_export_keeps_one_frame_of_background_grids(client, monkeypatch):
    """A per-frame H1 grid is dropped once the next frame's H1 list arrives
    and the cached H0 is synthesized once per link: at most about two
    frames' worth of link grids are alive (the old cache kept every row's)."""
    import weakref

    from seam_studio.services import sensing_dataset_export

    result = _trackb_run(client)
    n_links = 16
    assert sum(
        1 for f in result["frames"] if f["sensing"].get("background_h1") is not None
    ) == 4  # H1 solved (target moving) and stored every frame
    real = sensing_dataset_export.synthesize_cfr
    refs, peak = [], [0]

    def counting(*args, **kwargs):
        out = real(*args, **kwargs)
        refs.append(weakref.ref(out))
        peak[0] = max(peak[0], sum(r() is not None for r in refs))
        return out

    monkeypatch.setattr(sensing_dataset_export, "synthesize_cfr", counting)
    resp = _export(client, include_channel={"components": ["background_h0", "background_h1"]})
    assert resp.status_code == 200, resp.text
    assert len(refs) == n_links + 4 * n_links  # H0 once per link, H1 per frame and link
    assert peak[0] <= 2 * n_links + 2, peak[0]


def test_unstored_h1_cannot_be_exported(client):
    """Default store "h0": H1 fed the CFAR map but is not in the result, so
    background_h1 / total / pilot_ls answer 400; echo and H0 export, and the
    background index has no H1 source."""
    result = _trackb_run(client, background={"mode": "both"})
    rid = result["result_id"]
    assert all("background_h1" not in f["sensing"] for f in result["frames"])
    for channel in ({"components": ["background_h1"]}, {"components": ["total"]},
                    {"components": ["echo"], "noise": {"enabled": True}, "pilot_ls": True}):
        resp = _export(client, include_channel=channel)
        assert resp.status_code == 400, resp.text
        assert f"result {rid} solved H1 but did not store it" in resp.json()["detail"]
        assert "store 'both'" in resp.json()["detail"]
    ok = _export(client, include_channel={"components": ["echo", "background_h0"]},
                 include_background_paths=True)
    assert ok.status_code == 200, ok.text
    manifest = json.loads(_zip(client, ok.json()).read("manifest.json"))
    assert manifest["background_index"] == {rid: [[i, 0, None] for i in range(4)]}
    h0_only = _trackb_run(client, background={"mode": "h0_removed", "store": "none"})
    resp = _export(client, result_ids=[h0_only["result_id"]],
                   include_channel={"components": ["total"]})
    assert resp.status_code == 400 and "solved H0 but did not store it" in resp.json()["detail"]
