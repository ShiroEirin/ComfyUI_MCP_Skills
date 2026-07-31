"""Deterministic workflow parameter roles and dependency extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_GENERIC_FIELDS: dict[str, tuple[str, bool, str]] = {
    "text": ("prompt", True, "Text prompt"),
    "prompt": ("prompt", True, "Text prompt"),
    "seed": ("seed", False, "Random seed"),
    "width": ("width", False, "Image width"),
    "height": ("height", False, "Image height"),
    "batch_size": ("batch_size", False, "Batch size"),
    "size": ("size", False, "Image size"),
    "num": ("count", False, "Number of images"),
    "steps": ("steps", False, "Generation steps"),
    "filename_prefix": ("output_name", False, "Output file prefix"),
}
_MEDIA_FIELDS: dict[str, dict[str, tuple[str, bool, str]]] = {
    "audio": {
        "tags": ("prompt", True, "Music style/genre tags"),
        "lyrics": ("lyrics", False, "Song lyrics"),
        "bpm": ("tempo", False, "Beats per minute"),
        "duration": ("duration", False, "Audio duration"),
        "seconds": ("duration", False, "Duration in seconds"),
        "language": ("language", False, "Language code"),
        "keyscale": ("key", False, "Musical key and scale"),
        "cfg_scale": ("guidance", False, "Classifier-free guidance scale"),
        "temperature": ("temperature", False, "Sampling temperature"),
    },
    "video": {
        "format": ("format", False, "Output video format"),
        "codec": ("codec", False, "Video codec"),
        "frame_rate": ("frame_rate", False, "Video frame rate"),
        "fps": ("frame_rate", False, "Frames per second"),
        "noise_seed": ("seed", False, "Noise seed for video generation"),
        "cfg": ("guidance", False, "Classifier-free guidance scale"),
    },
}
_MEDIA_LOADERS: dict[tuple[str, str], tuple[str, str, str]] = {
    ("LoadImage", "image"): ("image", "image", "Upload an image"),
    ("LoadImageMask", "image"): ("image", "image", "Upload an image mask"),
    ("LoadImageOutput", "image"): (
        "image",
        "image",
        "Reference an image from this server's output history",
    ),
}
_MODEL_LOADERS: dict[str, tuple[str, str]] = {
    "CheckpointLoaderSimple": ("ckpt_name", "checkpoints"),
    "CheckpointLoader": ("ckpt_name", "checkpoints"),
    "LoraLoader": ("lora_name", "loras"),
    "LoraLoaderModelOnly": ("lora_name", "loras"),
    "VAELoader": ("vae_name", "vae"),
    "ControlNetLoader": ("control_net_name", "controlnet"),
    "CLIPLoader": ("clip_name", "text_encoders"),
    "UNETLoader": ("unet_name", "diffusion_models"),
    "unCLIPCheckpointLoader": ("ckpt_name", "checkpoints"),
    "StyleModelLoader": ("style_model_name", "style_models"),
    "CLIPVisionLoader": ("clip_name", "clip_vision"),
    "UpscaleModelLoader": ("model_name", "upscale_models"),
    "PhotoMakerLoader": ("photomaker_model_name", "photomaker"),
}


@dataclass(frozen=True, slots=True)
class ParameterRoleRegistry:
    """Versioned deterministic rules for exposing safe workflow inputs."""

    generic_fields: dict[str, tuple[str, bool, str]]
    media_fields: dict[str, dict[str, tuple[str, bool, str]]]
    media_loaders: dict[tuple[str, str], tuple[str, str, str]]

    @classmethod
    def default(cls) -> ParameterRoleRegistry:
        return cls(dict(_GENERIC_FIELDS), dict(_MEDIA_FIELDS), dict(_MEDIA_LOADERS))

    def extract(
        self,
        graph: dict[str, Any],
        *,
        media_type: str = "image",
        object_info: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        rules = dict(self.generic_fields)
        rules.update(self.media_fields.get(media_type, {}))
        candidates: list[dict[str, Any]] = []
        for node_id in sorted(graph, key=_node_sort_key):
            node = graph[node_id]
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type", "")).strip()
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            for field in sorted(inputs):
                value = inputs[field]
                if _is_connection(value):
                    continue
                media_rule = self.media_loaders.get((class_type, field))
                if media_rule is not None:
                    role, parameter_type, description = media_rule
                    required = True
                    storage_type = "output" if class_type == "LoadImageOutput" else ""
                elif field in rules:
                    role, required, description = rules[field]
                    parameter_type = _type_guess(value)
                    storage_type = ""
                else:
                    continue
                candidate: dict[str, Any] = {
                    "node_id": str(node_id),
                    "field": field,
                    "required": required,
                    "type": parameter_type,
                    "description": description,
                    "role": role,
                    "storage_type": storage_type,
                }
                if object_info is not None:
                    candidate.update(_input_constraints(object_info.get(class_type), field))
                candidates.append(candidate)
        return _name_parameters(candidates, graph)


@dataclass(frozen=True, slots=True)
class DependencyExtractorRegistry:
    """Versioned model-loader registry seeded from the legacy CLI contract."""

    model_loaders: dict[str, tuple[str, str]]

    @classmethod
    def default(cls) -> DependencyExtractorRegistry:
        return cls(dict(_MODEL_LOADERS))

    def extract(
        self,
        graph: dict[str, Any],
        *,
        object_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del object_info  # Reserved for versioned extractor predicates.
        nodes: set[str] = set()
        models: list[dict[str, str]] = []
        unverified: set[str] = set()
        for node_id in sorted(graph, key=_node_sort_key):
            node = graph[node_id]
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type", "")).strip()
            if not class_type:
                continue
            nodes.add(class_type)
            inputs = node.get("inputs")
            inputs = inputs if isinstance(inputs, dict) else {}
            loader = self.model_loaders.get(class_type)
            if loader is not None:
                field, folder = loader
                filename = inputs.get(field)
                if isinstance(filename, str) and filename:
                    models.append(
                        {
                            "filename": filename,
                            "folder": folder,
                            "loader_node": class_type,
                            "node_id": str(node_id),
                        }
                    )
            elif _looks_like_model_loader(class_type) and _has_filename_input(inputs):
                unverified.add(class_type)
        models.sort(key=lambda item: (item["folder"], item["filename"], item["node_id"]))
        return {
            "nodes": sorted(nodes),
            "models": models,
            "coverage": "partial" if unverified else "complete",
            "unverified_loaders": sorted(unverified),
        }


def _name_parameters(
    candidates: list[dict[str, Any]], graph: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in candidates:
        counts[item["field"]] = counts.get(item["field"], 0) + 1
    result: dict[str, dict[str, Any]] = {}
    for item in candidates:
        field = item["field"]
        name = "prompt" if field == "text" else field
        if counts[field] > 1:
            node = graph.get(item["node_id"], {})
            title = ""
            if isinstance(node, dict) and isinstance(node.get("_meta"), dict):
                title = str(node["_meta"].get("title", ""))
            name = f"{name}_{_normalize_name(title) or item['node_id']}"
        base = name
        index = 2
        while name in result:
            name = f"{base}_{index}"
            index += 1
        public = {key: value for key, value in item.items() if key not in {"storage_type"}}
        if item["storage_type"]:
            public["storage_type"] = item["storage_type"]
        result[name] = public
    return result


def _input_constraints(info: object, field: str) -> dict[str, Any]:
    if not isinstance(info, dict):
        return {}
    inputs = info.get("input")
    if not isinstance(inputs, dict):
        return {}
    definition: object = None
    for section in ("required", "optional"):
        values = inputs.get(section)
        if isinstance(values, dict) and field in values:
            definition = values[field]
            break
    if not isinstance(definition, list) or not definition:
        return {}
    result: dict[str, Any] = {}
    settings = definition[1] if len(definition) > 1 and isinstance(definition[1], dict) else {}
    minimum = settings.get("min")
    maximum = settings.get("max")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
        result["minimum"] = minimum
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        result["maximum"] = maximum
    options = definition[0] if isinstance(definition[0], list) else settings.get("options")
    if isinstance(options, list):
        valid = [value for value in options if isinstance(value, (str, int, float, bool))]
        if len(valid) == len(options) and len(valid) <= 200:
            result["enum"] = valid
        elif valid:
            result["options_preview"] = valid[:20]
            result["options_truncated"] = True
    return result


def _normalize_name(value: str) -> str:
    normalized = "_".join(value.lower().split())
    return "".join(
        character for character in normalized if character.isalnum() or character == "_"
    ).strip("_")


def _is_connection(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def _type_guess(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "string"


def _node_sort_key(value: object) -> tuple[int, int | str]:
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)


def _looks_like_model_loader(class_type: str) -> bool:
    name = class_type.casefold()
    return any(
        token in name
        for token in ("load", "model", "checkpoint", "lora", "vae", "clip", "unet", "gguf")
    )


def _has_filename_input(inputs: dict[str, Any]) -> bool:
    tokens = ("model", "ckpt", "lora", "vae", "clip", "unet", "filename", "file", "gguf")
    return any(
        isinstance(value, str) and value and any(token in field.casefold() for token in tokens)
        for field, value in inputs.items()
    )
