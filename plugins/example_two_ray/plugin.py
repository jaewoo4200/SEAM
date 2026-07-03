"""Example plugin: the classic two-ray ground-reflection path-loss model.

Anatomy of a plugin (see README.md in this folder for the long version):

    * A plugin is a self-contained folder ``plugins/<name>/`` with a
      ``plugin.py`` that defines exactly one ``register(registry)`` function.
    * ``register`` receives a registry object and calls its ``register_*``
      hooks to add backends / path-loss models / AI providers / exporters.
    * The module must import with NO project dependency — only the standard
      library — so the loader can import it in isolation and a plugin failure
      stays contained. This file imports only ``math``.

This plugin registers ONE path-loss model, ``two_ray_ground``.

Two-ray ground reflection
-------------------------
A direct ray plus a ground-reflected ray. Beyond the crossover (breakpoint)
distance the two rays combine so power falls off as d^4 rather than d^2, giving
the well-known 40*log10(d) slope and independence from frequency:

    PL(dB) = 40*log10(d)
             - 10*log10(Gt) - 10*log10(Gr)
             - 20*log10(ht) - 20*log10(hr)

where ht, hr are the TX/RX heights (m) above the reflecting ground and Gt, Gr
the linear antenna gains (here 1.0 / 0 dBi — the model is antenna-agnostic and
callers fold in real gains separately). Below the crossover distance

    d_c = 4 * pi * ht * hr * f / c

the ground-reflection lobe structure is not yet in the d^4 regime, so the model
is not valid there; we fall back to free-space path loss (FSPL) and flag it in
``notes`` with ``valid=False`` so a UI can grey the point out.
"""

from __future__ import annotations

import math

SPEED_OF_LIGHT = 299_792_458.0

# Antenna gains are handled by the link budget elsewhere, so this pure
# propagation model uses unity (0 dBi) gains. Exposed as constants so the
# formula reads cleanly and a fork can override them.
G_TX_LINEAR = 1.0
G_RX_LINEAR = 1.0


def _fspl_db(freq_hz: float, dist_m: float) -> float:
    """Free-space path loss 20*log10(4*pi*d*f/c); distance floored at 1 m."""
    d = max(dist_m, 1.0)
    return 20.0 * math.log10(4.0 * math.pi * d * freq_hz / SPEED_OF_LIGHT)


def _positions(obj: object) -> list[float]:
    """Best-effort 3D position from a device-like object or a raw sequence.

    Accepts anything with a ``.position`` attribute (our Device schema), a
    plain ``(x, y, z)`` sequence, or a mapping with a ``position`` key, so the
    model works whether the core passes Device objects, dicts, or tuples.
    Missing/short inputs default to the origin.
    """
    pos = getattr(obj, "position", None)
    if pos is None and isinstance(obj, dict):
        pos = obj.get("position")
    if pos is None:
        pos = obj
    try:
        seq = list(pos)  # type: ignore[arg-type]
    except TypeError:
        return [0.0, 0.0, 0.0]
    seq = [float(v) for v in seq[:3]]
    while len(seq) < 3:
        seq.append(0.0)
    return seq


def two_ray_ground(freq_hz: float, tx: object, rx: object, config: object) -> dict:
    """Two-ray ground-reflection path loss for one TX->RX link.

    Signature matches the ``register_path_loss_model`` contract:
    ``(freq_hz, tx, rx, config) -> {path_loss_db, valid, notes}``. ``tx``/``rx``
    are the link endpoints (Device-like: a ``.position`` [x, y, z] in meters,
    Z-up); ``config`` is unused here (heights and distance come from geometry).

    Returns a dict with:
        path_loss_db : float  — always populated (never NaN/inf)
        valid        : bool   — False in the near field (below crossover)
        notes        : str    — human-readable validity / fallback note
    """
    tx_pos = _positions(tx)
    rx_pos = _positions(rx)
    dist_3d = math.dist(tx_pos, rx_pos)
    d = max(dist_3d, 1.0)

    # Heights above the ground plane (Z-up canonical frame). Floored to a small
    # positive value so a device sitting exactly on the ground does not send the
    # log terms to -inf.
    ht = max(tx_pos[2], 0.1)
    hr = max(rx_pos[2], 0.1)

    # Crossover / breakpoint distance: below it the model is not in the d^4
    # far-field regime, so we fall back to FSPL and mark the point invalid.
    d_cross = 4.0 * math.pi * ht * hr * freq_hz / SPEED_OF_LIGHT

    if d < d_cross:
        return {
            "path_loss_db": round(_fspl_db(freq_hz, d), 4),
            "valid": False,
            "notes": (
                f"d={d:.1f} m below two-ray crossover d_c={d_cross:.1f} m; "
                "fell back to FSPL (near-field lobe structure not modeled)"
            ),
        }

    # Far-field two-ray: PL = 40log10(d) - 10log10(Gt Gr) - 20log10(ht hr).
    pl = (
        40.0 * math.log10(d)
        - 10.0 * math.log10(G_TX_LINEAR)
        - 10.0 * math.log10(G_RX_LINEAR)
        - 20.0 * math.log10(ht)
        - 20.0 * math.log10(hr)
    )
    return {
        "path_loss_db": round(pl, 4),
        "valid": True,
        "notes": (
            f"two-ray ground (d^4 regime); crossover d_c={d_cross:.1f} m, "
            f"ht={ht:.2f} m, hr={hr:.2f} m"
        ),
    }


def register(registry) -> None:
    """Entry point the plugin loader calls with the shared registry.

    Adds the ``two_ray_ground`` path-loss model. A plugin may make any number
    of ``register_*`` calls here; each is validated by the registry.
    """
    registry.register_path_loss_model("two_ray_ground", two_ray_ground)
