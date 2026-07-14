"""In-process Mitsuba path-traced render of the compiled RF projection.

Sionna RT ships an interactive scene preview but no still-image file export;
this closes that gap. We load ``rf/generated_scene.xml`` (compiling on demand
via the sionna backend when it is missing), build a perspective sensor from the
request's camera parameters, path-trace the geometry, and write a PNG.

Coordinate system: the scene is authored Z-up (HANDOFF.md invariant), so the
camera ``to_world`` is built with ``look_at(origin, target, up=[0, 0, 1])`` —
Mitsuba's default camera up is +Y, so passing +Z here is what keeps a Z-up
world upright in the image.

Material handling: the compiled XML references Sionna RT's ``radio-material`` /
``itu-radio-material`` BSDF plugins, which are RF-only and (a) are not
registered for a plain ``mi.load_file`` and (b) carry no visual appearance. For
a *visual* still we rewrite those BSDFs to a neutral Mitsuba ``diffuse`` before
loading, so the render depends only on Mitsuba — no Sionna import, no GPU
required beyond Mitsuba's own variant.

Errors are typed so the API can map them: missing Mitsuba / Sionna ->
``RenderUnavailableError`` (409); an unreadable or empty scene ->
``RenderSceneError`` (400).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.render import RenderRequest
from seam_studio.schemas.scene import Scene

# Caps mirrored from the schema; enforced again here so a direct service call
# (not just a validated request) cannot ask for an unbounded render.
MAX_SPP = 256
MAX_WIDTH = 1920
MAX_HEIGHT = 1080

SCENE_XML_REL = "rf/generated_scene.xml"
RENDER_DIR_REL = "export/renders"

# Radio-material BSDF blocks -> neutral diffuse. Non-greedy so each <bsdf>..
# </bsdf> collapses independently; DOTALL so nested <float>/<rgb> children
# (which a diffuse would reject) are dropped.
_RADIO_BSDF_RE = re.compile(
    r'<bsdf\s+type="(?:radio-material|itu-radio-material)"([^>]*)>.*?</bsdf>',
    re.DOTALL,
)
_ID_RE = re.compile(r'id="([^"]+)"')
_EMITTER_RE = re.compile(r"<emitter\b", re.IGNORECASE)

# A neutral white ambient so an RF projection (which never carries lights)
# still renders visible geometry instead of an all-black frame. Injected only
# when the scene declares no emitter of its own.
_AMBIENT_EMITTER = (
    '<emitter type="constant"><rgb name="radiance" value="1.0 1.0 1.0"/></emitter>'
)


class RenderError(RuntimeError):
    """Base for render failures."""


class RenderUnavailableError(RenderError):
    """Mitsuba (or, when a compile is needed, Sionna) is not available -> 409."""


class RenderSceneError(RenderError):
    """The scene could not be compiled/loaded/rendered -> 400."""


def _neutralize_radio_materials(xml_text: str) -> str:
    """Replace RF-only BSDFs with a neutral diffuse so plain Mitsuba can load.

    Preserves each BSDF ``id`` so the ``<ref id=... name="bsdf"/>`` on the
    shapes still resolves.
    """

    def repl(match: re.Match[str]) -> str:
        attrs = match.group(1)
        id_match = _ID_RE.search(attrs)
        id_part = f' id="{id_match.group(1)}"' if id_match else ""
        return (
            f'<bsdf type="diffuse"{id_part}>'
            '<rgb name="reflectance" value="0.6 0.6 0.62"/>'
            "</bsdf>"
        )

    return _RADIO_BSDF_RE.sub(repl, xml_text)


def _ensure_ambient(xml_text: str) -> str:
    """Inject a constant ambient emitter when the scene has none.

    The compiled RF projection never contains lights; without an emitter the
    path integrator returns a black frame. If the author (or an imported scene)
    already provides an emitter we leave it untouched.
    """
    if _EMITTER_RE.search(xml_text):
        return xml_text
    if "</scene>" in xml_text:
        return xml_text.replace("</scene>", _AMBIENT_EMITTER + "</scene>", 1)
    return xml_text


def _import_mitsuba():
    """Import Mitsuba and ensure a variant that coexists with Sionna is active.

    Mitsuba's variant is process-global and the *first* module to select one
    dictates it for the whole process: Sionna RT only initializes when imported
    with no variant set (or a compatible ``*_ad_*`` one) and pins
    ``cuda_ad_mono_polarized``; conversely, forcing e.g. ``scalar_rgb`` first
    makes a later ``import sionna.rt`` fail. To let render and RF solves coexist
    in any order within one process we:

      1. reuse whatever variant is already active, else
      2. if Sionna RT is installed, import it so *it* pins its variant (which
         renders our film fine -- mono variants just emit a luminance PNG), else
      3. pick a Mitsuba variant directly (GPU -> LLVM -> scalar) so a box
         without Sionna still renders.

    Raises RenderUnavailableError when Mitsuba is not installed at all.
    """
    try:
        import mitsuba as mi  # type: ignore
    except Exception as exc:  # noqa: BLE001 - any import failure means unavailable
        raise RenderUnavailableError(
            f"mitsuba is not available for rendering: {exc}"
        ) from exc

    if mi.variant() is not None:
        return mi  # respect whatever variant the process already established

    from seam_studio.services.availability import sionna_available

    if sionna_available():
        try:
            import sionna.rt  # noqa: F401 - pins the process Mitsuba variant
        except Exception:  # noqa: BLE001 - fall through to a direct variant pick
            pass
        if mi.variant() is not None:
            return mi

    available = set(mi.variants())
    for variant in ("cuda_ad_rgb", "llvm_ad_rgb", "scalar_rgb"):
        if variant in available:
            try:
                mi.set_variant(variant)
                return mi
            except Exception:  # noqa: BLE001 - try the next fallback variant
                continue
    raise RenderUnavailableError(
        f"no usable mitsuba variant among {sorted(available)}"
    )


def _ensure_scene_xml(
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
) -> Path:
    """Return the compiled scene XML path, compiling on demand if missing.

    Compilation goes through the sionna backend's ``compile`` (which delegates
    to the rf_compiler) so we reuse the exact projection the solver would use.
    """
    xml_path = project_dir / SCENE_XML_REL
    if xml_path.is_file():
        return xml_path

    try:
        from seam_studio.services.simulation_backends import get_backend
    except Exception as exc:  # noqa: BLE001
        raise RenderUnavailableError(
            f"cannot import the sionna backend to compile the scene: {exc}"
        ) from exc

    backend = get_backend("sionna")
    result = backend.compile(project_dir, scene, library)
    if not result.ok or not xml_path.is_file():
        errors = "; ".join(result.errors) or "compile produced no scene xml"
        raise RenderSceneError(f"scene compile failed: {errors}")
    return xml_path


def _load_geometry(mi, xml_path: Path):
    """Load the compiled XML with RF BSDFs neutralized to diffuse.

    The rewritten XML is written next to the original so its relative
    ``meshes/*.ply`` filenames still resolve, then removed.
    """
    try:
        raw = xml_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RenderSceneError(f"cannot read scene xml: {exc}") from exc

    rewritten = _ensure_ambient(_neutralize_radio_materials(raw))
    tmp_xml = xml_path.with_name(f"_render_{_timestamp()}.xml")
    try:
        tmp_xml.write_text(rewritten, encoding="utf-8")
        try:
            base = mi.load_file(str(tmp_xml))
        except Exception as exc:  # noqa: BLE001 - malformed / unloadable scene
            raise RenderSceneError(f"mitsuba could not load the scene: {exc}") from exc
    finally:
        tmp_xml.unlink(missing_ok=True)
    return base


def _timestamp() -> str:
    # Filesystem-safe UTC stamp, e.g. 20260703T091530_123456
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")


def render_scene(
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    request: RenderRequest,
) -> Path:
    """Path-trace the compiled scene from the requested camera and write a PNG.

    Returns the absolute path to the written PNG under
    ``<project>/export/renders/<timestamp>.png``. Raises RenderUnavailableError
    (409) or RenderSceneError (400) on the documented failure modes.
    """
    spp = min(int(request.spp), MAX_SPP)
    width = min(int(request.width), MAX_WIDTH)
    height = min(int(request.height), MAX_HEIGHT)

    mi = _import_mitsuba()
    xml_path = _ensure_scene_xml(project_dir, scene, library)
    base = _load_geometry(mi, xml_path)

    origin = [float(v) for v in request.camera_position]
    target = [float(v) for v in request.look_at]
    if origin == target:
        raise RenderSceneError("camera_position and look_at must differ")

    # Z-up world: pass up=[0,0,1] so look_at orients the film upright.
    to_world = mi.ScalarTransform4f().look_at(
        origin=origin, target=target, up=[0.0, 0.0, 1.0]
    )
    sensor = mi.load_dict(
        {
            "type": "perspective",
            "fov": float(request.fov_deg),
            "fov_axis": "x",
            "to_world": to_world,
            "film": {
                "type": "hdrfilm",
                "width": width,
                "height": height,
                "pixel_format": "rgb",
                "rfilter": {"type": "gaussian"},
            },
            "sampler": {"type": "independent", "sample_count": spp},
        }
    )
    integrator = mi.load_dict({"type": "path", "max_depth": 8})

    try:
        image = mi.render(base, sensor=sensor, integrator=integrator, spp=spp)
    except Exception as exc:  # noqa: BLE001 - solver / device failures
        raise RenderSceneError(f"render failed: {exc}") from exc

    out_dir = project_dir / RENDER_DIR_REL
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_timestamp()}.png"

    # Tonemap the linear HDR film to 8-bit sRGB and write synchronously via
    # Bitmap. (mi.util.write_bitmap defers the job on the CUDA variant and can
    # fail to flush; the Bitmap route is synchronous and gamma-correct.)
    try:
        bitmap = mi.Bitmap(image).convert(
            mi.Bitmap.PixelFormat.RGB, mi.Struct.Type.UInt8, srgb_gamma=True
        )
        bitmap.write(str(out_path))
    except Exception as exc:  # noqa: BLE001
        raise RenderSceneError(f"could not write PNG: {exc}") from exc

    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise RenderSceneError("render produced no output file")
    return out_path
