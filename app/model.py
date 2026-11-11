"""事实与上下文的数据模型。

设计要点：

- 概念身份是 QName（命名空间 + 本地名）。同名标签在不同命名空间下不是同一概念，
  模型层全程保留命名空间，绝不只按标签文本比较。
- Context / Unit 的 ``id`` 字段不参与相等性比较（``compare=False``）：
  同一报告里 ``c1`` 与 ``ctx-1`` 只要实体、期间、维度结构相同，语义上就是同一上下文。
- 所有结构不可变（frozen），事实一旦提取即定型；重述、投影都以新对象表达，
  旧结果不会被改写。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


class UnknownPrefix(Exception):
    """QName 前缀在文档中未声明。"""


@dataclass(frozen=True)
class QName:
    """展开后的概念/度量名。``ns`` 为 None 表示无命名空间（保留原文写法）。"""

    ns: str | None
    local: str

    def __str__(self) -> str:
        return f"{{{self.ns}}}{self.local}" if self.ns else self.local


def parse_qname(text: str, nsmap: dict[str, str]) -> QName:
    """按文档前缀表解析 ``prefix:local``；无前缀时返回无命名空间 QName。"""
    text = text.strip()
    if ":" in text:
        prefix, local = text.split(":", 1)
        if prefix not in nsmap:
            raise UnknownPrefix(f"未声明的前缀: {prefix!r}（{text!r}）")
        return QName(nsmap[prefix], local)
    return QName(None, text)


@dataclass(frozen=True)
class Period:
    """报告期间。``kind`` 为 ``instant`` 或 ``duration``。"""

    kind: str
    instant: date | None = None
    start: date | None = None
    end: date | None = None

    def __str__(self) -> str:
        if self.kind == "instant":
            return f"instant:{self.instant}"
        return f"duration:{self.start}..{self.end}"


@dataclass(frozen=True)
class Entity:
    scheme: str | None
    identifier: str

    def __str__(self) -> str:
        return f"{self.scheme}:{self.identifier}" if self.scheme else self.identifier


@dataclass(frozen=True)
class Dimension:
    """显式维度成员。``origin`` 记录来自 segment 还是 scenario。"""

    axis: QName
    member: QName
    origin: str = "scenario"

    def __str__(self) -> str:
        return f"{self.axis}={self.member}({self.origin})"


@dataclass(frozen=True)
class Context:
    """xbrli:context。``id`` 只是文档内引用符，不参与语义比较。"""

    id: str = field(compare=False)
    entity: Entity
    period: Period
    dimensions: frozenset[Dimension] = frozenset()


@dataclass(frozen=True)
class Unit:
    """xbrli:unit。``measures`` 为分子度量，``divide`` 为分母度量（如强度指标）。"""

    id: str = field(compare=False)
    measures: tuple[QName, ...] = ()
    divide: tuple[QName, ...] = ()

    def __str__(self) -> str:
        num = "*".join(str(m) for m in self.measures) or "1"
        if self.divide:
            den = "*".join(str(m) for m in self.divide)
            return f"{num}/{den}"
        return num


@dataclass(frozen=True)
class Provenance:
    """事实的来源位置与解析版本。溯源查询以此回到报告原文。"""

    source_hash: str        # 文档内容 sha256
    source_name: str        # 来源标识（路径或名称）
    line: int               # 原文行号（1 起）
    column: int             # 原文列号
    parser_version: str     # 提取器版本，重跑可复现


@dataclass
class Fact:
    """一条 XBRL 事实及其全部溯源信息。

    ``value`` 为规范化结果：数值型是 Decimal（已应用 format/scale/sign），
    文本型是 str，nil 事实为 None。``value_raw`` 永远保留原文写法。
    """

    concept: QName
    context: Context | None
    unit: Unit | None
    kind: str                              # nonFraction | nonNumeric | item
    value_raw: str
    value: Decimal | str | None
    decimals: str | None = None
    precision: str | None = None
    scale: int = 0
    sign: str | None = None
    fmt: str | None = None
    nil: bool = False
    hidden: bool = False
    provenance: Provenance | None = None
    normalization: tuple[str, ...] = ()    # 规范化步骤说明，供追溯解释

    @property
    def fact_id(self) -> str:
        """稳定标识：同一文档同一位置重复解析结果一致。"""
        p = self.provenance
        ctx = self.context.id if self.context else "-"
        unit = self.unit.id if self.unit else "-"
        basis = "|".join(
            [
                p.source_hash if p else "-",
                str(self.concept),
                ctx,
                unit,
                str(p.line if p else 0),
                str(p.column if p else 0),
                self.value_raw,
            ]
        )
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
