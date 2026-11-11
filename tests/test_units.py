"""单位注册表：量纲兼容是换算前提。"""
import unittest
from decimal import Decimal

from app.model import QName, Unit
from app.units import (
    IncompatibleUnits,
    UnknownMeasure,
    default_registry,
    vector_label,
)

U = "https://example.invalid/units"


def unit(local, divide=None):
    return Unit(
        id="",
        measures=(QName(U, local),),
        divide=tuple(QName(U, d) for d in (divide or ())),
    )


class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = default_registry()

    def test_mass_conversion(self):
        self.assertEqual(
            self.reg.convert(Decimal("120"), unit("tonne"), unit("kg")),
            Decimal("120000"),
        )

    def test_co2e_family_conversion(self):
        self.assertEqual(
            self.reg.convert(Decimal("4800000"), unit("kgCO2e"), unit("tCO2e")),
            Decimal("4800.000"),
        )

    def test_energy_conversion(self):
        self.assertEqual(
            self.reg.convert(Decimal("2500000"), unit("kWh"), unit("MWh")),
            Decimal("2500"),
        )

    def test_incompatible_raises(self):
        with self.assertRaises(IncompatibleUnits):
            self.reg.convert(Decimal("1"), unit("kg"), unit("count"))

    def test_co2e_is_not_plain_mass(self):
        # tCO2e 与 tonne 不可自动换算：量纲种类不同
        with self.assertRaises(IncompatibleUnits):
            self.reg.convert(Decimal("1"), unit("tCO2e"), unit("tonne"))

    def test_compound_unit_vector(self):
        intensity = Unit(id="", measures=(QName(U, "kgCO2e"),), divide=(QName(U, "MWh"),))
        vector, factor = self.reg.unit_vector(intensity)
        self.assertEqual(vector_label(vector), "mass_co2e_per_energy")
        same = Unit(id="", measures=(QName(U, "tCO2e"),), divide=(QName(U, "GJ"),))
        self.assertTrue(self.reg.compatible(intensity, same))

    def test_unknown_measure(self):
        with self.assertRaises(UnknownMeasure):
            self.reg.unit_vector(unit("lightyear"))

    def test_unprefixed_measure_fallback(self):
        # 旧式未加前缀写法按本地名唯一匹配
        legacy = Unit(id="", measures=(QName(None, "kg"),))
        vector, _ = self.reg.unit_vector(legacy)
        self.assertEqual(vector_label(vector), "mass")

    def test_taxonomy_declared_units(self):
        self.reg.load_taxonomy_units(
            U, {"score": {"dimension": {"score": 1}, "to_base": "1"}}
        )
        vector, _ = self.reg.unit_vector(unit("score"))
        self.assertEqual(vector_label(vector), "score")


if __name__ == "__main__":
    unittest.main()
