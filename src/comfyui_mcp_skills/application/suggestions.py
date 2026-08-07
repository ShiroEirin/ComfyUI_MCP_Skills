"""Evidence-driven parameter suggestions from local run history.

Correlates resolved plan inputs (``execution_plans.resolved_inputs_json``)
with job outcomes (``jobs.status``) to surface parameter values that were
actually used on successful runs. This is evidence-based, not memory-based:
static community guidance lives in ``model_guidance`` and both channels are
meant to be consumed together (guidance as fallback, history as evidence).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_SCAN_LIMIT = 500
_MAX_PARAMETERS = 20
_MAX_VALUES_PER_PARAMETER = 3
_MAX_VALUE_LENGTH = 256


class SuggestionService:
    def __init__(self, store_path: str | Any) -> None:
        self._path = str(store_path)

    def suggest(self, owner_id: str, workflow_id: str = "") -> dict[str, Any]:
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("owner_id must be a non-empty string")
        if len(owner_id) > 128:
            raise ValueError("owner_id must be at most 128 characters")
        if workflow_id and (not isinstance(workflow_id, str) or len(workflow_id) > 128):
            raise ValueError("workflow_id must be at most 128 characters")
        rows = self._scan(owner_id, workflow_id)
        tallies: dict[str, dict[str, dict[str, int]]] = {}
        for resolved_json, status in rows:
            try:
                resolved = json.loads(resolved_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(resolved, dict):
                continue
            success = status == "completed"
            for name, value in resolved.items():
                if not isinstance(name, str) or not name:
                    continue
                if isinstance(value, (dict, list)):
                    continue
                if isinstance(value, bool):
                    rendered = "true" if value else "false"
                elif isinstance(value, str):
                    rendered = value
                    if not rendered or len(rendered) > _MAX_VALUE_LENGTH:
                        continue
                elif isinstance(value, (int, float)):
                    rendered = str(value)
                else:
                    continue
                parameter = tallies.setdefault(name, {})
                entry = parameter.setdefault(rendered, {"runs": 0, "successes": 0})
                entry["runs"] += 1
                if success:
                    entry["successes"] += 1
        suggestions: list[dict[str, Any]] = []
        for name in sorted(tallies):
            if len(suggestions) >= _MAX_PARAMETERS:
                break
            entries = tallies[name]
            ranked = sorted(
                entries.items(),
                key=lambda item: (-item[1]["runs"], -item[1]["successes"], item[0]),
            )
            values = [
                {
                    "value": value,
                    "runs": stats["runs"],
                    "success_rate": round(stats["successes"] / stats["runs"], 3),
                }
                for value, stats in ranked[:_MAX_VALUES_PER_PARAMETER]
            ]
            suggestions.append({"parameter": name, "values": values})
        return {
            "owner_id": owner_id,
            "workflow_id": workflow_id,
            "scanned_jobs": len(rows),
            "suggestions": suggestions,
        }

    def _scan(self, owner_id: str, workflow_id: str) -> list[tuple[str, str]]:
        query = """
            SELECT e.resolved_inputs_json, j.status
            FROM execution_plans AS e
            JOIN execution_plan_owners AS o ON o.plan_id = e.plan_id
            JOIN jobs AS j ON j.plan_id = e.plan_id AND j.owner_id = o.owner_id
            WHERE o.owner_id = ?
        """
        parameters: list[Any] = [owner_id]
        if workflow_id:
            query += " AND j.workflow_id = ?"
            parameters.append(workflow_id)
        query += " AND j.status IN ('completed', 'failed') LIMIT ?"
        parameters.append(_SCAN_LIMIT)
        connection = sqlite3.connect(self._path, timeout=5.0)
        try:
            return [
                (str(row[0]), str(row[1]))
                for row in connection.execute(query, parameters).fetchall()
            ]
        finally:
            connection.close()
