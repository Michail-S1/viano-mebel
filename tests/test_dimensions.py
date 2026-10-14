import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.dimensions import make_matcher, axes_mm

class SizeFilterTests(unittest.TestCase):
    def test_separators_decimals_and_whole_numbers(self):
        match = make_matcher({"dimensions": "800 × 2200 * 520"})
        for value in ("800х2200х520", "800 / 2200 / 520", "Ш: 800; В: 2200; Г: 520"):
            self.assertTrue(match(value), value)
        self.assertFalse(match("1800х2200х520"))
        self.assertTrue(make_matcher({"dimensions": "0.4 × 2.2"} )("0,4х2,2х0,520"))
        self.assertTrue(make_matcher({"dimensions": "800"})("800x2200x520"))
        self.assertFalse(make_matcher({"dimensions": "800"})("1800x2200x520"))
    def test_repeated_values_and_missing_values(self):
        self.assertFalse(make_matcher({"dimensions": "800x800"})("800x600x500"))
        self.assertTrue(make_matcher({"dimensions": "800x800"})("800x800x500"))
        self.assertFalse(make_matcher({"dimensions": "800"})(""))
        self.assertFalse(make_matcher({"dimensions": "1"})("800х2200х520 (№1)"))
    def test_explicit_labels_and_unit_conversion(self):
        self.assertEqual(axes_mm("Ш: 80 см; В: 220 см; Г: 52 см"), {"width":800,"height":2200,"depth":520})
        self.assertEqual(axes_mm("Ш×Г×В: 0,8х0,52х2,2 м"), {"width":800,"depth":520,"height":2200})
        self.assertEqual(axes_mm("800x2200x520 мм"), {})
    def test_unknown_units_and_axes_are_not_guessed(self):
        self.assertEqual(axes_mm("800x2200x520"), {})
        self.assertEqual(axes_mm("800x2200x520", "whd"), {})
        self.assertEqual(axes_mm("Ш: 800; В: 2200; Г: 520"), {})
        self.assertEqual(axes_mm("800x2200x520", "wdh", "mm"), {"width":800,"depth":2200,"height":520})
        self.assertEqual(axes_mm("0,8х2,2х0,52", "whd", "m"), {"width":800,"height":2200,"depth":520})
    def test_range_boundaries_combination_and_source_unit(self):
        params={"width_min":"800","width_max":"800","height_min":"2199","depth_max":"520"}
        match=make_matcher(params)
        self.assertTrue(match("Ш: 80 см; В: 220 см; Г: 52 см"))
        self.assertFalse(match("Ш: 81 см; В: 220 см; Г: 52 см"))
        self.assertFalse(match("800х2200х520"))
        self.assertTrue(make_matcher({**params,"dimension_order":"whd","dimension_unit":"mm"})("800х2200х520"))
        self.assertFalse(make_matcher({**params,"dimensions":"900"})("Ш: 80 см; В: 220 см; Г: 52 см"))
    def test_width_up_to_1901_is_inclusive_and_does_not_search_height(self):
        match=make_matcher({"width_max":"1901","dimension_order":"whd","dimension_unit":"mm"})
        self.assertTrue(match("1900×2200×500"))
        self.assertTrue(match("1901×2200×500"))
        self.assertFalse(match("1901.1×2200×500"))
        self.assertFalse(match("2000×1900×500"))
        self.assertTrue(make_matcher({"dimensions":"1900"})("2000×1900×500"))
    def test_bad_ranges_and_parameters(self):
        for params in ({"width_min":"nan"},{"height_max":"-1"},{"depth_min":"900","depth_max":"800"},
                       {"dimensions":"шкаф"},{"dimension_order":"guess"},{"dimension_unit":"feet"}):
            with self.assertRaises(ValueError, msg=str(params)):
                make_matcher(params)
    def test_partial_labels_and_explicit_units_win(self):
        self.assertTrue(make_matcher({"width_max":"900","dimension_unit":"m"})("Ширина: 80 см"))
        self.assertFalse(make_matcher({"height_max":"900","dimension_unit":"mm"})("Ширина: 800"))
        self.assertEqual(axes_mm("Ш: 800 мм; Ш: 900 мм"), {})

if __name__ == "__main__": unittest.main()
