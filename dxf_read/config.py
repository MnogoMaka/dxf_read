"""Константы проекта: слои, типы объектов, пути, модели из .env."""

from __future__ import annotations

import contextvars
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
        return
    load_dotenv(path, override=False)


_load_env_file(PROJECT_ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


#сервисы (.env)
LLM_MODEL = env("LLM_MODEL", "")
LLM_BASE_URL = env("LLM_BASE_URL", "")
LLM_API_KEY = env("LLM_API_KEY", "")
DEFAULT_SOURCE = env("DEFAULT_SOURCE", "input_dwg/ПЗУ Касимовская 33 (1).dwg")
BLENDER = env("BLENDER")
ODA_CONVERTER = env("ODA_CONVERTER")

#Совместимые имена для вызовов LLM
DEFAULT_MODEL = LLM_MODEL
DEFAULT_BASE_URL = LLM_BASE_URL
DEFAULT_API_KEY = LLM_API_KEY

INPUT_DIR = resolve_path(env("INPUT_DIR", "input_dwg"))
OUTPUT_DIR = resolve_path(env("OUTPUT_DIR", "output"))
DXF_OUTPUT_DIR = resolve_path(env("DXF_OUTPUT_DIR", str(OUTPUT_DIR / "_dxf")))
UNDERLAY_SCRIPT = PACKAGE_DIR / "build_hatch_underlay.py"
ROOT_COLLECTION = "SITE_UNDERLAY"

#геометрия
FLATTENING_DISTANCE_M = 0.02
CONTOUR_SNAP_TOLERANCE_M = 0.02
MIN_ARRANGEMENT_AREA_M2 = 0.05
# Рамка листа и копии «Ном.пера» бывают десятки километров. Площадка меньше.
MAX_CONTOUR_SPAN_M = 8000.0
# Открытые линии ограждений становятся узкими полигонами-препятствиями.
FENCE_WIDTH_M = 0.20
# Остров газона мельче этой доли от самого большого куска того же слоя не строится.
GREEN_ISLAND_KEEP_RATIO = 0.05
# Слой 0 / HATCH: SOLID-заливка внутри контура стен здания.
GENERIC_FILL_ASSOCIATE_M = 15.0
FILL_POLYLINE_TOKENS = ("ЗАЛИВ", "ЗАПОЛН", "ПЯТН")
DEFAULT_BATCH_SIZE = 600
DXF_VERSION = "ACAD2018"
PROJECT_CLUSTER_CATEGORIES = frozenset({"pavement", "green", "building"})

CURVE_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE", "HELIX"}
FILL_TYPES = {"HATCH", "MPOLYGON"}
QUAD_TYPES = {"SOLID", "TRACE", "3DFACE"}
TEXT_TYPES = {"TEXT", "MTEXT"}

#конвертеры
ODA_SEARCH_ROOTS = [
    Path(r"C:\Program Files\ODA"),
    Path(r"C:\Program Files (x86)\ODA"),
    Path(r"C:\Program Files"),
]
BLENDER_SEARCH_ROOTS = [
    Path(r"C:\Program Files\Blender Foundation"),
    Path(r"C:\Program Files (x86)\Blender Foundation"),
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\Blender"),
    Path(r"C:\Program Files\Steam\steamapps\common\Blender"),
]

#типы объектов площадки
TARGET_OBJECT_TYPES: dict[str, str] = {
    "site_boundary": (
        "граница участка, ГПЗУ — не граница улицы, не рамка листа и не красные линии"
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
        "газоны, леса и газоны, озеленение поверхностью — не деревья, не кусты, не цветники и не клумбы"
    ),
    "vegetation": (
        "деревья, кустарники, цветники и клумбы как отметки — не полосы газона, не живая изгородь-забор"
    ),
    "furniture": (
        "МАФ: скамьи, урны, игровое и спортивное оборудование — не выноски"
    ),
    "fence": (
        "ограды, заборы, ограждения участка — не бордюр и не оси"
    ),
}

EXCLUDE_HINT = (
    "оформление листов и рамки, штампы, размеры и выноски, ведомости, "
    "экспликации, легенда и условные обозначения, схемы движения и ОДИ, "
    "разбивочные оси и оси дорог, инженерные сети, трассы, выпуски, колодцы, "
    "топосъёмка, горизонтали, вертикалка, подписи улиц LABEL_, "
    "зоны ограничений (СЗЗ), интерьер, Defpoints, копии «Ном._пера», "
    "Civil 3D GRID/TABLE/VOLUME, PDF-подложки, граница улицы если есть граница участка, "
    "красные линии"
)

# Подстроки в «хвосте» имени слоя (после xref $0$ / |) → тип объекта.
LAYER_CATEGORY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("green", (
        "ГАЗОН", "ОЗЕЛЕН", "ЛЕСА И ГАЗОН",
        "ЗЕЛЕНАЯ ЗОНА", "ЗЕЛЕНЬ",
    )),
    ("pavement", (
        "АСФАЛЬТ", "ТРОТУАР", "ПРОЕЗД", "ГОСТЕВ", "РЕЗИН", "КРОШК",
        "ПЛИТК", "ДОРОГ", "УДС", "ЛОТКИ", "БОРТ", "ТБО",
        "ПЛОЩАДК", "ОТМОСТК", "МОЩЕН", "ПАРКОВ", "ПАРКИНГ",
    )),
    ("vegetation", ("ДЕРЕВ", "КУСТАР", "ЦВЕТНИК", "КЛУМБ")),
    ("furniture", ("МАФ", "ФОНАР", "ФОНТАН")),
    ("fence", ("ОГРАД", "ОГРАЖД", "ЗАБОР")),
    ("building", (
        "ШТРИХОВКИ_АР", "ШТРИХ_ЗД", "ЗДАНИ", "НАВИС", "КРЫЛЬЦ",
        "НАВЕС", "СТУПЕН", "ЛЕСТН", "ПАНДУС", "ПАВИЛЬОН",
        "СТЕН", "ЗАЛИВ", "ЗАПОЛН",
    )),
    ("site_boundary", ("ГРАНИЦА УЧАСТ", "ГПЗУ")),
)

# Xref Civil/NanoCAD: «Блок$0$Слой», «Подложка|Слой». Снимать все уровни.
XREF_LEAF_RE = re.compile(r"^.*?(?:\$0\$|\|)")
PENA_SUFFIX_RE = re.compile(r"_НОМ\._ПЕРА__\d+$", re.IGNORECASE)
GENERIC_BLOCK_LAYERS = frozenset({"0", "DEFPOINTS", "HATCH", "LAYER"})
JUNK_LAYER_TOKENS = (
    "НОМ._ПЕРА", "LABEL_", "POINTS_GRID", "POINTS_GRADE", "NULL_WORKS",
    "ИНТЕРЬЕР", "ГОРИЗОНТАЛ", "ВЕРТИКАЛК", "DEFPOINTS", "ЭКСПЛИКАЦ",
    "ВЫНОСК", "ШТАМП", "C-ROAD-", "ТПП_РАЗМЕР", "ТПП_ТЕКСТ",
    "РАЗМЕР", "РАЗМЕТК", "ОСИ", "ОСЕВ", "OSEV",
    "ТЕКСТ", "PDF ", "PDF_",
    "ВИДОВОЙ ЭКРАН", "ОДД", "ДВИЖЕНИ",
    "ХАРАКТЕРИСТИКА", "ТРАССА", "ВЕНТИЛЯТОР", "КОЛОД", "СЕТИ",
    "ГРАНИЦА УЛИЦ", "ГРАНИЦА ЗАКАЗ", "ГРАНИЦА РАСТИТЕЛЬНОСТ",
    "СИТУАЦИОН", "КАРТОГРАМ", "КАТОГРАМ", "ЛЕГЕНД",
    "REV_А-", "REV_A-",
    "VOLUME", "TABLE", "GRID",
)

ARRANGEMENT_PRIORITY: dict[str, int] = {
    "site_boundary": 0,
    "fence": 1,
    "green": 2,
    "pavement": 3,
    "furniture": 4,
    "vegetation": 5,
    "building": 6,
    "none": 7,
}

HATCH_DRAW_COLORS: dict[str, tuple[float, float, float]] = {
    "green": (0.42, 0.68, 0.32),
    "pavement": (0.50, 0.50, 0.52),
    "building": (0.78, 0.74, 0.68),
    "furniture": (0.80, 0.55, 0.20),
    "vegetation": (0.22, 0.48, 0.20),
    "fence": (0.35, 0.28, 0.22),
    "site_boundary": (0.20, 0.20, 0.20),
    "none": (0.72, 0.72, 0.70),
}

HATCH_DRAW_ORDER = (
    "green", "pavement", "building", "furniture", "vegetation", "fence", "site_boundary", "none",
)

# --- промпт и разбор ответа LLM ---
SYSTEM_PROMPT = (
    "Ты помогаешь разбирать чертежи планировки участка (ПЗУ) в формате DXF. "
    "Тебе дают имена слоёв AutoCAD, а ты определяешь, к какому типу "
    "объектов площадки относится каждый слой. "
    "Не рассуждай вслух. Верни только один JSON-объект, без markdown, "
    "без тегов channel/think и без текста до или после JSON. "
    "JSON должен быть строгим: без висячих запятых."
)

# Ключи бесшовных картинок. none в ответ модели входит, файла у него нет.
SURFACE_TEXTURE_KEYS = ("grass", "tile", "sidewalk", "asphalt")

TEXTURE_SYSTEM_PROMPT = (
    "Ты назначаешь бесшовную текстуру покрытия слоям чертежа планировки участка. "
    "Тебе дают точные имена слоёв AutoCAD, а ты выбираешь одну текстуру на слой. "
    "Не рассуждай вслух. Верни только один JSON-объект, без markdown, "
    "без тегов channel/think и без текста до или после JSON. "
    "JSON должен быть строгим: без висячих запятых."
)

# __LAYERS__ подменяется массивом имён. Фигурные скобки в примере ответа здесь намеренно.
TEXTURE_INSTRUCTIONS = """\
Текстуры. В ответе у каждого слоя ровно один ключ:
- grass — газон, полоса озеленения, зелёная поверхность. Не дерево, не куст, не цветник, не клумба и не живая изгородь.
- tile — плитка, брусчатка, мощение плиткой.
- sidewalk — тротуар, пешеходный проход.
- asphalt — асфальт, проезд, проезжая часть, дорога, гостевая стоянка, парковка, паркинг.
- none — текстуры нет. Сюда относятся резина, резиновая крошка, резиновое покрытие, безопасное покрытие площадки, борт, поребрик, лоток, площадка ТБО, разметка, здание, забор, дерево, куст, МАФ, граница участка и любое имя, в котором нет материала покрытия.

Как выбирать, читая имя слева направо:
1. Номер листа, «ГП», «УДС», имя xref, «$0$» и «|» материалом не являются. Пропусти их и смотри имя дальше.
2. Первое слово, которое называет одну из текстур выше, и есть ответ. Слово правее не перебивает его, даже если оно тоже называет покрытие.
3. «асфальт_тротуар» — asphalt: асфальт стоит раньше тротуара.
4. «тротуар_асфальт» — sidewalk: тротуар стоит раньше асфальта.
5. «тротуарная_плитка» и «тротуарная плитка» — sidewalk: тротуар стоит раньше плитки.
6. «плитка_тротуар» — tile: плитка стоит раньше тротуара.
7. «резиновая_крошка_плитка» — none: слово про резину стоит раньше плитки и картинки не просит.
8. «газон_тротуар» — grass: газон стоит раньше тротуара.
9. Если такого слова нет — none. Не назначай текстуру по догадке и не ставь grass только из-за того, что слой кажется зелёным.

Имена слоёв из чертежа — JSON-массив точных строк:
[
__LAYERS__
]

Верни JSON-объект: ключ — точное имя слоя из массива, значение — grass, tile, sidewalk, asphalt или none.
Каждый слой укажи ровно один раз. Копируй имя целиком, ничего не отрезай и не придумывай. Не ставь запятую перед }.
Пример: {"1 ГП_асфальт_тротуар": "asphalt", "Газон": "grass", "Борт": "none"}
"""

@dataclass
class PipelineSettings:
    """Параметры прогона. Пустое переопределение не используется: его даёт settings()."""

    generic_fill_associate_m: float = 15.0
    min_arrangement_area_m2: float = 0.05
    flattening_distance_m: float = 0.02
    contour_snap_tolerance_m: float = 0.02
    contour_arrangement_mode: str = "layer"
    contour_from_fills: bool = False
    max_contour_span_m: float = 8000.0
    fence_width_m: float = FENCE_WIDTH_M
    cut_vegetation: bool = False
    omit_symbols: bool = False
    omit_fences: bool = False
    scene_scale: float = 1.0
    origin_mode: str = "mass"
    batch_size: int = 600
    llm_timeout: float = 300.0
    fill_polyline_tokens: tuple[str, ...] = ("ЗАЛИВ", "ЗАПОЛН", "ПЯТН")
    junk_layer_tokens: tuple[str, ...] = ()
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    oda_converter: str = ""
    output_dir: str = ""


_SETTINGS: contextvars.ContextVar[PipelineSettings | None] = contextvars.ContextVar(
    "dwgreader_pipeline_settings",
    default=None,
)


def settings() -> PipelineSettings:
    """Текущие параметры: переопределение аддона или значения этого модуля."""
    custom = _SETTINGS.get()
    if custom is not None:
        return custom
    return PipelineSettings(
        generic_fill_associate_m=GENERIC_FILL_ASSOCIATE_M,
        min_arrangement_area_m2=MIN_ARRANGEMENT_AREA_M2,
        flattening_distance_m=FLATTENING_DISTANCE_M,
        contour_snap_tolerance_m=CONTOUR_SNAP_TOLERANCE_M,
        contour_arrangement_mode="layer",
        contour_from_fills=False,
        max_contour_span_m=MAX_CONTOUR_SPAN_M,
        cut_vegetation=False,
        omit_symbols=False,
        omit_fences=False,
        scene_scale=1.0,
        origin_mode="mass",
        batch_size=DEFAULT_BATCH_SIZE,
        fill_polyline_tokens=FILL_POLYLINE_TOKENS,
        junk_layer_tokens=JUNK_LAYER_TOKENS,
        llm_model=DEFAULT_MODEL,
        llm_base_url=DEFAULT_BASE_URL,
        llm_api_key=DEFAULT_API_KEY,
        oda_converter=ODA_CONVERTER,
        output_dir=str(OUTPUT_DIR),
    )


@contextmanager
def use_settings(custom: PipelineSettings):
    token = _SETTINGS.set(custom)
    try:
        yield custom
    finally:
        _SETTINGS.reset(token)


HINT_SUFFIX_RE = re.compile(r"\s*\[.*?\]\s*$")
SHEET_PREFIX_RE = re.compile(r"^\d+[_\s]+")
CHANNEL_FINAL_RE = re.compile(r"<\|channel\|>final", re.IGNORECASE)
THINK_BLOCK_RE = re.compile(
    r"<think>.*?</think>"
    r"|<\|channel\|>thought.*?(?=<\|channel\|>final|$)"
    r"|<\|start\|>.*?<\|channel\|>thought.*?(?=<\|channel\|>final|$)",
    re.DOTALL | re.IGNORECASE,
)
