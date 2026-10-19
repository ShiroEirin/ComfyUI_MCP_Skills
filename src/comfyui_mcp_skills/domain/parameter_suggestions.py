"""Heuristics for which workflow inputs are worth exposing as parameters.

The table encodes ComfyUI's common loader/sampler conventions: a checkpoint name,
a LoRA and its weights, sampler knobs, latent size. Suggestions are advisory --
they are returned to the caller, which decides which ones to expose through
``comfyui.admin.workflow.change.plan`` / ``commit``.
"""

from __future__ import annotations

from typing import Any

SUGGESTED_PARAMETER_FIELDS: dict[str, tuple[str, ...]] = {
    "CheckpointLoaderSimple": ("ckpt_name",),
    "CheckpointLoader": ("ckpt_name", "config_name"),
    "unCLIPCheckpointLoader": ("ckpt_name",),
    "ImageOnlyCheckpointLoader": ("ckpt_name",),
    "LoraLoader": ("lora_name", "strength_model", "strength_clip"),
    "LoraLoaderModelOnly": ("lora_name", "strength_model"),
    "VAELoader": ("vae_name",),
    "CLIPSetLastLayer": ("stop_at_clip_layer",),
    "UpscaleModelLoader": ("model_name",),
    "ControlNetLoader": ("control_net_name",),
    "CLIPTextEncode": ("text",),
    "EmptyLatentImage": ("width", "height", "batch_size"),
    "EmptySD3LatentImage": ("width", "height", "batch_size"),
    "KSampler": ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise"),
    "KSamplerAdvanced": (
        "noise_seed",
        "steps",
        "cfg",
        "sampler_name",
        "scheduler",
        "start_at_step",
        "end_at_step",
        "add_noise",
        "return_with_leftover_noise",
    ),
    "SaveImage": ("filename_prefix",),
}
MAX_SUGGESTIONS = 50


def _node_sort_key(node_id: str) -> tuple[int, str]:
    try:
        return (0, f"{int(node_id):012d}")
    except ValueError:
        return (1, node_id)


def _is_connection(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def suggest_parameters(
    graph: dict[str, Any], parameters: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return inputs worth exposing as workflow parameters.

    Wired inputs (``["node", index]``) are skipped -- a parameter cannot target a
    connection -- and inputs already exposed are not suggested twice.
    """
    exposed = {
        (str(metadata.get("node_id", "")), str(metadata.get("field", "")))
        for metadata in parameters.values()
        if isinstance(metadata, dict)
    }
    suggestions: list[dict[str, Any]] = []
    for node_id in sorted(graph, key=_node_sort_key):
        node = graph.get(node_id)
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        inputs = node.get("inputs")
        if not isinstance(class_type, str) or not isinstance(inputs, dict):
            continue
        fields = SUGGESTED_PARAMETER_FIELDS.get(class_type)
        if fields is None:
            continue
        for field in fields:
            if field not in inputs or _is_connection(inputs[field]):
                continue
            if (node_id, field) in exposed:
                continue
            suggestions.append(
                {
                    "node_id": node_id,
                    "class_type": class_type,
                    "field": field,
                    "value": inputs[field],
                    "suggested_name": f"{class_type.lower()}_{field}_{node_id}".lower(),
                }
            )
            if len(suggestions) >= MAX_SUGGESTIONS:
                return suggestions
    return suggestions
