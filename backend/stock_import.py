"""Read hierarchical warehouse exports without confusing stock totals with unit prices."""
import re
from .colors import derive_color


def factory_label(value, known_only=False):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    key = text.casefold().replace("ё", "е")
    aliases = [(r"(?:бтс|bts)", "БТС"), (r"дсв", "ДСВ"), (r"миф", "МИФ"), (r"альбина", "Альбина"),
               (r"ал[её]нушка", "Алёнушка"), (r"эста", "ЭСТА Мебель"), (r"пирамида", "Пирамида"),
               (r"регион[ -]*058", "Регион 058"), (r"апогей", "Апогей"), (r"стендмебель", "Стендмебель"),
               (r"регина", "Регина"), (r"росток", "Росток"),
               (r"(?:нн|nn)[ -]*мебель", "NN-Мебель"), (r"sv[ -]*мебель", "SV-Мебель"),
               (r"(?:vim|вим)", "VIM-MEBEL"), (r"скиф", "Скиф"),
               (r"атлант", "Атлант"), (r"каспийск", "Каспийск"),
               (r"сакура", "Сакура"), (r"висма", "Висма"),
               (r"кристалл", "Кристалл"), (r"кладов", "Кладов"),
               (r"релакс", "Релакс"), (r"ulstone", "Ulstone"),
               (r"^город$", "Город"), (r"^отмк$", "ОТМК")]
    for pattern, label in aliases:
        if re.search(r"(?<!\w)(?:" + pattern + r")(?!\w)", key):
            return label
    return "" if known_only else text


def layout(sheet):
    from .importer import normalize_text, _col, FIELDS
    rows = sheet.row_numbers[:20]
    # 1C price exports split article/name and the price label across three rows.
    for n in rows:
        cells = {c:normalize_text(v).casefold() for c,v in sheet.rows[n].items()}
        articles = [c for c,v in cells.items() if v == "артикул"]
        names = [c for c,v in cells.items() if v.startswith("номенклатура")]
        prices = [(r,c) for r in rows if n<=r<=n+4 for c,v in sheet.rows[r].items()
                  if normalize_text(v).casefold()=="цена"]
        if articles and names and prices:
            price_row,price_col=prices[0]
            mapping=dict.fromkeys(FIELDS);mapping.update(name=_col(names[0]),sku=_col(articles[0]),price=_col(price_col))
            manufacturer=next((factory_label(v,known_only=True) for r in rows if r<=n
                               for v in sheet.rows[r].values() if factory_label(v,known_only=True)),"")
            headers=[{"column":_col(articles[0]),"label":"Артикул"},
                     {"column":_col(names[0]),"label":"Номенклатура / характеристика"},
                     {"column":_col(price_col),"label":"Цена"}]
            for r in rows:
                for c,v in sheet.rows[r].items():
                    if normalize_text(v).casefold()=="остаток":headers.append({"column":_col(c),"label":"Остаток"})
            return {"kind":"price-export","header_row":price_row,"mapping":mapping,"headers":headers,
                    "factories":[manufacturer] if manufacturer else [],"factory":manufacturer}
    for n in rows[:8]:
        cells = {c:normalize_text(v).casefold() for c,v in sheet.rows[n].items()}
        prices = [c for c,v in cells.items() if v == "цена"]
        quantity = any(v == "остаток" for v in cells.values())
        total = any(v == "сумма" for v in cells.values())
        names = [c for next_n in rows if n < next_n <= n+3
                 for c,v in sheet.rows[next_n].items() if normalize_text(v).casefold() == "номенклатура"]
        if prices and quantity and total and names:
            mapping = dict.fromkeys(FIELDS);mapping.update(name=_col(names[0]),price=_col(prices[0]))
            headers = [{"column":_col(c),"label":"Номенклатура / характеристика" if c==names[0] else normalize_text(sheet.rows[n].get(c))}
                       for c in sorted(set(cells)|set(names))]
            factories = list(dict.fromkeys(factory_label(sheet.rows[r].get(1)) for r in sheet.row_numbers
                         if sheet.cell_indents.get((r,1)) == 1 and normalize_text(sheet.rows[r].get(1))))
            return {"header_row":n+2,"mapping":mapping,"headers":headers,"factories":factories,
                    "factory":factories[0] if len(factories)==1 else ""}
    return None


def parse(sheet, filename, factory, configuration, info):
    if info.get("kind")=="price-export":
        return parse_price_export(sheet,filename,factory,configuration,info)
    from .importer import normalize_text, _price, _dimensions, _colnum
    mapping = {**info["mapping"], **(configuration.get("mapping") or {})}
    name_col, price_col = _colnum(mapping.get("name")), _colnum(mapping.get("price"))
    if not name_col or not price_col:
        return [], [{"message":"Выберите номенклатуру и цену за единицу.","source_file":filename,"source_sheet":sheet.name}]
    label = next((h["label"].casefold() for h in info["headers"] if _colnum(h["column"])==price_col), "")
    if label in ("сумма", "остаток"):
        return [], [{"message":"Для исходной цены выберите «Цена», а не остаток или сумму за весь запас.","source_file":filename,"source_sheet":sheet.name}]
    numbers = [n for n in sheet.row_numbers if n > info["header_row"]]
    stack, products, issues = [], [], []
    for i,n in enumerate(numbers):
        row = sheet.rows[n]
        name = normalize_text(row.get(name_col))
        level = sheet.cell_indents.get((n,name_col),0)
        while stack and stack[-1][0] >= level:
            stack.pop()
        price = _price(row.get(price_col))
        next_level = sheet.cell_indents.get((numbers[i+1],name_col),0) if i+1 < len(numbers) else -1
        # Group/subtotal rows have descendants and no unit price. Never use column Sum.
        if price is None and (next_level > level or level < 2):
            if name:stack.append((level,name,n))
            continue
        if level < 2:
            continue
        ancestors = [(l,label,r) for l,label,r in stack if l>=2]
        parent = ancestors[-1] if ancestors else None
        if parent:
            full_name = parent[1] + (" — " + name if name and name != parent[1] else "")
            parent_row = parent[2]
            collection = ancestors[-2][1] if len(ancestors)>1 else sheet.name
            characteristic = name if name != parent[1] else ""
        else:
            full_name, parent_row, collection, characteristic = name,n,sheet.name,""
        if not full_name or not re.search(r"[A-Za-zА-Яа-яЁё]",full_name):
            issues.append({"message":"Не найдено название родительского товара для характеристики.","name":name,
                           "source_file":filename,"source_sheet":sheet.name,"source_row":n})
            continue
        source_factory = next((factory_label(label) for l,label,r in reversed(stack) if l==1),"")
        actual_factory = normalize_text(configuration.get("factory")) or factory or source_factory
        dimensions = _dimensions(sheet,parent_row,mapping,full_name)
        component = "Фасад" if re.search(r"(?i)\bфасад", full_name) else "Корпус" if re.search(r"(?i)\bкорпус",full_name) else ""
        variant = " · ".join(x for x in (component,characteristic) if x)
        product = {"name":full_name,"factory":actual_factory,"collection":collection,"sku":"",
                   "color":derive_color(characteristic or full_name),"dimensions":dimensions,"variant":variant,
                   "base_price":price if price and price>0 else None,"source_file":filename,"source_sheet":sheet.name,"source_row":n,
                   "name_context":{"original_name":name,"parent_name":parent[1] if parent else "", "parent_row":parent_row,
                                   "kind":"warehouse-characteristic"}}
        products.append(product)
        if not price or price<=0:
            issues.append({"name":full_name,"message":"Нет корректной цены за единицу.","source_file":filename,"source_sheet":sheet.name,"source_row":n})
    return products,issues


def parse_price_export(sheet,filename,factory,configuration,info):
    from .importer import normalize_text,_price,_dimensions,_colnum,_service_name
    mapping={**info["mapping"],**(configuration.get("mapping") or {})}
    if not mapping.get("name") or not mapping.get("price"):
        return [],[{"message":"Выберите номенклатуру и цену.","source_file":filename,"source_sheet":sheet.name}]
    selected_label=next((h["label"] for h in info["headers"] if h["column"]==mapping["price"]),"")
    if selected_label=="Остаток":
        return [],[{"message":"Остаток не является ценой. Выберите столбец «Цена».","source_file":filename,"source_sheet":sheet.name}]
    products,issues=[],[]
    for n in sheet.row_numbers:
        if n<=info["header_row"]:continue
        # Summary/characteristic rows repeat the name in a merged A:M cell.
        # Only the original populated nomenclature cell is a product row.
        original=normalize_text(sheet.rows[n].get(_colnum(mapping["name"])))
        if not original or _service_name(original):continue
        name=re.sub(r",\s*,?\s*(?:шт|компл)\.?$","",original,flags=re.I).rstrip(', ')
        price=_price(sheet.get(n,mapping["price"]))
        sku=normalize_text(sheet.get(n,mapping.get("sku")))
        quoted=re.search(r'["«]([^"»]+)["»]',name)
        collection=normalize_text(sheet.get(n,mapping.get("collection"))) or (quoted[1] if quoted else "")
        color=normalize_text(sheet.get(n,mapping.get("color"))) or derive_color(name)
        product={"name":name,"factory":normalize_text(configuration.get("factory")) or factory or info["factory"],
                 "sku":sku,"collection":collection,"color":color,"dimensions":_dimensions(sheet,n,mapping,name),"variant":"",
                 "base_price":price if price and price>0 else None,"source_file":filename,"source_sheet":sheet.name,"source_row":n,
                 "name_context":{"kind":"price-export","original_name":original,"parent_name":""},
                 "identity_context":{"kind":"1c-nomenclature","version":1}}
        products.append(product)
        if not price or price<=0:
            issues.append({"name":name,"message":"Нет корректной исходной цены.","source_file":filename,"source_sheet":sheet.name,"source_row":n})
    return products,issues
