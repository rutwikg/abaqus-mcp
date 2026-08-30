"""Spec-level remedies for failures the deck cannot fix.

Every rule in :mod:`fixes` patches a keyword deck. Some failures are not in the
deck at all: a negative Jacobian or an excessively distorted element is a
property of the *mesh*, and no amount of editing ``*STATIC`` will repair it. The
only real remedy is to mesh again, which means going back to the spec and
rebuilding through CAE.

That is a different loop shape -- build, run, and on a mesh failure rebuild
rather than re-run -- so it lives here, separate from the deck rules.

Refinement is deliberately the only remedy. A finer seed is a numerical choice
that converges towards the true solution, so it is safe to apply automatically.
Swapping the element type or order changes what is being modelled and stays a
decision for the author.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .report import JobReport

# Failures that mean "the mesh is wrong", not "the solver settings are wrong".
MESH_FAILURE_CATEGORIES = frozenset({
    "negative_jacobian",
    "excessive_distortion",
    "element_definition",
})

# Seed size as a fraction of the ORIGINAL spec value, not of the previous
# attempt. Compounding halvings reach absurd element counts in three steps;
# an explicit ladder keeps the worst case predictable.
_SIZE_LADDER = (0.5, 0.3, 0.2)


@dataclass
class MeshFix:
    """A spec-level repair, mirroring :class:`fixes.FixAction`."""

    rule: str
    description: str
    details: Dict[str, str] = field(default_factory=dict)
    caveat: Optional[str] = None


def mesh_failure_categories(report: Optional[JobReport]) -> List[str]:
    """Mesh-related categories present in ``report`` (any diagnostic level).

    Distortion is often reported as a warning while the error is the downstream
    convergence failure -- the same pattern as negative eigenvalues -- so this
    does not restrict itself to error level.
    """
    if report is None:
        return []
    return [c for c in report.categories if c in MESH_FAILURE_CATEGORIES]


def is_mesh_failure(report: Optional[JobReport]) -> bool:
    if report is None:
        return False
    return not report.succeeded and bool(mesh_failure_categories(report))


def refine_mesh(
    spec: Dict[str, Any], attempt: int
) -> Optional[Tuple[Dict[str, Any], MeshFix]]:
    """Return ``(patched_spec, MeshFix)`` with a finer seed, or None.

    None means the ladder is exhausted or the spec carries no usable mesh size,
    in which case the caller should stop rather than rebuild identically.
    """
    if attempt >= len(_SIZE_LADDER):
        return None

    mesh = spec.get("mesh") or {}
    original = mesh.get("size")
    try:
        original = float(original)
    except (TypeError, ValueError):
        return None
    if original <= 0:
        return None

    new_size = round(original * _SIZE_LADDER[attempt], 6)
    if new_size >= original:
        return None

    patched = json.loads(json.dumps(spec))  # deep copy; never mutate the caller's
    patched.setdefault("mesh", {})["size"] = new_size

    # 3D element count scales roughly with the inverse cube of the seed, so say
    # so plainly -- a 5x refinement is a ~125x mesh and the user should know
    # before the job is queued rather than after.
    growth = (original / new_size) ** 3
    return patched, MeshFix(
        rule="mesh_refinement",
        description=(
            "Re-meshed with seed size %g -> %g (%.0f%% of original) to repair "
            "distorted/inverted elements." % (original, new_size,
                                              100.0 * _SIZE_LADDER[attempt])
        ),
        details={
            "previous_size": "%g" % original,
            "new_size": "%g" % new_size,
            "approx_element_growth": "%.0fx" % growth,
        },
        caveat=(
            "Refinement multiplies the element count by roughly %.0fx in 3D, so "
            "solve time and memory grow accordingly. If the distortion is caused "
            "by the geometry itself (a sliver face or a near-zero-radius fillet), "
            "refining will not fix it -- clean up the CAD instead."
            % growth
        ),
    )
