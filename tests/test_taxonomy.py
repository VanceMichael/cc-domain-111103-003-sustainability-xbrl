"""taxonomy 版本化：extension 链、环与缺失的局部标记、概念映射。"""
import unittest
from datetime import date
from pathlib import Path

from app.taxonomy import TaxonomyRegistry


def registry():
    reg = TaxonomyRegistry()
    reg.load_dir(Path("fixtures/taxonomy"))
    return reg


class ExtensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = registry()

    def test_extension_inherits_base(self):
        tax = self.reg.get("ext-acme-2026")
        self.assertIn("Waste", tax.concepts)          # 来自 base-2026
        self.assertIn("WaterWithdrawal", tax.concepts)  # 自身新增
        self.assertEqual(tax.lineage, ["base-2026", "ext-acme-2026"])

    def test_extension_ref_to_base_concept(self):
        tax = self.reg.get("ext-acme-2026")
        total = tax.concepts["TotalWaste"]
        self.assertEqual(total.status, "ok")
        self.assertEqual(total.dimension, "mass")  # 继承自基础概念 Waste

    def test_extends_cycle_marked_locally(self):
        tax = self.reg.get("ext-cycle-a")
        self.assertTrue(any("环" in p for p in tax.problems))
        # 环上的概念被标记，无关概念不受影响
        self.assertEqual(tax.concepts["Alpha"].status, "cycle")
        self.assertEqual(tax.concepts["Beta"].status, "cycle")
        self.assertEqual(tax.concepts["Gamma"].status, "ok")

    def test_missing_reference_marked_locally(self):
        tax = self.reg.get("ext-missing")
        self.assertTrue(any("ext-ghost" in p for p in tax.problems))
        self.assertEqual(tax.concepts["Orphan"].status, "missing")
        self.assertEqual(tax.concepts["Fine"].status, "ok")
        # 基础概念不因扩展的问题而消失
        self.assertEqual(tax.concepts["Waste"].status, "ok")

    def test_broken_alias_marked(self):
        tax = self.reg.get("ext-missing")
        self.assertTrue(any("Ghost" in p for p in tax.problems))
        mapping = tax.map_name("Ghost")
        self.assertEqual(mapping.status, "broken")

    def test_cycle_does_not_contaminate_parent(self):
        # ext-cycle-b 的环标记不应改写 base 版本的概念状态
        base = self.reg.get("base-2026")
        self.assertTrue(all(c.status == "ok" for c in base.concepts.values()))


class MappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = registry()

    def test_alias_mapping(self):
        m = self.reg.map_concept("WasteGenerated", "base-2026")
        self.assertEqual((m.status, m.target), ("ok", "Waste"))

    def test_replaces_implies_alias(self):
        tax = self.reg.get("base-2026")
        self.assertEqual(tax.aliases.get("WasteGenerated"), "Waste")

    def test_unchanged_concept(self):
        m = self.reg.map_concept("Workers", "base-2026")
        self.assertEqual((m.status, m.target), ("unchanged", "Workers"))

    def test_unmapped_concept(self):
        m = self.reg.map_concept("NoSuchThing", "base-2026")
        self.assertEqual(m.status, "unmapped")

    def test_version_for_date(self):
        self.assertEqual(
            self.reg.version_for_date(date(2025, 6, 1)).version, "base-2025"
        )
        self.assertEqual(
            self.reg.version_for_date(date(2026, 1, 15)).version, "base-2026"
        )

    def test_legacy_shorthand_format(self):
        reg = TaxonomyRegistry()
        reg.load("fixtures/taxonomy.json")
        tax = reg.get("2026-synthetic")
        self.assertEqual(tax.concepts["Waste"].dimension, "mass")
        self.assertEqual(tax.aliases.get("WasteGenerated"), "Waste")


if __name__ == "__main__":
    unittest.main()
