"""面向研究员的查询：跨企业可比值与按新 taxonomy 的重新投影。

- ``compare_concept``：把不同企业同一概念的事实换算到目标单位后并列，
  每行都带换算因子、taxonomy 版本与重复判定，比较结果可解释；
  量纲不兼容只在该行标记错误，不中断整体查询。
- ``reproject_view``：把某时点的视图按目标 taxonomy 版本的概念映射重新投影，
  结果作为新的命名投影存入 ``store.projections``，原始视图与事实保持不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.dedupe import FactGroup
from app.model import QName, Unit
from app.store import FactStore
from app.taxonomy import TaxonomyRegistry
from app.units import IncompatibleUnits, UnitRegistry, UnknownMeasure


@dataclass
class CompareRow:
    entity: str
    concept: str
    period: str
    dimensions: tuple[str, ...]
    filing_id: str
    taxonomy_version: str
    value: object
    unit: str
    converted: object = None      # 换算到目标单位后的值
    factor: object = None
    judgment: str = ""
    error: str | None = None


def compare_concept(
    store: FactStore,
    registry: UnitRegistry,
    concept_local: str,
    entities: list[str],
    target: Unit | None = None,
    as_of: datetime | None = None,
    dimensions: frozenset | None = None,
) -> list[CompareRow]:
    """跨企业比较同一概念。``dimensions`` 可限定维度组合（如只比 Scope1）。"""
    rows: list[CompareRow] = []
    for entity in entities:
        view = store.view(entity=entity, as_of=as_of)
        for entry in view.entries.values():
            concept = entry.key[0]
            if not isinstance(concept, QName) or concept.local != concept_local:
                continue
            if dimensions is not None and entry.key[3] != dimensions:
                continue
            group = entry.group
            fact = group.chosen
            filing = store.get(entry.filing_id)
            dims = tuple(sorted(map(str, entry.key[3] or ())))
            row = CompareRow(
                entity=entity,
                concept=str(concept),
                period=str(entry.key[2]),
                dimensions=dims,
                filing_id=entry.filing_id,
                taxonomy_version=filing.taxonomy_version,
                value=fact.value if fact else None,
                unit=str(fact.unit) if fact and fact.unit else "",
                judgment=group.classification.value,
            )
            if fact is None:
                row.error = "该组存在未决冲突，无代表值"
            elif target is not None and fact.unit is not None:
                try:
                    row.factor = registry.conversion_factor(fact.unit, target)
                    row.converted = fact.value * row.factor
                except (IncompatibleUnits, UnknownMeasure) as exc:
                    row.error = str(exc)
            rows.append(row)
    return rows


@dataclass
class ProjectedEntry:
    """视图条目在新 taxonomy 下的投影。原条目始终可从 ``group`` 回到原事实。"""

    original_key: tuple
    concept_from: str
    concept_to: str | None
    status: str                   # unchanged | ok | unmapped | broken | foreign
    reason: str | None
    filing_id: str
    group: FactGroup

    @property
    def projected_key(self) -> tuple:
        concept = self.original_key[0]
        if self.concept_to and isinstance(concept, QName):
            concept = QName(concept.ns, self.concept_to)
        return (concept,) + tuple(self.original_key[1:])


@dataclass
class Projection:
    """一次重新投影的结果。按名存储，永不覆盖既有视图或投影。"""

    name: str
    target_version: str
    as_of: datetime | None
    entity: str | None
    entries: list[ProjectedEntry] = field(default_factory=list)

    def current(self) -> dict[tuple, ProjectedEntry]:
        """投影后的当前视图：同名新概念按键合并，后申报的 filing 优先。"""
        out: dict[tuple, ProjectedEntry] = {}
        for entry in self.entries:
            out[entry.projected_key] = entry
        return out


def reproject_view(
    store: FactStore,
    taxonomies: TaxonomyRegistry,
    target_version: str,
    name: str,
    entity: str | None = None,
    as_of: datetime | None = None,
) -> Projection:
    """把视图按目标版本的概念映射重新投影，结果以 ``name`` 存入 store.projections。"""
    if name in store.projections:
        raise ValueError(f"投影名已存在: {name}（投影只增不改）")
    tax = taxonomies.get(target_version)
    view = store.view(entity=entity, as_of=as_of)
    projection = Projection(name, target_version, as_of, entity)
    for key, entry in view.entries.items():
        concept = key[0]
        if not isinstance(concept, QName) or concept.ns != tax.namespace:
            projection.entries.append(
                ProjectedEntry(key, str(concept), None, "foreign",
                               "命名空间不属于目标 taxonomy", entry.filing_id, entry.group)
            )
            continue
        mapping = tax.map_name(concept.local)
        projection.entries.append(
            ProjectedEntry(
                key,
                concept.local,
                mapping.target,
                mapping.status,
                mapping.reason,
                entry.filing_id,
                entry.group,
            )
        )
    store.projections[name] = projection
    return projection
