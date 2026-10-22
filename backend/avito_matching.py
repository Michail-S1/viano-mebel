"""Explainable candidate suggestions. No score ever confirms a product link."""
import re
from html import unescape
from functools import lru_cache
from difflib import SequenceMatcher

from backend.importer import normalize_text
from backend.colors import derive_color
from backend.avito_dimensions import compare as compare_dimensions
from backend.search import is_short_word, whole_word_matches


LABELS = {
    "confirmed": "Подтверждено пользователем",
    "likely": "Высокая вероятность — требуется подтверждение",
    "multiple": "Несколько возможных товаров",
    "insufficient": "Недостаточно информации",
    "conflict": "Противоречивые характеристики",
    "not_found": "Соответствие не найдено",
}


def norm(value):
    return re.sub(r"\s+", " ", normalize_text(value).casefold().replace("ё", "е")).strip()


def words(value):
    return set(re.findall(r"[\w-]+", norm(value)))


@lru_cache(maxsize=4096)
def plain(value):
    """Descriptions are data; discard markup, decode entities and keep word boundaries."""
    text = re.sub(r"<\s*(?:br\b[^>]*|/[ph][1-6]?\s*|/div\s*|/li\s*)>", "\n", str(value or ""), flags=re.I)
    return unescape(re.sub(r"<[^>]*>", " ", text))


@lru_cache(maxsize=32768)
def canonical(value):
    return " ".join(re.findall(r"[a-zа-я0-9]+", norm(plain(value))))


# The leading furniture noun identifies the item, not every accessory mentioned
# later ("кровать без матраса", "прихожая со шкафом"). Multiword subtypes win ties.
TYPE_RULES = (
    ("part", "Комплектующее", r"\b(?:фасад\w*|столешниц\w*|ручк[аиу]\w*|основани\w*|ламел\w*)"),
    ("bed", "Кровать", r"\b(?:кроват\w*|тахт\w*)"),
    ("mattress", "Матрас", r"\bматрас\w*"),
    ("dresser", "Комод", r"\bкомод\w*"),
    ("wardrobe", "Шкаф", r"\b(?:шкаф\w*|гардероб\w*|буфет\w*|пенал\w*)"),
    ("bedside", "Прикроватная тумба", r"\b(?:тумб\w*\s+прикроват\w*|прикроват\w*\s+тумб\w*)"),
    ("tv_stand", "Тумба под ТВ", r"\bтумб\w*\s+(?:(?:под|для)\s+)?(?:тв|tv|телевизор\w*)\b"),
    ("shoe_cabinet", "Обувница", r"\bобувниц\w*"),
    ("cabinet", "Тумба", r"\bтумб\w*"),
    ("desk", "Письменный / компьютерный стол", r"\b(?:(?:письменн\w*|компьютерн\w*)\s+стол\w*|стол\w*\s+(?:письменн\w*|компьютерн\w*))"),
    ("coffee_table", "Журнальный стол", r"\b(?:журнальн\w*\s+стол\w*|стол\w*\s+журнальн\w*)"),
    ("table", "Стол", r"\bстол(?:ы|а|ик\w*|ов|у|ом|е)?\b"),
    ("chair", "Стул", r"\b(?:стул\w*|табурет\w*)"),
    ("armchair", "Кресло", r"\bкресл\w*"),
    ("sofa", "Диван", r"\b(?:диван\w*|соф[аыу]\w*)"),
    ("shelving", "Стеллаж", r"\b(?:стеллаж\w*|этажерк\w*)"),
    ("shelf", "Полка", r"\bпол(?:ка|ки|ок|ку|кой|очк\w*)\b"),
    ("hallway", "Прихожая", r"\bприхож\w*"),
    ("kitchen", "Кухня", r"\bкухн(?:я|и|ю|ей|е)\b"),
    ("bedroom_set", "Спальный гарнитур", r"\b(?:спальн(?:я|и|ю|ей|е)|спальн\w*\s+гарнитур\w*)\b"),
    ("living_set", "Гостиная / стенка", r"\b(?:гостин(?:ая|ые|ую)|стенк\w*)\b"),
    ("mirror", "Зеркало", r"\bзеркал\w*"),
)


@lru_cache(maxsize=32768)
def furniture_type(value):
    text = norm(plain(value))
    matches = []
    for index, (kind, label, pattern) in enumerate(TYPE_RULES):
        match = re.search(pattern, text)
        if match:
            matches.append((match.start(), -len(match.group()), index, kind, label))
    if not matches:
        return None
    first = min(matches)
    return {"kind": first[3], "label": first[4]}


def same_type(left, right):
    if left == right:
        return True
    families = ({"cabinet", "bedside", "tv_stand", "shoe_cabinet"},
                {"table", "desk", "coffee_table"})
    # A generic "тумба" or "стол" accepts its subtypes; known different
    # subtypes do not become equivalent to one another.
    return any(left in group and right in group and (left == generic or right == generic)
               for generic, group in zip(("cabinet", "table"), families))


COLLECTION_PREFIX = re.compile(r"^(?:(?:мебель для спальни|модульная система|модульная спальня|спальня|гостиная|прихожая|детская|нкм|мс)\s+)+")
GENERIC_COLLECTION = re.compile(r"^(?:кроват\w*|шкаф\w*|комод\w*|модульн\w*|систем\w*|готов\w*|решени\w*|мебел\w*|кухон\w*|гарнитур\w*|стол\w*|тумб\w*|стеллаж\w*|журнальн\w*|компьютерн\w*|письменн\w*|детск\w*|коллекц\w*|фабрик\w*|под|от|для|бренд\w*)$")


def collection_key(value):
    text = canonical(value)
    return COLLECTION_PREFIX.sub("", text)


def useful_collection(value):
    key = collection_key(value)
    if re.fullmatch(r"(?:фасад\w*|фурнитур\w*|ручк\w*|корпус\w*|двер\w*|модул\w*|комплектующ\w*|аксессуар\w*|полк\w*|зеркал\w*|матрас\w*)", key):
        return False
    return len(key) >= 3 and len(key.split()) <= 6 and not all(GENERIC_COLLECTION.fullmatch(w) for w in key.split())


def collection_in(value, target):
    """Boundary-aware model names: МСП-1 == МСП 1, but Версаль != Версаль-5."""
    haystack, needle = canonical(value), collection_key(target)
    if not needle:
        return False
    match = re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack)
    if not match:
        return False
    rest = haystack[match.end():].lstrip()
    return not (not needle[-1].isdigit() and re.match(r"^\d+\b", rest))


def collection_evidence(listing, known_collections=()):
    found = []
    explicit = listing.get("attributes", {}).get("collection")
    if explicit:
        found.append({"name": explicit, "source": "Характеристики объявления", "explicit": True})
    description = plain(listing.get("description", ""))
    marker = r"\b(?:коллекци(?:я|и|ю|ей)|сери(?:я|и|ю))\s*(?:мебели\s*)?(?::|[-–—])?\s*"
    for match in re.finditer(marker, description, re.I):
        tail = description[match.end():].lstrip()
        if re.match(r"(?:от|для|под|бренда|кроватей|мебели)\b", tail, re.I):
            continue
        quoted = re.match(r'[«"“„]([^»"”\n]{1,90})[»"”]', tail)
        if quoted:
            name = quoted[1].strip()
        else:
            # A labelled unquoted name is accepted only with a clear ending,
            # or when the remaining line is just a short model name.
            piece = re.split(r"\s+(?:от\b|фабрик\w*\b|производител\w*\b|в\s+цвете\b)|[!\n]", tail, maxsplit=1, flags=re.I)[0].strip(" .:;—–-")
            name = piece if len(piece.split()) <= 4 else ""
        if name and useful_collection(name):
            found.append({"name": name, "source": "Коллекция в описании", "explicit": True})
    if not found:
        text = listing.get("title", "") + " " + description
        matches = []
        for name in known_collections:
            key = collection_key(name)
            # Color headings and supplier palettes often occupy collection
            # cells; they are not evidence of a model series.
            palette = any(char in name for char in (",", ";", "|"))
            color = norm(derive_color(key)) == norm(key)
            if not palette and not color and useful_collection(name) and collection_in(text, key):
                matches.append(key)
        # Do not turn a shared prefix into a second collection.
        for name in sorted(set(matches), key=len, reverse=True):
            if not any(collection_in(old["name"], name) for old in found):
                found.append({"name": name, "source": "Название серии в тексте", "explicit": False})
    result = {}
    for item in found:
        result.setdefault(collection_key(item["name"]), item)
    return list(result.values())


def match_context(listing, known_collections=()):
    kind = furniture_type(listing.get("title", ""))
    source = "Название объявления"
    characteristics = listing.get("source_details", {}).get("characteristics", {})
    if not kind:
        for key in ("Подтип тумбы", "Тип товара", "Подвид товара", "Тип мебели"):
            text = characteristics.get(key, "")
            # Broad catalog groups must not decide the type by their first noun.
            if not re.search(r"[,|]|\sи\s", text):
                kind = furniture_type(text)
                if kind:
                    source = key
                    break
    return {"item_type": {**kind, "source": source} if kind else None,
            "model": model_evidence(listing), "sleep_width_mm": sleep_width(listing.get('description', '')),
            "collections": collection_evidence(listing, known_collections)}


@lru_cache(maxsize=32768)
def identifier_text(value):
    return re.sub(r"(?<=[a-zа-я])(?=\d)|(?<=\d)(?=[a-zа-я])", " ", canonical(value))


def model_in(value, model):
    return (' ' + identifier_text(model) + ' ') in (' ' + identifier_text(value) + ' ')


def model_evidence(listing):
    # A seller's explicit introduction supplies a model independently of the
    # often generic collection column. Never use the advertising boilerplate.
    text = plain(listing.get('description', ''))[:500]
    match = re.search(r"\bнов(?:ая|ый|ое|ые)\s+(?:кроват\w*|комод\w*|тумб\w*|шкаф\w*|стол\w*|стеллаж\w*|диван\w*)\s+(.{1,100}?)\s+от\s+(?:фабрик\w*|производител\w*)", text, re.I)
    if not match:
        return None
    name = match[1].strip(' «»"“”.,!')
    if not name or len(name.split()) > 5 or re.search(r"\b(?:двуспальн\w*|односпальн\w*|нов\w*|бел\w*|ширин\w*|высот\w*|см|мм)\b", name, re.I):
        return None
    return {'name': name, 'source': 'Модель в первой строке описания'}


def sleep_width(value):
    match = re.search(r"спально[её]\s+место\s*[:—–-]?\s*(\d+(?:[.,]\d+)?)\s*[xх×*]\s*\d+(?:[.,]\d+)?\s*(мм|см|м)\b", plain(value), re.I)
    return int(float(match[1].replace(',', '.')) * {'мм': 1, 'см': 10, 'м': 1000}[match[2].lower()]) if match else None


def search_matches(text, query):
    value = ' ' + identifier_text(text) + ' '
    # Codes retain their sequence: К3.6 must not match К-6.3 or К-3.60.
    codes = re.findall(r"\b[a-zа-я]{1,4}[- ]?\d+(?:[.-]\d+)*\b", norm(query))
    if any(not model_in(text, code) for code in codes):
        return False
    return all(whole_word_matches(value, word) if word.isdigit() or is_short_word(word) else word in value
               for word in identifier_text(query).split())


@lru_cache(maxsize=2048)
def listing_text(title, description):
    return norm(plain(title + " " + description))


def dimensions(value):
    """Normalize separators only: never guess units or dimension order."""
    return re.sub(r"\s*([xх×*/])\s*", "×", norm(value)).replace(",", ".")


def color_key(value):
    # Grammatical forms are equivalent; distinct shades and component order
    # remain significant. Do not guess supplier abbreviations or palettes.
    text = re.sub(r"\s*/\s*", " / ", norm(value))
    for stem, base in (('бел', 'белый'), ('черн', 'черный'), ('сер', 'серый'),
                       ('бежев', 'бежевый'), ('коричнев', 'коричневый')):
        text = re.sub(r'\b'+stem+r'(?:ая|ое|ые|ый)\b', base, text)
    return text


def partial_color_description(left, right):
    """A stated decor occurs as a complete component, not an assumed synonym.

    «Меланж» in «Графит / МДФ Меланж» needs review; «белый» does not
    match «белый жемчуг», nor does one shared part equate two mixed colors.
    """
    def parts(value):
        return {re.sub(r'^(?:мдф|лдсп|дсп)\s+', '', part).strip()
                for part in re.split(r'\s*[/;|]\s*', color_key(value)) if part.strip()}
    first, second = parts(left), parts(right)
    return bool(first and second and
                ((len(first) == 1 and first <= second) or (len(second) == 1 and second <= first)))


def factory_mentions(listing, factories):
    text = listing_text(listing.get("title", ""), listing.get("description", ""))
    return [name for name in factories if len(norm(name)) >= 3 and
            re.search(r"(?<!\w)" + re.escape(norm(name)) + r"(?!\w)", text)]


def evaluate(listing, product):
    attrs = listing.get("attributes", {})
    reasons, conflicts, missing, warnings = [], [], [], []
    rank, strong = 0, 0
    text = listing_text(listing.get("title", ""), listing.get("description", ""))
    context = listing.get("match_context") or match_context(listing)
    dimension_check = compare_dimensions(listing, product, plain)
    conflicts.extend(dimension_check['conflicts'])
    warnings.extend(dimension_check['warnings'])
    if re.search(r'\bnew\b', product.get('name', ''), re.I) and not re.search(r'\bnew\b', plain(listing.get('title', '')+' '+listing.get('description', '')), re.I):
        warnings.append('В каталоге указана модификация NEW, в объявлении она не указана. Проверьте версию товара')
    model = context.get('model')
    model_match = bool(model and any(model_in(product.get(key, ''), model['name']) for key in ('name', 'sku', 'collection')))
    model_mismatch = bool(model and not model_match)
    if model_mismatch:
        conflicts.append('В описании указана модель «' + model['name'] + '»; в товаре она не найдена')
    elif model_match:
        reasons.append('Модель из описания совпадает: ' + model['name'])
        rank += 12
    width = context.get('sleep_width_mm')
    if width and furniture_type(product.get('name', '')) and furniture_type(product.get('name', ''))['kind'] == 'bed' and str(width) in canonical(product.get('name', '')).split():
        reasons.append('Число ширины спального места ' + str(width) + ' мм найдено в названии товара; проверить размер и вариант')
        rank += 8
    expected_type = context["item_type"]
    product_type = furniture_type(product.get("name", ""))
    type_match = 0
    type_mismatch = bool(expected_type and product_type and not same_type(expected_type["kind"], product_type["kind"]))
    if type_mismatch:
        conflicts.append("Тип мебели: объявление — " + expected_type["label"] + ", товар — " + product_type["label"])
    elif expected_type and product_type:
        reasons.append("Тип мебели совпадает: " + expected_type["label"])
        type_match = 2 if expected_type["kind"] == product_type["kind"] else 1
    elif expected_type:
        missing.append("Тип товара в каталоге не распознан")
    target_collections = context["collections"]
    matching_collections = [c for c in target_collections if collection_in(product.get("collection", ""), c["name"]) or collection_in(product.get("name", ""), c["name"])]
    collection_match = int(bool(matching_collections))
    collection_mismatch = bool(target_collections and not matching_collections)
    if collection_mismatch:
        conflicts.append("В объявлении указана серия «" + " / ".join(c["name"] for c in target_collections) + "»; в товаре она не найдена")
    for item in matching_collections:
        reasons.append(item["source"] + ": «" + item["name"] + "» найдена в товаре")
    if len(target_collections) > 1 and any(c["explicit"] for c in target_collections):
        conflicts.append("В объявлении названы разные коллекции; требуется уточнение")
    mentioned = listing.get("factory_mentions", [])
    if len(mentioned) > 1:
        conflicts.append("В тексте упомянуто несколько фабрик; уточните производителя")
    elif mentioned and norm(product.get("factory")) != norm(mentioned[0]):
        conflicts.append("В тексте указан другой производитель: " + mentioned[0])
    for field, label in (("factory", "Фабрика"), ("collection", "Коллекция"),
                         ("sku", "Артикул"), ("color", "Цвет"),
                         ("dimensions", "Размеры"), ("variant", "Комплектация"),
                         ("material", "Материал")):
        if field == "collection":
            if not target_collections:
                missing.append(label)
            continue
        external, internal = attrs.get(field, ""), product.get(field, "")
        if field == 'dimensions' and dimension_check['rows'] and any(r['difference_mm'] is not None for r in dimension_check['rows']):
            # Axis comparison above distinguishes a small discrepancy from a
            # different variant; it never compares the bed's sleeping place.
            if not dimension_check['conflicts'] and not dimension_check['warnings']:
                reasons.append('Габариты: совпадают явно определённые оси и единицы')
                rank += 3
                strong += 1
            continue
        convert = dimensions if field == "dimensions" else color_key if field == "color" else norm
        if external and internal:
            if convert(external) == convert(internal):
                reasons.append(label + ": совпадает явное значение")
                rank += 5 if field == "factory" else 3
                strong += 1
            else:
                message = label + ": «" + str(external) + "» / «" + str(internal) + "»"
                if field == 'dimensions':
                    warnings.append(message + '. Формат или единицы не позволяют сравнить оси; проверьте ошибку сведений вручную')
                elif field == 'color' and partial_color_description(external, internal):
                    warnings.append(message + '. Указанный декор найден в составном описании цвета. Проверьте нужный вариант вручную; точное совпадение не установлено')
                elif field == 'color':
                    warnings.append(message + '. Цвет различается. Проверьте нужный вариант вручную; сведения старого объявления могут быть ошибочны')
                else:
                    conflicts.append(message)
        elif not external:
            missing.append(label)
            if internal and field in ("factory", "collection", "sku"):
                value = norm(internal)
                if re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text):
                    reasons.append(label + ": упомянуто в тексте, требуется проверка")
                    rank += 2
        else:
            missing.append(label + " в каталоге")
    title, name = norm(listing.get("title")), norm(product.get("name"))
    overlap = len(words(title) & words(name))
    similarity = SequenceMatcher(None, title, name).ratio() if title and name else 0
    if title and title == name:
        reasons.append("Название совпадает; само по себе не подтверждает товар")
        rank += 4
    elif overlap:
        reasons.append("Есть общие слова в названии")
        rank += min(3, overlap)
    relevant = bool(overlap or strong or collection_match or model_match or type_match or similarity >= .65)
    if conflicts:
        category = "conflict"
    elif attrs.get("factory") and strong + collection_match >= 3 and overlap:
        category = "likely"
    else:
        category = "insufficient"
    return {"reasons": reasons, "conflicts": conflicts, "missing": missing, 'warnings': warnings,
            'dimension_check': dimension_check,
            "category": category, "rank": rank, "relevant": relevant,
            "type_mismatch": type_mismatch, "collection_mismatch": collection_mismatch,
            "model_mismatch": model_mismatch, "model_match": model_match,
            "type_match": type_match, "collection_match": collection_match}


def compatibility(listing, product, snapshot):
    result = evaluate({**listing, '_dimension_unit': snapshot.get('_dimension_unit', '')}, product)
    if result['warnings'] != snapshot.get('_reviewed_warnings', []):
        result['conflicts'].append('Изменившиеся предупреждения требуют повторного ручного подтверждения')
    # Changed names, variants or factories invalidate a manual confirmation,
    # including changes in fields absent from the original advertisement.
    for key in ("name", "factory", "collection", "sku", "color", "dimensions", "variant"):
        if norm(product.get(key)) != norm(snapshot.get(key)):
            result["conflicts"].append("После подтверждения изменилось поле товара: " + key)
    return result
