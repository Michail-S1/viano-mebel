"""Import regression tests. Tiny fixtures are OOXML zip packages, not authored artifacts."""
import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".analysis" / "python"))
sys.path.insert(0, str(ROOT / "vendor"))
from backend.importer import (inspect_file, parse_file, product_key, match_signature,
                              normalize_text, _price)

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def fixture(path, sheets):
    """sheets = {name: {rows:[{column:value}], merges:[A2:A3]}}."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '</Types>')
        archive.writestr("xl/workbook.xml",
            f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets>' + "".join(
                f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
                for i, name in enumerate(sheets, 1)) + '</sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
            "".join(f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
                for i in range(1, len(sheets) + 1)) + '</Relationships>')
        for index, (_, data) in enumerate(sheets.items(), 1):
            rows_xml = []
            for number, values in enumerate(data["rows"], 1):
                cells = []
                for col, value in values.items():
                    if value is None:
                        continue
                    reference = f"{col}{number}"
                    if isinstance(value, (float, int)) and not isinstance(value, bool):
                        cells.append(f'<c r="{reference}"><v>{value}</v></c>')
                    else:
                        cells.append(f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">'
                                     f'{escape(str(value))}</t></is></c>')
                rows_xml.append(f'<row r="{number}">' + "".join(cells) + '</row>')
            merges = "".join(f'<mergeCell ref="{ref}"/>' for ref in data.get("merges", []))
            # Format-only cells and misleading dimensions are intentional.
            archive.writestr(f"xl/worksheets/sheet{index}.xml",
                f'<worksheet xmlns="{NS}"><dimension ref="A1:XFD1048576"/>'
                '<sheetData>' + "".join(rows_xml) + '<row r="501"><c r="XFC501" s="0"/></row>'
                '</sheetData><mergeCells>' + merges + '</mergeCells></worksheet>')


class ImporterSyntheticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "new.xlsx"
        self.rows = [
            {"A": "Артикул", "B": "Наименование", "C": "Коллекция", "D": "Цвет",
             "E": "Размеры", "F": "Цена", "G": "РРЦ"},
            {"A": "S01", "B": "Тумба 1", "C": "Линия", "D": "Дуб", "E": "400*500*600",
             "F": 1000, "G": 2000},
            {"A": "S01", "B": "Тумба 1", "C": "Линия", "D": "Белый", "E": "400*500*600",
             "F": 1200, "G": 2400},
            {"A": "S02", "B": "Шкаф 2", "C": "Линия", "D": "Дуб", "F": None},
            {"A": "S03", "B": "Кровать 3", "C": "Линия", "F": "уточните"},
            {"B": "Отдел продаж: +7 978 1234567", "F": 89781234567},
            {"B": "Итого", "F": 2200},
        ]
        fixture(self.path, {"Коллекция": {"rows": self.rows}})
        self.config = {"factory": "Фабрика", "sheets": [
            {"name": "Коллекция", "selected": True, "mapping": {
                "name": "B", "price": "F", "sku": "A", "color": "D",
                "dimensions": "E", "collection": "C"}}]}

    def tearDown(self):
        self.temp.cleanup()

    def test_sparse_bounds_and_mapping_inspection(self):
        start = time.perf_counter()
        result = inspect_file(str(self.path), "upload-1")
        self.assertLess(time.perf_counter() - start, 2)
        self.assertEqual(result["file_id"], "upload-1")
        sheet = result["sheets"][0]
        self.assertEqual(sheet["mapping"]["name"], "B")
        self.assertEqual(sheet["mapping"]["price"], "F")
        self.assertEqual(sheet["count"], 4)
        self.assertEqual([p["B"] for p in sheet["sample_rows"][:2]], ["Тумба 1", "Тумба 1"])
        self.assertNotIn("XFC", [item["column"] for item in sheet["headers"]])

    def test_missing_prices_are_issues_and_services_are_not_products(self):
        result = parse_file(str(self.path), self.config)
        self.assertEqual(len(result["products"]), 4)
        self.assertEqual(len(result["issues"]), 2)
        self.assertEqual([p["base_price"] for p in result["products"]], [1000.0, 1200.0, None, None])
        self.assertEqual([p["source_row"] for p in result["products"]], [2, 3, 4, 5])
        self.assertTrue(all(p["factory"] == "Фабрика" for p in result["products"]))

    def test_price_column_override_is_used(self):
        self.config["sheets"][0]["mapping"]["price"] = "G"
        result = parse_file(str(self.path), self.config)
        self.assertEqual(result["products"][0]["base_price"], 2000)
        self.assertEqual(result["products"][1]["base_price"], 2400)

    def test_disabled_sheet_is_never_parsed(self):
        self.config["sheets"][0]["selected"] = False
        self.assertEqual(parse_file(str(self.path), self.config)["products"], [])

    def test_two_variants_get_different_keys(self):
        products = parse_file(str(self.path), self.config)["products"]
        self.assertNotEqual(product_key(products[0]), product_key(products[1]))
        self.assertNotEqual(match_signature(products[0]), match_signature(products[1]))

    def test_identity_survives_price_source_and_name_change_with_sku(self):
        original = parse_file(str(self.path), self.config)["products"][0]
        changed = {**original, "base_price": 7000, "source_row": 50,
                   "source_file": "new price.xlsx", "name": "Новая подпись"}
        self.assertEqual(product_key(original), product_key(changed))
        self.assertNotEqual(product_key(original), product_key({**changed, "collection": "Другая"}))
        self.assertNotEqual(product_key(original), product_key({**changed, "variant": "Фасад"}))

    def test_without_sku_dimensions_and_name_distinguish_items(self):
        original = {"factory": "Ф", "name": "Шкаф", "collection": "Коллекция",
                    "dimensions": "400*500*600", "color": "Белый"}
        self.assertNotEqual(product_key(original), product_key({**original, "name": "Шкаф 2"}))
        self.assertNotEqual(product_key(original), product_key({**original, "dimensions": "400*500*700"}))
        self.assertEqual(product_key(original), product_key({**original, "name": " ШКАФ  "}))

    def test_merged_price_is_one_product_and_colours_are_preserved(self):
        fixture(self.path, {"Коллекция": {"rows": [
            {"A": "Наименование", "B": "Цена", "C": "Цвет", "D": "Размеры"},
            {"A": "Комод 1", "B": 9000, "C": "Белый", "D": "600*700*400"},
            {},
            {},
            {"A": "Комод 2", "B": 7000, "C": "Дуб", "D": "500*600*400"},
            {"B": 8000, "C": "Белый"},
        ], "merges": ["A2:A4", "B2:B4", "C2:C4", "D2:D4"]}})
        result = parse_file(str(self.path), {"factory": "Ф"})
        self.assertEqual([p["name"] for p in result["products"]], ["Комод 1", "Комод 2", "Комод 2"])
        self.assertEqual(result["products"][-1]["dimensions"], "500*600*400")
        self.assertEqual(result["products"][-1]["color"], "Белый")

    def test_unknown_factory_stays_empty(self):
        inspection = inspect_file(str(self.path), "id")
        self.assertEqual(inspection["factory"], "")
        self.assertTrue(inspection["warnings"])
        self.assertEqual(parse_file(str(self.path), {})["products"][0]["factory"], "")

    def test_russian_number_and_invalid_money(self):
        self.assertEqual(_price("12 345,60 ₽"), 12345.6)
        self.assertEqual(_price(0), 0)
        self.assertIsNone(_price("уточнить"))
        self.assertIsNone(_price(-10))
        self.assertIsNone(_price(float("nan")))
        self.assertIsNone(_price(89781234567))
        self.assertEqual(normalize_text(" Каркас №01 \n Пенал "), "Каркас №01 Пенал")

    def test_two_dimensional_notation_is_preserved_without_inferred_units(self):
        self.rows[1]["E"] = None
        self.rows[1]["B"] = "Кровать 1600*2000"
        fixture(self.path, {"Коллекция": {"rows": self.rows}})
        product = parse_file(str(self.path), self.config)["products"][0]
        self.assertEqual(product["dimensions"], "1600*2000")


class RealPriceRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.downloads = Path.home() / "Downloads"

    def load_price(self, name):
        path = self.downloads / name
        if not path.is_file():
            self.skipTest("Representative supplier sample not present on this machine.")
        view = inspect_file(str(path), "real")
        return view, parse_file(str(path), {"factory": view["factory"], "sheets": view["sheets"]})

    def test_rostok_three_rows_one_item(self):
        _, parsed = self.load_price("ОПТ Росток от 04.09..2026.xls")
        products = parsed["products"]
        sample = [p for p in products if p["source_row"] == 13]
        self.assertEqual(len(sample), 1)
        self.assertEqual(sample[0]["base_price"], 32700)
        self.assertEqual(sample[0]["dimensions"], "Ш: 2 400; В: 2 150; Г: 600")
        self.assertNotIn(14, [p["source_row"] for p in products])
        self.assertNotIn(15, [p["source_row"] for p in products])
        self.assertTrue(all(p["collection"] != products[0]["name"] for p in products))

    def test_albina_collections_and_variants(self):
        view, parsed = self.load_price("Прайс корпус Альбина ОПТ  01.10.26.xlsx")
        self.assertFalse(view["sheets"][1]["selected"])
        self.assertEqual(parsed["products"][0]["collection"], "Заря")
        self.assertEqual(parsed["products"][0]["dimensions"], "450*448*403")
        self.assertGreater(len(parsed["products"]), 300)
        self.assertFalse(parsed["issues"])

    def test_pyramid_empty_full_name_uses_actual_names(self):
        view, parsed = self.load_price("опт -Пирамида- от 12.10.2026.xlsx")
        self.assertEqual(view["sheets"][0]["mapping"]["name"], "A")
        self.assertEqual(parsed["products"][0]["name"], "Легенда В-44 (шкаф высокий 2290*400*420)")
        self.assertFalse(parsed["issues"])
        self.assertTrue(all(not p["name"].startswith("***") for p in parsed["products"]))

    def test_esta_format_only_wide_columns(self):
        view, parsed = self.load_price("Прайс ЭСТА Мебель с 05.10.2026.xlsx")
        self.assertEqual(view["factory"], "ЭСТА Мебель")
        self.assertEqual(view["sheets"][0]["mapping"]["price"], "B")
        self.assertGreater(len(parsed["products"]), 400)
        self.assertEqual(parsed["products"][0]["dimensions"], "900/2200/520")

    def test_stand_kitchen_module_and_component_identity(self):
        filename = "ОПТ Стендмебель кухни (белый) 16.09.2026.xls"
        view, parsed = self.load_price(filename)
        self.assertEqual(view["sheets"][0]["mapping"]["sku"], "E+H")
        first = parsed["products"][0]
        self.assertEqual(first["variant"], "Модуль")
        self.assertEqual(first["sku"], "Ф100")
        config = {"factory": view["factory"], "sheets": [{**view["sheets"][0]}]}
        config["sheets"][0]["mapping"] = {**config["sheets"][0]["mapping"], "price": "J"}
        components = parse_file(str(self.downloads / filename), config)["products"]
        self.assertEqual(components[0]["variant"], "Фасад")
        self.assertNotEqual(product_key(first), product_key(components[0]))

    def test_stand_first_block_and_full_names(self):
        view, parsed = self.load_price("ОПТ Стендмебель корпус от 16.09.2026.xlsx")
        self.assertEqual(view["sheets"][0]["mapping"]["name"], "G")
        self.assertEqual(parsed["products"][0]["source_row"], 10)
        self.assertIn("АЛИСА", parsed["products"][0]["name"])

    def test_untitled_factory_not_invented(self):
        view, parsed = self.load_price("Untitled.xls")
        self.assertEqual(view["factory"], "")
        self.assertEqual(parsed["products"][0]["factory"], "")
        self.assertEqual(parsed["products"][0]["collection"], "Сиде")
        self.assertEqual(parsed["products"][0]["sku"], "12902")

    def test_sv_nn_factory_and_all_price_choices(self):
        view, parsed = self.load_price("SV-Мебель, NN-Мебель_ОПТ_15.10.26.xlsx")
        nn = next(s for s in view["sheets"] if s["name"] == "NN-МЕБЕЛЬ")
        self.assertEqual(nn["mapping"]["price"], "F")
        self.assertIn("G", [h["column"] for h in nn["headers"]])
        self.assertIn("H", [h["column"] for h in nn["headers"]])
        self.assertEqual(parsed["products"][0]["factory"], "NN-Мебель")
        self.assertFalse(view["sheets"][0]["selected"])
        self.assertFalse(view["sheets"][1]["selected"])


if __name__ == "__main__":
    unittest.main()
