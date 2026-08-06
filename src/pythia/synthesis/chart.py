"""Deterministic Vega-Lite v5 specs. Chart choice is a function of data shape, never a model's.

Two properties are load-bearing. First, **untrusted strings never become field references or
data keys** — a perfectly ordinary Greek column called ``Δείκτης [2021=100]`` is read by Vega
as a nested accessor and the chart renders empty with no error, and a hostile one is worse.
Data keys are the fixed synthetic ``dim``/``value``/``series``; human labels go only into
titles, which the runtime escapes. Second, ``validate_spec`` enforces that no expression-
bearing key reaches the browser, so "we do not emit expressions" is checked rather than
intended.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from config import Settings, get_settings

from pythia.synthesis.models import ChartKind, ChartSpec, CoercedKind, FactTable, Operation

_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"

#: Keys we are willing to emit. Everything outside this set is rejected outright rather than
#: filtered, so a future edit cannot quietly widen the surface.
_ALLOWED_KEYS = frozenset({
    "$schema", "title", "text", "subtitle", "description", "data", "values", "mark", "type",
    "point", "encoding", "x", "y", "color", "tooltip", "field", "axis", "scale", "sort",
    "zero", "domain", "legend", "width", "height", "config", "view", "stroke", "labelAngle",
    "labelLimit", "titleLimit", "format", "timeUnit", "interpolate", "strokeWidth",
    # `range` carries the categorical colour list. Vega-Lite accepts only scalars or arrays of
    # scalars here — never an expression — and the leaf-scalar check below still applies, so
    # allowing it does not widen the executable surface. Note that allowlisting `scale` alone
    # was NOT sufficient: the guard filters every key at every depth.
    "range",
})

#: Categorical series colours: Okabe-Ito, reordered so its orange (#e69f00) and yellow
#: (#f0e442) come last.
#:
#: Two rules meet here. Vega-Lite's default (tableau10) is not colourblind-safe, and the amber
#: UI accent means "actionable" — so a series painted amber-adjacent both misreads for
#: colourblind readers and quietly steals the accent's only job. Okabe-Ito fixes the first;
#: pushing its two warm entries past the point realistic series counts reach fixes the second.
_SERIES_COLORS: tuple[str, ...] = (
    "#0072b2",  # blue
    "#009e73",  # bluish green
    "#cc79a7",  # reddish purple
    "#56b4e9",  # sky blue
    "#d55e00",  # vermillion
    "#000000",  # black
    "#e69f00",  # orange — nearest the accent, so last but one
    "#f0e442",  # yellow — lowest contrast on white, so last
)

#: Keys that make a Vega-Lite document executable or network-reachable.
_FORBIDDEN_KEYS = frozenset({
    "url", "signal", "expr", "datasets", "params", "calculate", "transform", "selection",
    "on", "bind", "loader",
})


class UnsafeSpecError(Exception):
    """A generated spec contained something we refuse to hand to a browser."""


#: The only keys a plotted data row may carry. Fixed and synthetic, so no publisher-supplied
#: string ever becomes a field name.
_DATA_KEYS = frozenset({"dim", "value", "series"})


def validate_spec(spec: Any, *, _depth: int = 0) -> None:
    """Assert a spec is inert data: allowlisted keys, no expressions, scalar leaves."""
    if _depth > 12:
        raise UnsafeSpecError("spec nested too deeply")
    if isinstance(spec, dict):
        for key, value in spec.items():
            if not isinstance(key, str):
                raise UnsafeSpecError(f"non-string spec key: {key!r}")
            if key in _FORBIDDEN_KEYS:
                raise UnsafeSpecError(f"forbidden Vega-Lite key: {key!r}")
            if key not in _ALLOWED_KEYS:
                raise UnsafeSpecError(f"key not on the allowlist: {key!r}")
            if key == "values":
                _validate_rows(value)
            else:
                validate_spec(value, _depth=_depth + 1)
    elif isinstance(spec, list):
        for item in spec:
            validate_spec(item, _depth=_depth + 1)
    elif not isinstance(spec, str | int | float | bool | None):
        raise UnsafeSpecError(f"non-scalar leaf in spec: {type(spec).__name__}")


def _validate_rows(rows: Any) -> None:
    """Assert the inlined data is flat rows keyed only by the synthetic field names."""
    if not isinstance(rows, list):
        raise UnsafeSpecError("data.values must be a list of rows")
    for row in rows:
        if not isinstance(row, dict):
            raise UnsafeSpecError("data.values must hold flat objects")
        extra = set(row) - _DATA_KEYS
        if extra:
            raise UnsafeSpecError(f"data row carries non-synthetic keys: {sorted(extra)}")
        for value in row.values():
            if not isinstance(value, str | int | float | bool | None):
                raise UnsafeSpecError("data row values must be scalars")


def vega_type(kind: CoercedKind) -> str:
    """Map a *coerced* kind to a Vega-Lite encoding type.

    Deliberately not ``Column.type``: that is inferred from the first 200 uncoerced rows, so
    the Greek-comma measure ``86,6`` is ``text`` there. Encoding it as nominal makes the axis
    sort lexically — ``'100' < '86,6'`` — and scrambles the chart it was meant to draw.
    """
    if kind is CoercedKind.DECIMAL:
        return "quantitative"
    if kind is CoercedKind.TEMPORAL:
        return "temporal"
    return "nominal"


def build_spec(
    facts: FactTable, *, title: str, caveat: str | None = None, complete: bool = True,
    label: str = "", settings: Settings | None = None,
) -> ChartSpec | None:
    """Build a validated spec, or ``None`` when no chart would inform.

    A single figure is a sentence, not a chart, and a listing has nothing to plot.
    """
    cfg = settings or get_settings()
    if facts.operation is Operation.LISTING or not facts.series:
        return None
    if len(facts.series) < 2:
        return None

    temporal = facts.operation is Operation.NONE and facts.series_field is not None or (
        facts.operation is Operation.NONE and _looks_temporal(facts)
    )
    kind = ChartKind.LINE if temporal else ChartKind.BAR
    points = _bounded_points(facts, cfg)
    if points is None:
        return None

    encoding: dict[str, Any] = {
        "x": {
            "field": "dim",
            "type": "temporal" if kind is ChartKind.LINE else "nominal",
            "axis": {"title": label or (facts.dimension or ""), "labelLimit": 160},
            **({} if kind is ChartKind.LINE else {"sort": _sort_order(complete)}),
        },
        "y": {
            "field": "value",
            "type": "quantitative",
            "axis": {"title": facts.measure or ""},
            # A non-zero-based bar axis exaggerates differences; free to prevent.
            "scale": {"zero": kind is ChartKind.BAR},
        },
    }
    if facts.series_field:
        encoding["color"] = {"field": "series", "type": "nominal",
                             "legend": {"title": "", "labelLimit": 160},
                             "scale": {"range": list(_SERIES_COLORS)}}

    spec: dict[str, Any] = {
        "$schema": _SCHEMA,
        # Literal strings only. A signal or calculate here would be an expression-injection
        # vector, and the caveat is exactly the text an attacker would want to rewrite.
        "title": {"text": title, **({"subtitle": caveat} if caveat else {})},
        "description": title,
        "width": 640,
        "height": 320,
        "data": {"values": points},
        "mark": {"type": kind.value, **({"point": True} if kind is ChartKind.LINE else {})},
        "encoding": encoding,
    }
    validate_spec(spec)
    if len(json.dumps(spec, ensure_ascii=False)) > cfg.synthesis_chart_max_bytes:
        return None
    return ChartSpec(vega_lite=spec, kind=kind, title=title, caveat=caveat)


def _looks_temporal(facts: FactTable) -> bool:
    """Report whether the series' x values are ISO dates."""
    sample = str(facts.series[0].get("dim", ""))
    return len(sample) >= 7 and sample[:4].isdigit() and sample[4:5] == "-"


def _sort_order(complete: bool) -> Any:
    """Sort bars by value only when the table is complete.

    Ranking a truncated table is a superlative claim rendered visually: the categories that
    were never fetched read as absent rather than unknown.
    """
    return "-y" if complete else "x"


def _bounded_points(facts: FactTable, cfg: Settings) -> list[dict[str, Any]] | None:
    """Serialise the charted series, bounded by the configured point ceiling.

    Over the ceiling we decline rather than subsample: dropping every Nth point silently
    changes the shape of the series, which is the same class of failure as the row cap.
    """
    if len(facts.series) > cfg.synthesis_chart_max_points:
        return None
    points: list[dict[str, Any]] = []
    for row in facts.series:
        point: dict[str, Any] = {}
        for key in ("dim", "value", "series"):
            if key not in row:
                continue
            value = row[key]
            # float() only at the browser boundary, and never back into a Fact.
            point[key] = float(value) if isinstance(value, Decimal) else value
        points.append(point)
    return points
