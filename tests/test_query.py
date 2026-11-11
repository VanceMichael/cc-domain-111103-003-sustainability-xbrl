"""查询：跨企业可比值、量纲防线、重新投影不覆盖旧结果。"""
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.model import QName, Unit
from app.pipeline import ingest_filing, load_taxonomies
from app.query import compare_concept, reproject_view
from app.store import FactStore
from app.units import default_registry

FILINGS = Path("fixtures/filings")
U = "https://example.invalid/units"


def build_store():
    taxonomies = load_taxonomies()
    store = FactStore(taxonomies)
    for d in ("001-initial", "002-restatement", "003-competitor"):
        ingest_filing(store, taxonomies, FILINGS / d)
    return store


class CompareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = build_store()
        cls.registry = default_registry()
        # Scope1 维度组合取自当前视图
        cls.scope1 = None
        for key in cls.store.view(entity="SYNTH-01").entries:
            if key[0].local == "Emissions" and key[3]:
                cls.scope1 = key[3]
                break

    def test_cross_entity_comparison_explained(self):
        rows = compare_concept(
            self.store, self.registry, "Emissions",
            ["SYNTH-01", "SYNTH-02"],
            target=Unit(id="", measures=(QName(U, "tCO2e"),)),
            dimensions=self.scope1,
        )
        self.assertEqual(len(rows), 2)
        by_entity = {r.entity: r for r in rows}
        self.assertEqual(by_entity["SYNTH-01"].converted, Decimal("4500"))
        self.assertEqual(by_entity["SYNTH-02"].converted, Decimal("4800.000"))
        self.assertEqual(by_entity["SYNTH-02"].factor, Decimal("0.001"))
        # 每行都带判断与版本，比较结果可解释
        self.assertTrue(all(r.taxonomy_version for r in rows))
        self.assertTrue(all(r.judgment for r in rows))

    def test_incompatible_target_marked_per_row(self):
        rows = compare_concept(
            self.store, self.registry, "Workers", ["SYNTH-01"],
            target=Unit(id="", measures=(QName(U, "tCO2e"),)),
        )
        self.assertEqual(len(rows), 1)
        self.assertIn("量纲不兼容", rows[0].error)
        self.assertIsNone(rows[0].converted)


class ReprojectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = build_store()
        cls.taxonomies = cls.store.taxonomies

    def test_reproject_maps_old_names(self):
        proj = reproject_view(
            self.store, self.taxonomies, "base-2026", "p1", entity="SYNTH-01"
        )
        mapped = {e.concept_from: e for e in proj.entries}
        self.assertEqual(mapped["WasteGenerated"].concept_to, "Waste")
        self.assertEqual(mapped["WasteGenerated"].status, "ok")
        self.assertEqual(mapped["Workers"].status, "unchanged")

    def test_reproject_does_not_rewrite_originals(self):
        before = {f.concept.local for f in self.store.get("f-001").facts}
        reproject_view(self.store, self.taxonomies, "base-2026", "p2", entity="SYNTH-01")
        after = {f.concept.local for f in self.store.get("f-001").facts}
        self.assertEqual(before, after)
        self.assertIn("WasteGenerated", after)  # 旧事实保持当时名称

    def test_projection_collapses_to_current_names(self):
        proj = reproject_view(
            self.store, self.taxonomies, "base-2026", "p3", entity="SYNTH-01"
        )
        current = proj.current()
        waste_entries = [e for k, e in current.items() if k[0].local == "Waste"]
        # 主口径（无维度）只剩一条：后申报的 f-002 覆盖 f-001 的投影
        main = [e for e in waste_entries if not e.original_key[3]]
        self.assertEqual(len(main), 1)
        self.assertEqual(main[0].filing_id, "f-002")
        self.assertEqual(main[0].group.chosen.value, Decimal("135"))

    def test_projection_is_additive(self):
        reproject_view(self.store, self.taxonomies, "base-2026", "p4", entity="SYNTH-01")
        with self.assertRaises(ValueError):
            reproject_view(
                self.store, self.taxonomies, "base-2026", "p4", entity="SYNTH-01"
            )
        self.assertIn("p4", self.store.projections)

    def test_foreign_namespace_passes_through(self):
        proj = reproject_view(
            self.store, self.taxonomies, "base-2026", "p5", entity="SYNTH-01"
        )
        foreign = [e for e in proj.entries if e.status == "foreign"]
        self.assertEqual(len(foreign), 1)

    def test_as_of_projection_uses_old_values(self):
        proj = reproject_view(
            self.store, self.taxonomies, "base-2026", "p6",
            entity="SYNTH-01", as_of=datetime(2026, 5, 1),
        )
        current = proj.current()
        waste = [e for k, e in current.items()
                 if k[0].local == "Waste" and not k[3]]
        self.assertEqual(waste[0].group.chosen.value, Decimal("120.0"))


if __name__ == "__main__":
    unittest.main()
