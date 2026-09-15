"""Unattended parametric sweeps with a parked-failure queue.

A sweep of explicit crash runs is 12-36 hours of solver time. That does not fit
inside an MCP tool call (clients time out) and it does not fit inside one
interactive session either. So the sweep runs as a detached process that:

  * applies the deterministic fix rules (``loop.autocorrect_run``),
  * applies the physical-validity gate to anything that converged,
  * and **parks** whatever it cannot resolve, with the full diagnostic bundle
    an analyst would need to reason about it.

Nothing is ever silently dropped. Every design ends in exactly one of:

  ``valid``      converged and the energy balance is believable
  ``parked``     needs judgement -- awaiting a model, with diagnostics attached
  ``abandoned``  given up on, with a recorded human-readable reason

Parked designs are drained later: a model reads the bundle, proposes a deck
edit, and the fix is applied and re-run. That keeps the reasoning auditable --
the record of what failed, what was diagnosed, what changed, and whether it
then converged *is* the product.

State is written incrementally and atomically, so the sweep survives a crash,
a reboot or a closed client, and re-running it resumes rather than restarting.
"""

from __future__ import annotations

import csv
import json
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import CONFIG, AbaqusConfig
from .loop import LoopResult, autocorrect_run
from .report import JobReport
from .results import extract_results
from .validity import ValidityThresholds, ValidityVerdict, assess

STATUS_VALID = "valid"
STATUS_PARKED = "parked"
STATUS_ABANDONED = "abandoned"
TERMINAL = (STATUS_VALID, STATUS_ABANDONED)


@dataclass
class Design:
    """One point in the sweep."""

    design_id: str
    params: Dict[str, Any] = field(default_factory=dict)
    inp: Optional[str] = None              # a deck to run...
    spec: Optional[Dict[str, Any]] = None  # ...or a spec to build one from


@dataclass
class DesignOutcome:
    design_id: str
    status: str
    params: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    fixes: List[str] = field(default_factory=list)
    elapsed_s: float = 0.0


def sweep_dir(name: str, cfg: AbaqusConfig = CONFIG) -> Path:
    d = cfg.runs_dir / "_sweeps" / name
    (d / "parked").mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def _state_path(name: str, cfg: AbaqusConfig) -> Path:
    return sweep_dir(name, cfg) / "state.json"


def load_state(name: str, cfg: AbaqusConfig = CONFIG) -> Dict[str, DesignOutcome]:
    p = _state_path(name, cfg)
    if not p.is_file():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {k: DesignOutcome(**v) for k, v in raw.items()}


def save_state(name: str, state: Dict[str, DesignOutcome],
               cfg: AbaqusConfig = CONFIG) -> None:
    p = _state_path(name, cfg)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: asdict(v) for k, v in state.items()}, indent=2),
                   encoding="utf-8")
    tmp.replace(p)  # atomic: a crash mid-write must not destroy the sweep


# --------------------------------------------------------------------------
# the diagnostic bundle -- what a model reasons over
# --------------------------------------------------------------------------

def _tail(path: Path, n: int) -> str:
    if not path.is_file():
        return ""
    return "\n".join(path.read_text(errors="replace").splitlines()[-n:])


def build_bundle(
    design: Design,
    loop_result: Optional[LoopResult],
    verdict: Optional[ValidityVerdict],
    cfg: AbaqusConfig = CONFIG,
) -> Dict[str, Any]:
    """Everything needed to diagnose this design without the solver to hand.

    Deliberately includes the fixes already attempted: without that a model
    re-proposes the edit that just failed and the loop oscillates.
    """
    job_dir = cfg.runs_dir / design.design_id
    report: Optional[JobReport] = loop_result.final_report if loop_result else None

    bundle: Dict[str, Any] = {
        "design_id": design.design_id,
        "params": design.params,
        "spec": design.spec,
        "job_dir": str(job_dir),
        "solver_status": report.status.value if report else "not run",
        "succeeded": bool(report.succeeded) if report else False,
    }

    if report is not None:
        bundle["error_categories"] = list(report.error_categories)
        bundle["categories"] = list(report.categories)
        bundle["errors"] = [
            {"category": d.category, "text": d.text[:400],
             "node": d.node, "element": d.element, "dof": d.dof}
            for d in report.errors[:20]
        ]

    bundle["sta_tail"] = _tail(job_dir / (design.design_id + ".sta"), 25)
    bundle["msg_tail"] = _tail(job_dir / (design.design_id + ".msg"), 80)

    dat = job_dir / (design.design_id + ".dat")
    if dat.is_file():
        # Only the input-processor errors matter here, and .dat is enormous.
        lines = dat.read_text(errors="replace").splitlines()
        errs = [ln for ln in lines if "ERROR" in ln.upper()]
        bundle["dat_errors"] = "\n".join(errs[:40])

    deck = job_dir / (design.design_id + ".inp")
    if deck.is_file():
        bundle["deck"] = deck.read_text(errors="replace")[:20000]
        bundle["deck_path"] = str(deck)

    if loop_result is not None:
        bundle["attempts"] = [
            {"iteration": a.iteration, "status": a.status,
             "fix": a.fix.description if a.fix else None}
            for a in loop_result.attempts
        ]
        bundle["fixes_already_tried"] = [
            a.fix.rule for a in loop_result.attempts if a.fix
        ]
        bundle["stopped_reason"] = loop_result.stopped_reason
        bundle["caveats"] = loop_result.caveats

    if verdict is not None:
        bundle["validity"] = {
            "valid": verdict.valid,
            "failures": verdict.failures,
            "checks": verdict.checks,
            "metrics": verdict.metrics,
            "remedy": verdict.remedy,
        }
    return bundle


def park(name: str, design: Design, bundle: Dict[str, Any], why: str,
         cfg: AbaqusConfig = CONFIG) -> Path:
    bundle = dict(bundle)
    bundle["parked_because"] = why
    bundle["parked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    p = sweep_dir(name, cfg) / "parked" / (design.design_id + ".json")
    p.write_text(json.dumps(bundle, indent=2, default=str), encoding="utf-8")
    return p


def unpark(name: str, design_id: str, cfg: AbaqusConfig = CONFIG) -> bool:
    p = sweep_dir(name, cfg) / "parked" / (design_id + ".json")
    if p.is_file():
        p.unlink()
        return True
    return False


def list_parked(name: str, cfg: AbaqusConfig = CONFIG) -> List[Dict[str, Any]]:
    """Compact index of what is waiting for judgement."""
    out = []
    for p in sorted((sweep_dir(name, cfg) / "parked").glob("*.json")):
        try:
            b = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "design_id": b.get("design_id", p.stem),
            "parked_because": b.get("parked_because", ""),
            "solver_status": b.get("solver_status"),
            "error_categories": b.get("error_categories", []),
            "validity_failures": (b.get("validity") or {}).get("failures", []),
            "fixes_already_tried": b.get("fixes_already_tried", []),
        })
    return out


def get_parked(name: str, design_id: str,
               cfg: AbaqusConfig = CONFIG) -> Optional[Dict[str, Any]]:
    p = sweep_dir(name, cfg) / "parked" / (design_id + ".json")
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------

def evaluate_design(
    design: Design,
    max_iters: int = 5,
    cpus: int = 4,
    timeout_s: int = 7200,
    quasi_static: bool = False,
    thresholds: ValidityThresholds = ValidityThresholds(),
    metrics_fn: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    cfg: AbaqusConfig = CONFIG,
) -> Tuple[DesignOutcome, Optional[Dict[str, Any]]]:
    """Run one design. Returns ``(outcome, bundle_or_None)``.

    A bundle comes back whenever the design needs judgement -- whether because
    it failed outright, or because it converged to something not believable.
    """
    t0 = time.time()
    if not design.inp:
        raise ValueError("design %s has no .inp (build it from its spec first)"
                         % design.design_id)

    loop = autocorrect_run(design.inp, job_name=design.design_id,
                           max_iters=max_iters, cpus=cpus,
                           timeout_s=timeout_s, cfg=cfg)
    fixes = [a.fix.description for a in loop.attempts if a.fix]
    elapsed = round(time.time() - t0, 1)

    if not loop.succeeded:
        out = DesignOutcome(design.design_id, STATUS_PARKED, design.params,
                            reason="did not converge: %s" % loop.stopped_reason,
                            attempts=len(loop.attempts), fixes=fixes,
                            elapsed_s=elapsed)
        return out, build_bundle(design, loop, None, cfg)

    # Converged. That is not the same as correct.
    res = extract_results(design.design_id, cfg=cfg)
    verdict = assess(res, thresholds=thresholds, quasi_static=quasi_static)

    metrics: Dict[str, Any] = dict(verdict.metrics)
    if metrics_fn is not None:
        try:
            metrics.update(metrics_fn(design.design_id, res) or {})
        except Exception as exc:
            metrics["metrics_error"] = str(exc)

    if not verdict.valid:
        out = DesignOutcome(design.design_id, STATUS_PARKED, design.params,
                            reason="converged but rejected: %s"
                                   % ", ".join(verdict.failures),
                            metrics=metrics, attempts=len(loop.attempts),
                            fixes=fixes, elapsed_s=elapsed)
        return out, build_bundle(design, loop, verdict, cfg)

    out = DesignOutcome(design.design_id, STATUS_VALID, design.params,
                        reason="; ".join(verdict.checks) or "converged",
                        metrics=metrics, attempts=len(loop.attempts),
                        fixes=fixes, elapsed_s=elapsed)
    return out, None


def run_sweep(
    name: str,
    designs: List[Design],
    build_fn: Optional[Callable[[Design], Optional[str]]] = None,
    resume: bool = True,
    cfg: AbaqusConfig = CONFIG,
    **eval_kwargs: Any,
) -> Dict[str, DesignOutcome]:
    """Run every design, parking whatever needs judgement. Resumable.

    ``build_fn`` turns a Design into an .inp path (e.g. via the CAE builder).
    It may return None for an unbuildable design, which is then *abandoned
    with a reason* rather than skipped silently.
    """
    state = load_state(name, cfg) if resume else {}
    log = sweep_dir(name, cfg) / "sweep.log"

    def note(msg: str) -> None:
        line = "%s  %s" % (time.strftime("%H:%M:%S"), msg)
        with log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    done = sum(1 for o in state.values() if o.status in TERMINAL)
    note("sweep '%s': %d designs, %d already terminal" % (name, len(designs), done))

    for d in designs:
        prev = state.get(d.design_id)
        if prev is not None and prev.status in TERMINAL:
            continue
        try:
            if d.inp is None and build_fn is not None:
                d.inp = build_fn(d)
            if d.inp is None:
                state[d.design_id] = DesignOutcome(
                    d.design_id, STATUS_ABANDONED, d.params,
                    reason="could not be built into a deck")
                note("%s ABANDONED (no deck)" % d.design_id)
                save_state(name, state, cfg)
                continue

            outcome, bundle = evaluate_design(d, cfg=cfg, **eval_kwargs)
            if bundle is not None:
                park(name, d, bundle, outcome.reason, cfg)
            else:
                unpark(name, d.design_id, cfg)
            state[d.design_id] = outcome
            note("%s %s (%s) in %.0fs" % (d.design_id, outcome.status.upper(),
                                          outcome.reason[:80], outcome.elapsed_s))
        except Exception as exc:
            # An unexpected crash must still leave a record, not a gap.
            state[d.design_id] = DesignOutcome(
                d.design_id, STATUS_PARKED, d.params,
                reason="sweep error: %s" % exc)
            park(name, d, {"design_id": d.design_id, "params": d.params,
                           "traceback": traceback.format_exc()},
                 "unhandled error in the sweep runner", cfg)
            note("%s ERROR: %s" % (d.design_id, exc))
        save_state(name, state, cfg)

    write_table(name, state, cfg)
    note("sweep '%s' finished: %s" % (name, summarise(state)))
    return state


def summarise(state: Dict[str, DesignOutcome]) -> str:
    counts: Dict[str, int] = {}
    for o in state.values():
        counts[o.status] = counts.get(o.status, 0) + 1
    return ", ".join("%d %s" % (v, k) for k, v in sorted(counts.items())) or "empty"


def write_table(name: str, state: Dict[str, DesignOutcome],
                cfg: AbaqusConfig = CONFIG) -> Path:
    """Machine-readable sweep table.

    Parked and abandoned rows are included on purpose: a table containing only
    the successes is exactly the sanitised record this design is meant to
    prevent.
    """
    rows = [asdict(o) for o in state.values()]
    param_keys: List[str] = []
    metric_keys: List[str] = []
    for r in rows:
        for k in r["params"]:
            if k not in param_keys:
                param_keys.append(k)
        for k in r["metrics"]:
            if k not in metric_keys:
                metric_keys.append(k)
    base = ["design_id", "status", "reason", "attempts", "elapsed_s"]
    cols = base + param_keys + metric_keys

    p = sweep_dir(name, cfg) / "results.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in sorted(rows, key=lambda x: x["design_id"]):
            row = [r.get(c, "") for c in base]
            row += [r["params"].get(c, "") for c in param_keys]
            row += [r["metrics"].get(c, "") for c in metric_keys]
            w.writerow(row)
    return p


# --------------------------------------------------------------------------
# draining the queue -- where a model's reasoning lands
# --------------------------------------------------------------------------

# Edits that would fake success rather than achieve it. A model under pressure
# to make a job pass can "fix" it by deleting the output it is judged on, or by
# loosening the criterion. Those edits are refused, not merely discouraged.
_PROTECTED = ("*OUTPUT", "*ENERGY OUTPUT", "*NODE OUTPUT", "*ELEMENT OUTPUT",
              "*HISTORY OUTPUT", "*FIELD OUTPUT")


class RefusedEdit(Exception):
    """A proposed edit would destroy the evidence rather than fix the model."""


def _check_edits(before: str, after: str) -> None:
    for kw in _PROTECTED:
        if before.upper().count(kw) > after.upper().count(kw):
            raise RefusedEdit(
                "edit removes a %s request. A run is judged on its output; "
                "deleting the output is not a fix." % kw)
    if len(after) < 0.5 * len(before):
        raise RefusedEdit(
            "edit removes more than half the deck (%d -> %d chars); that is a "
            "rewrite, not a repair." % (len(before), len(after)))


def apply_reasoned_fix(
    name: str,
    design_id: str,
    edits: List[Dict[str, str]],
    rationale: str,
    cfg: AbaqusConfig = CONFIG,
    **eval_kwargs: Any,
) -> Dict[str, Any]:
    """Apply a model-proposed deck edit to a parked design and re-run it.

    ``edits`` is a list of ``{"find": ..., "replace": ...}``. Literal
    find/replace rather than a wholesale rewrite, because the point of this
    whole exercise is a record of *what changed and why* -- and a diff of two
    20 kB decks is not that record.

    Every attempt is appended to the fix log whether it worked or not.
    """
    bundle = get_parked(name, design_id, cfg)
    if bundle is None:
        raise ValueError("no parked design %r in sweep %r" % (design_id, name))
    deck_path = Path(bundle.get("deck_path") or "")
    if not deck_path.is_file():
        raise ValueError("parked design %r has no deck on disk" % design_id)

    before = deck_path.read_text(errors="replace")
    after = before
    applied: List[str] = []
    for i, e in enumerate(edits):
        find, repl = e.get("find", ""), e.get("replace", "")
        if not find:
            raise ValueError("edit %d has no 'find'" % i)
        n = after.count(find)
        if n == 0:
            raise ValueError("edit %d: %r does not appear in the deck" % (i, find[:80]))
        after = after.replace(find, repl)
        applied.append("%r -> %r (%d occurrence%s)"
                       % (find[:60], repl[:60], n, "" if n == 1 else "s"))

    _check_edits(before, after)

    # Snapshot the pre-fix deck so the change is inspectable afterwards.
    snap = deck_path.with_suffix(".prefix.inp")
    if not snap.exists():
        snap.write_text(before, encoding="utf-8")
    deck_path.write_text(after, encoding="utf-8")

    design = Design(design_id=design_id,
                    params=bundle.get("params") or {},
                    inp=str(deck_path),
                    spec=bundle.get("spec"))
    outcome, new_bundle = evaluate_design(design, cfg=cfg, **eval_kwargs)

    state = load_state(name, cfg)
    state[design_id] = outcome
    save_state(name, state, cfg)

    if new_bundle is not None:
        park(name, design, new_bundle, outcome.reason, cfg)
    else:
        unpark(name, design_id, cfg)

    entry = {
        "design_id": design_id,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "was": bundle.get("parked_because", ""),
        "rationale": rationale,
        "edits": applied,
        "outcome": outcome.status,
        "reason": outcome.reason,
    }
    with (sweep_dir(name, cfg) / "fixlog.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def fix_log(name: str, cfg: AbaqusConfig = CONFIG) -> List[Dict[str, Any]]:
    """Every reasoned fix attempted, successful or not.

    This is the narrative artefact: what failed, what was diagnosed, what
    changed, and whether it then converged.
    """
    p = sweep_dir(name, cfg) / "fixlog.jsonl"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out
