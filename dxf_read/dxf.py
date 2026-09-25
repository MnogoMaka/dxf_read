from __future__ import annotations

import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

logging.getLogger("ezdxf").setLevel(logging.ERROR)

import ezdxf
from ezdxf import path as ezpath
from ezdxf.document import Drawing
from ezdxf.entities import DXFEntity, Insert
from shapely.geometry import LineString, Polygon as ShapelyPolygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

from . import config
from .config import (
    ARRANGEMENT_PRIORITY,
    BLENDER,
    BLENDER_SEARCH_ROOTS,
    CHANNEL_FINAL_RE,
    CURVE_TYPES,
    DXF_OUTPUT_DIR,
    DXF_VERSION,
    EXCLUDE_HINT,
    FILL_TYPES,
    GENERIC_BLOCK_LAYERS,
    HATCH_DRAW_COLORS,
    HATCH_DRAW_ORDER,
    HINT_SUFFIX_RE,
    LAYER_CATEGORY_HINTS,
    ODA_CONVERTER,
    ODA_SEARCH_ROOTS,
    OUTPUT_DIR,
    PENA_SUFFIX_RE,
    PROJECT_CLUSTER_CATEGORIES,
    QUAD_TYPES,
    ROOT_COLLECTION,
    SHEET_PREFIX_RE,
    SYSTEM_PROMPT,
    TARGET_OBJECT_TYPES,
    TEXT_TYPES,
    THINK_BLOCK_RE,
    UNDERLAY_SCRIPT,
    XREF_LEAF_RE,
)


def normalize_name(value: str | None) -> str:
    """Нормализует имя слоя для устойчивого сравнения (регистр, Ё/Е)."""
    return (value or "").upper().replace("Ё", "Е").strip()


def layer_leaf(name: str) -> str:
    """Хвост имени после всех xref-префиксов «Блок$0$» / «Подложка|» и номера листа."""
    text = normalize_name(name)
    while True:
        stripped = XREF_LEAF_RE.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    text = PENA_SUFFIX_RE.sub("", text).strip(" _-|")
    return SHEET_PREFIX_RE.sub("", text).strip() or normalize_name(name)


def is_junk_layer(name: str) -> bool:
    """Служебные копии, подписи, Civil 3D, интерьер — не площадка.

    Токены смотрим только в хвосте: префикс xref («…трасса|Газон») сам по себе
    не должен выкидывать нормальный слой.
    """
    text = normalize_name(name)
    if PENA_SUFFIX_RE.search(text):
        return True
    probe = layer_leaf(name)
    return any(token in probe for token in config.settings().junk_layer_tokens)


def is_generic_block_layer(name: str) -> bool:
    """Слой внутри блока без собственного смысла — можно взять категорию вставки."""
    return layer_leaf(name) in GENERIC_BLOCK_LAYERS


def is_area_polyline_layer(name: str) -> bool:
    """Замкнутая полилиния на слое заливки пятна, не контур стен."""
    text = layer_leaf(name)
    return any(token in text for token in config.settings().fill_polyline_tokens)

# ФУНКЦИЯ 1. Перечень слоёв

@dataclass
class LayerStat:
    """Слой и то, что на нём фактически лежит."""
    name: str
    entity_counts: Counter = field(default_factory=Counter)
    block_entity_counts: Counter = field(default_factory=Counter)
    color: int = 7
    linetype: str = ""
    is_off: bool = False
    is_frozen: bool = False
    defined_in_table: bool = True

    @property
    def total(self) -> int:
        return sum(self.entity_counts.values()) + sum(self.block_entity_counts.values())

    def summary(self) -> str:
        combined = self.entity_counts + self.block_entity_counts
        return ", ".join(f"{name}:{count}" for name, count in combined.most_common(5))


def list_layers(document: Drawing, include_blocks: bool = True) -> list[str]:
    """
    include_blocks=True учитывать и объекты внутри вставленных блоков
    """
    stats = _layer_report(document, include_blocks=include_blocks)
    return sorted(item.name for item in stats)


def _layer_report(document: Drawing, include_blocks: bool = True) -> list[LayerStat]:
    """Список слоёв с составом (сколько и каких сущностей на каждом)."""
    modelspace = document.modelspace()
    stats: dict[str, LayerStat] = {}

    def get(name: str, defined: bool = False) -> LayerStat:
        item = stats.get(name)
        if item is None:
            item = LayerStat(name=name, defined_in_table=defined)
            stats[name] = item
        return item

    for layer in document.layers:
        item = get(str(layer.dxf.name), defined=True)
        item.color = layer.get_color()
        item.linetype = str(layer.dxf.linetype)
        item.is_off = layer.is_off()
        item.is_frozen = layer.is_frozen()

    for entity in modelspace:
        get(str(entity.dxf.layer)).entity_counts[entity.dxftype()] += 1

    if include_blocks:
        for insert in modelspace.query("INSERT"):
            for entity, layer, _ in _iter_block_content(insert):
                get(layer).block_entity_counts[entity.dxftype()] += 1

    return sorted(stats.values(), key=lambda item: (-item.total, item.name))

def _virtual_block_entities(insert: Insert) -> list[DXFEntity]:
    """Сущности блока в координатах вставки. Нет определения — пустой список."""
    try:
        if insert.block() is None:
            return []
        return list(insert.virtual_entities())
    except Exception:
        return []


def _iter_block_content(
    insert: Insert,
    parent_layer: str | None = None,
    ancestors: frozenset[str] | None = None,
) -> Iterator[tuple[DXFEntity, str, tuple[str, ...]]]:
    max_insert_chain = 64
    """Рекурсивный обход INSERT"""
    name = str(insert.dxf.name)
    walked = ancestors if ancestors is not None else frozenset()

    if not name or name in walked:
        return
    if len(walked) >= max_insert_chain:
        return

    children = _virtual_block_entities(insert)
    if not children:
        return

    insert_layer = _resolve_layer(insert, parent_layer)
    block_path = (name,)
    next_ancestors = walked | {name}

    for child in children:
        if "PROXY" in child.dxftype():
            continue
        layer = _resolve_layer(child, insert_layer)
        if not isinstance(child, Insert):
            yield child, layer, block_path
            continue
        for item, item_layer, path in _iter_block_content(
            child, insert_layer, next_ancestors,
        ):
            yield item, item_layer, block_path + path
        yield child, layer, block_path


def _resolve_layer(entity: DXFEntity, parent_layer: str | None) -> str:
    """Слой "0" внутри блока означает слой вставки — как в AutoCAD."""
    layer = str(entity.dxf.layer)
    if parent_layer and normalize_name(layer) == "0":
        return parent_layer
    return layer

@dataclass
class LayerSelection:
    """Результат отбора: категория -> слои, плюс то, что не подошло."""

    by_category: dict[str, list[str]] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)
    invented: list[str] = field(default_factory=list)
    unknown_categories: dict[str, list[str]] = field(default_factory=dict)
    model: str = ""

    @property
    def layers(self) -> list[str]:
        """Плоский список нужных слоёв — вход для функции 3."""
        return sorted({layer for layers in self.by_category.values() for layer in layers})

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "by_category": self.by_category,
            "rejected": self.rejected,
            "invented": self.invented,
            "unknown_categories": self.unknown_categories,
        }


def _build_prompt(layer_names: Sequence[str], hints: Mapping[str, str] | None = None) -> str:
    categories = "\n".join(f"- {c}: {d}" for c, d in TARGET_OBJECT_TYPES.items())
    quoted = ",\n".join(f"  {json.dumps(name, ensure_ascii=False)}" for name in layer_names)

    hint_block = ""
    if hints:
        hint_lines = [
            f"  {json.dumps(name, ensure_ascii=False)}: {hints[name]}"
            for name in layer_names
            if hints.get(name)
        ]
        if hint_lines:
            hint_block = (
                "\n\nСостав слоя — только подсказка, в JSON не копируй:\n"
                + "\n".join(hint_lines)
            )

    return (
        "Категории объектов площадки:\n"
        f"{categories}\n\n"
        f"Не относятся к площадке и должны быть отброшены: {EXCLUDE_HINT}.\n\n"
        "Имена слоёв из чертежа — JSON-массив точных строк:\n"
        f"[\n{quoted}\n]"
        f"{hint_block}\n\n"
        "Верни JSON: ключ — категория из списка выше, значение — массив "
        "имён слоёв этой категории. Правила:\n"
        "1. Копируй строку из массива целиком. Ведущие «1 », «2 », «!», «_» — "
        "часть имени AutoCAD, не номер пункта и не маркдаун. Не отрезай их.\n"
        "2. Слой указывай не более одного раза.\n"
        "3. Слои, которые не относятся ни к одной категории, не включай.\n"
        "4. Если подходящих слоёв нет, верни пустой объект {}.\n"
        "5. Префиксы xref «блок$0$», «подложка|», «output[1]_» — часть имени, не отрезай.\n"
        "6. Не придумывай и не сокращай имена. Не ставь запятую перед } или ].\n"
        "7. Не бери колодцы, вентиляторы, горизонтали, размеры, подписи улиц, интерьер.\n"
        'Пример ответа: {"pavement": ["1 ГП_УДС_Тротуары"], "green": ["1 ГП_Штриховки_газон"]}'
    )


def _resolve_layer_name(raw: str, allowed: Mapping[str, str]) -> str | None:
    """Сопоставляет имя из ответа LLM с реальным слоем.

    Модель часто отрезает номер листа («1 Граница участка» → «Граница участка»)
    или дописывает «состав». Это не выдуманное имя, если совпадение уникально.
    """
    cleaned = HINT_SUFFIX_RE.sub("", str(raw)).strip().strip("\"'")
    cleaned = re.sub(r"\s*состав\s*:.*$", "", cleaned, flags=re.IGNORECASE).strip()
    if not cleaned:
        return None
    key = normalize_name(cleaned)
    if key in allowed:
        return allowed[key]
    for allowed_key, original in sorted(allowed.items(), key=lambda item: -len(item[0])):
        if key.startswith(allowed_key + " ") or key.startswith(allowed_key + "["):
            return original

    stripped_key = SHEET_PREFIX_RE.sub("", key)
    hits: list[str] = []
    for allowed_key, original in allowed.items():
        stripped_allowed = SHEET_PREFIX_RE.sub("", allowed_key)
        if stripped_key == stripped_allowed or key == stripped_allowed:
            hits.append(original)
    unique = list(dict.fromkeys(hits))
    if len(unique) == 1:
        return unique[0]
    return _fuzzy_layer(key, allowed)


def _prefer_layer(names: Sequence[str]) -> str:
    clean = [name for name in names if not is_junk_layer(name)]
    pool = list(clean or names)
    return min(
        pool,
        key=lambda name: (
            name.lower().startswith("output["),
            "$0$" in name,
            "|" in name,
            len(name),
        ),
    )


def _fuzzy_layer(key: str, allowed: Mapping[str, str]) -> str | None:
    """Сопоставляет опечатку или хвост xref с реальным слоем."""
    leaf = layer_leaf(key)
    if len(leaf) < 4:
        return None
    by_leaf: dict[str, list[str]] = {}
    for allowed_key, original in allowed.items():
        by_leaf.setdefault(layer_leaf(allowed_key), []).append(original)
    if leaf in by_leaf:
        return _prefer_layer(by_leaf[leaf])
    close = difflib.get_close_matches(leaf, list(by_leaf), n=1, cutoff=0.84)
    if close:
        return _prefer_layer(by_leaf[close[0]])
    return None


def select_target_layers(
    layer_names: Sequence[str],
    hints: Mapping[str, str] | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    batch_size: int | None = None,
    timeout: float | None = None,
) -> LayerSelection:
    """
    Функция 2. Имена слоёв нужные нам слои по категориям через LLM.
    """
    cfg = config.settings()
    model = model or cfg.llm_model
    base_url = base_url or cfg.llm_base_url
    api_key = cfg.llm_api_key if api_key is None else api_key
    batch_size = cfg.batch_size if batch_size is None else batch_size
    timeout = cfg.llm_timeout if timeout is None else timeout
    selection = LayerSelection(model=model)
    allowed = {normalize_name(name): name for name in layer_names}
    used: set[str] = set()

    for batch in _chunks(list(layer_names), batch_size):
        reply = _request_chat_completion(
            system=SYSTEM_PROMPT,
            user=_build_prompt(batch, hints),
            model=model, base_url=base_url, api_key=api_key, timeout=timeout,
        )
        try:
            parsed = _parse_selection(reply)
        except ValueError:
            try:
                reply = _request_chat_completion(
                    system=SYSTEM_PROMPT,
                    user=_build_prompt(batch, hints)
                    + "\n\nОтвет — только JSON, без thinking, без пояснений и без висячих запятых.",
                    model=model, base_url=base_url, api_key=api_key, timeout=timeout,
                )
                parsed = _parse_selection(reply)
            except ValueError as error:
                print(f"Пропуск пакета классификации ({len(batch)} слоёв): {error}")
                continue

        for category, layers in parsed.items():
            if category not in TARGET_OBJECT_TYPES:
                selection.unknown_categories.setdefault(category, []).extend(str(n) for n in layers)
                continue

            for name in layers:
                original = _resolve_layer_name(str(name), allowed)
                if original is None:
                    selection.invented.append(str(name))
                    continue
                if original in used:
                    continue
                used.add(original)
                selection.by_category.setdefault(category, []).append(original)

    for layers in selection.by_category.values():
        layers.sort()

    selection.rejected = sorted(set(layer_names) - used)
    return selection


def _message_text(message: Mapping[str, Any] | None) -> str:
    """Собирает текст из content / reasoning"""
    if not message:
        return ""

    chunks: list[str] = []
    for key in ("content", "reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            chunks.append(value)
        elif isinstance(value, list):
            for part in value:
                if isinstance(part, dict):
                    text = part.get("text") or part.get("content") or ""
                    if text:
                        chunks.append(str(text))
                elif part:
                    chunks.append(str(part))
    return "\n".join(chunks)


def _strip_reasoning(text: str) -> str:
    """Убирает thinking-каналы"""
    if CHANNEL_FINAL_RE.search(text):
        text = CHANNEL_FINAL_RE.split(text, maxsplit=1)[-1]
    text = THINK_BLOCK_RE.sub("", text)
    text = re.sub(r"<\|[^|>]+\|>", " ", text)
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE)
    return text.strip()


def _repair_json_text(text: str) -> str:
    """Чинит типичный JSON от LLM: висячие запятые перед } и ]."""
    return re.sub(r",\s*([}\]])", r"\1", text)


def _iter_json_objects(text: str) -> Iterator[dict[str, Any]]:
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            return
        chunk = text[start:]
        try:
            data, consumed = decoder.raw_decode(chunk)
        except json.JSONDecodeError:
            try:
                data, consumed = decoder.raw_decode(_repair_json_text(chunk))
            except json.JSONDecodeError:
                index = start + 1
                continue
        if isinstance(data, dict):
            yield data
        index = start + max(consumed, 1)


def _score_selection(data: dict[str, Any]) -> int:
    score = 0
    for category, layers in data.items():
        if category not in TARGET_OBJECT_TYPES:
            continue
        if isinstance(layers, str):
            layers = [layers]
        if isinstance(layers, list):
            score += 10 + len(layers)
    return score


def _parse_selection(reply: str) -> dict[str, list[str]]:
    """Достаёт JSON из ответа модели, даже если вокруг thinking-теги."""
    cleaned = _repair_json_text(_strip_reasoning(reply))
    candidates = list(_iter_json_objects(cleaned))
    if not candidates:
        candidates = list(_iter_json_objects(_repair_json_text(reply)))
    if not candidates:
        raise ValueError(f"Модель вернула ответ без JSON:\n{reply[:500]}")

    data = max(candidates, key=_score_selection)
    result: dict[str, list[str]] = {}
    for category, layers in data.items():
        if isinstance(layers, str):
            layers = [layers]
        if not isinstance(layers, list):
            continue
        result[str(category)] = [str(layer) for layer in layers]
    return result


def _request_chat_completion(
    system: str, user: str, model: str,
    base_url: str | None = None, api_key: str | None = None, timeout: float | None = None,
) -> str:
    """Один запрос к OpenAI-совместимому API. Возвращает текст ответа."""
    cfg = config.settings()
    base_url = cfg.llm_base_url if base_url is None else base_url
    api_key = cfg.llm_api_key if api_key is None else api_key
    timeout = cfg.llm_timeout if timeout is None else timeout
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.0,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # Старые серверы не принимают response_format — повторяем без него.
        if error.code in {400, 422} and "response_format" in payload:
            payload.pop("response_format", None)
            request = urllib.request.Request(
                url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers, method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        else:
            raise ConnectionError(
                f"Не удалось обратиться к модели по адресу {url}: {error}\n"
                "Проверьте, что сервер запущен, и задайте LLM_BASE_URL и LLM_MODEL."
            ) from error
    except urllib.error.URLError as error:
        raise ConnectionError(
            f"Не удалось обратиться к модели по адресу {url}: {error}\n"
            "Проверьте, что сервер запущен, и задайте LLM_BASE_URL и LLM_MODEL."
        ) from error

    choice = data["choices"][0]
    text = _message_text(choice.get("message"))
    if not text.strip():
        text = str(choice.get("text") or "")
    return text


def _chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), max(1, size)):
        yield items[start:start + size]


# ФУНКЦИЯ 3.


@dataclass
class Shape:
    layer: str
    dxftype: str
    kind: str
    vertices: list[tuple[float, float, float]] = field(default_factory=list)
    holes: list[list[tuple[float, float, float]]] = field(default_factory=list)
    closed: bool = False
    handle: str = ""
    block_path: tuple[str, ...] = ()
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer, "dxftype": self.dxftype, "kind": self.kind,
            "closed": self.closed, "handle": self.handle,
            "block_path": list(self.block_path),
            "vertices": [list(p) for p in self.vertices],
            "holes": [[list(p) for p in hole] for hole in self.holes],
            "properties": self.properties,
        }


def read_layer_geometry(
    document: Drawing,
    layer_names: Iterable[str],
    include_blocks: bool = True,
) -> list[Shape]:
    modelspace = document.modelspace()

    wanted = {normalize_name(name) for name in layer_names}
    # Чертёж уже в метрах, $INSUNITS не используем.
    scale = 1.0

    shapes: list[Shape] = []

    for entity in modelspace:
        layer = str(entity.dxf.layer)
        if normalize_name(layer) in wanted:
            shapes.extend(_entity_shapes(entity, layer, scale, ()))

        if not include_blocks or not isinstance(entity, Insert):
            continue

        for child, child_layer, block_path in _iter_block_content(entity):
            if normalize_name(child_layer) in wanted:
                shapes.extend(_entity_shapes(child, child_layer, scale, block_path))

    return shapes


def _entity_shapes(
    entity: DXFEntity, layer: str, scale: float, block_path: tuple[str, ...],
) -> list[Shape]:
    """Переводит одну сущность DXF в набор Shape. Неподходящие -> []."""
    entity_type = entity.dxftype()
    handle = str(entity.dxf.get("handle", "") or "")

    def make(kind: str, **kwargs: Any) -> Shape:
        return Shape(layer=layer, dxftype=entity_type, kind=kind, handle=handle,
                     block_path=block_path, **kwargs)

    if entity_type == "INSERT":
        return [make(
            "point", vertices=[_scaled_point(entity.dxf.insert, scale)],
            properties={
                "block_name": str(entity.dxf.name),
                "rotation_deg": round(float(entity.dxf.rotation), 4),
                "scale_x": round(float(entity.dxf.xscale), 6),
                "scale_y": round(float(entity.dxf.yscale), 6),
                "attributes": {str(a.dxf.tag): str(a.dxf.text) for a in entity.attribs},
            },
        )]

    if entity_type == "POINT":
        return [make("point", vertices=[_scaled_point(entity.dxf.location, scale)])]

    if entity_type in TEXT_TYPES:
        text = entity.plain_text() if entity_type == "MTEXT" else str(entity.dxf.text)
        return [make("text", vertices=[_scaled_point(entity.dxf.insert, scale)], properties={"text": text})]

    if entity_type in FILL_TYPES:
        return [
            make("polygon", vertices=outer, holes=holes, closed=True, properties={
                "pattern_name": str(entity.dxf.get("pattern_name", "")),
                "solid_fill": bool(entity.dxf.get("solid_fill", 0)),
                "hatch_style": int(entity.dxf.get("hatch_style", 0)),
            })
            for outer, holes in _hatch_rings(entity, scale)
        ]

    if entity_type in QUAD_TYPES:
        corners = _quad_corners(entity, scale)
        return [make("polygon", vertices=corners, closed=True)] if len(corners) >= 3 else []

    if entity_type in CURVE_TYPES:
        return [
            make("polygon" if closed else "polyline", vertices=points, closed=closed)
            for points, closed in _flattened_paths(entity, scale)
        ]

    return []


def _scaled_point(point: Any, scale: float) -> tuple[float, float, float]:
    return (
        round(float(point.x) * scale, 4),
        round(float(point.y) * scale, 4),
        round(float(getattr(point, "z", 0.0)) * scale, 4),
    )


def _flattening_distance(scale: float) -> float:
    distance = config.settings().flattening_distance_m
    return distance / scale if scale else distance


def _path_vertices(path: ezpath.Path, scale: float) -> list[tuple[float, float, float]]:
    points: list[tuple[float, float, float]] = []
    for vertex in path.flattening(_flattening_distance(scale)):
        point = _scaled_point(vertex, scale)
        if not points or point != points[-1]:
            points.append(point)
    return points


def _close_ring(points: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """Убирает дублирующую замыкающую точку."""
    if len(points) > 1 and points[0] == points[-1]:
        return points[:-1]
    return points


def _flattened_paths(
    entity: DXFEntity, scale: float,
) -> list[tuple[list[tuple[float, float, float]], bool]]:
    """Кривая спрямлённые контуры в мировых координатах."""
    if entity.dxftype() == "POLYLINE" and (entity.is_polygon_mesh or entity.is_poly_face_mesh):
        return []

    explicit_closed = _entity_is_closed(entity)
    result = []

    for sub_path in ezpath.make_path(entity).sub_paths():
        points = _path_vertices(sub_path, scale)
        closed = bool(sub_path.is_closed) or explicit_closed
        if closed:
            points = _close_ring(points)
        if len(points) < 2:
            continue
        result.append((points, closed))

    return result


def _entity_is_closed(entity: DXFEntity) -> bool:
    entity_type = entity.dxftype()
    if entity_type == "LWPOLYLINE":
        return bool(entity.closed)
    if entity_type == "POLYLINE":
        return bool(entity.is_closed)
    if entity_type in {"CIRCLE", "ELLIPSE"}:
        return True
    return False


def _fill_rings(
    entity: DXFEntity, scale: float,
) -> list[tuple[list[tuple[float, float, float]], list[list[tuple[float, float, float]]]]]:
    """Контуры площади: HATCH, SOLID/TRACE/3DFACE, замкнутая полилиния."""
    entity_type = entity.dxftype()
    if entity_type in FILL_TYPES:
        return _hatch_rings(entity, scale)
    if entity_type in QUAD_TYPES:
        corners = _quad_corners(entity, scale)
        return [(corners, [])] if len(corners) >= 3 else []
    if entity_type in {"LWPOLYLINE", "POLYLINE"}:
        rings: list[tuple[list[tuple[float, float, float]], list[list[tuple[float, float, float]]]]] = []
        for points, closed in _flattened_paths(entity, scale):
            if closed and len(points) >= 3:
                rings.append((points, []))
        return rings
    return []


def _hatch_rings(
    entity: DXFEntity, scale: float,
) -> list[tuple[list[tuple[float, float, float]], list[list[tuple[float, float, float]]]]]:
    paths = [sub_path for boundary in ezpath.from_hatch(entity) for sub_path in boundary.sub_paths()]
    if not paths:
        return []

    ignore_holes = int(entity.dxf.get("hatch_style", 0)) == 2
    result = []

    for exterior, holes in _walk_polygon_structure(ezpath.make_polygon_structure(paths), ignore_holes):
        outer = _close_ring(_path_vertices(exterior, scale))
        if len(outer) < 3:
            continue

        rings = []
        for hole in holes:
            ring = _close_ring(_path_vertices(hole, scale))
            if len(ring) >= 3:
                rings.append(ring)

        result.append((outer, rings))

    return result


def _walk_polygon_structure(node: Any, ignore_holes: bool) -> Iterator[tuple[ezpath.Path, list[ezpath.Path]]]:
    """
    Разбирает вложенность контуров ezdxf на пары (внешний, отверстия).
    """
    if not isinstance(node, (list, tuple)):
        yield node, []
        return
    if not node:
        return

    exterior = node[0]
    if isinstance(exterior, (list, tuple)):
        for child in node:
            yield from _walk_polygon_structure(child, ignore_holes)
        return

    holes: list[ezpath.Path] = []
    islands: list[Any] = []

    for child in node[1:]:
        if isinstance(child, (list, tuple)):
            if not child:
                continue
            holes.append(child[0])
            islands.extend(child[1:])
        else:
            holes.append(child)

    yield exterior, ([] if ignore_holes else holes)

    if not ignore_holes:
        for island in islands:
            yield from _walk_polygon_structure(island, ignore_holes)


def _quad_corners(entity: DXFEntity, scale: float) -> list[tuple[float, float, float]]:
    """SOLID, TRACE, 3DFACE: четыре точки в порядке обхода контура."""
    corners: list[tuple[float, float, float]] = []
    for name in ("vtx0", "vtx1", "vtx3", "vtx2"):
        if entity.dxf.hasattr(name):
            point = _scaled_point(entity.dxf.get(name), scale)
            if not corners or point != corners[-1]:
                corners.append(point)
    return _close_ring(corners)


def shapes_to_json(shapes: Sequence[Shape], path: str | Path, source: str = "") -> Path:
    """Сохраняет геометрию в JSON для передачи между шагами конвейера."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "source": source, "units": "meters", "count": len(shapes),
        "shapes": [shape.to_dict() for shape in shapes],
    }

    with output.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=1)

    return output


# ФУНКЦИЯ 4.


def infer_layer_category(name: str) -> str | None:
    """Тип объекта по хвосту имени слоя, без xref-префикса."""
    if is_junk_layer(name):
        return None
    text = layer_leaf(name)
    for category, tokens in LAYER_CATEGORY_HINTS:
        if any(token in text for token in tokens):
            return category
    return None


def refine_layer_groups(
    by_category: Mapping[str, Sequence[str]],
    all_layers: Sequence[str] | None = None,
) -> dict[str, list[str]]:
    """Правит группы ИИ: хвост имени важнее ответа модели. Добавляет явные слои."""
    refined: dict[str, list[str]] = {}
    seen: set[str] = set()
    for category, names in by_category.items():
        mapped = category if category in TARGET_OBJECT_TYPES else ""
        for name in names:
            if name in seen or is_junk_layer(name):
                continue
            seen.add(name)
            guessed = infer_layer_category(name) or mapped
            if guessed in TARGET_OBJECT_TYPES:
                refined.setdefault(guessed, []).append(name)
    for name in all_layers or ():
        if name in seen or is_junk_layer(name):
            continue
        guessed = infer_layer_category(name)
        if guessed in TARGET_OBJECT_TYPES:
            seen.add(name)
            refined.setdefault(guessed, []).append(name)
    return {key: sorted(value) for key, value in refined.items() if value}


def _category_for_layer(layer: str, layer_to_category: Mapping[str, str]) -> str | None:
    guessed = infer_layer_category(layer)
    if guessed:
        return guessed
    key = normalize_name(layer)
    if key in layer_to_category:
        return layer_to_category[key]
    return layer_to_category.get(layer_leaf(layer))


@dataclass
class HatchPiece:
    """Одна заливка HATCH после спрямления контуров."""

    category: str
    layer: str
    vertices: list[tuple[float, float, float]]
    holes: list[list[tuple[float, float, float]]] = field(default_factory=list)
    handle: str = ""
    block_path: tuple[str, ...] = ()

    @property
    def from_block(self) -> bool:
        return bool(self.block_path)


def extract_classified_hatches(
    document: Drawing,
    by_category: Mapping[str, Sequence[str]],
    include_blocks: bool = True,
) -> list[HatchPiece]:
    """Собирает заливки слоёв из групп, включая геометрию внутри INSERT.
    Если вставка стоит на выбранном слое (типично здание-блок), берём и те HATCH
    внутри блока, чей собственный слой в группы не входил — иначе заливка пропадает.
    Слой заливки, если он сам классифицирован, важнее слоя вставки.
    SOLID-HATCH на слое 0 внутри контура стен относится к зданию.
    """
    all_names = [str(layer.dxf.name) for layer in document.layers]
    groups = refine_layer_groups(by_category, all_names)
    layer_to_category: dict[str, str] = {}
    for category, names in groups.items():
        for name in names:
            layer_to_category[normalize_name(name)] = category
            layer_to_category.setdefault(layer_leaf(name), category)
    # Чертёж уже в метрах, $INSUNITS не используем.
    scale = 1.0
    pieces: list[HatchPiece] = []
    orphans: list[tuple[DXFEntity, str, tuple[str, ...]]] = []

    def emit(
        entity: DXFEntity,
        layer: str,
        category: str,
        block_path: tuple[str, ...],
    ) -> None:
        handle = str(entity.dxf.get("handle", "") or "")
        for outer, holes in _fill_rings(entity, scale):
            pieces.append(HatchPiece(
                category=category,
                layer=layer,
                vertices=outer,
                holes=holes,
                handle=handle,
                block_path=block_path,
            ))

    def consider(
        entity: DXFEntity,
        layer: str,
        chosen: str | None,
        block_path: tuple[str, ...],
    ) -> None:
        entity_type = entity.dxftype()
        if entity_type in FILL_TYPES:
            if chosen:
                emit(entity, layer, chosen, block_path)
            elif (
                is_generic_block_layer(layer)
                and not is_junk_layer(layer)
                and bool(entity.dxf.get("solid_fill", 0))
            ):
                orphans.append((entity, layer, block_path))
            return
        if chosen is None:
            return
        if entity_type in QUAD_TYPES:
            emit(entity, layer, chosen, block_path)
            return
        if entity_type in {"LWPOLYLINE", "POLYLINE"} and is_area_polyline_layer(layer):
            emit(entity, layer, chosen, block_path)

    for entity in document.modelspace():
        layer = str(entity.dxf.layer)
        category = _category_for_layer(layer, layer_to_category)
        consider(entity, layer, category, ())

        if not include_blocks or not isinstance(entity, Insert):
            continue

        for child, child_layer, block_path in _iter_block_content(entity):
            chosen = _category_for_layer(child_layer, layer_to_category)
            if (
                chosen is None
                and category
                and is_generic_block_layer(child_layer)
                and not is_junk_layer(child_layer)
            ):
                chosen = category
            consider(child, child_layer, chosen, block_path)

    _assign_generic_fills_to_buildings(pieces, orphans, scale)
    return pieces


def _assign_generic_fills_to_buildings(
    pieces: list[HatchPiece],
    orphans: Sequence[tuple[DXFEntity, str, tuple[str, ...]]],
    scale: float,
) -> None:
    """Слой 0: сплошная штриховка внутри пятна стен — это заливка здания."""
    if not orphans:
        return
    building_polys: list[ShapelyPolygon] = []
    building_layers: list[str] = []
    for piece in pieces:
        if piece.category != "building":
            continue
        for polygon in _piece_polygons(piece):
            building_polys.append(polygon)
            building_layers.append(piece.layer)
    if not building_polys:
        return

    tree = STRtree(building_polys)
    radius = config.settings().generic_fill_associate_m
    for entity, layer, block_path in orphans:
        handle = str(entity.dxf.get("handle", "") or "")
        for outer, holes in _hatch_rings(entity, scale):
            try:
                geom = ShapelyPolygon(
                    [(point[0], point[1]) for point in outer],
                    [[(point[0], point[1]) for point in ring] for ring in holes],
                )
            except Exception:
                continue
            if geom.is_empty:
                continue
            try:
                geom = make_valid(geom)
            except Exception:
                pass
            centroid = geom.centroid
            probe = centroid.buffer(radius) if radius else centroid
            nearby_idx = [int(index) for index in tree.query(probe)]
            if not nearby_idx:
                continue
            nearby = [building_polys[index] for index in nearby_idx]
            try:
                hull = unary_union(nearby).convex_hull
            except Exception:
                continue
            if hull.is_empty or hull.area <= 0:
                continue
            if geom.area > hull.area * 2:
                continue
            try:
                inside = hull.contains(centroid) or hull.covers(centroid)
            except Exception:
                continue
            if not inside:
                continue
            host_layer = building_layers[nearby_idx[0]]
            best = None
            for index in nearby_idx:
                distance = building_polys[index].distance(centroid)
                if best is None or distance < best:
                    best = distance
                    host_layer = building_layers[index]
            pieces.append(HatchPiece(
                category="building",
                layer=host_layer,
                vertices=outer,
                holes=holes,
                handle=handle,
                block_path=block_path,
            ))


def arrange_hatch_faces(
    pieces: Sequence[HatchPiece],
) -> list[tuple[BaseGeometry, str]]:
    clustered = _cluster_hatch_pieces(pieces)
    labeled: list[tuple[ShapelyPolygon, str]] = []
    for piece in clustered:
        for polygon in _piece_polygons(piece):
            if polygon.area >= config.settings().min_arrangement_area_m2:
                labeled.append((polygon, piece.category))
    if not labeled:
        return []

    lines: list[LineString] = []
    for polygon, _category in labeled:
        lines.append(LineString(polygon.exterior.coords))
        for interior in polygon.interiors:
            if len(interior.coords) >= 4:
                lines.append(LineString(interior.coords))

    try:
        noded = unary_union(lines)
        faces = [face for face in polygonize(noded) if face.area >= config.settings().min_arrangement_area_m2]
    except Exception:
        faces = []

    if not faces:
        return [(polygon, category) for polygon, category in labeled]

    sources = [polygon for polygon, _category in labeled]
    tree = STRtree(sources)
    arranged: list[tuple[BaseGeometry, str]] = []
    for face in faces:
        point = face.representative_point()
        covering: list[str] = []
        for index in tree.query(point):
            source = sources[int(index)]
            try:
                covers = source.covers(point) or source.contains(point)
            except Exception:
                continue
            if covers:
                covering.append(labeled[int(index)][1])
        if not covering:
            continue
        category = max(covering, key=lambda name: ARRANGEMENT_PRIORITY.get(name, 0))
        arranged.append((face, category))

    merged: dict[str, list[BaseGeometry]] = {}
    for face, category in arranged:
        merged.setdefault(category, []).append(face)
    return [
        (unary_union(geoms), category)
        for category, geoms in merged.items()
        if geoms
    ]


def draw_classified_hatches(
    document: Drawing,
    by_category: Mapping[str, Sequence[str]],
    *,
    include_blocks: bool = True,
    show: bool = True,
    save_path: str | Path | None = None,
) -> dict[str, Any]:
    """Функция 4. Группы слоёв все HATCH (и из INSERT) arrangement → план.
    """
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MplPath
    import matplotlib.pyplot as plt

    pieces = extract_classified_hatches(document, by_category, include_blocks=include_blocks)
    from_blocks = sum(1 for piece in pieces if piece.from_block)
    print(
        f"HATCH извлечено: {len(pieces)} "
        f"(в Модели {len(pieces) - from_blocks}, внутри INSERT {from_blocks})"
    )
    by_cat = Counter(piece.category for piece in pieces)
    for category, count in sorted(by_cat.items()):
        print(f"  {category}: {count}")

    if not pieces:
        print("В выбранных группах нет HATCH.")
        return {"pieces": 0, "faces": 0, "from_blocks": 0}

    faces = arrange_hatch_faces(pieces)
    print(f"Граней arrangement: {len(faces)}")

    figure, axes = plt.subplots(figsize=(10, 10))
    drawn = 0
    for category in HATCH_DRAW_ORDER:
        color = HATCH_DRAW_COLORS.get(category, (0.5, 0.5, 0.5))
        for geometry, face_category in faces:
            if face_category != category:
                continue
            for polygon in _iter_shapely_polygons(geometry):
                axes.add_patch(_polygon_patch(polygon, color, MplPath, PathPatch))
                drawn += 1

    axes.set_aspect("equal", adjustable="box")
    _focus_axes_on_geometries(axes, [geometry for geometry, _ in faces])
    axes.set_title(f"HATCH arrangement  ({drawn} полигонов)")
    axes.set_xlabel("x, м")
    axes.set_ylabel("y, м")
    figure.tight_layout()

    if save_path is not None:
        output = Path(save_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(output, dpi=150)
        print(f"Сохранено: {output}")

    if show:
        plt.show()
    else:
        plt.close(figure)

    return {
        "pieces": len(pieces),
        "from_blocks": from_blocks,
        "faces": drawn,
        "by_category": dict(by_cat),
    }


def _piece_polygons(piece: HatchPiece) -> list[ShapelyPolygon]:
    outer = [(point[0], point[1]) for point in piece.vertices]
    holes = [[(point[0], point[1]) for point in ring] for ring in piece.holes]
    try:
        geom = ShapelyPolygon(outer, holes)
    except Exception:
        return []
    if geom.is_empty:
        return []
    if not geom.is_valid:
        geom = make_valid(geom)
    return [
        polygon
        for polygon in _iter_shapely_polygons(geom)
        if polygon.area >= config.settings().min_arrangement_area_m2
    ]


def _iter_shapely_polygons(geometry: BaseGeometry) -> Iterator[ShapelyPolygon]:
    if geometry is None or geometry.is_empty:
        return
    geom_type = geometry.geom_type
    if geom_type == "Polygon":
        yield geometry  # type: ignore[misc]
    elif geom_type == "MultiPolygon":
        yield from geometry.geoms  # type: ignore[union-attr]
    elif geom_type == "GeometryCollection":
        for item in geometry.geoms:  # type: ignore[union-attr]
            yield from _iter_shapely_polygons(item)


def _cluster_hatch_pieces(pieces: Sequence[HatchPiece]) -> list[HatchPiece]:
    """Оставляет одно пятно площадки.

    В DWG часто два мира: участок и ситуационный план / легенда в сотнях
    метров в стороне. Берём связную группу в радиусе SITE_CLUSTER_RADIUS_M
    с наибольшей площадью дорог, газонов и зданий — не самое большое
    одиночное пятно (им может оказаться рамка ситплана).
    """
    ranked: list[HatchPiece] = []
    for piece in pieces:
        if _ring_area(piece.vertices) >= config.settings().min_arrangement_area_m2:
            ranked.append(piece)
    if not ranked:
        return list(pieces)

    clusters: list[list[HatchPiece]] = []
    remaining = list(ranked)
    while remaining:
        group = [remaining.pop(0)]
        changed = True
        while changed:
            changed = False
            next_remaining: list[HatchPiece] = []
            group_rings = [item.vertices for item in group]
            for piece in remaining:
                if _near_hatch_cluster(piece.vertices, group_rings):
                    group.append(piece)
                    changed = True
                else:
                    next_remaining.append(piece)
            remaining = next_remaining
        clusters.append(group)

    def score(group: Sequence[HatchPiece]) -> tuple[float, float]:
        project = 0.0
        total = 0.0
        for piece in group:
            area = _ring_area(piece.vertices)
            total += area
            if piece.category in PROJECT_CLUSTER_CATEGORIES:
                project += area
        return project, total

    return list(max(clusters, key=score))


def _ring_area(ring: Sequence[tuple[float, float, float]]) -> float:
    area = 0.0
    for (x1, y1, _), (x2, y2, _) in zip(ring, list(ring[1:]) + [ring[0]]):
        area += x1 * y2 - x2 * y1
    return abs(area) * 0.5


def _centroid_xy(ring: Sequence[tuple[float, float, float]]) -> tuple[float, float]:
    return (
        sum(point[0] for point in ring) / len(ring),
        sum(point[1] for point in ring) / len(ring),
    )


def _near_hatch_cluster(
    ring: Sequence[tuple[float, float, float]],
    cluster: Sequence[Sequence[tuple[float, float, float]]],
) -> bool:
    cx, cy = _centroid_xy(ring)
    radius_sq = config.settings().site_cluster_radius_m ** 2
    for other in cluster:
        ox, oy = _centroid_xy(other)
        if (cx - ox) ** 2 + (cy - oy) ** 2 <= radius_sq:
            return True
    return False


def _polygon_patch(polygon: ShapelyPolygon, color: tuple[float, float, float], mpl_path: Any, path_patch: Any) -> Any:
    vertices: list[tuple[float, float]] = []
    codes: list[int] = []

    def add_ring(coords: Sequence[tuple[float, float]]) -> None:
        ring = list(coords)
        if len(ring) >= 2 and ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) < 3:
            return
        start = (float(ring[0][0]), float(ring[0][1]))
        vertices.append(start)
        codes.append(mpl_path.MOVETO)
        for point in ring[1:]:
            vertices.append((float(point[0]), float(point[1])))
            codes.append(mpl_path.LINETO)
        vertices.append(start)
        codes.append(mpl_path.CLOSEPOLY)

    add_ring(polygon.exterior.coords)
    for interior in polygon.interiors:
        add_ring(interior.coords)
    path = mpl_path(vertices, codes)
    return path_patch(
        path,
        facecolor=(*color, 0.85),
        edgecolor=(0.15, 0.15, 0.15, 0.4),
        linewidth=0.2,
    )


def arrange_hatch_faces_by_layer(
    pieces: Sequence[HatchPiece],
) -> list[tuple[BaseGeometry, str, str]]:
    """Arrangement, как в функции 4, но грань помнит слой AutoCAD.

    Нужно, чтобы в Blender каждый DXF-слой стал отдельной коллекцией.
    """
    clustered = _cluster_hatch_pieces(pieces)
    labeled: list[tuple[ShapelyPolygon, str, str]] = []
    for piece in clustered:
        for polygon in _piece_polygons(piece):
            if polygon.area >= config.settings().min_arrangement_area_m2:
                labeled.append((polygon, piece.category, piece.layer))
    if not labeled:
        return []

    lines: list[LineString] = []
    for polygon, _category, _layer in labeled:
        lines.append(LineString(polygon.exterior.coords))
        for interior in polygon.interiors:
            if len(interior.coords) >= 4:
                lines.append(LineString(interior.coords))

    try:
        noded = unary_union(lines)
        faces = [face for face in polygonize(noded) if face.area >= config.settings().min_arrangement_area_m2]
    except Exception:
        faces = []

    if not faces:
        merged: dict[str, list[BaseGeometry]] = {}
        category_of: dict[str, str] = {}
        for polygon, category, layer in labeled:
            merged.setdefault(layer, []).append(polygon)
            category_of.setdefault(layer, category)
        return [
            (unary_union(geoms), category_of[layer], layer)
            for layer, geoms in merged.items()
            if geoms
        ]

    sources = [polygon for polygon, _category, _layer in labeled]
    tree = STRtree(sources)
    arranged: list[tuple[BaseGeometry, str, str]] = []
    for face in faces:
        point = face.representative_point()
        covering: list[tuple[int, str, str]] = []
        for index in tree.query(point):
            source = sources[int(index)]
            try:
                covers = source.covers(point) or source.contains(point)
            except Exception:
                continue
            if covers:
                category, layer = labeled[int(index)][1], labeled[int(index)][2]
                covering.append((ARRANGEMENT_PRIORITY.get(category, 0), category, layer))
        if not covering:
            continue
        _priority, category, layer = max(covering, key=lambda item: item[0])
        arranged.append((face, category, layer))

    merged_faces: dict[str, list[BaseGeometry]] = {}
    category_of = {}
    for face, category, layer in arranged:
        merged_faces.setdefault(layer, []).append(face)
        category_of.setdefault(layer, category)
    return [
        (unary_union(geoms), category_of[layer], layer)
        for layer, geoms in merged_faces.items()
        if geoms
    ]


def _earcut_polygon(polygon: ShapelyPolygon) -> tuple[Any, Any]:
    """Наружа + отверстия → треугольники. Высоты нет, только XY."""
    import numpy as np
    import mapbox_earcut as earcut

    oriented = orient(polygon, sign=1.0)
    exterior = np.asarray(list(oriented.exterior.coords)[:-1], dtype=np.float64)
    if len(exterior) < 3:
        return None, None
    rings = [exterior[:, :2]]
    counts = [len(exterior)]
    for hole in oriented.interiors:
        coords = np.asarray(list(hole.coords)[:-1], dtype=np.float64)
        if len(coords) < 3:
            continue
        rings.append(coords[:, :2])
        counts.append(len(coords))
    vertices = np.vstack(rings)
    indices = earcut.triangulate_float64(vertices, np.cumsum(counts))
    if len(indices) < 3:
        return None, None
    faces = np.asarray(indices, dtype=np.int64).reshape(-1, 3)
    return vertices, faces


def _layer_mesh(
    geometry: BaseGeometry,
    origin: tuple[float, float],
) -> tuple[list[list[float]], list[list[int]]]:
    ox, oy = origin
    vertices: list[list[float]] = []
    faces: list[list[int]] = []
    for polygon in _iter_shapely_polygons(geometry):
        if polygon.area < config.settings().min_arrangement_area_m2:
            continue
        try:
            xy, tri = _earcut_polygon(polygon)
        except Exception:
            continue
        if xy is None or tri is None:
            continue
        offset = len(vertices)
        for point in xy:
            vertices.append([
                round(float(point[0]) - ox, 4),
                round(float(point[1]) - oy, 4),
                0.0,
            ])
        for a, b, c in tri:
            faces.append([int(a) + offset, int(b) + offset, int(c) + offset])
    return vertices, faces


def _geometry_origin(
    geometries: Sequence[BaseGeometry],
    categories: Sequence[str] | None = None,
) -> tuple[float, float]:
    """Центр масс площади, не середина bbox.
    Копия/рамка в километрах от участка растягивает bbox: origin падает
    в пустоту, в Blender вокруг сетки ничего не видно.
    """
    chosen: list[BaseGeometry] = []
    if categories is not None and len(categories) == len(geometries):
        chosen = [
            geom
            for geom, category in zip(geometries, categories)
            if category in PROJECT_CLUSTER_CATEGORIES
            and geom is not None
            and not geom.is_empty
        ]
    if not chosen:
        chosen = [
            geom for geom in geometries
            if geom is not None and not geom.is_empty
        ]
    weight_x = 0.0
    weight_y = 0.0
    total = 0.0
    for geom in chosen:
        area = float(geom.area)
        if area <= 0:
            continue
        centroid = geom.centroid
        weight_x += centroid.x * area
        weight_y += centroid.y * area
        total += area
    if total <= 0:
        return 0.0, 0.0
    return weight_x / total, weight_y / total


def _find_blender() -> Path | None:
    if BLENDER and Path(BLENDER).exists():
        return Path(BLENDER)
    which = shutil.which("blender")
    if which:
        return Path(which)
    candidates: list[Path] = []
    for root in BLENDER_SEARCH_ROOTS:
        if not root.is_dir():
            continue
        if (root / "blender.exe").is_file():
            candidates.append(root / "blender.exe")
        candidates.extend(root.glob("Blender */blender.exe"))
    return max(candidates, default=None)


def find_oda_converter(explicit: str | Path | None = None) -> Path:
    """Ищет ODAFileConverter.exe (DWG → DXF)."""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(f"Не найден ODA File Converter:\n{path}")
        return path
    if ODA_CONVERTER and Path(ODA_CONVERTER).is_file():
        return Path(ODA_CONVERTER)
    candidates: list[Path] = []
    for root in ODA_SEARCH_ROOTS:
        if root.is_dir():
            candidates.extend(root.glob("ODAFileConverter*/ODAFileConverter.exe"))
            candidates.extend(root.glob("ODAFileConverter.exe"))
    if not candidates:
        raise FileNotFoundError(
            "Не найден ODAFileConverter.exe. Установите ODA File Converter "
            "или задайте ODA_CONVERTER / аргумент oda_exe."
        )
    return max(candidates, key=lambda item: item.stat().st_mtime)


def _hide_process_windows(pid: int) -> None:
    """Прячет окна процесса. ODA File Converter рисует своё окно после старта."""
    if os.name != "nt" or pid <= 0:
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        proc_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if proc_id.value == pid:
            user32.ShowWindow(hwnd, 0)
        return True

    user32.EnumWindows(callback, 0)


def _run_hidden(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Запуск без консоли и без окна программы."""
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        startupinfo=startupinfo,
        creationflags=creationflags,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    while True:
        if os.name == "nt":
            _hide_process_windows(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=0.05)
            break
        except subprocess.TimeoutExpired:
            continue
    return subprocess.CompletedProcess(command, proc.returncode or 0, stdout, stderr)


def convert_dwg_to_dxf(
    dwg_path: str | Path,
    output_dir: str | Path | None = None,
    oda_exe: str | Path | None = None,
) -> Path:
    """Конвертирует один DWG в DXF через ODA File Converter."""
    source = Path(dwg_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Нет файла DWG: {source}")
    target_dir = Path(output_dir or DXF_OUTPUT_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    converter = find_oda_converter(oda_exe)
    with tempfile.TemporaryDirectory() as raw:
        incoming = Path(raw) / "in"
        outgoing = Path(raw) / "out"
        incoming.mkdir()
        outgoing.mkdir()
        shutil.copy2(source, incoming / source.name)
        command = [
            str(converter), str(incoming), str(outgoing),
            DXF_VERSION, "DXF", "0", "1", "*.DWG",
        ]
        print("ODA:", converter)
        result = _run_hidden(command)
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr)
        produced = list(outgoing.glob("*.dxf"))
        if not produced:
            raise RuntimeError(
                f"ODA не создал DXF из {source.name}, код {result.returncode}"
            )
        target = target_dir / produced[0].name
        shutil.copy2(produced[0], target)
    print(f"DXF: {target}")
    return target


def _drawing_path(document: Drawing, fallback: str | Path | None = None) -> Path:
    filename = getattr(document, "filename", None) or fallback or "drawing.dxf"
    return Path(filename)


def open_drawing(
    path: str | Path,
    *,
    dxf_dir: str | Path | None = None,
    oda_exe: str | Path | None = None,
) -> Drawing:
    """Читает чертёж один раз: DWG конвертирует в DXF, возвращает Drawing."""
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".dxf":
        if not source.is_file():
            raise FileNotFoundError(f"Нет файла DXF: {source}")
        dxf_path = source.resolve()
    elif suffix == ".dwg":
        dxf_path = convert_dwg_to_dxf(source, dxf_dir, oda_exe)
    else:
        raise ValueError(f"Нужен .dwg или .dxf, получено: {source.suffix}")
    document = ezdxf.readfile(str(dxf_path))
    print(f"Чертёж прочитан: {dxf_path}")
    return document


def _result_dir() -> Path:
    raw = str(config.settings().output_dir or "").strip()
    path = Path(raw) if raw else OUTPUT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_pipeline(
    path: str | Path,
    *,
    oda_exe: str | Path | None = None,
    classify: bool = True,
    by_category: Mapping[str, Sequence[str]] | None = None, # пример by_category={"building": ["ГП_Здания", "ГП_Крыльца"],"pavement": ["ГП_Тротуар"],"green": ["ГП_Газон"],}
    draw_preview: bool = False,
    run_blender: bool = True,
) -> dict[str, Any]:
    """Полный пайплайн"""
    cfg = config.settings()
    oda = oda_exe if oda_exe is not None else (cfg.oda_converter or None)
    out = _result_dir()
    document = open_drawing(path, dxf_dir=out / "_dxf", oda_exe=oda or None)
    source = _drawing_path(document, path)
    layers = list_layers(document)
    print(f"Слоёв в файле: {len(layers)}")

    selection: LayerSelection | None = None
    if by_category is not None:
        groups = refine_layer_groups(by_category, layers)
    elif classify:
        selection = select_target_layers(layers)
        groups = refine_layer_groups(selection.by_category, layers)
        selection.by_category = groups
        print("Отобрано по категориям:")
        for category, names in groups.items():
            print(f"  {category}: {names}")
        if selection.invented:
            print("Модель придумала несуществующие имена:", selection.invented)
        layers_json = out / f"{source.stem}_layers.json"
        layers_json.parent.mkdir(parents=True, exist_ok=True)
        with layers_json.open("w", encoding="utf-8") as file:
            json.dump(selection.to_dict(), file, ensure_ascii=False, indent=2)
        print(f"Классификация: {layers_json}")
    else:
        raise ValueError("Нужна классификация слоёв или готовый by_category.")
    print("Типы после правки имён слоёв:")
    for category, names in groups.items():
        print(f"  {category}: {names}")

    if draw_preview:
        draw_classified_hatches(
            document, groups, show=False,
            save_path=str(out / f"{source.stem}_hatches.png"),
        )

    underlay = export_blender_underlay(
        document, groups,
        json_path=str(out / f"{source.stem}_underlay.json"),
        blend_path=str(out / f"{source.stem}_underlay.blend"),
        run_blender=run_blender,
    )
    return {
        "dxf": str(source),
        "layers": len(layers),
        "by_category": groups,
        "underlay": underlay,
    }


def export_blender_underlay(
    document: Drawing,
    by_category: Mapping[str, Sequence[str]],
    *,
    include_blocks: bool = True,
    json_path: str | Path | None = None,
    blend_path: str | Path | None = None,
    run_blender: bool = True,
) -> dict[str, Any]:
    """
    Каждый слой AutoCAD становится отдельной коллекцией. Без выдавливания:
    HATCH через earcut режется на треугольники в Z=0.
    """
    source = _drawing_path(document)
    json_output = Path(json_path or OUTPUT_DIR / f"{source.stem}_underlay.json")
    blend_output = Path(blend_path or OUTPUT_DIR / f"{source.stem}_underlay.blend")

    pieces = extract_classified_hatches(document, by_category, include_blocks=include_blocks)
    from_blocks = sum(1 for piece in pieces if piece.from_block)
    print(
        f"HATCH для подложки: {len(pieces)} "
        f"(Модель {len(pieces) - from_blocks}, INSERT {from_blocks})"
    )
    if not pieces:
        return {"layers": 0, "json": None, "blend": None}

    faces = arrange_hatch_faces_by_layer(pieces)
    origin = _geometry_origin(
        [geometry for geometry, _category, _layer in faces],
        [category for _geometry, category, _layer in faces],
    )
    layers_payload: list[dict[str, Any]] = []
    for geometry, category, layer in sorted(faces, key=lambda item: item[2]):
        vertices, triangles = _layer_mesh(geometry, origin)
        if not triangles:
            continue
        color = HATCH_DRAW_COLORS.get(category, (0.55, 0.55, 0.55))
        layers_payload.append({
            "dxf_layer": layer,
            "category": category,
            "color": list(color),
            "vertices": vertices,
            "faces": triangles,
        })
        print(f"  слой {layer!r}: {len(vertices)} вершин, {len(triangles)} треугольников")

    payload = {
        "source": str(source),
        "units": "meters",
        "kind": "hatch_underlay",
        "origin": [round(origin[0], 4), round(origin[1], 4)],
        "root_collection": ROOT_COLLECTION,
        "layers": layers_payload,
    }
    json_output.parent.mkdir(parents=True, exist_ok=True)
    with json_output.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False)
    print(f"JSON подложки: {json_output}")

    blender = _find_blender() if run_blender else None
    script = UNDERLAY_SCRIPT
    if blender is None:
        print(
            "Blender не найден. Соберите .blend командой:\n"
            f"  blender --background --python {script} -- "
            f"{json_output} {blend_output}"
        )
        return {
            "layers": len(layers_payload),
            "json": str(json_output),
            "blend": None,
            "from_blocks": from_blocks,
        }

    import subprocess

    blend_output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(blender),
        "--background",
        "--python",
        str(script),
        "--",
        str(json_output.resolve()),
        str(blend_output.resolve()),
    ]
    print("Blender:", blender)
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        print(f"Blender завершился с кодом {completed.returncode}")
        return {
            "layers": len(layers_payload),
            "json": str(json_output),
            "blend": None,
            "from_blocks": from_blocks,
        }
    print(f"BLEND: {blend_output}")
    return {
        "layers": len(layers_payload),
        "json": str(json_output),
        "blend": str(blend_output),
        "from_blocks": from_blocks,
    }

def _focus_axes_on_geometries(axes: Any, geometries: Sequence[BaseGeometry]) -> None:
    xs: list[float] = []
    ys: list[float] = []
    for geometry in geometries:
        if geometry is None or geometry.is_empty:
            continue
        min_x, min_y, max_x, max_y = geometry.bounds
        xs.extend((min_x, max_x))
        ys.extend((min_y, max_y))
    if not xs:
        return
    pad_x = (max(xs) - min(xs)) * 0.08 or 1.0
    pad_y = (max(ys) - min(ys)) * 0.08 or 1.0
    axes.set_xlim(min(xs) - pad_x, max(xs) + pad_x)
    axes.set_ylim(min(ys) - pad_y, max(ys) + pad_y)


