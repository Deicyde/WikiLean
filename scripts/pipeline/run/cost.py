#!/usr/bin/env python3
"""Stage 10: what a run cost, from the agents' own envelopes.

docs/PIPELINE.md section 3 (stage 10). Every agent call leaves a JSON envelope under
``<tend root>/usage/``: the SDK's result object, with ``total_cost_usd``, ``duration_ms``
and the per-model token counts. The harvest brings them home, and this is the one place
that adds them up — the receipt's ``cost`` block and, through it, the tally's.

Two things it is careful about, both learned from a run that under-reported:

* **a call that left no priced envelope is not free.** ``agent_call`` records a call that
  timed out or was signalled with ``total_cost_usd: null`` and its elapsed time; those
  are counted as ``unpriced_calls`` and ``unpriced_seconds``, never dropped, and the
  dollar figure is then a **lower bound**, which :func:`render` says out loud.
* **the envelope count is the call count.** A model named in no price table is listed in
  ``unpriced``, and its tokens still appear in ``tokens``: a figure nobody could price is
  stated rather than left out.

Ported from ``cost/fleet.py``'s envelope basis and ``run_cost``/``tasks_of``. What
changes: the price table is optional and the envelope's own ``total_cost_usd`` is the
primary figure, since that is what the SDK already priced; no workspace is read, and the
task count comes from the run's own configuration.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

#: The basis a cost block was computed on.
BASIS_ENVELOPES = "envelopes"
#: The key a receipt carries its cost block under.
COST_KEY = "cost"


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def envelope_files(usage_dir: Path) -> list[Path]:
    """Every kept envelope under ``usage/``, sorted; an unreadable one is left out."""
    directory = Path(usage_dir)
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.rglob("*.json") if path.is_file())


def cost_block(usage_dir: Path, *, price_table_sha256: str = "") -> dict:
    """The ``cost`` block of a run, over the envelopes in ``usage_dir``.

    ``{basis, envelope_count, message_count, models, priced, unpriced, unpriced_calls,
    unpriced_seconds, tokens, total_tokens, usd, usd_by_model, price_table_sha256}``.
    """
    paths = envelope_files(usage_dir)
    usd = 0.0
    usd_by_model: dict[str, float] = {}
    tokens: dict[str, dict[str, int]] = {}
    unpriced_calls = 0
    unpriced_seconds = 0.0
    unreadable = 0
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable += 1
            continue
        if not isinstance(data, dict):
            unreadable += 1
            continue
        for model, row in (data.get("modelUsage") or {}).items():
            if not isinstance(row, dict):
                continue
            counts = tokens.setdefault(str(model), {"input": 0, "output": 0,
                                                    "cache_read": 0, "cache_write": 0})
            counts["input"] += int(row.get("inputTokens") or 0)
            counts["output"] += int(row.get("outputTokens") or 0)
            counts["cache_read"] += int(row.get("cacheReadInputTokens") or 0)
            counts["cache_write"] += int(row.get("cacheCreationInputTokens") or 0)
            cost = _number(row.get("costUSD"))
            if cost is not None:
                usd_by_model[str(model)] = round(usd_by_model.get(str(model), 0.0) + cost, 9)
        total = _number(data.get("total_cost_usd"))
        if total is not None:
            usd += total
            continue
        # a call that left no priced envelope: its seconds are recorded, never dropped
        unpriced_calls += 1
        seconds = _number((data.get("agent_call") or {}).get("elapsed_s")) \
            if isinstance(data.get("agent_call"), dict) else None
        if seconds is None:
            millis = _number(data.get("duration_ms"))
            seconds = millis / 1000.0 if millis is not None else None
        unpriced_seconds += max(0.0, seconds or 0.0)
    models = sorted(tokens)
    return {
        "basis": BASIS_ENVELOPES,
        "envelope_count": len(paths),
        "message_count": len(paths),
        "transcript_count": 0,
        "models": models,
        "priced": sorted(usd_by_model),
        "unpriced": [model for model in models if model not in usd_by_model],
        "unpriced_calls": unpriced_calls,
        "unpriced_seconds": round(unpriced_seconds, 1),
        "unreadable": unreadable,
        "tokens": {model: dict(sorted(counts.items())) for model, counts in sorted(tokens.items())},
        "total_tokens": {key: sum(counts.get(key, 0) for counts in tokens.values())
                         for key in ("cache_read", "cache_write", "input", "output")},
        "usd": round(usd, 6),
        "usd_by_model": {model: round(value, 6) for model, value in sorted(usd_by_model.items())},
        "price_table_sha256": price_table_sha256,
    }


def tasks_of(run_config: Mapping[str, Any] | None,
             receipt: Mapping[str, Any] | None) -> tuple[int | None, str]:
    """``(task count, where it came from)`` for a harvested run."""
    tasks = (run_config or {}).get("tasks")
    count = tasks.get("count") if isinstance(tasks, dict) else tasks
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return count, "run.yaml tasks.count"
    actual = ((receipt or {}).get("actual") or {}).get("count") if receipt else None
    if isinstance(actual, int) and actual > 0:
        return actual, "finding files (no run.yaml task count)"
    return None, "unknown"


def run_cost(run_id: str, receipt: Mapping[str, Any] | None,
             run_config: Mapping[str, Any] | None = None) -> dict | None:
    """The per-run summary the tally records, or None when the receipt has no cost block."""
    block = receipt.get(COST_KEY) if isinstance(receipt, Mapping) else None
    if not isinstance(block, Mapping):
        return None
    tasks, tasks_from = tasks_of(run_config, receipt)
    usd = float(block.get("usd") or 0.0)
    return {
        "run_id": run_id,
        "usd": usd,
        "tasks": tasks,
        "tasks_from": tasks_from,
        "usd_per_task": (round(usd / tasks, 4) if tasks else None),
        "models": sorted(block.get("tokens") or {}),
        "priced": list(block.get("priced") or []),
        "unpriced": list(block.get("unpriced") or []),
        "transcript_count": block.get("transcript_count"),
        "message_count": block.get("message_count"),
        "price_table_sha256": block.get("price_table_sha256"),
        "basis": block.get("basis") or BASIS_ENVELOPES,
        "envelope_count": block.get("envelope_count"),
        "unpriced_calls": block.get("unpriced_calls") or 0,
    }


def render(costs: Mapping[str, dict | None]) -> list[str]:
    """One line per run with a cost block, and one naming the runs without."""
    lines, missing = [], []
    for run_id in sorted(costs):
        entry = costs[run_id]
        if not entry:
            missing.append(run_id)
            continue
        per_task = (f"${entry['usd_per_task']:.4f} a task over {entry['tasks']} tasks, from "
                    f"{entry['tasks_from']}" if entry.get("usd_per_task") is not None
                    else "no task count to divide by")
        note = (f"; {entry['unpriced_calls']} call(s) left no priced envelope, so it is a "
                "lower bound" if entry.get("unpriced_calls") else "")
        lines.append(f"cost {run_id}: ${entry['usd']:.2f} ({per_task}; models "
                     f"{', '.join(entry['models']) or 'none'}; "
                     f"{entry.get('envelope_count') or 0} envelope(s){note})")
    if missing:
        lines.append(f"cost not recorded for run(s) {', '.join(missing)}: their harvest brought "
                     "home no agent envelopes")
    return lines


def runs_cost(receipts: Mapping[str, Mapping[str, Any] | None],
              configs: Mapping[str, Mapping[str, Any] | None] | None = None
              ) -> dict[str, dict | None]:
    """:func:`run_cost` for every run of a tally."""
    configs = configs or {}
    return {str(run_id): run_cost(str(run_id), receipt, configs.get(run_id))
            for run_id, receipt in receipts.items()}


def iter_envelope_names(names: Iterable[str]) -> list[str]:
    """The envelope names, sorted: used only to report what a usage directory holds."""
    return sorted(str(name) for name in names)
