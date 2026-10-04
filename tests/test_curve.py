import importlib.util
import pathlib
import sys
import unittest

MODULE = pathlib.Path(__file__).parents[1] / "src" / "fan_curve.py"
spec = importlib.util.spec_from_file_location("fan_curve", MODULE)
fan_curve = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = fan_curve
spec.loader.exec_module(fan_curve)


class CurveTests(unittest.TestCase):
    curve = ((0, 0), (60, 25), (65, 35), (70, 50), (75, 75), (80, 100))

    def test_raw_bands(self):
        self.assertEqual(fan_curve.raw_band(self.curve, 59), 0)
        self.assertEqual(fan_curve.raw_band(self.curve, 60), 1)
        self.assertEqual(fan_curve.raw_band(self.curve, 66), 2)
        self.assertEqual(fan_curve.raw_band(self.curve, 100), 5)

    def test_up_is_immediate(self):
        self.assertEqual(fan_curve.select_band(self.curve, 65, 1, 2), 2)

    def test_down_has_hysteresis(self):
        # Entered 35% at 65C: hold it at 64/63.1-ish integer 64/63? With
        # integer sensors and "temp > threshold-H", 64 holds, 63 drops.
        self.assertEqual(fan_curve.select_band(self.curve, 64, 2, 2), 2)
        self.assertEqual(fan_curve.select_band(self.curve, 63, 2, 2), 1)

    def test_large_drop_can_skip_bands(self):
        self.assertEqual(fan_curve.select_band(self.curve, 50, 5, 2), 0)


if __name__ == "__main__":
    unittest.main()
