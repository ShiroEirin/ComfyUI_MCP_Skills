"""Immutable Experiment values, deterministic expansion, and budget accounting."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from jsonschema import Draft202012Validator

from comfyui_mcp_skills.domain.workflow_schema import build_input_schema

FailurePolicy = Literal["continue", "stop_new", "cancel_queued"]
CancelMode = Literal["stop_new", "cancel_queued"]
ExpansionMode = Literal["matrix", "zip", "sample", "explicit"]

_FAILURE_POLICIES = frozenset({"continue", "stop_new", "cancel_queued"})
_CANCEL_MODES = frozenset({"stop_new", "cancel_queued"})
_MAX_VARIANTS = 10_000
_BUDGET_KEYS = (
    "max_variants",
    "max_concurrency",
    "max_pixels",
    "max_outputs",
    "max_seconds",
)
_DEFAULT_RUBRICS: dict[str, dict[str, tuple[float, float]]] = {
    "v1": {
        "quality": (0.0, 5.0),
        "prompt_adherence": (0.0, 5.0),
        "technical_quality": (0.0, 5.0),
    }
}


@dataclass(frozen=True, slots=True)
class ExpandedVariants:
    mode: ExpansionMode
    arguments: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class BudgetTotals:
    variants: int
    concurrency: int
    pixels: int
    outputs: int
    seconds: float

    def to_dict(self) -> dict[str, int | float]:
        return {
            "variants": self.variants,
            "concurrency": self.concurrency,
            "pixels": self.pixels,
            "outputs": self.outputs,
            "seconds": self.seconds,
        }


@dataclass(frozen=True, slots=True)
class ResolvedVariant:
    """One validated Variant represented only by values differing from its base."""

    overrides: dict[str, Any]
    parameter_digest: str
    pixels: int
    outputs: int
    seconds: float


@dataclass(frozen=True, slots=True)
class ResolvedVariants:
    mode: ExpansionMode
    base_arguments: dict[str, Any]
    variants: tuple[ResolvedVariant, ...]

    def totals(self, *, concurrency: int) -> BudgetTotals:
        return BudgetTotals(
            variants=len(self.variants),
            concurrency=concurrency,
            pixels=sum(item.pixels for item in self.variants),
            outputs=sum(item.outputs for item in self.variants),
            seconds=sum(item.seconds for item in self.variants),
        )


@dataclass(frozen=True, slots=True)
class ScoreDimension:
    minimum: float
    maximum: float


@dataclass(frozen=True, slots=True)
class RatingRubric:
    version: str
    dimensions: Mapping[str, ScoreDimension]


def expand_variants(
    expansion: object,
    base_arguments: object,
    *,
    max_variants: int,
) -> ExpandedVariants:
    """Normalize one bounded expansion into compact per-Variant overrides."""
    normalized = _json_object(expansion, "expansion")
    _json_object(base_arguments, "base_arguments")
    mode = normalized.get("mode")
    if mode not in {"matrix", "zip", "sample", "explicit"}:
        raise ValueError("expansion mode must be matrix, zip, sample, or explicit")
    if mode == "explicit":
        explicit = json_copy(normalized.get("variants"), "expansion.variants")
        if not isinstance(explicit, list) or not explicit:
            raise ValueError("explicit expansion variants must be a non-empty list")
        if len(explicit) > max_variants:
            raise ValueError("variant count exceeds max_variants budget")
        arguments = tuple(_json_object(variant, "explicit variant") for variant in explicit)
        return ExpandedVariants("explicit", arguments)
    parameters = _parameter_lists(normalized.get("parameters"))
    count: int
    values: Iterable[tuple[Any, ...]]
    names = tuple(sorted(parameters))
    if mode == "matrix":
        count = _product_size(parameters)
        values = itertools.product(*(parameters[name] for name in names))
    elif mode == "zip":
        lengths = {len(parameters[name]) for name in names}
        if len(lengths) != 1:
            raise ValueError("zip expansion parameter lists must have equal lengths")
        count = lengths.pop()
        values = zip(*(parameters[name] for name in names))
    else:
        population = _product_size(parameters)
        seed = normalized.get("seed")
        raw_count = normalized.get("count")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ValueError("sample seed must be an integer")
        if not isinstance(raw_count, int) or isinstance(raw_count, bool) or raw_count <= 0:
            raise ValueError("sample count must be a positive integer")
        count = raw_count
        if count > population:
            raise ValueError("sample count exceeds the unique parameter combinations")
        values = _sample_rows(parameters, names, seed=seed, count=count)
    if count > max_variants:
        raise ValueError("variant count exceeds max_variants budget")
    arguments = tuple({name: value for name, value in zip(names, row)} for row in values)
    return ExpandedVariants(mode, arguments)  # type: ignore[arg-type]


def resolve_variants(
    expanded: ExpandedVariants,
    base_arguments: object,
    planning_context: Mapping[str, Any],
) -> ResolvedVariants:
    """Apply immutable schema defaults and validate every expanded Variant."""
    context = _planning_context(planning_context)
    base = _apply_defaults(base_arguments, context["parameter_schema"], "base_arguments")
    base_digest = canonical_digest(base)
    validator = _argument_validator(context["parameter_schema"])
    resolved: list[ResolvedVariant] = []
    for index, overrides in enumerate(expanded.arguments):
        merged = {**base, **_json_object(overrides, f"variant[{index}].overrides")}
        arguments = _resolve_arguments(merged, validator, f"variant[{index}]")
        compact = {
            key: arguments[key] for key in sorted(arguments) if base.get(key) != arguments[key]
        }
        pixels = _pixels(arguments, context)
        batch = _batch_size(arguments, context)
        outputs = context["output_cardinality"] * batch
        seconds = context["trusted_seconds_per_run"]
        resolved.append(
            ResolvedVariant(
                compact,
                canonical_digest(["resolved-variant-v2", base_digest, compact]),
                pixels,
                outputs,
                seconds,
            )
        )
    return ResolvedVariants(expanded.mode, base, tuple(resolved))


_HARD_LIMITS: dict[str, int | float] = {
    "max_variants": 10_000,
    "max_concurrency": 64,
    "max_pixels": 10**15,
    "max_outputs": 100_000,
    "max_seconds": 31_536_000.0,
}


def _planning_context(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("planning context must be an object")
    result = json_copy(dict(value), "planning_context")
    for field in ("revision_id", "deployment_id", "content_digest"):
        item = result.get(field)
        if not isinstance(item, str) or not item:
            raise ValueError(f"planning context {field} is required")
    digest = result["content_digest"]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("planning context content_digest must be lowercase SHA-256")
    schema = result.get("parameter_schema")
    if not isinstance(schema, Mapping):
        raise ValueError("planning context parameter_schema is required")
    result["parameter_schema"] = json_copy(schema, "parameter_schema")
    output_cardinality = result.get("output_cardinality")
    if (
        not isinstance(output_cardinality, int)
        or isinstance(output_cardinality, bool)
        or output_cardinality <= 0
    ):
        raise ValueError("planning context output_cardinality must be positive")
    seconds = result.get("trusted_seconds_per_run")
    contract = result["parameter_schema"].get("_output_contract")
    outputs = contract.get("outputs") if isinstance(contract, Mapping) else None
    if (
        not isinstance(contract, Mapping)
        or contract.get("coverage") != "complete"
        or not isinstance(outputs, list)
        or len(outputs) != output_cardinality
    ):
        raise ValueError("planning context output cardinality is unknown or inconsistent")
    if not _finite_number(seconds) or float(seconds) <= 0:
        raise ValueError("planning context trusted seconds estimate is unknown")
    slots = result.get("execution_slots")
    if not isinstance(slots, int) or isinstance(slots, bool) or not 1 <= slots <= 64:
        raise ValueError("planning context execution_slots must be between 1 and 64")
    quota = result.get("subject_submission_quota")
    if not isinstance(quota, int) or isinstance(quota, bool) or quota < 0:
        raise ValueError("planning context subject_submission_quota is invalid")
    result["trusted_seconds_per_run"] = float(seconds)
    result["output_cardinality"] = output_cardinality
    result["execution_slots"] = slots
    result["subject_submission_quota"] = quota
    return result


def _schema_parameters(schema: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = schema.get("parameters", schema)
    if not isinstance(raw, Mapping):
        raise ValueError("parameter_schema parameters must be an object")
    result: dict[str, dict[str, Any]] = {}
    for name, metadata in raw.items():
        if not isinstance(name, str) or not name or not isinstance(metadata, Mapping):
            raise ValueError("parameter_schema contains invalid parameter metadata")
        result[name] = dict(metadata)
    return result


def _apply_defaults(value: object, schema: Mapping[str, Any], field: str) -> dict[str, Any]:
    arguments = _json_object(value, field)
    for name, metadata in _schema_parameters(schema).items():
        if name not in arguments and "default" in metadata:
            arguments[name] = json_copy(metadata["default"], f"{field}.{name}.default")
    return arguments


def _argument_validator(schema: Mapping[str, Any]) -> Draft202012Validator:
    try:
        return Draft202012Validator(build_input_schema(_schema_parameters(schema)))
    except (TypeError, ValueError) as exc:
        raise ValueError("planning context parameter schema is invalid") from exc


def _resolve_arguments(
    value: Mapping[str, Any], validator: Draft202012Validator, field: str
) -> dict[str, Any]:
    arguments = dict(value)
    errors = sorted(
        validator.iter_errors(arguments),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path)
        raise ValueError(
            f"{field} schema validation failed at {location or '<root>'}: {error.message}"
        )
    return arguments


def _metadata_by_role(schema: Mapping[str, Any], role: str) -> list[str]:
    return [
        name
        for name, metadata in _schema_parameters(schema).items()
        if metadata.get("role") == role
    ]


def _cost_value(arguments: Mapping[str, Any], schema: Mapping[str, Any], role: str) -> int:
    names = _metadata_by_role(schema, role)
    if len(names) != 1:
        raise ValueError(f"pixel estimate is unknown for {role}")
    try:
        return _positive_integer(arguments.get(names[0]), names[0])
    except ValueError as exc:
        raise ValueError(f"pixel estimate is unknown for {role}") from exc


def _pixels(arguments: Mapping[str, Any], context: Mapping[str, Any]) -> int:
    schema = context["parameter_schema"]
    width = _cost_value(arguments, schema, "width")
    height = _cost_value(arguments, schema, "height")
    return width * height * _batch_size(arguments, context)


def _batch_size(arguments: Mapping[str, Any], context: Mapping[str, Any]) -> int:
    schema = context["parameter_schema"]
    names = _metadata_by_role(schema, "batch_size")
    if not names:
        return 1
    if len(names) != 1 or not _positive_integer(arguments.get(names[0]), names[0]):
        raise ValueError("output cardinality is unknown for batch size")
    return int(arguments[names[0]])


def validate_failure_policy(value: object) -> FailurePolicy:
    if not isinstance(value, str) or value not in _FAILURE_POLICIES:
        raise ValueError("failure_policy must be continue, stop_new, or cancel_queued")
    return value  # type: ignore[return-value]


def validate_cancel_mode(value: object) -> CancelMode:
    if not isinstance(value, str) or value not in _CANCEL_MODES:
        raise ValueError("cancel mode must be stop_new or cancel_queued")
    return value  # type: ignore[return-value]


def normalize_rubrics(
    value: Mapping[str, Mapping[str, tuple[float, float]]] | None,
) -> Mapping[str, RatingRubric]:
    source: Mapping[str, Mapping[str, tuple[float, float]]] = (
        _DEFAULT_RUBRICS if value is None else value
    )
    if not source or len(source) > 32:
        raise ValueError("rubrics must contain between 1 and 32 versions")
    result: dict[str, RatingRubric] = {}
    for version, raw_dimensions in source.items():
        if not _safe_name(version):
            raise ValueError("rubric versions must be safe non-empty names")
        if not isinstance(raw_dimensions, Mapping) or not 1 <= len(raw_dimensions) <= 32:
            raise ValueError("each rubric must contain between 1 and 32 dimensions")
        dimensions: dict[str, ScoreDimension] = {}
        for name, raw_bounds in raw_dimensions.items():
            if not _safe_name(name):
                raise ValueError("rubric dimensions must be safe non-empty names")
            if not isinstance(raw_bounds, (tuple, list)) or len(raw_bounds) != 2:
                raise ValueError(f"rubric dimension {name} must have minimum and maximum bounds")
            minimum, maximum = raw_bounds
            if not _finite_number(minimum) or not _finite_number(maximum) or minimum >= maximum:
                raise ValueError(f"rubric dimension {name} bounds are invalid")
            dimensions[name] = ScoreDimension(float(minimum), float(maximum))
        result[version] = RatingRubric(version, MappingProxyType(dimensions))
    return MappingProxyType(result)


def validate_rating_scores(
    rubrics: Mapping[str, RatingRubric], rubric_version: object, scores: object
) -> tuple[str, dict[str, float]]:
    if not isinstance(rubric_version, str) or rubric_version not in rubrics:
        raise ValueError("rubric_version is unknown")
    rubric = rubrics[rubric_version]
    if not isinstance(scores, Mapping) or set(scores) != set(rubric.dimensions):
        raise ValueError("scores dimensions must exactly match rubric dimensions")
    normalized: dict[str, float] = {}
    for name in sorted(rubric.dimensions):
        value = scores[name]
        bounds = rubric.dimensions[name]
        if not _finite_number(value) or not bounds.minimum <= float(value) <= bounds.maximum:
            raise ValueError(f"score {name} must be between {bounds.minimum} and {bounds.maximum}")
        normalized[name] = float(value)
    return rubric_version, normalized


def validate_budgets(value: object) -> dict[str, int | float]:
    budgets = _json_object(value, "budgets")
    if set(budgets) != set(_BUDGET_KEYS):
        raise ValueError(f"budgets must contain exactly {', '.join(_BUDGET_KEYS)}")
    normalized: dict[str, int | float] = {}
    for key in _BUDGET_KEYS:
        item = budgets[key]
        if key == "max_seconds":
            if not _finite_number(item) or float(item) <= 0:
                raise ValueError(f"{key} must be a positive finite number")
            normalized[key] = float(item)
        else:
            if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
                raise ValueError(f"{key} must be a positive integer")
            normalized[key] = item
        if normalized[key] > _HARD_LIMITS[key]:
            raise ValueError(f"{key} exceeds server hard ceiling {_HARD_LIMITS[key]}")
    return normalized


def calculate_budget_totals(
    variants: tuple[ResolvedVariant, ...], *, concurrency: int
) -> BudgetTotals:
    """Aggregate costs already derived from pinned workflow semantics."""
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency <= 0:
        raise ValueError("concurrency must be a positive integer")
    if not all(isinstance(variant, ResolvedVariant) for variant in variants):
        raise ValueError("budget totals require resolved pinned Variants")
    return BudgetTotals(
        variants=len(variants),
        concurrency=concurrency,
        pixels=sum(variant.pixels for variant in variants),
        outputs=sum(variant.outputs for variant in variants),
        seconds=sum(variant.seconds for variant in variants),
    )


def enforce_budgets(totals: BudgetTotals, budgets: dict[str, int | float]) -> None:
    comparisons = (
        ("variants", totals.variants, budgets["max_variants"]),
        ("concurrency", totals.concurrency, budgets["max_concurrency"]),
        ("pixels", totals.pixels, budgets["max_pixels"]),
        ("outputs", totals.outputs, budgets["max_outputs"]),
        ("seconds", totals.seconds, budgets["max_seconds"]),
    )
    for name, actual, maximum in comparisons:
        if actual > maximum:
            raise ValueError(f"{name} budget exceeded: {actual} > {maximum}")


def canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def json_copy(value: object, field: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must contain only finite JSON values") from exc


def _json_object(value: object, field: str) -> dict[str, Any]:
    copied = json_copy(value, field)
    if not isinstance(copied, dict) or not all(isinstance(key, str) for key in copied):
        raise ValueError(f"{field} must be a JSON object")
    return copied


def _parameter_lists(value: object) -> dict[str, list[Any]]:
    parameters = _json_object(value, "expansion.parameters")
    if not parameters:
        raise ValueError("expansion.parameters must not be empty")
    for name, values in parameters.items():
        if not name or not isinstance(values, list) or not values:
            raise ValueError("each expansion parameter must have a name and non-empty value list")
    return parameters


def _product_size(parameters: dict[str, list[Any]]) -> int:
    result = 1
    for values in parameters.values():
        result *= len(values)
    return result


def _sample_rows(
    parameters: dict[str, list[Any]],
    names: tuple[str, ...],
    *,
    seed: int,
    count: int,
) -> tuple[tuple[Any, ...], ...]:
    population = _product_size(parameters)
    offset = seed % population
    digest = hashlib.sha256(str(seed).encode("ascii")).digest()
    step = int.from_bytes(digest, "big") % population
    step = step or 1
    while math.gcd(step, population) != 1:
        step = step % population + 1
    indices = ((offset + step * position) % population for position in range(count))
    return tuple(_combination_at(parameters, names, index) for index in indices)


def _combination_at(
    parameters: dict[str, list[Any]], names: tuple[str, ...], index: int
) -> tuple[Any, ...]:
    selected: list[Any] = [None] * len(names)
    for position in range(len(names) - 1, -1, -1):
        values = parameters[names[position]]
        index, value_index = divmod(index, len(values))
        selected[position] = values[value_index]
    return tuple(selected)


def _safe_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and all(
            character.isascii() and (character.isalnum() or character in "_-")
            for character in value
        )
    )


def _finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _positive_integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value
