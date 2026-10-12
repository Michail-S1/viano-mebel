"""Colour extraction tests against supplier wording and model/size false positives."""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.colors import derive_color


class ColorTests(unittest.TestCase):
    def test_supplier_shades_and_spelling_variants(self):
        for shade in ["Блю", "Вижн", "Вижен", "Кашемир", "Кашмир", "Латте",
                      "Графит", "Айс", "Айвори", "Антарес"]:
            with self.subTest(shade=shade):
                self.assertEqual(derive_color("Фасад " + shade), shade)

    def test_compound_supplier_shades(self):
        for shade in ["Браш Деним Блю", "Браш Лайт Грей", "Шарли Pink", "Пластер Бьянко",
                      "Графит софт NEW", "3Д Айс", "Атлантик Софт", "Дуб Юкон",
                      "Жемчуг шервуд", "Холст вулканический", "Экзотическое дерево Амазония"]:
            with self.subTest(shade=shade):
                self.assertEqual(derive_color("Фасад (" + shade + ")"), shade)

    def test_source_capitalization_and_separators_survive(self):
        self.assertEqual(derive_color("Шкаф (Дуб сонома / Белый)"), "Дуб сонома / Белый")
        self.assertEqual(derive_color("Комод (Кашемир, Кашемир)"), "Кашемир, Кашемир")
        self.assertEqual(derive_color("Комод (ГРАФИТ+ДУБ КРАФТ)"), "ГРАФИТ+ДУБ КРАФТ")

    def test_multiple_parentheses_preserve_full_nested_palette(self):
        name = "Кухня Техно-5 3,2*2,24м (490) (Сонома/Браш Блю, Белый (Кастилло темный))"
        self.assertEqual(derive_color(name), "Сонома/Браш Блю, Белый (Кастилло темный)")

    def test_explicit_colour_is_authoritative(self):
        name = "Комод (Блю)"
        self.assertEqual(derive_color(name, existing="Gray"), "Gray")
        self.assertEqual(derive_color(name, existing="Фасад Белый/Корпус Венге"),
                         "Фасад Белый/Корпус Венге")
        self.assertEqual(derive_color(name, existing=" Неизвестный авторский оттенок "),
                         " Неизвестный авторский оттенок ")

    def test_collection_name_is_not_colour(self):
        for name, collection in [
            ("Кухня Роза 2м", "Роза"), ("Комод Кашемир 900", "Кашемир"),
            ("Пенал Оазис", "Оазис"), ("Кухня Магнолия В400", "Магнолия"),
            ("КУХНЯ АЙС 2.4", "Кухни"), ("Кухня АЙС 2.4", ""),
        ]:
            with self.subTest(name=name):
                self.assertEqual(derive_color(name, collection), "")

    def test_collection_palette_never_assigns_a_selected_colour(self):
        self.assertEqual(derive_color("Верхний шкаф Виано АНП 400",
                                     "Аверса, Баунти, Блю, Вижн, Иридиум"), "")
        self.assertEqual(derive_color("Верхний шкаф Фенис АНП 400",
                                     "3Д Айс, Арт Голд, Палм Маус"), "")

    def test_collection_finish_mentioned_later_remains_valid(self):
        self.assertEqual(derive_color("Кухня Роза Фасад (Роза)", "Роза"), "Роза")
        self.assertEqual(derive_color("Комод Кашемир (Белый)", "Кашемир"), "Белый")

    def test_quoted_finish_like_model_stays_empty(self):
        self.assertEqual(derive_color('Комод «Графит»'), "")
        self.assertEqual(derive_color('Стол "Лайт"'), "")
        self.assertEqual(derive_color('Комод цвет: «Графит»'), "Графит")

    def test_quoted_model_with_finish_is_extracted(self):
        name = "МС Роза / Антресоль 450 «Марсель Бежевый глянец» — (Базовая комплектация)"
        self.assertEqual(derive_color(name, "МС Роза"), "Бежевый глянец")
        name = "МС Роза / Антресоль 450 «Марсель Фреш Меркури» — (Базовая комплектация)"
        self.assertEqual(derive_color(name, "МС Роза"), "Фреш Меркури")

    def test_finish_before_dimensions_is_retained(self):
        self.assertEqual(derive_color("ФАСАД ШН-400 ЛАТТЕ 396/716", "Кухни"), "ЛАТТЕ")
        self.assertEqual(derive_color("Фасад ШВ-400 графит 396*716", "Кухни"), "графит")

    def test_component_roles_are_not_lost(self):
        name = "Шкаф (Корпус: Венге / Фасад: Белфорт)"
        self.assertEqual(derive_color(name), "Корпус: Венге / Фасад: Белфорт")
        name = "Шкаф корпус Венге/фасад Белфорт"
        self.assertEqual(derive_color(name), "корпус Венге/фасад Белфорт")

    def test_english_and_standard_colour_codes(self):
        self.assertEqual(derive_color("Опора Н-049 (Н-120) Black"), "Black")
        self.assertEqual(derive_color("Комод (White/Gray)"), "White/Gray")
        name = "Опора профильная (барная 820х550) OPR-P130 (RAL 9005 чёрный, бархат) KR002"
        self.assertEqual(derive_color(name), "RAL 9005 чёрный, бархат")
        self.assertEqual(derive_color("Фасад (NCS S 1002-Y)"), "NCS S 1002-Y")

    def test_word_boundaries_and_unrelated_modules(self):
        for name in ["Направляющая выдвижная 450", "Шкаф с выдвижными ящиками",
                     "Стол Айсберг", "Шкаф кашемировый механизм", "Опора FS079-032",
                     "Фасад ШВГ600/450 (DELL)", "Ручка (Ким)"]:
            with self.subTest(name=name):
                self.assertEqual(derive_color(name), "")

    def test_noncolour_parentheses_do_not_assign_colour(self):
        for name in ["Мойка 60*60 (Левая, Правая)", "Шкаф (Базовая комплектация)",
                     "Кровать (1600*2000)", "Фасад (МДФ)", "Корзина (Бутылочница)",
                     "Столешница (некромленая)", "Фасад (Дерево)"]:
            with self.subTest(name=name):
                self.assertEqual(derive_color(name), "")

    def test_variant_only_requires_complete_colour_expression(self):
        self.assertEqual(derive_color("Фасад ШН400", variant="Блю"), "Блю")
        self.assertEqual(derive_color("Фасад ШН400", variant="Корпус"), "")
        self.assertEqual(derive_color("Фасад ШН400", variant="Блю или Вижн"), "")

    def test_no_mutation_of_source_strings(self):
        name = "Нэнси New Тумба ТВ (Браш Блю, Белый)"
        original = name[:]
        derive_color(name)
        self.assertEqual(name, original)


class CatalogueAuditTests(unittest.TestCase):
    def test_full_catalogue_keeps_explicit_values_and_fills_real_names(self):
        database = ROOT / "data" / "catalog.sqlite3"
        if not database.is_file():
            self.skipTest("Local catalogue is not available on this machine")
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            records = connection.execute("SELECT name,collection,variant,color FROM products").fetchall()
        if len(records) < 40000:
            self.skipTest("Representative catalogue not loaded")
        upgraded = 0
        for name, collection, variant, existing in records:
            result = derive_color(name, collection, variant, existing)
            if existing:
                self.assertEqual(result, existing)
            elif result:
                upgraded += 1
        self.assertGreater(upgraded, 22000)
        self.assertEqual(derive_color("КУХНЯ АЙС 2.4", "Кухни"), "")


if __name__ == "__main__":
    unittest.main()
