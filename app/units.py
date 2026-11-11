"""单位注册与量纲兼容检查。

每个度量（measure）声明一个量纲向量（如 ``mass``、``mass_co2e``、``energy``）
和到该量纲基准单位的换算因子。换算前必须验证两侧量纲向量完全一致，
否则抛出 :class:`IncompatibleUnits` —— 千克不能换算成人次，tCO2e 也不能换算成吨普通废物。

复合单位（``kgCO2e/MWh`` 等 divide 结构）按分子分母向量加减合成。
注册表可由 taxonomy 文件的 ``units`` 节扩展，随 taxonomy 版本一起固定到 filing。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.model import QName, Unit

Vector = tuple[tuple[str, int], ...]

XBRLI = "http://www.xbrl.org/2003/instance"


class IncompatibleUnits(Exception):
    """两侧单位量纲不一致，禁止换算。"""


class UnknownMeasure(Exception):
    """度量未在注册表声明。"""


def _vector(items) -> Vector:
    if hasattr(items, "items"):
        items = items.items()
    return tuple(sorted((k, int(v)) for k, v in items if v))


def _vec_add(a: Vector, b: Vector, sign: int) -> Vector:
    merged: dict[str, int] = dict(a)
    for k, v in b:
        merged[k] = merged.get(k, 0) + sign * v
        if merged[k] == 0:
            del merged[k]
    return _vector(merged.items())


def vector_label(vector: Vector) -> str:
    """量纲向量的可读标签，如 ``mass_co2e_per_energy``。"""
    pos = sorted(k for k, v in vector if v > 0)
    neg = sorted(k for k, v in vector if v < 0)
    label = "_".join(pos) or "pure"
    if neg:
        label += "_per_" + "_".join(neg)
    return label


@dataclass(frozen=True)
class Measure:
    qname: QName
    vector: Vector
    to_base: Decimal  # 到该量纲基准单位的乘数


class UnitRegistry:
    def __init__(self):
        self._measures: dict[QName, Measure] = {}
        self._canonical: dict[Vector, QName] = {}

    def declare(self, qname: QName, vector, to_base) -> None:
        measure = Measure(qname=qname, vector=_vector(vector), to_base=Decimal(str(to_base)))
        self._measures[qname] = measure
        self._canonical.setdefault(measure.vector, qname)

    def merge(self, other: "UnitRegistry") -> None:
        for m in other._measures.values():
            self.declare(m.qname, m.vector, m.to_base)

    def load_taxonomy_units(self, namespace: str | None, units: dict) -> None:
        """并入 taxonomy 文件中声明的度量。"""
        for local, spec in (units or {}).items():
            qn = QName(namespace, local)
            self.declare(qn, spec.get("dimension", {}), spec.get("to_base", "1"))

    def resolve(self, qname: QName) -> Measure:
        if qname in self._measures:
            return self._measures[qname]
        if qname.ns is None:
            # 兼容未加前缀的旧式写法：按本地名唯一匹配
            candidates = [m for q, m in self._measures.items() if q.local == qname.local]
            if len(candidates) == 1:
                return candidates[0]
            if not candidates:
                raise UnknownMeasure(f"未注册的度量: {qname}")
            raise UnknownMeasure(f"度量 {qname} 在多个命名空间下均有声明，需加前缀")
        raise UnknownMeasure(f"未注册的度量: {qname}")

    def unit_vector(self, unit: Unit) -> tuple[Vector, Decimal]:
        """合成单位的量纲向量与到基准单位的总因子。"""
        vector: Vector = ()
        factor = Decimal(1)
        for qn in unit.measures:
            m = self.resolve(qn)
            vector = _vec_add(vector, m.vector, +1)
            factor *= m.to_base
        for qn in unit.divide:
            m = self.resolve(qn)
            vector = _vec_add(vector, m.vector, -1)
            factor /= m.to_base
        return vector, factor

    def compatible(self, a: Unit, b: Unit) -> bool:
        try:
            return self.unit_vector(a)[0] == self.unit_vector(b)[0]
        except UnknownMeasure:
            return False

    def conversion_factor(self, from_unit: Unit, to_unit: Unit) -> Decimal:
        """from × factor = to。量纲不同即抛 :class:`IncompatibleUnits`。"""
        va, fa = self.unit_vector(from_unit)
        vb, fb = self.unit_vector(to_unit)
        if va != vb:
            raise IncompatibleUnits(
                f"量纲不兼容: {from_unit}（{vector_label(va)}）vs "
                f"{to_unit}（{vector_label(vb)}）"
            )
        return fa / fb

    def convert(self, value: Decimal, from_unit: Unit, to_unit: Unit) -> Decimal:
        return value * self.conversion_factor(from_unit, to_unit)

    def canonical_measure(self, vector: Vector) -> QName | None:
        return self._canonical.get(vector)


_UNITS_NS = "https://example.invalid/units"
_ISO4217 = "http://www.xbrl.org/2003/iso4217"


def default_registry() -> UnitRegistry:
    """合成披露场景的内置度量表。基准单位：kg、kgCO2e、J、count、m2、m3、CNY、pure。"""
    reg = UnitRegistry()
    u = lambda local: QName(_UNITS_NS, local)
    reg.declare(u("g"), {"mass": 1}, "0.001")
    reg.declare(u("kg"), {"mass": 1}, "1")
    reg.declare(u("tonne"), {"mass": 1}, "1000")
    reg.declare(u("kgCO2e"), {"mass_co2e": 1}, "1")
    reg.declare(u("tCO2e"), {"mass_co2e": 1}, "1000")
    reg.declare(u("count"), {"count": 1}, "1")
    reg.declare(u("J"), {"energy": 1}, "1")
    reg.declare(u("Wh"), {"energy": 1}, "3600")
    reg.declare(u("kWh"), {"energy": 1}, "3600000")
    reg.declare(u("MWh"), {"energy": 1}, "3600000000")
    reg.declare(u("GJ"), {"energy": 1}, "1000000000")
    reg.declare(u("m2"), {"area": 1}, "1")
    reg.declare(u("L"), {"volume": 1}, "0.001")
    reg.declare(u("m3"), {"volume": 1}, "1")
    reg.declare(u("h"), {"time": 1}, "1")
    reg.declare(QName(XBRLI, "pure"), {}, "1")
    reg.declare(QName(_ISO4217, "CNY"), {"monetary_CNY": 1}, "1")
    return reg
