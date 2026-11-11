"""filing 存储、双时态视图、事实追溯与差异查询。

- 每个 filing 不可变地保存提取结果，并固定其申报时使用的 taxonomy 版本与解析器版本；
  之后发布的新 taxonomy 不会改写当时可见的事实。
- 当前视图按（概念, 实体, 期间, 维度）选择申报时间最新的 filing 内容；
  ``as_of`` 视图回到任一历史时点，重述前的旧 filing 始终可查。
- 追溯（trace）从规范化数值回到报告、原文位置、原始上下文与解析版本。
- 差异（diff）比较两个 filing，可选地先按目标 taxonomy 版本投影概念名，
  使准则更名与真实重述在差异中分开。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.dedupe import FactGroup
from app.model import Fact, QName
from app.taxonomy import TaxonomyRegistry
from app.units import IncompatibleUnits, UnitRegistry, UnknownMeasure


@dataclass
class Filing:
    id: str
    entity: str
    filed_at: datetime
    source_name: str
    source_hash: str
    taxonomy_version: str
    parser_version: str
    supersedes: str | None
    facts: tuple[Fact, ...]
    groups: tuple[FactGroup, ...]
    problems: tuple[str, ...] = ()


@dataclass
class ViewEntry:
    key: tuple
    filing_id: str
    group: FactGroup
    superseded: tuple[tuple[str, FactGroup], ...] = ()  # (旧 filing id, 旧组)


@dataclass
class View:
    as_of: datetime | None
    entries: dict[tuple, ViewEntry]

    def for_entity(self, entity: str) -> "View":
        kept = {
            k: v
            for k, v in self.entries.items()
            if k[1] is not None and k[1].identifier == entity
        }
        return View(self.as_of, kept)


@dataclass
class Trace:
    """一条事实的完整溯源链。"""

    fact: Fact
    filing: Filing
    group: FactGroup | None
    concept_status: str          # 在 filing 固定的 taxonomy 中的状态
    concept_problem: str | None

    def explain(self) -> str:
        f, p = self.fact, self.fact.provenance
        lines = [
            f"事实 {f.fact_id}  概念 {f.concept}",
            f"  报告: {p.source_name} 行 {p.line} 列 {p.column}"
            f"（sha256 {p.source_hash[:12]}…）",
            f"  原文: {f.value_raw!r} → 规范化值: {f.value}"
            + (f" {f.unit}" if f.unit else ""),
        ]
        if f.context:
            lines.append(
                f"  上下文: 实体 {f.context.entity} 期间 {f.context.period}"
                + (f" 维度 {sorted(map(str, f.context.dimensions))}" if f.context.dimensions else "")
            )
        for step in f.normalization:
            lines.append(f"  规范化: {step}")
        lines.append(
            f"  解析版本: 提取器 {p.parser_version} / taxonomy {self.filing.taxonomy_version}"
            f"（概念状态 {self.concept_status}）"
        )
        lines.append(
            f"  申报: {self.filing.id} 于 {self.filing.filed_at.isoformat()}"
        )
        if self.group:
            lines.append(f"  重复判定: {self.group.classification.value} — {self.group.rationale}")
        if self.concept_problem:
            lines.append(f"  概念问题: {self.concept_problem}")
        return "\n".join(lines)


@dataclass
class DiffEntry:
    key: tuple
    status: str                 # added | removed | changed | unchanged
    old: FactGroup | None
    new: FactGroup | None
    delta: object = None        # 数值差（单位可换算时）
    note: str = ""


@dataclass
class Diff:
    a: str
    b: str
    entries: list[DiffEntry]
    projected_to: str | None = None

    def summary(self) -> dict:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.status] = out.get(e.status, 0) + 1
        return out


class FactStore:
    def __init__(self, taxonomies: TaxonomyRegistry | None = None):
        self.taxonomies = taxonomies or TaxonomyRegistry()
        self._filings: dict[str, Filing] = {}
        self._order: list[str] = []
        self.projections: dict[str, object] = {}  # 重新投影结果，按名保存，互不覆盖

    # ---- 写入 ---------------------------------------------------------

    def add_filing(self, filing: Filing) -> None:
        if filing.id in self._filings:
            raise ValueError(f"filing 已存在: {filing.id}")
        self._filings[filing.id] = filing
        self._order.append(filing.id)

    def get(self, filing_id: str) -> Filing:
        if filing_id not in self._filings:
            raise KeyError(f"未知 filing: {filing_id}")
        return self._filings[filing_id]

    def filings(self, entity: str | None = None) -> list[Filing]:
        out = [self._filings[i] for i in self._order]
        if entity is not None:
            out = [f for f in out if f.entity == entity]
        return sorted(out, key=lambda f: (f.filed_at, self._order.index(f.id)))

    # ---- 视图 ---------------------------------------------------------

    def view(self, entity: str | None = None, as_of: datetime | None = None) -> View:
        """当前视图（或 as_of 时点视图）：每个语义键取申报时间最新的 filing。"""
        entries: dict[tuple, ViewEntry] = {}
        for filing in self.filings(entity):
            if as_of is not None and filing.filed_at > as_of:
                continue
            for group in filing.groups:
                key = group.key
                old = entries.get(key)
                superseded = (old.superseded + ((old.filing_id, old.group),)) if old else ()
                entries[key] = ViewEntry(key, filing.id, group, superseded)
        return View(as_of, entries)

    # ---- 追溯 ---------------------------------------------------------

    def trace(self, fact_id: str) -> Trace:
        for filing in self.filings():
            for fact in filing.facts:
                if fact.fact_id == fact_id:
                    group = next(
                        (g for g in filing.groups if fact in g.members), None
                    )
                    status, problem = self._concept_status(filing, fact.concept)
                    return Trace(fact, filing, group, status, problem)
        raise KeyError(f"未知事实: {fact_id}")

    def _concept_status(self, filing: Filing, concept: QName) -> tuple[str, str | None]:
        try:
            tax = self.taxonomies.get(filing.taxonomy_version)
        except KeyError:
            return "taxonomy-unavailable", f"taxonomy {filing.taxonomy_version} 未登记"
        if concept.ns != tax.namespace:
            return "foreign", f"命名空间不属于该 taxonomy（{concept.ns}）"
        c = tax.concept(concept.local)
        if c is None:
            return "undefined", "概念未在该版本定义"
        return c.status, c.problem

    # ---- 差异 ---------------------------------------------------------

    def diff(
        self,
        a_id: str,
        b_id: str,
        registry: UnitRegistry,
        project_to: str | None = None,
    ) -> Diff:
        """比较两个 filing。``project_to`` 先把双方概念名投影到该 taxonomy 版本。"""
        a, b = self.get(a_id), self.get(b_id)

        def keyed(filing: Filing) -> dict[tuple, FactGroup]:
            out = {}
            for g in filing.groups:
                key = g.key
                if project_to is not None:
                    concept = key[0]
                    if isinstance(concept, QName):
                        tax = self.taxonomies.get(project_to)
                        if concept.ns == tax.namespace:
                            mapping = tax.map_name(concept.local)
                            if mapping.target:
                                concept = QName(concept.ns, mapping.target)
                    key = (concept,) + tuple(key[1:])
                out[key] = g
            return out

        ga, gb = keyed(a), keyed(b)
        entries: list[DiffEntry] = []
        for key in sorted(set(ga) | set(gb), key=repr):
            old, new = ga.get(key), gb.get(key)
            if old is None:
                entries.append(DiffEntry(key, "added", None, new))
                continue
            if new is None:
                entries.append(DiffEntry(key, "removed", old, None))
                continue
            entries.append(self._compare_groups(key, old, new, registry))
        return Diff(a_id, b_id, entries, projected_to=project_to)

    def _compare_groups(
        self, key: tuple, old: FactGroup, new: FactGroup, registry: UnitRegistry
    ) -> DiffEntry:
        fo, fn = old.chosen, new.chosen
        if fo is None or fn is None:
            return DiffEntry(key, "changed", old, new,
                             note="至少一侧存在未决冲突，无法自动比较")
        if fo.value == fn.value and fo.unit == fn.unit:
            return DiffEntry(key, "unchanged", old, new)
        if isinstance(fo.value, str) or isinstance(fn.value, str):
            return DiffEntry(key, "changed", old, new,
                             note=f"文本变化: {fo.value_raw!r} → {fn.value_raw!r}")
        if fo.value is None or fn.value is None:
            return DiffEntry(key, "changed", old, new, note="nil 状态变化")
        try:
            new_in_old = (
                fn.value if fn.unit == fo.unit else registry.convert(fn.value, fn.unit, fo.unit)
            )
            delta = new_in_old - fo.value
            note = ""
            if fn.unit != fo.unit:
                note = f"单位变化 {fn.unit} → {fo.unit}，已按量纲换算后比较"
            status = "unchanged" if delta == 0 else "changed"
            return DiffEntry(key, status, old, new, delta=delta, note=note)
        except (IncompatibleUnits, UnknownMeasure) as exc:
            return DiffEntry(key, "changed", old, new, note=f"单位不可比: {exc}")
