"""Inline XBRL 提取：溯源位置、规范化、QName 身份、维度。"""
import unittest
from decimal import Decimal
from pathlib import Path

from app.extract import extract
from app.model import QName

ESG = "https://example.invalid/esg"
ESG2 = "https://example.invalid/esg2"


def load(name="fixtures/filings/001-initial/report.xhtml"):
    return extract(Path(name).read_bytes(), source_name=name)


class InlineExtractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = load()

    def fact(self, local, ctx=None, raw=None):
        for f in self.doc.facts:
            if f.concept.local != local:
                continue
            if ctx and (f.context is None or f.context.id != ctx):
                continue
            if raw is not None and f.value_raw != raw:
                continue
            return f
        raise AssertionError(f"未找到事实 {local} ctx={ctx} raw={raw}")

    def test_provenance_position(self):
        f = self.fact("Emissions", ctx="c3")
        self.assertGreater(f.provenance.line, 0)
        self.assertGreater(f.provenance.column, 0)
        self.assertEqual(f.provenance.parser_version, "ix-extract/1.0")
        self.assertEqual(len(f.provenance.source_hash), 64)

    def test_format_scale_sign(self):
        self.assertEqual(self.fact("Emissions", ctx="c3").value, Decimal("4500"))  # "4,500"
        self.assertEqual(self.fact("Energy", ctx="c2", raw="2500").value, Decimal("2500000"))  # scale=3
        self.assertEqual(self.fact("EmissionsReduction").value, Decimal("-300"))  # sign="-"

    def test_raw_text_preserved(self):
        f = self.fact("Emissions", ctx="c3")
        self.assertEqual(f.value_raw, "4,500")
        self.assertTrue(any("numdotdecimal" in s for s in f.normalization))

    def test_nonnumeric_text(self):
        f = self.fact("SafetyPolicy", ctx="c1")
        self.assertEqual(f.kind, "nonNumeric")
        self.assertIn("虚构", f.value)

    def test_hidden_flag(self):
        hidden = [f for f in self.doc.facts if f.hidden]
        self.assertEqual(len(hidden), 2)
        self.assertEqual({f.concept.local for f in hidden}, {"WasteGenerated", "SafetyDrills"})

    def test_same_local_name_different_namespace(self):
        esg = self.fact("WasteGenerated", ctx="c2", raw="120")
        esg2 = self.fact("WasteGenerated", ctx="c2", raw="999")
        self.assertNotEqual(esg.concept, esg2.concept)
        self.assertEqual(esg.concept, QName(ESG, "WasteGenerated"))
        self.assertEqual(esg2.concept, QName(ESG2, "WasteGenerated"))

    def test_dimensions(self):
        c3 = self.doc.contexts["c3"]
        self.assertEqual(len(c3.dimensions), 1)
        dim = next(iter(c3.dimensions))
        self.assertEqual(dim.axis, QName(ESG, "ScopeAxis"))
        self.assertEqual(dim.member, QName(ESG, "Scope1"))
        self.assertEqual(dim.origin, "segment")
        c4 = self.doc.contexts["c4"]
        self.assertEqual(next(iter(c4.dimensions)).origin, "scenario")

    def test_compound_unit(self):
        u = self.doc.units["u-kgmwh"]
        self.assertEqual(len(u.measures), 1)
        self.assertEqual(len(u.divide), 1)

    def test_period_and_entity(self):
        c1 = self.doc.contexts["c1"]
        self.assertEqual(c1.period.kind, "instant")
        self.assertEqual(str(c1.period.instant), "2026-12-31")
        self.assertEqual(c1.entity.identifier, "SYNTH-01")
        c2 = self.doc.contexts["c2"]
        self.assertEqual(c2.period.kind, "duration")

    def test_context_id_not_semantic(self):
        # id 不参与上下文相等性：结构相同即同一上下文
        from app.model import Context
        c2 = self.doc.contexts["c2"]
        clone = Context(id="other-id", entity=c2.entity, period=c2.period,
                        dimensions=c2.dimensions)
        self.assertEqual(c2, clone)


class PlainInstanceTests(unittest.TestCase):
    def test_plain_instance_via_new_extractor(self):
        doc = load("fixtures/report.xml")
        self.assertEqual(len(doc.facts), 1)
        f = doc.facts[0]
        self.assertEqual(f.concept, QName("https://example.invalid/esg", "Waste"))
        self.assertEqual(f.value, Decimal("120"))
        self.assertEqual(f.kind, "item")

    def test_invalid_xml_raises(self):
        with self.assertRaises(Exception):
            extract(b"<xbrl")


if __name__ == "__main__":
    unittest.main()
