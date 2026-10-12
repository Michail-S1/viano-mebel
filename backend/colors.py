"""Conservative furniture finish extraction from source product names.

No database mutations and no identity changes. Explicit colour always wins.
The vocabulary was audited against all 44,160 local catalogue records and the
supplier lists. Collection palettes never imply an individual item's colour.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# Exact supplier shade names and common spelling variants. Longest matches win;
# this retains "Браш Деним Блю", "Графит софт NEW" and other compound finishes.
# Models with ambiguous colour-like names are handled separately below.
_COLOR_DATA = """
Атлантик софт|Шимо светлый|Шимо темный|Дуб янтарный|Молочная|Молочное|Золотой|Золотой металлик|Cr глянцевый|Бежевый глянец|Бежевый матовый|Белый супермат|Белая матовая|Ольха|Весна|Мрамор итал.|Мрамор итальян.
Блю|Вижн|Вижен|Айс|Латте|Кашемир|Кашмир|Кашнмир|Кашемир Новый|Кашемир глянец
Белый|Белая|Белое|Белые|Черный|Черная|Черное|Черные|Серый|Серая|Серое|Серые
Беж|Бежевый|Бежевая|Бежевые|Зеленый|Зеленая|Зеленые|Голубой|Голубая
Синий|Синяя|Желтый|Желтая|Красный|Красная|Коричневый|Коричневая
Розовый|Розовая|Фиолетовый|Фиолетовая|Сиреневый|Сиреневая|Изумруд|Изумрудный
Молоко|Молочный|Крем|Кремовый|Сталь|Керамика|Керамика глянец|Керамика графит
Керамика белый|Керамика бежевый|Моно Керамика|Фисташка|Фисташковый структурный
Фисташковый текстурный|Графит|Графит серый|Графит глянец|Графит матовый
Графит структурный|Графит софт|Графит софт NEW|Графит Скин|Графит Меланж
Серый графит 0162|Серый шелк|Серый софт|Серый дым|Серый дым софт|Серый дом софт
Серый эмалит|Белый эмалит|Серый металлик|Черный металлик|Белый металлик
Белый глянец|Белый глянцевый|Белый гянец|Белый глянец холодный|Белый глянец Бруно
Белый матовый|Белый софт|Белый Скин|Белый снег|Белый снег софт|Белый жемчуг
Белый текстурный|Белая текстурная|Белая тесктурная|Белый шагрень|Черная шагрень
Серая шагрень|Белый гладкий|Белый бриллиант|Белый поры дерева|Белое дерево
Белый Антарес|Белый Каспий|Черный Каспий|Серый Каспий|Каспий черное золото
Белый ФВ|Венге ФВ|Дуб беленый ФВ|Дуб венге ФВ|Сандал светлый ФВ
Черный глянец|Черный матовый|Черный Тунис|Черный чай|Королевский синий
Глиняный серый|Сумеречный голубой|Голубой текстурный|Пыльная роза
Пепельный беж|Теплый бежевый|Оливково-зеленый|Слоновая кость
Браш Блю|Браш Вайт|Браш Графит|Браш Деним Блю|Браш Лайт Грей
Браш Мени Грин|Браш Текстура Грей|Шарли Pink|Шарли Белый|Шарли Бриз|Шарли Мокко
Пластер Аворио|Пластер Блэк|Пластер Бьянко|Пластер Скай
Лофт Базилик|Лофт Крем|Лофт Оникс|Лофт Папирус|Лофт Гранит|Лофт Дюна
Лофт Кашемир|Лофт Сланец|Силк Анис|Холст белый|Холст вулкан|Холст вулканический
Холст латте|Холст сапфировый|Холст темно-зеленый|Экзотическое дерево Амазония
Дуб Крафт|Дуб Крафт Белый|Дуб Крафт серый|Дуб Крафт золото|Дуб Крафт золотой
Дуб Крафт табачный|Крафт|Крафт светлый|Крафт золотой|Крафт серый|Крафт табачный
Дуб Сонома|Дуб Сонома светлый|Сонома|Дуб Юкон|Дуб Вотан|Дуб беленый
Дуб Венге|Дуб белфорт|Белфорт|Венге|Венге цаво|Венге тиковое дерево
Дуб Баррик|Дуб бунратти|Дуб гранж песочный|Дуб Делано|Дуб Делано серый
Дуб Золотой|Дуб Каселла|Дуб Кремовый|Дуб Кремовый матовый|Дуб майский
Дуб молочный|Дуб сакраменто|Дуб светлый|Дуб темный|Дуб Сильвер|Дуб Сторсун
Дуб Сторсунд|Дуб Торонто|Дуб Тортуга|Дуб цикорий|Дуб Эдда|Дуб Эльза|Дуб Ниагара
Канадский дуб Пацифик|Ясень Анкор|Ясень Анкор светлый|Анкор|Анкор светлый
Ясень Шимо|Ясень Шимо светлый|Ясень Шимо темный|Шимо светл|Шимо темн
Ясень светлый|Ясень темный|Ясень|Орех|Бук|Береза темная|Карельская береза
Винтажная сосна|Сосна Скания милк|Сандал|Сандал светлый|Акация темн
Ольха матовый|Вишня|Черешня|Светлое дерево|Белое дерево
Бетон графит|Бетон светлый|Бетон темный|Бетон снежный|Данко Бетон
Камень светлый|Камень темный|Серый камень|Железный камень|Шелковый камень
Железная руда|Королевский опал светлый|Мрамор белый|Мрамор белый софт
Мрамор черный|Мрамор Блэк софт|Мрамор глянец|Мрамор золото|Мрамор бежевый
Мрамор итальянский|Мраморный берег|Гранит Бежевый|Гранит белый|Гранит Черный
Гранит Грей|Гранит Маус|Гранит Оникс|Кварц бежевый|Кварц черный
Штукатурка белая|Штукатурка серая|Цемент|Цемент светлый|Цемент темный
Арктик|Деним голубой|Мускат|Мускат структурный|Шато Индиго|Шато Мята
Мокко|Бордо|Винтерберг|Анкор св.|Эйфория QR-17|Хризантема OR-06|Иксора OR-10
Аверса|Айвори|Антарес|Антрацит|Базальт|Баунти|Бланж|Бетао|Вайт|Бьянко
Ваниль|Ваниль глянец|Ваниль кожа|Ваниль софт|Вино софт|Виноград|Виноград софт
Горчица|Горчица софт|Грей|Грей софт|Зефир|Иридиум|Железо|Жемчуг|Жемчуг Шервуд
Кастилло|Кастилло темный|Кофе|Кофе с молоком|Лазурь софт|Лайт|Лайт Грей|Лид
Липа белый|Липа пепел|Лунный свет|Магнолия|Малахит|Маренго|Маренго софт
Маус|Маус софт|Меланж|Меланж BS|Ментол|Мираж|Нарцисс|Нарцисс софт|Нео
Оазис|Океан|Океан софт|Океания|Олива|Олива софт|Паладина|Паладина светлая
Пепел|Персик|Персик софт|Платинум серый|Полярная звезда|Пудра|Пудра Скин
Пурпур|Роза|Рубелит|Сатин|Семолина беж|Семолина бежевая|Скай|Смоки
Смородина|Снег глянец|Софт Ольбия|Софт Вайт|Софт Графит|Софт Грей
Стоун|Сэнд|Табак|Торос черный|Трюфель|Туман|Турин|Фреска|Шадэ
Фреш Графит|Фреш Дюна|Фреш Меркури|Фреш Оазис|Фреш Саванна|Фреш Шифон
Черника софт|Шоколад|Американский Шоколад|Горький шоколад|Шторм глянец|Эспрессо
Астана ЭШ Уайт|Астана Эш Уайт|Орхидея сиреневая|Асфальт|Асфальт супермат
Угольный софт|Ультрамарин белый|Ультрамарин софт|Артвуд|Ателье светлый
Ателье светлое|Лагуна Беж|Лагуна Пепел|Ландыш Кашемир
Матовый Белый|Матовая Мята|Матовый Вижен|Матовый Новый серый
Матовый Светло-серый|Матовый Серый шторм|Матовое золото
3Д Айс|3Д Голд|3Д Графит|3Д Грин|3Д Маус|3Д Тирамису
Арт Голд|Арт Металл|Арт Сталь|Палм Графит|Палм Грей|Палм Грин|Палм Лайт|Палм Маус
Белая кожа|Красная кожа|Ваниль кожа|Кожа Бежевая|Кожа Коричневая|Кожа Серая
Кожа Олива|Кожа Шоколад|Зеленый Скин|Бежевый Скин|Бежевый софт
Лазурь софт|Бежевый мат-05|Бежевый мат. 05|Белая мат. JS9029-28
Апельсин|Джинс|Десерт|Дымчатый|Клубника|Лайм|Малина|Маки|Сирень|Слива софт|Яблоко
Милано Белый|Милано Гавань|Милано Графит|Милано Дымка|Милано Малахит
Золото|Матовое золото|Старое золото|Черное золото|Серебро|Бронза|Старая бронза
Старое олово|Розовая медь|Хром|Алюминий|Аллюминий
Black|White|Pink|Blue|Grey|Gray|Ice|Latte|Cashmere|Ivory|Gold|Silver|Wenge
"""
COLOR_PHRASES = tuple(sorted(set(
    re.sub(r"\s+", " ", phrase).strip()
    for phrase in re.split(r"[|\n]", _COLOR_DATA) if phrase.strip()
), key=lambda phrase: (-len(phrase), phrase.casefold())))
# A model may bear a finish-like name. Without explicit colour syntax these
# words alone are not enough to assign a colour.
_MODEL_LIKE = {
    "роза", "магнолия", "нарцисс", "оазис", "олива", "керамика", "мираж",
    "пепел", "турин", "мальта", "престиж", "весна", "севиль", "маки", "ландыш",
}
_MODEL_ADDITIONAL = ("Мальта", "Мальта грей")  # unambiguous inside colour groups

def _normal(value) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value or ""))).strip()

def _fold(value) -> str:
    return _normal(value).casefold().replace("ё", "е")

def _pattern(phrase: str) -> str:
    text = re.escape(phrase.casefold().replace("ё", "е"))
    return text.replace(r"\ ", r"\s+").replace("е", "[её]")

_COLOR_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё])(?:" +
    "|".join(_pattern(phrase) for phrase in COLOR_PHRASES + _MODEL_ADDITIONAL) +
    r")(?:\s+(?:глянец|глянцевый|глянцевая|софт|скин|супермат|матовый|матовая|"
    r"текстурный|текстурная|структурный|структурная|холодный|NEW|BS))*(?![A-Za-zА-Яа-яЁё])", re.I)
_STANDARD_CODE = re.compile(r"\b(?:RAL\s*\d{4}|NCS\s*(?:S\s*)?\d{4}-[A-Z0-9]+)\b", re.I)
_SUPPLIER_CODE = re.compile(r"\b(?:NFT\d+-\d+|QR-\d+|OR-\d+|JS\d+-\d+|D\d{4})\b", re.I)
_CONTEXT = re.compile(r"\b(?:цвет|расцветка|декор|оттенок|корпус|фасад)\s*[:=]?\s*$", re.I)
_ALLOWED = re.compile(
    r"\b(?:цвет|расцветка|декор|оттенок|корпус|фасад|корпуса|фасада|кромка|"
    r"лдсп|мдф|лмдф|пвх|эмаль|эмалит|матовый|матовая|матовое|матовые|мат[.]?|"
    r"глянец|глянцевый|глянцевая|глянцевое|глянцевые|глянц|"
    r"софт|скин|структурный|текстурный|текстурная|шагрень|супермат|"
    r"холодный|бархат|поры|дерева|new|bs|и|c|с)\b", re.I)
_BASIC_ADJECTIVE = re.compile(
    r"^(?:бел|черн|чёрн|сер|беж|зелен|зелён|голуб|син|красн|желт|жёлт|розов|"
    r"коричнев|фиолетов|сиренев)", re.I)

def _parentheses(text: str):
    """Balanced outer and inner groups; no dimensions or model guessing."""
    stack, result = [], []
    for index, char in enumerate(text):
        if char == "(":
            stack.append(index)
        elif char == ")" and stack:
            result.append((stack.pop() + 1, index))
    return sorted(result, key=lambda item: (item[0], -item[1]))

def _matches(text: str):
    matches = [(match.start(), match.end(), match.group()) for match in _COLOR_RE.finditer(text)]
    matches.extend((match.start(), match.end(), match.group()) for match in _STANDARD_CODE.finditer(text))
    matches.sort()
    # Standard codes and colour phrases are disjoint in the audited sources.
    return matches

def _palette(text: str) -> bool:
    matches = _matches(text)
    if not matches:
        return False
    remainder = text
    for start, end, _ in reversed(matches):
        remainder = remainder[:start] + " " + remainder[end:]
    remainder = _SUPPLIER_CODE.sub(" ", remainder)
    remainder = _ALLOWED.sub(" ", remainder)
    return not bool(re.search(r"[A-Za-zА-Яа-яЁё0-9]", remainder))

def _collection_spans(text: str, collection: str):
    # Real collection palettes are frequently comma separated. Never infer a
    # specific selected finish from them or mask each of their colour entries.
    spans = []
    raw = _normal(collection)
    if not raw or re.search(r"[,;|]", raw):
        return spans
    # A catalogue group may accidentally equal a complete product description,
    # including finishes and module codes. It is not a model exclusion span.
    candidates = [] if re.search(r"\d", raw) else [raw]
    # "МС Роза", "Кухня Магнолия", etc.
    words = re.findall(r"[A-Za-zА-Яа-яЁё-]+", raw)
    for word in words:
        if _fold(word) in _MODEL_LIKE:
            candidates.append(word)
    for candidate in candidates:
        regex = re.compile(r"(?<![A-Za-zА-Яа-яЁё])" + _pattern(candidate) +
                           r"(?![A-Za-zА-Яа-яЁё])", re.I)
        spans.extend((m.start(), m.end()) for m in regex.finditer(text))
    return spans

def _in_group(start, end, groups):
    return any(a <= start and end <= b for a, b in groups)

def _quoted_model(text: str, start: int, end: int) -> bool:
    for match in re.finditer(r'["«]([^"»]+)["»]', text):
        if match.start(1) <= start and end <= match.end(1):
            # An entire colour-like quoted phrase can be a model name. A quoted
            # model+finish ("Марсель Бежевый глянец") contains a real finish.
            if _palette(match.group(1)) and not _CONTEXT.search(text[:match.start()].rstrip()):
                return True
    return False

def _render(text: str, matches) -> str:
    if not matches:
        return ""
    first, last = matches[0][0], matches[-1][1]
    candidate = text[first:last]
    prefix = text[:first]
    label = re.search(r"\b(?:корпус|фасад|кромка)\s*[:=]?\s*$", prefix, re.I)
    if label and (len(matches) > 1 or re.search(r"[:=]", label.group())):
        candidate = text[label.start():last]
    if _palette(candidate):
        return candidate.strip(" ,;")
    # Separate genuine colours interrupted by product-description text rather
    # than copying that description into the colour field.
    result = []
    for _, _, value in matches:
        if _fold(value) not in {_fold(item) for item in result}:
            result.append(value)
    return "; ".join(result)

@lru_cache(maxsize=100_000)
def derive_color(name, collection="", variant="", existing="") -> str:
    """Return explicit colour unchanged, else an evidenced finish from the name.

    Unknown or ambiguous models yield ''. Collection is exclusion context only.
    Name and input records are never changed. Source capitalization/separators
    are retained, including body/facade and nested worktop combinations.
    """
    if isinstance(existing, str) and existing.strip():
        return existing
    text = _normal(name)
    groups = _parentheses(text)
    model_spans = _collection_spans(text, collection)
    all_matches = _matches(text)
    accepted = []
    for start, end, value in all_matches:
        parenthesized = _in_group(start, end, groups)
        explicit = bool(_CONTEXT.search(text[:start].rstrip()))
        if not parenthesized and any(a <= start and end <= b for a, b in model_spans):
            continue
        if _quoted_model(text, start, end) and not explicit:
            continue
        if _fold(value) in _MODEL_LIKE and not parenthesized and not explicit:
            continue
        # "Кухня АЙС 2.4" names a collection, unlike "Фасад 3Д Айс".
        before = text[:start].strip()
        after = text[end:].strip()
        if (not parenthesized and not explicit and not _BASIC_ADJECTIVE.match(value) and
                re.fullmatch(r"(?i)(?:кухня|гостиная|спальня)", before) and
                (not after or re.match(r"^\d", after))):
            continue
        quoted_finish = bool(re.match(r'^[»"](?:\s*[—-]\s*\([^)]*\))?$', after))
        metal_finish = _fold(value) in {"хром", "золото", "серебро", "бронза", "алюминий", "аллюминий"}
        # Finish adjectives before nouns (e.g. Белая тумба) remain valid;
        # proprietary shades outside parentheses normally follow the item.
        if (not parenthesized and not explicit and not quoted_finish and not metal_finish
                and not _BASIC_ADJECTIVE.match(value)):
            suffix = text[end:].strip(" ,.;")
            if suffix and not re.fullmatch(
                    r"(?i)(?:NEW|BS|\d+[.]?\d*\s*(?:мм|см|м)?|"
                    r"\d+(?:[.,]\d+)?\s*[хx*/]\s*\d+(?:[.,]\d+)?(?:\s*[хx*/]\s*\d+(?:[.,]\d+)?)?|"
                    r"[A-ZА-Я]{1,8}[- ]?\d+(?:[/-]\d+)*|\([^)]*\))", suffix):
                if not _palette(text[start:]):
                    continue
        accepted.append((start, end, value))
    if accepted:
        # Prefer the largest fully recognised source palette enclosing matches.
        regions = []
        for a, b in groups:
            inside = [m for m in accepted if a <= m[0] and m[1] <= b]
            if inside and _palette(text[a:b]):
                regions.append((a, b, text[a:b]))
        maximal = [r for r in regions if not any(
            other[0] <= r[0] and r[1] <= other[1] and other != r for other in regions)]
        if maximal:
            outside = [m for m in accepted if not any(a <= m[0] and m[1] <= b for a, b, _ in maximal)]
            if not outside:
                return "; ".join(r[2].strip() for r in maximal)
        return _render(text, accepted)
    # Only a complete finish expression can supply colour from variant.
    variant_text = _normal(variant)
    if variant_text and _palette(variant_text):
        return variant_text
    return ""
