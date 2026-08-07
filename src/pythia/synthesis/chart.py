"""Deterministic Apache ECharts options. Chart choice is a function of data shape, never a
model's.

Three properties are load-bearing.

**Untrusted strings never become field references or data keys.** Series values are plain
arrays and category labels are plain strings; nothing publisher-supplied is ever interpreted
as an accessor. Human labels go only into titles, names and legend entries, which the runtime
escapes.

**No option we emit can carry executable code.** ECharts accepts JavaScript functions for
``formatter``, ``renderItem`` and friends. Those cannot survive JSON transport — the option
ships inside a ``<script type="application/json">`` and is parsed, never evaluated — but
``validate_spec`` refuses them by name anyway, so the guarantee does not rest on the transport
staying JSON forever.

**Ordering is decided in Python, not by the renderer.** Bars are sorted here when the table is
complete, so what determines the ranking is inspectable and testable rather than a renderer
flag (ADR-0008 amendment).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from config import Settings, get_settings

from pythia.synthesis.models import ChartKind, ChartSpec, CoercedKind, FactTable, Operation

#: Keys we are willing to emit. Everything outside this set is rejected outright rather than
#: filtered, so a future edit cannot quietly widen the surface. Derived from the option this
#: module actually builds — if you add an option key, add it here deliberately.
_ALLOWED_KEYS = frozenset({
    # top level
    "animation", "backgroundColor", "color", "aria", "title", "grid", "tooltip", "legend",
    "xAxis", "yAxis", "series",
    # title
    "text", "subtext", "left", "top",
    # aria / accessibility
    "enabled", "decal", "show", "description",
    # grid
    "right", "bottom", "containLabel",
    # tooltip / legend
    "trigger", "orient",
    # axes
    "type", "name", "nameLocation", "nameGap", "data", "axisLabel", "hideOverlap",
    "boundaryGap", "splitLine", "lineStyle",
    # series
    "barMaxWidth", "showSymbol", "symbolSize", "smooth",
})

#: Keys that would make an ECharts option executable, network-reachable, or otherwise
#: interpreted rather than displayed. None of these can survive JSON, but the guard names them
#: so the guarantee does not depend on the transport.
_FORBIDDEN_KEYS = frozenset({
    "formatter", "valueFormatter", "labelFormatter", "renderItem", "tooltipFormatter",
    "url", "expr", "signal", "on", "bind", "loader", "transform", "dataset", "function",
    "callback", "onclick", "handler", "graphic", "media", "toolbox",
})

#: Categorical series colours: Okabe-Ito, reordered so its orange (#e69f00) and yellow
#: (#f0e442) come last.
#:
#: Two rules meet here. A renderer's default palette is not colourblind-safe, and the amber UI
#: accent means "actionable" — so a series painted amber-adjacent both misreads for colourblind
#: readers and quietly steals the accent's only job. Okabe-Ito fixes the first; pushing its two
#: warm entries past the point realistic series counts reach fixes the second. ECharts' decal
#: patterns (enabled below) add a second, non-colour channel on top.
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


class UnsafeSpecError(Exception):
    """A generated option contained something we refuse to hand to a browser."""


def validate_spec(spec: Any, *, _depth: int = 0) -> None:
    """Assert an option is inert data: allowlisted keys, no executable keys, scalar leaves."""
    if _depth > 12:
        raise UnsafeSpecError("spec nested too deeply")
    if isinstance(spec, dict):
        for key, value in spec.items():
            if not isinstance(key, str):
                raise UnsafeSpecError(f"non-string spec key: {key!r}")
            if key in _FORBIDDEN_KEYS:
                raise UnsafeSpecError(f"forbidden ECharts key: {key!r}")
            if key not in _ALLOWED_KEYS:
                raise UnsafeSpecError(f"key not on the allowlist: {key!r}")
            if key == "data":
                _validate_data(value)
            else:
                validate_spec(value, _depth=_depth + 1)
    elif isinstance(spec, list):
        for item in spec:
            validate_spec(item, _depth=_depth + 1)
    elif not isinstance(spec, str | int | float | bool | None):
        raise UnsafeSpecError(f"non-scalar leaf in spec: {type(spec).__name__}")


def _validate_data(rows: Any) -> None:
    """Assert plotted data is scalars, or ``[x, y]`` scalar pairs, and nothing else.

    This is where publisher-controlled values land, so it is the strictest check in the
    module: no objects, no nesting beyond a pair, no callables.
    """
    if not isinstance(rows, list):
        raise UnsafeSpecError("data must be a list")
    for row in rows:
        if isinstance(row, list):
            if len(row) != 2:
                raise UnsafeSpecError("a data pair must hold exactly [x, y]")
            for value in row:
                if not isinstance(value, str | int | float | bool | None):
                    raise UnsafeSpecError("data pair values must be scalars")
        elif not isinstance(row, str | int | float | bool | None):
            raise UnsafeSpecError(f"data must hold scalars or pairs, got {type(row).__name__}")


def axis_type(kind: CoercedKind) -> str:
    """Map a *coerced* kind to an ECharts axis type.

    Deliberately not ``Column.type``: that is inferred from the first 200 uncoerced rows, so
    the Greek-comma measure ``86,6`` is ``text`` there. Treating it as a category makes the
    axis sort lexically — ``'100' < '86,6'`` — and scrambles the chart it was meant to draw.
    """
    if kind is CoercedKind.DECIMAL:
        return "value"
    if kind is CoercedKind.TEMPORAL:
        return "time"
    return "category"


def build_spec(
    facts: FactTable, *, title: str, caveat: str | None = None, complete: bool = True,
    label: str = "", settings: Settings | None = None,
) -> ChartSpec | None:
    """Build a validated option, or ``None`` when no chart would inform.

    A single figure is a sentence, not a chart, and a listing has nothing to plot.
    """
    cfg = settings or get_settings()
    if facts.operation is Operation.LISTING or not facts.series:
        return None
    if len(facts.series) < 2:
        return None

    points = _bounded_points(facts, cfg)
    if points is None:
        return None

    temporal = facts.operation is Operation.NONE and (
        facts.series_field is not None or _looks_temporal(facts)
    )
    grouped = bool(facts.series_field)
    kind = (
        ChartKind.LINE if temporal
        else ChartKind.GROUPED_BAR if grouped
        else ChartKind.BAR
    )

    series, categories = _series(points, kind, complete)
    if not series:
        return None

    option: dict[str, Any] = {
        # Motion is opt-in from the client, which honours prefers-reduced-motion; a chart that
        # animates on arrival regardless of that setting is a WCAG problem, not a flourish.
        "animation": False,
        "backgroundColor": "transparent",
        "color": list(_SERIES_COLORS),
        # Decals give a second, non-colour channel, which is what DESIGN.md's "never encode
        # meaning in hue alone" rule asks for and Vega-Lite could not provide.
        "aria": {"enabled": True, "decal": {"show": True}, "description": title},
        # Literal strings only. The caveat is exactly the text an attacker would want to
        # rewrite, so it goes in as inert text and is never a template.
        "title": {"text": title, "subtext": caveat or "", "left": "left"},
        "grid": {"left": 8, "right": 16, "top": 64, "bottom": 24, "containLabel": True},
        "tooltip": {"trigger": "axis"},
        "xAxis": {
            "type": "time" if kind is ChartKind.LINE else "category",
            "name": label or (facts.dimension or ""),
            "nameLocation": "middle",
            "nameGap": 34,
            "axisLabel": {"hideOverlap": True},
            **({"data": categories} if kind is not ChartKind.LINE else {}),
            **({"boundaryGap": True} if kind is not ChartKind.LINE else {}),
        },
        "yAxis": {
            "type": "value",
            "name": facts.measure or "",
            "nameLocation": "end",
            "splitLine": {"show": True},
        },
        "series": series,
    }
    if grouped:
        option["legend"] = {"top": "bottom", "orient": "horizontal"}

    validate_spec(option)
    if len(json.dumps(option, ensure_ascii=False)) > cfg.synthesis_chart_max_bytes:
        return None
    return ChartSpec(option=option, kind=kind, title=title, caveat=caveat)


def _series(
    points: list[dict[str, Any]], kind: ChartKind, complete: bool
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Group points into ECharts series, and return the category axis values with them."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for point in points:
        groups.setdefault(str(point.get("series", "")), []).append(point)

    if kind is ChartKind.LINE:
        # A time axis takes [x, y] pairs, so categories are not needed and gaps stay real
        # rather than being evenly spaced by a category axis.
        return (
            [
                {"name": name, "type": "line", "showSymbol": True, "symbolSize": 5,
                 "smooth": False,  # smoothing across a gap would imply data we do not have
                 "data": [[point.get("dim"), point.get("value")] for point in rows]}
                for name, rows in groups.items()
            ],
            [],
        )

    # Bars: order is decided here, not by a renderer flag, so the ranking is inspectable.
    # Ranking a truncated table is a superlative claim rendered visually — the categories that
    # were never fetched would read as absent rather than unknown — so sort only when complete.
    ordered = _ordered_categories(points, complete)
    index = {category: position for position, category in enumerate(ordered)}
    built = []
    for name, rows in groups.items():
        values: list[Any] = [None] * len(ordered)
        for point in rows:
            position = index.get(str(point.get("dim")))
            if position is not None:
                values[position] = point.get("value")
        built.append({"name": name, "type": "bar", "barMaxWidth": 48, "data": values})
    return built, ordered


def _ordered_categories(points: list[dict[str, Any]], complete: bool) -> list[str]:
    """Category order: by descending value when the table is complete, else as published."""
    seen: dict[str, float] = {}
    for point in points:
        category = str(point.get("dim"))
        value = point.get("value")
        numeric = float(value) if isinstance(value, int | float) else 0.0
        seen[category] = seen.get(category, 0.0) + numeric
    if complete:
        return sorted(seen, key=lambda category: (-seen[category], category))
    return list(seen)


def _looks_temporal(facts: FactTable) -> bool:
    """Report whether the series' x values are ISO dates."""
    sample = str(facts.series[0].get("dim", ""))
    return len(sample) >= 7 and sample[:4].isdigit() and sample[4:5] == "-"


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
