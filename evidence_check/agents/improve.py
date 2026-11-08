"""Safe self-improvement: typed proposals, an evaluation gate in code, human approval, shadow mode, one-step rollback.

Where proposals come from
  - Systematic search over past outcomes (benchmark results and cases people closed): one small change at a time
    to a check weight or a threshold, kept only if it improves the objective. Search before creativity.
  - Reviewer agreement: a check whose reasons reviewers mostly dismiss gets a lower weight proposed.
  - Red-team evasions: variants that slipped through become new evaluation cases.

What a proposal can change (and nothing else)
  thresholds (review / investigate), per-check weights in [0, 2], enabling or disabling a check. These are exactly
  re-evaluable by re-scoring stored findings, so the gate's numbers are not estimates.

The gate rejects a proposal that lowers recall on ANY manipulation type, raises the false-alarm rate by more than
0.5 percentage points, or raises the worst segment's false-alarm rate by more than 1 point.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..models import Finding, Level
from ..policy import DomainPolicy, load_policy, score

KINDS = {"set_threshold", "set_check_weight", "enable_check", "disable_check", "add_eval_case"}


@dataclass
class Proposal:
    kind: str
    domain: str
    params: dict[str, Any]
    source: str  # search | reviewer_agreement | red_team | person
    rationale: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "proposed"  # proposed -> gated_pass|gated_fail -> shadow -> live | rejected | rolled_back
    gate: dict | None = None
    created: float = field(default_factory=time.time)
    history: list[dict] = field(default_factory=list)

    def validate(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown proposal kind {self.kind!r}")
        p = self.params
        if self.kind == "set_threshold":
            if p.get("level") not in ("review", "investigate") or not 0.05 <= float(p.get("value", -1)) <= 0.95:
                raise ValueError("set_threshold needs level review|investigate and value in [0.05, 0.95]")
        if self.kind == "set_check_weight" and (not p.get("check") or not 0.0 <= float(p.get("value", -1)) <= 2.0):
            raise ValueError("set_check_weight needs a check and a value in [0, 2]")
        if self.kind in ("enable_check", "disable_check") and not p.get("check"):
            raise ValueError(f"{self.kind} needs a check")


def apply(policy: DomainPolicy, overrides: dict) -> DomainPolicy:
    p = policy.model_copy(deep=True)
    for lv, v in overrides.get("thresholds", {}).items():
        setattr(p.thresholds, lv, v)
    p.check_weights.update(overrides.get("check_weights", {}))
    p.checks.update(overrides.get("checks", {}))
    if overrides:
        p.version = f"{policy.version}+{overrides.get('_version', 'x')}"
    return p


def with_proposal(overrides: dict, prop: Proposal) -> dict:
    o = json.loads(json.dumps(overrides))
    if prop.kind == "set_threshold":
        o.setdefault("thresholds", {})[prop.params["level"]] = float(prop.params["value"])
    elif prop.kind == "set_check_weight":
        o.setdefault("check_weights", {})[prop.params["check"]] = float(prop.params["value"])
    elif prop.kind in ("enable_check", "disable_check"):
        o.setdefault("checks", {})[prop.params["check"]] = prop.kind == "enable_check"
    return o


# -- evaluation on labelled rows ---------------------------------------------------
# A row: {"domain", "fraud", "manipulation", "findings": [finding dicts], "segment": {...}}

def evaluate_rows(rows: list[dict], policy: DomainPolicy) -> dict:
    by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    seg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    fa = n_clean = 0
    for r in rows:
        fs = [f for f in map(Finding.model_validate, r["findings"]) if policy.enabled(f.check)]
        flagged = score(fs, policy)[1] is not Level.CLEAR
        if r["fraud"]:
            t = by_type[r.get("manipulation") or "unknown"]
            t[0] += flagged
            t[1] += 1
        else:
            n_clean += 1
            fa += flagged
            for k, v in (r.get("segment") or {}).items():
                s = seg[f"{k}={v}"]
                s[0] += flagged
                s[1] += 1
    caught = sum(v[0] for v in by_type.values())
    n_fraud = sum(v[1] for v in by_type.values())
    return {"recall": caught / max(1, n_fraud), "false_alarm_rate": fa / max(1, n_clean),
            "recall_by_type": {k: v[0] / v[1] for k, v in by_type.items()},
            "worst_segment_fa": max((v[0] / v[1] for v in seg.values() if v[1] >= 5), default=0.0),
            "n_fraud": n_fraud, "n_clean": n_clean}


def gate(rows: list[dict], before: DomainPolicy, after: DomainPolicy, fa_tol: float = 0.005, seg_tol: float = 0.01) -> dict:
    a, b = evaluate_rows(rows, before), evaluate_rows(rows, after)
    reasons = []
    for t, r in a["recall_by_type"].items():
        if b["recall_by_type"].get(t, 0) < r - 1e-9:
            reasons.append(f"recall on '{t}' drops from {r:.0%} to {b['recall_by_type'].get(t, 0):.0%}")
    if b["false_alarm_rate"] > a["false_alarm_rate"] + fa_tol:
        reasons.append(f"false alarms rise from {a['false_alarm_rate']:.1%} to {b['false_alarm_rate']:.1%}")
    if b["worst_segment_fa"] > a["worst_segment_fa"] + seg_tol:
        reasons.append(f"worst segment's false alarms rise from {a['worst_segment_fa']:.1%} to {b['worst_segment_fa']:.1%}")
    if a["n_fraud"] == 0 or a["n_clean"] == 0:
        reasons.append("not enough labelled cases of both kinds to judge")
    return {"passed": not reasons, "reasons": reasons, "before": a, "after": b}


# -- proposal sources ----------------------------------------------------------------

def objective(m: dict) -> float:
    return m["recall"] - 3.0 * m["false_alarm_rate"]


def search(rows: list[dict], domain: str, base: DomainPolicy, overrides: dict | None = None, max_proposals: int = 5) -> list[Proposal]:
    """One-change-at-a-time search; returns the changes that improve the objective AND pass the gate, best first."""
    overrides = overrides or {}
    rows = [r for r in rows if r["domain"] == domain]
    current = apply(base, overrides)
    m0 = evaluate_rows(rows, current)
    checks = sorted({f["check"] for r in rows for f in r["findings"] if f["severity"] != "info"})
    candidates = [Proposal("set_threshold", domain, {"level": "review", "value": v}, "search")
                  for v in (0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5) if abs(v - current.thresholds.review) > 1e-9]
    for c in checks:
        w0 = current.check_weights.get(c, 1.0)
        candidates += [Proposal("set_check_weight", domain, {"check": c, "value": w}, "search")
                       for w in (0.5, 0.75, 1.25, 1.5, 2.0) if abs(w - w0) > 1e-9]
    scored = []
    for p in candidates:
        cand = apply(base, with_proposal(overrides, p))
        g = gate(rows, current, cand)
        gain = objective(g["after"]) - objective(m0)
        if g["passed"] and gain > 1e-9:
            p.gate, p.status = g, "gated_pass"
            p.rationale = (f"On {m0['n_fraud']} fraud and {m0['n_clean']} legitimate past cases: recall "
                           f"{m0['recall']:.1%} -> {g['after']['recall']:.1%}, false alarms {m0['false_alarm_rate']:.1%} -> "
                           f"{g['after']['false_alarm_rate']:.1%}; no manipulation type loses recall.")
            scored.append((gain, p))
    scored.sort(key=lambda gp: -gp[0])
    return [p for _, p in scored[:max_proposals]]


def from_reviewer_agreement(precision: dict[str, dict], domain: str, base: DomainPolicy, min_decisions: int = 10,
                            low: float = 0.3) -> list[Proposal]:
    out = []
    for check, s in precision.items():
        n = s["confirmed"] + s["dismissed"]
        if n >= min_decisions and s["precision"] is not None and s["precision"] < low:
            w = round(base.check_weights.get(check, 1.0) * 0.75, 2)
            out.append(Proposal("set_check_weight", domain, {"check": check, "value": w}, "reviewer_agreement",
                                rationale=f"Reviewers confirmed only {s['confirmed']} of {n} '{check}' reasons ({s['precision']:.0%})."))
    return out


# -- governance: versions, approval, shadow, rollback ----------------------------------

class PolicyStore:
    """Policy versions and proposals per domain, in <data>/policies.json. Base policies stay in the domain YAML."""

    def __init__(self, data_dir: str | Path, audit=None):
        self.path = Path(data_dir) / "policies.json"
        self.audit = audit or (lambda *a, **k: None)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {"domains": {}, "proposals": {}}

    def _save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, default=str))
        tmp.replace(self.path)

    def _dom(self, d: dict, domain: str) -> dict:
        return d["domains"].setdefault(domain, {"versions": [{"v": 0, "overrides": {}, "by": "base", "ts": time.time()}], "live": 0})

    def live_overrides(self, domain: str) -> dict:
        d = self._load()
        dom = self._dom(d, domain)
        o = next(v["overrides"] for v in dom["versions"] if v["v"] == dom["live"])
        return {**o, "_version": dom["live"]} if dom["live"] else o

    def policy(self, domain: str) -> DomainPolicy:
        return apply(load_policy(domain), self.live_overrides(domain))

    def shadow(self, domain: str) -> tuple[Proposal, DomainPolicy] | None:
        d = self._load()
        for p in d["proposals"].values():
            if p["domain"] == domain and p["status"] == "shadow":
                prop = Proposal(**p)
                return prop, apply(load_policy(domain), with_proposal(self.live_overrides(domain), prop))
        return None

    def submit(self, prop: Proposal, rows: list[dict] | None = None, actor: str = "agent") -> Proposal:
        prop.validate()
        with self._lock:
            d = self._load()
            if prop.kind != "add_eval_case" and rows is not None:
                base = load_policy(prop.domain)
                cur = self.live_overrides(prop.domain)
                prop.gate = gate([r for r in rows if r["domain"] == prop.domain], apply(base, cur), apply(base, with_proposal(cur, prop)))
                prop.status = "gated_pass" if prop.gate["passed"] else "gated_fail"
            prop.history.append({"ts": time.time(), "by": actor, "status": prop.status})
            d["proposals"][prop.id] = asdict(prop)
            self._save(d)
        self.audit(actor, "proposal", prop.id, {"kind": prop.kind, "params": prop.params, "status": prop.status})
        return prop

    def proposals(self, status: str | None = None) -> list[dict]:
        return [p for p in self._load()["proposals"].values() if status is None or p["status"] == status]

    def _transition(self, pid: str, to: str, actor: str, allowed_from: set[str]) -> dict:
        with self._lock:
            d = self._load()
            p = d["proposals"].get(pid)
            if not p:
                raise KeyError(pid)
            if p["status"] not in allowed_from:
                raise ValueError(f"proposal is {p['status']}; cannot move to {to}")
            if to == "shadow" and any(q["status"] == "shadow" and q["domain"] == p["domain"] for q in d["proposals"].values()):
                raise ValueError("another proposal is already in shadow for this domain")
            p["status"] = to
            p["history"].append({"ts": time.time(), "by": actor, "status": to})
            if to == "live":
                dom = self._dom(d, p["domain"])
                cur = next(v["overrides"] for v in dom["versions"] if v["v"] == dom["live"])
                new_v = max(v["v"] for v in dom["versions"]) + 1
                dom["versions"].append({"v": new_v, "overrides": with_proposal(cur, Proposal(**p)), "by": actor,
                                        "ts": time.time(), "proposal": pid, "previous": dom["live"]})
                dom["live"] = new_v
            self._save(d)
        self.audit(actor, f"proposal_{to}", pid, {"domain": p["domain"]})
        return p

    def approve_for_shadow(self, pid: str, actor: str) -> dict:
        """A person approves a gated proposal; it first runs in shadow next to the live policy."""
        return self._transition(pid, "shadow", actor, {"gated_pass"})

    def promote(self, pid: str, actor: str) -> dict:
        return self._transition(pid, "live", actor, {"shadow"})

    def reject(self, pid: str, actor: str) -> dict:
        return self._transition(pid, "rejected", actor, {"proposed", "gated_pass", "gated_fail", "shadow"})

    def rollback(self, domain: str, actor: str) -> int:
        """Restore the previous live version in one step."""
        with self._lock:
            d = self._load()
            dom = self._dom(d, domain)
            cur = next(v for v in dom["versions"] if v["v"] == dom["live"])
            if dom["live"] == 0:
                raise ValueError("already on the base policy")
            dom["live"] = cur.get("previous", 0)
            if cur.get("proposal") and cur["proposal"] in d["proposals"]:
                d["proposals"][cur["proposal"]]["status"] = "rolled_back"
            self._save(d)
        self.audit(actor, "policy_rollback", domain, {"to_version": dom["live"]})
        return dom["live"]
