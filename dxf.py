from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import ezdxf
from ezdxf import path as ezpath
from ezdxf.document import Drawing
from ezdxf.entities import DXFEntity, Insert

# Точность спрямления дуг, сплайнов и эллипсов, м.
FLATTENING_DISTANCE_M = 0.02

MAX_BLOCK_DEPTH = 6

# $INSUNITS -> коэффициент перевода в метры.
INSUNITS_TO_METERS: dict[int, float] = {
    0: 1.0, 1: 0.0254, 2: 0.3048, 3: 1609.344, 4: 0.001, 5: 0.01,
    6: 1.0, 7: 1000.0, 8: 2.54e-8, 9: 2.54e-5, 10: 0.9144, 11: 1e-10,
    12: 1e-9, 13: 1e-6, 14: 0.1, 15: 10.0, 16: 100.0, 17: 1e9, 20: 0.3048,
}


def normalize_name(value: str | None) -> str:
    """Нормализует имя слоя для устойчивого сравнения (регистр, Ё/Е)."""
    return (value or "").upper().replace("Ё", "Е").strip()

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


def list_layers(dxf_path: str | Path, include_blocks: bool = True) -> list[str]:
    """
    include_blocks=True учитывать и объекты внутри вставленных блоков
    """
    stats = _layer_report(dxf_path, include_blocks=include_blocks)
    return sorted(item.name for item in stats)


def _layer_report(dxf_path: str | Path, include_blocks: bool = True) -> list[LayerStat]:
    """Список слоёв с составом (сколько и каких сущностей на каждом)."""
    document = ezdxf.readfile(str(dxf_path))
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


def _iter_block_content(
    insert: Insert, parent_layer: str | None = None, depth: int = 0,
) -> Iterator[tuple[DXFEntity, str, tuple[str, ...]]]:
    """Рекурсивно обходит содержимое вставки блока (до MAX_BLOCK_DEPTH)."""
    insert_layer = _resolve_layer(insert, parent_layer)
    block_path = (str(insert.dxf.name),)

    try:
        children = list(insert.virtual_entities())
    except Exception:
        return

    for child in children:
        layer = _resolve_layer(child, insert_layer)
        if isinstance(child, Insert):
            if depth < MAX_BLOCK_DEPTH:
                for item, item_layer, path in _iter_block_content(child, insert_layer, depth + 1):
                    yield item, item_layer, block_path + path
            yield child, layer, block_path
            continue
        yield child, layer, block_path


def _resolve_layer(entity: DXFEntity, parent_layer: str | None) -> str:
    """Слой "0" внутри блока означает слой вставки — как в AutoCAD."""
    layer = str(entity.dxf.layer)
    if parent_layer and normalize_name(layer) == "0":
        return parent_layer
    return layer

# ФУНКЦИЯ 2. Отбор нужных слоёв языковой моделью

# Категории объектов площадки, которые нас интересуют.
# TARGET_OBJECT_TYPES: dict[str, str] = {
#     "site_boundary": "граница участка, границы ГПЗУ, красные линии участка",
#     "building_footprint": "пятна застройки, контуры и штриховки зданий, ТП, БКТ",
#     "building_overhang": "нависающие части зданий, консоли, эркеры",
#     "canopy": "навесы, козырьки",
#     "porch": "крыльца, входные группы, ступени, пандусы",
#     "driveway": "проезды, внутриквартальные дороги, асфальт проездов",
#     "sidewalk": "тротуары, пешеходные дорожки, плитка пешеходных зон",
#     "guest_parking": "гостевые стоянки, парковочные площадки, машиноместа",
#     "road_marking": "разметка проездов и стоянок, разметка МГН",
#     "rubber_surface": "резиновая крошка, покрытие детских и спортивных площадок",
#     "waste_platform": "площадки ТБО, мусорные площадки, контейнерные площадки",
#     "lawn": "газоны, обычное озеленение, посевной газон",
#     "flowerbed": "клумбы, цветники, цветочное озеленение",
#     "tree": "деревья, отдельно стоящие деревья, крупномеры — не ограждения и не полосы леса",
#     "shrub": "кустарники, кусты, живые изгороди — не заборы",
#     "maf": "МАФ: скамьи, урны, велопарковки, игровое и спортивное оборудование — не выноски",
#     "curbs": "бортовой камень, лотки, поребрики — не оси дорог",
#     "fence": "ограды, заборы, ограждения участка",
#     "terrain": "рельеф: горизонтали, отметки, топографическая поверхность",
# }
TARGET_OBJECT_TYPES: dict[str, str] = {
    "site_boundary": (
        "граница участка, ГПЗУ, красные линии участка — не граница улицы, не рамка листа"
    ),
    "building": (
        "здания и пристройки: пятна, контуры, штриховки, ТП, БКТ, "
        "нависания, навесы, козырьки, крыльца, ступени, пандусы"
    ),
    "pavement": (
        "твёрдые покрытия: проезды, асфальт, тротуары, плитка, "
        "гостевые стоянки, резиновая крошка, площадки ТБО — не разметка"
    ),
    "green": (
        "газоны, цветники, клумбы, озеленение поверхностью — не деревья и не кусты"
    ),
    "vegetation": (
        "деревья и кустарники как посадки/блоки — не полосы леса, не живая изгородь-забор"
    ),
    "furniture": (
        "МАФ: скамьи, урны, игровое и спортивное оборудование — не выноски"
    ),
    "fence": (
        "ограды, заборы, ограждения участка — не бордюр и не оси"
    ),
}

# Слои, которые нужно отсеять: оформление листа, размеры, легенда, схемы.
EXCLUDE_HINT = (
    "оформление листов и рамки, штампы, размеры и выноски, ведомости, "
    "экспликации, легенда и условные обозначения, схемы движения и ОДИ, "
    "разбивочные оси и оси дорог, инженерные сети, трассы коммуникаций, "
    "топосъёмка и геодезические пункты, зоны ограничений (СЗЗ), "
    "служебные слои вида Defpoints, граница улицы если есть граница участка"
)

DEFAULT_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:1234/v1")
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "google/gemma-4-e4b")
DEFAULT_API_KEY = os.environ.get("LLM_API_KEY", "")

# Слоёв в чертеже бывает много
DEFAULT_BATCH_SIZE = 40

SYSTEM_PROMPT = (
    "Ты помогаешь разбирать чертежи планировки участка (ПЗУ) в формате DXF. "
    "Тебе дают имена слоёв AutoCAD, а ты определяешь, к какому типу "
    "объектов площадки относится каждый слой. "
    "Не рассуждай вслух. Верни только один JSON-объект, без markdown, "
    "без тегов channel/think и без текста до или после JSON."
)

THINK_BLOCK_RE = re.compile(
    r"<think>.*?</think>"
    r"|<\|channel\|>thought.*?(?=<\|channel\|>final|$)"
    r"|<\|start\|>.*?<\|channel\|>thought.*?(?=<\|channel\|>final|$)",
    re.DOTALL | re.IGNORECASE,
)
CHANNEL_FINAL_RE = re.compile(r"<\|channel\|>final", re.IGNORECASE)


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


HINT_SUFFIX_RE = re.compile(r"\s*\[.*?\]\s*$")
# Номер листа в имени слоя AutoCAD: «1 Граница участка», «1_ГП_…», «2 СЗЗ_…».
SHEET_PREFIX_RE = re.compile(r"^\d+[_\s]+")


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
        'Пример ответа: {"sidewalk": ["1 ГП_УДС_Тротуары"], "lawn": ["1 ГП_Штриховки_газон"]}'
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
    return None


def select_target_layers(
    layer_names: Sequence[str],
    hints: Mapping[str, str] | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str = DEFAULT_API_KEY,
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout: float = 300.0,
) -> LayerSelection:
    """
    Функция 2. Имена слоёв нужные нам слои по категориям через LLM.
    """
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
            reply = _request_chat_completion(
                system=SYSTEM_PROMPT,
                user=_build_prompt(batch, hints)
                + "\n\nОтвет — только JSON, без thinking и без пояснений.",
                model=model, base_url=base_url, api_key=api_key, timeout=timeout,
            )
            parsed = _parse_selection(reply)

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
    """Собирает текст из content / reasoning — Gemma кладёт мысли в отдельные поля."""
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
    """Убирает thinking-каналы Gemma/Harmony и markdown-ограждение."""
    if CHANNEL_FINAL_RE.search(text):
        text = CHANNEL_FINAL_RE.split(text, maxsplit=1)[-1]
    text = THINK_BLOCK_RE.sub("", text)
    text = re.sub(r"<\|[^|>]+\|>", " ", text)
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE)
    return text.strip()


def _iter_json_objects(text: str) -> Iterator[dict[str, Any]]:
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            return
        try:
            data, consumed = decoder.raw_decode(text[start:])
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
    candidates = list(_iter_json_objects(_strip_reasoning(reply)))
    if not candidates:
        candidates = list(_iter_json_objects(reply))
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
    base_url: str = DEFAULT_BASE_URL, api_key: str = DEFAULT_API_KEY, timeout: float = 300.0,
) -> str:
    """Один запрос к OpenAI-совместимому API. Возвращает текст ответа."""
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

CURVE_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE", "HELIX"}
FILL_TYPES = {"HATCH", "MPOLYGON"}
QUAD_TYPES = {"SOLID", "TRACE", "3DFACE"}
TEXT_TYPES = {"TEXT", "MTEXT"}


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
    dxf_path: str | Path,
    layer_names: Iterable[str],
    include_blocks: bool = True,
    to_meters: bool = True,
) -> list[Shape]:
    document = ezdxf.readfile(str(dxf_path))
    modelspace = document.modelspace()

    wanted = {normalize_name(name) for name in layer_names}
    scale = _unit_scale(document) if to_meters else 1.0

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


def _unit_scale(document: Drawing) -> float:
    """Коэффициент перевода координат чертежа в метры по $INSUNITS."""
    insunits = int(document.header.get("$INSUNITS", 0) or 0)
    scale = INSUNITS_TO_METERS.get(insunits)
    if scale is None:
        raise ValueError(f"Неизвестное значение $INSUNITS = {insunits}.")
    return scale


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
    return FLATTENING_DISTANCE_M / scale if scale else FLATTENING_DISTANCE_M


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
    """Кривая -> спрямлённые контуры в мировых координатах."""
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

if "__main__" == __name__:
    #print(_layer_report("output_dxf/ПЗУ Касимовская 33.dxf"))
    #print(list_layers("output_dxf/ПЗУ Касимовская 33.dxf"))
    DXF_PATH = "output_dxf/ПЗУ Касимовская 33 (1).dxf"

    # Функция 1
    layers = list_layers(DXF_PATH)
    print(f"Слоёв в файле: {len(layers)}")
    print(layers)

    # Функция 2
    selection = select_target_layers(layers)

    print("\nОтобрано по категориям:")
    for category, layer_names in selection.by_category.items():
        print(f"  {category}: {layer_names}")

    if selection.invented:
        print("Модель придумала несуществующие имена:", selection.invented)

    target_layers = selection.by_category.get("site_boundary", [])

    # Функция 3
    shapes = read_layer_geometry(DXF_PATH, target_layers)

    print(f"\nОбъектов геометрии получено: {len(shapes)}")
    for shape in shapes[:5]:
        print(f"  слой={shape.layer!r} тип={shape.dxftype} kind={shape.kind} "
              f"вершин={len(shape.vertices)} отверстий={len(shape.holes)}")

    for shape in shapes[:3]:
        print(f"\nслой={shape.layer!r} тип={shape.dxftype} kind={shape.kind}")
        print("вершины (x, y, z), м:")
        for point in shape.vertices:
            print(" ", point)
        for i, hole in enumerate(shape.holes):
            print(f"отверстие {i}:")
            for point in hole:
                print("  ", point)
