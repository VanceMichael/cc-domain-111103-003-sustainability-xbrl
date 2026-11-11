"""重复事实分类：格式重复、精度变体、单位变体、同上下文冲突。"""
import unittest
from decimal import Decimal
from pathlib import Path

from app.dedupe import DupClass, classify_facts
from app.extract import extract
from app.units import default_registry


def groups_by_concept():
    doc = extract(
        Path("fixtures/filings/001-initial/report.xhtml").read_bytes(), "f-001"
    )
    groups = classify_facts(doc.facts, default_registry())
    out = {}
    for g in groups:
        out.setdefault(g.key[0].local, []).append(g)
    return out


class DedupeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.groups = groups_by_concept()

    def group(self, local, dims=False):
        for g in self.groups[local]:
            if bool(g.key[3]) == dims:
                return g
        raise AssertionError(f"未找到组 {local} dims={dims}")

    def test_format_duplicate_keeps_all_sources(self):
        g = self.group("SafetyDrills")
        self.assertEqual(g.classification, DupClass.FORMAT_DUPLICATE)
        self.assertEqual(len(g.members), 2)  # 正文 + 隐藏区，全部保留
        self.assertTrue(any(m.hidden for m in g.members))
        self.assertIsNotNone(g.chosen)

    def test_precision_variant(self):
        g = self.group("WasteGenerated")
        self.assertEqual(g.classification, DupClass.PRECISION_VARIANT)
        self.assertEqual(len(g.members), 3)
        # 判定取精度最高者（decimals=1），其余保留
        self.assertEqual(g.chosen.decimals, "1")
        self.assertEqual(g.chosen.value, Decimal("120.0"))

    def test_unit_variant(self):
        g = self.group("Energy")
        self.assertEqual(g.classification, DupClass.UNIT_VARIANT)
        self.assertEqual(len(g.members), 2)
        units = {m.unit.id for m in g.members}
        self.assertEqual(units, {"u-mwh", "u-kwh"})
        self.assertEqual(g.chosen.unit.id, "u-mwh")

    def test_conflict_not_auto_resolved(self):
        g = self.group("Workers")
        self.assertEqual(g.classification, DupClass.CONFLICT)
        self.assertIsNone(g.chosen)  # 不静默选第一个
        self.assertTrue(g.needs_review)
        self.assertEqual({m.value for m in g.members}, {Decimal("35"), Decimal("36")})

    def test_dimension_splits_groups(self):
        emissions = self.groups["Emissions"]
        self.assertEqual(len(emissions), 2)  # Scope1 / Scope2 各自成组
        self.assertTrue(all(g.classification == DupClass.SINGLE for g in emissions))

    def test_foreign_namespace_separate_group(self):
        waste_groups = self.groups["WasteGenerated"]
        # esg 命名空间 2 组（c2 主口径 + c5 北区），esg2 命名空间 1 组
        self.assertEqual(len(waste_groups), 3)
        foreign = [g for g in waste_groups if g.key[0].ns.endswith("esg2")]
        self.assertEqual(len(foreign), 1)
        self.assertEqual(foreign[0].chosen.value, Decimal("999"))


if __name__ == "__main__":
    unittest.main()
