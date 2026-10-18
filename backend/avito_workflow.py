"""Local review queues. Hints never establish a product or manufacturer link."""
import re

from backend.avito_matching import (canonical, collection_evidence, collection_in,
                                   collection_key, furniture_type, norm, plain,
                                   useful_collection)
from backend.colors import derive_color


def exclusion(listing):
    title = norm(plain(listing.get("title", "")))
    kind = furniture_type(title)
    if re.search(r"\bпод\s+ключ\b", title):
        return "Предложение под ключ: цена зависит от состава"
    # The main subject matters: «тумба для прихожей» is an individual item.
    if kind and kind["kind"] in ("hallway", "kitchen", "bedroom_set", "living_set"):
        return "Комплект или модульная мебель: требуется отдельный состав"
    # «Детская кровать» and «спальный шкаф» still describe individual items.
    title_for_groups = re.sub(r"\b(?:детская|детские|спальн\w*)\s+(?=кроват|шкаф|комод|тумб|стол|стул|матрас|полк|стеллаж)", "", title)
    group = re.search(r"\b(?:спальня|детская|детские|гарнитур\w*|комплект(?:ы|а)?|набор\w*|стенк\w*)\b", title_for_groups)
    if group:
        subject = re.search(r"\b(?:кроват\w*|комод\w*|тумб\w*|шкаф\w*|стол\w*|стул\w*|диван\w*|матрас\w*|полк\w*|стеллаж\w*|фасад\w*)", title_for_groups)
        if not subject or group.start() <= subject.start():
            return "Комплект или подбор мебели: нельзя назначить цену одного товара"
    # Only explicit composition language, never a generic promotional mention.
    description = norm(plain(listing.get("description", "")))
    if re.search(r"(?:в\s+(?:стоимость|цену)\s+(?:комплекта\s+)?входят|состав\s+комплекта\s*:)" , description):
        return "В описании указан состав комплекта; требуется ручная проверка"
    return ""


class ReviewIndex:
    def __init__(self, rows):
        self.factories = sorted({r["factory"] for r in rows if r["factory"]})
        self.owners = {}
        for row in rows:
            name = row["collection"] or ""
            key = collection_key(name)
            if useful_collection(name) and not any(c in name for c in ",;|") and norm(derive_color(key)) != norm(key):
                self.owners.setdefault(key, set()).add(row["factory"])
        keys = sorted(self.owners, key=len, reverse=True)
        self.pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(k) for k in keys) + r")(?!\w)") if keys else None

    def classify(self, listing, linked_factory=None, manually_excluded=False):
        attrs = listing.get("attributes", {})
        text = canonical(listing.get("title", "") + " " + plain(listing.get("description", "")))
        mentions = [f for f in self.factories if re.search(r"(?<!\w)" + re.escape(canonical(f)) + r"(?!\w)", text)]
        explicit = attrs.get("factory")
        if explicit:
            mentions = sorted(set(mentions + [explicit]))
        collections = collection_evidence(listing)
        if not collections and self.pattern:
            collections = [{"name": m[0], "source": "Название серии в тексте", "explicit": False}
                           for m in self.pattern.finditer(text) if collection_in(text, m[0])]
        names = list(dict.fromkeys(collection_key(c["name"]) for c in collections))
        owners = set()
        for name in names:
            owners.update(self.owners.get(collection_key(name), set()))
        suggested = mentions[0] if len(mentions) == 1 else next(iter(owners)) if not mentions and len(owners) == 1 else None
        basis = "Фабрика указана в сведениях или тексте" if len(mentions) == 1 else "Коллекция найдена в каталоге только этой фабрики; проверить" if suggested else "Производитель не установлен или признаки неоднозначны"
        reason = exclusion(listing) or ("Исключено пользователем" if manually_excluded else "")
        kind = furniture_type(listing.get("title", ""))
        return {"excluded_reason": reason, "suggested_factory": suggested,
                "factory_basis": basis, "factory_mentions": mentions,
                "collections": names, "item_type": kind,
                "queue": "excluded" if reason else "linked" if linked_factory else "suggested" if suggested else "unassigned"}
