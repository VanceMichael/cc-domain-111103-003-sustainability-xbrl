"""同一概念同一上下文下重复事实的分类与判定。

重复不一定是错误：可能是页面多处重复（格式重复）、精度不同的重报、
可换算单位下的一致重报，或同上下文的真实冲突。本模块对每组事实给出判定，
但永远不丢弃任何成员 —— 全部来源保留在 ``FactGroup.members`` 中。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from app.model import Fact
from app.units import IncompatibleUnits, UnitRegistry, UnknownMeasure


class DupClass(str, Enum):
    SINGLE = "single"                      # 仅一条
    FORMAT_DUPLICATE = "format-duplicate"  # 值、单位、精度完全一致的多处出现
    PRECISION_VARIANT = "precision-variant"  # 同值同单位但精度不同
    UNIT_VARIANT = "unit-variant"          # 单位不同但换算后一致
    CONFLICT = "conflict"                  # 同上下文值冲突，需人工复核


@dataclass
class FactGroup:
    key: tuple
    members: list[Fact]
    classification: DupClass
    chosen: Fact | None
    rationale: str

    @property
    def needs_review(self) -> bool:
        return self.classification == DupClass.CONFLICT


def group_key(fact: Fact) -> tuple:
    """语义分组键：概念 + 实体 + 期间 + 维度（不含单位与文档内 id）。"""
    ctx = fact.context
    if ctx is None:
        return (fact.concept, None, None, None)
    return (fact.concept, ctx.entity, ctx.period, ctx.dimensions)


def precision_rank(fact: Fact) -> float:
    """隐含精度：decimals 直接取值（INF 视为正无穷）；precision 换算；缺失最低。"""
    if fact.decimals is not None:
        if fact.decimals == "INF":
            return float("inf")
        try:
            return float(fact.decimals)
        except ValueError:
            pass
    if fact.precision is not None and isinstance(fact.value, Decimal):
        try:
            return float(fact.precision) - 1 - fact.value.adjusted()
        except (ValueError, ArithmeticError):
            pass
    return float("-inf")


def _position(fact: Fact) -> str:
    p = fact.provenance
    if p is None:
        return "?"
    tag = "（隐藏区）" if fact.hidden else ""
    return f"{p.source_name}:{p.line}{tag}"


def classify_group(key: tuple, members: list[Fact], registry: UnitRegistry) -> FactGroup:
    members = sorted(members, key=lambda f: (f.provenance.line if f.provenance else 0,
                                             f.provenance.column if f.provenance else 0))
    if len(members) == 1:
        return FactGroup(key, members, DupClass.SINGLE, members[0], "仅一条事实")

    where = "、".join(_position(m) for m in members)

    # 文本型事实：只能按原文比较
    if any(isinstance(m.value, str) for m in members):
        texts = {m.value for m in members}
        if len(texts) == 1:
            return FactGroup(key, members, DupClass.FORMAT_DUPLICATE, members[0],
                             f"文本完全一致的多处出现（{where}），全部保留")
        return FactGroup(key, members, DupClass.CONFLICT, None,
                         f"同上下文文本不一致（{where}），需人工复核")

    # nil 与数值混杂
    if any(m.value is None for m in members):
        if all(m.value is None for m in members):
            return FactGroup(key, members, DupClass.FORMAT_DUPLICATE, members[0],
                             f"均为 nil 的重复（{where}）")
        return FactGroup(key, members, DupClass.CONFLICT, None,
                         f"nil 与数值混杂（{where}），需人工复核")

    same_unit = len({m.unit for m in members}) == 1
    same_precision = len({(m.decimals, m.precision) for m in members}) == 1
    same_value = len({m.value for m in members}) == 1

    if same_unit and same_value and same_precision:
        return FactGroup(key, members, DupClass.FORMAT_DUPLICATE, members[0],
                         f"值/单位/精度完全一致的多处出现（{where}），全部保留")

    # 尝试统一到首个成员的单位后比较
    base_unit = members[0].unit
    converted: list[Decimal] = []
    try:
        for m in members:
            converted.append(
                m.value if m.unit == base_unit else registry.convert(m.value, m.unit, base_unit)
            )
    except (IncompatibleUnits, UnknownMeasure) as exc:
        return FactGroup(key, members, DupClass.CONFLICT, None,
                         f"单位无法统一（{exc}；{where}），需人工复核")

    if len(set(converted)) > 1:
        detail = "、".join(f"{m.value_raw}{m.unit or ''}@{_position(m)}" for m in members)
        return FactGroup(key, members, DupClass.CONFLICT, None,
                         f"同上下文值冲突（{detail}），不自动取舍，需人工复核")

    if not same_unit:
        vector = registry.unit_vector(base_unit)[0]
        canonical = registry.canonical_measure(vector)
        chosen = next(
            (m for m in members
             if m.unit and canonical in (m.unit.measures or ())),
            max(members, key=precision_rank),
        )
        factors = "、".join(
            f"{m.value} {m.unit}" for m in members
        )
        return FactGroup(key, members, DupClass.UNIT_VARIANT, chosen,
                         f"单位不同但换算后一致（{factors}），全部保留")

    chosen = max(members, key=precision_rank)
    ranks = "、".join(f"{m.value_raw}(decimals={m.decimals})" for m in members)
    return FactGroup(key, members, DupClass.PRECISION_VARIANT, chosen,
                     f"同值不同精度（{ranks}），取精度最高者，全部保留")


def classify_facts(facts: list[Fact], registry: UnitRegistry) -> list[FactGroup]:
    groups: dict[tuple, list[Fact]] = {}
    for fact in facts:
        groups.setdefault(group_key(fact), []).append(fact)
    return [classify_group(key, members, registry) for key, members in groups.items()]
