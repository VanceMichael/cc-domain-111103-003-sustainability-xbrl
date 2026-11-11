"""存储与视图：重述双时态、taxonomy 版本固定、追溯、差异。"""
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.pipeline import ingest_filing, load_taxonomies
from app.store import FactStore
from app.units import default_registry

FILINGS = Path("fixtures/filings")


def build_store():
    taxonomies = load_taxonomies()
    store = FactStore(taxonomies)
    for d in ("001-initial", "002-restatement", "003-competitor"):
        ingest_filing(store, taxonomies, FILINGS / d)
    return store


def entry(view, local, dims=None):
    for key, e in view.entries.items():
        if key[0].local == local and (dims is None or key[3] == dims):
            return e
    raise AssertionError(f"视图中未找到 {local}")


class RestatementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = build_store()

    def test_current_view_uses_latest_filing(self):
        view = self.store.view(entity="SYNTH-01")
        self.assertEqual(entry(view, "Workers").group.chosen.value, Decimal("36"))
        self.assertEqual(entry(view, "Waste").group.chosen.value, Decimal("135"))

    def test_as_of_view_keeps_old_filing(self):
        view = self.store.view(entity="SYNTH-01", as_of=datetime(2026, 5, 1))
        # 重述前：旧概念名、旧值
        self.assertEqual(
            entry(view, "WasteGenerated", dims=frozenset()).group.chosen.value,
            Decimal("120.0"),
        )
        with self.assertRaises(AssertionError):
            entry(view, "Waste")  # 当时还没有新名称
        # 冲突在当时未决
        self.assertIsNone(entry(view, "Workers").group.chosen)

    def test_old_filing_remains_queryable(self):
        filing = self.store.get("f-001")
        self.assertEqual(len(filing.facts), 16)
        self.assertEqual(filing.supersedes, None)
        self.assertEqual(self.store.get("f-002").supersedes, "f-001")

    def test_view_entry_records_superseded(self):
        view = self.store.view(entity="SYNTH-01")
        e = entry(view, "Workers")
        self.assertEqual(e.filing_id, "f-002")
        self.assertEqual(e.superseded[0][0], "f-001")

    def test_taxonomy_pinned_at_filing_time(self):
        # 新 taxonomy 发布后，旧 filing 的可见事实不被改写
        self.assertEqual(self.store.get("f-001").taxonomy_version, "base-2025")
        self.assertEqual(self.store.get("f-002").taxonomy_version, "base-2026")
        view = self.store.view(entity="SYNTH-01")
        names = {k[0].local for k in view.entries}
        self.assertIn("WasteGenerated", names)  # 旧名称仍按当时版本呈现
        self.assertIn("Waste", names)


class TraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = build_store()

    def test_trace_back_to_source(self):
        view = self.store.view(entity="SYNTH-01")
        fact = entry(view, "Waste").group.chosen
        trace = self.store.trace(fact.fact_id)
        self.assertEqual(trace.filing.id, "f-002")
        self.assertEqual(trace.fact.value_raw, "135")
        self.assertGreater(trace.fact.provenance.line, 0)
        self.assertEqual(trace.fact.provenance.parser_version, "ix-extract/1.0")
        self.assertEqual(trace.concept_status, "ok")
        text = trace.explain()
        self.assertIn("002-restatement", text)
        self.assertIn("base-2026", text)

    def test_trace_normalization_steps(self):
        view = self.store.view(entity="SYNTH-01", as_of=datetime(2026, 5, 1))
        for key, e in view.entries.items():
            if key[0].local == "EmissionsReduction":
                fact = e.group.chosen
                break
        trace = self.store.trace(fact.fact_id)
        self.assertTrue(any("取负" in s for s in trace.fact.normalization))

    def test_trace_marks_foreign_namespace(self):
        view = self.store.view(entity="SYNTH-01", as_of=datetime(2026, 5, 1))
        for key, e in view.entries.items():
            if key[0].ns and key[0].ns.endswith("esg2"):
                fact = e.group.chosen
                break
        trace = self.store.trace(fact.fact_id)
        self.assertEqual(trace.concept_status, "foreign")


class DiffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = build_store()
        cls.registry = default_registry()

    def test_plain_diff_shows_rename_as_add_remove(self):
        diff = self.store.diff("f-001", "f-002", self.registry)
        statuses = {(e.key[0].local, e.status) for e in diff.entries}
        self.assertIn(("Waste", "added"), statuses)
        self.assertIn(("WasteGenerated", "removed"), statuses)

    def test_projected_diff_separates_rename_from_restatement(self):
        diff = self.store.diff("f-001", "f-002", self.registry, project_to="base-2026")
        waste = [e for e in diff.entries
                 if e.key[0].local == "Waste" and e.status == "changed"]
        self.assertEqual(len(waste), 1)
        self.assertEqual(waste[0].delta, Decimal("15.0"))  # 120.0 → 135
        # 更名本身不再表现为 added
        self.assertFalse(any(e.key[0].local == "Waste" and e.status == "added"
                             for e in diff.entries))

    def test_diff_conflict_marked(self):
        diff = self.store.diff("f-001", "f-002", self.registry)
        workers = [e for e in diff.entries if e.key[0].local == "Workers"]
        self.assertIn("冲突", workers[0].note)


if __name__ == "__main__":
    unittest.main()
