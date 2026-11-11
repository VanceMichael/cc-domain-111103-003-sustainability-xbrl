"""Inline XBRL / 普通 XBRL instance 的事实提取。

只用标准库 SAX 解析，逐元素记录行/列位置，事实永远携带原文位置与原始写法。
解析中遇到的问题（未知前缀、缺失 contextRef、未知 format 等）只做局部记录，
写入 ``ExtractedDoc.problems`` 或事实的 ``normalization`` 备注，不中断整体提取。

已支持的 ixt 数值格式：numdotdecimal（默认）、numcommadecimal、numdash、zerodash。
暂不支持：ix:tuple、ix:fraction、ix:continuation 链、typedMember —— 遇到时记录问题并跳过。
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from xml.sax import make_parser
from xml.sax.handler import ContentHandler, feature_namespaces

from app.model import (
    Context,
    Dimension,
    Entity,
    Fact,
    Period,
    Provenance,
    QName,
    Unit,
    UnknownPrefix,
    parse_qname,
)

XBRLI = "http://www.xbrl.org/2003/instance"
IX = "http://www.xbrl.org/2013/inlineXBRL"
XBRLDI = "http://www.xbrl.org/2006/xbrldi"

PARSER_VERSION = "ix-extract/1.0"

_WS = re.compile(r"\s+")
_DASHES = {"-", "–", "—"}
# 普通空格、不换行空格、窄不换行空格（千分位常见写法）
_SPACES = {" ", " ", " "}


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _strip_spaces(s: str) -> str:
    for ch in _SPACES:
        s = s.replace(ch, "")
    return s


def _numdotdecimal(s: str) -> tuple[str, bool]:
    """``1,234.5`` / ``(1,234.5)`` / ``-`` → 规范十进制串与括号负号。"""
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1].strip()
    s = _strip_spaces(s).replace(",", "")
    if s in _DASHES:
        s = "0"
    return s, neg


def _numcommadecimal(s: str) -> tuple[str, bool]:
    """``1.234,5`` 欧式记法。"""
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1].strip()
    s = _strip_spaces(s).replace(".", "").replace(",", ".")
    if s in _DASHES:
        s = "0"
    return s, neg


def _numdash(s: str) -> tuple[str, bool]:
    return ("0", False) if s in _DASHES else _numdotdecimal(s)


_TRANSFORMS = {
    "numdotdecimal": _numdotdecimal,
    "numcommadecimal": _numcommadecimal,
    "numspacedot": _numdotdecimal,
    "numdash": _numdash,
    "zerodash": _numdash,
}


def normalize_number(
    raw: str, fmt: str | None, sign: str | None, scale: int
) -> tuple[Decimal | None, list[str], str | None]:
    """把 Inline XBRL 数值原文规范化为 Decimal。

    返回 (值, 步骤说明, 问题)。任一步失败都不会抛出，问题写入返回值。
    """
    steps: list[str] = []
    text = _collapse(raw)
    fmt_local = fmt.split(":", 1)[-1] if fmt else None
    transform = _TRANSFORMS.get(fmt_local or "numdotdecimal")
    if fmt and fmt_local not in _TRANSFORMS:
        steps.append(f"未知 format {fmt!r}，按 numdotdecimal 处理")
    body, paren_neg = transform(text)
    if fmt_local or paren_neg:
        steps.append(f"format={fmt_local or 'numdotdecimal'}: {text!r} → {body!r}")
    try:
        value = Decimal(body)
    except InvalidOperation:
        return None, steps, f"数值无法解析: {raw!r}"
    if paren_neg:
        value = -value
        steps.append("括号负号 → 取负")
    if sign == "-":
        value = -value
        steps.append("sign='-' → 取负")
    if scale:
        value = value * (Decimal(10) ** scale)
        steps.append(f"scale={scale} → ×10^{scale}")
    return value, steps, None


@dataclass
class ExtractedDoc:
    source_name: str
    source_hash: str
    contexts: dict[str, Context] = field(default_factory=dict)
    units: dict[str, Unit] = field(default_factory=dict)
    facts: list[Fact] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _attr(attrs, local: str, uri: str | None = None) -> str | None:
    return attrs.get((uri, local))


class _Handler(ContentHandler):
    def __init__(self, doc: ExtractedDoc):
        super().__init__()
        self.doc = doc
        self._locator = None
        self._nsmaps: list[dict[str, str]] = [{}]
        self._captures: list[dict] = []      # 打开中的文本捕获（含事实）
        self._ctx: dict | None = None
        self._unit: dict | None = None
        self._section: str | None = None     # segment / scenario
        self._hidden = 0
        self._fact_builders: list[dict] = []

    # ---- 基础事件 -----------------------------------------------------

    def setDocumentLocator(self, locator):
        self._locator = locator

    def startPrefixMapping(self, prefix, uri):
        self._nsmaps.append(dict(self._nsmaps[-1]))
        self._nsmaps[-1][prefix or ""] = uri

    def endPrefixMapping(self, prefix):
        self._nsmaps.pop()

    def characters(self, content):
        for cap in self._captures:
            cap["buf"].append(content)

    def _pos(self) -> tuple[int, int]:
        if self._locator is None:
            return 0, 0
        return self._locator.getLineNumber(), self._locator.getColumnNumber()

    def _qname(self, text: str) -> QName:
        try:
            return parse_qname(text, self._nsmaps[-1])
        except UnknownPrefix as exc:
            self.doc.problems.append(f"{exc}（行 {self._pos()[0]}）")
            return QName(None, text.strip())

    # ---- 元素开始 -----------------------------------------------------

    def startElementNS(self, name, qname, attrs):
        uri, local = name
        line, col = self._pos()

        if (uri, local) == (IX, "hidden"):
            self._hidden += 1
            return
        if (uri, local) == (XBRLI, "context"):
            self._ctx = {"id": _attr(attrs, "id"), "dims": []}
            return
        if (uri, local) == (XBRLI, "unit"):
            self._unit = {"id": _attr(attrs, "id"), "measures": [], "divide": []}
            return
        if self._ctx is not None and (uri, local) in (
            (XBRLI, "segment"),
            (XBRLI, "scenario"),
        ):
            self._section = local
            return
        if self._ctx is not None and (uri, local) in (
            (XBRLI, "identifier"),
            (XBRLI, "instant"),
            (XBRLI, "startDate"),
            (XBRLI, "endDate"),
        ):
            self._captures.append({"kind": local, "name": name, "buf": [], "attrs": attrs})
            return
        if self._ctx is not None and (uri, local) == (XBRLDI, "explicitMember"):
            self._captures.append(
                {"kind": "explicitMember", "name": name, "buf": [], "attrs": attrs}
            )
            return
        if self._ctx is not None and (uri, local) == (XBRLDI, "typedMember"):
            self.doc.problems.append(f"行 {line}: typedMember 维度暂不支持，已跳过")
            return
        if self._unit is not None and (uri, local) in (
            (XBRLI, "measure"),
            (XBRLI, "numeratorMeasures"),
            (XBRLI, "denominatorMeasures"),
        ):
            self._captures.append({"kind": local, "name": name, "buf": [], "attrs": attrs})
            return

        if uri == IX and local in ("nonFraction", "nonNumeric"):
            name_attr = _attr(attrs, "name") or ""
            builder = {
                "kind": local,
                "concept": self._qname(name_attr) if name_attr else QName(None, "?"),
                "contextRef": _attr(attrs, "contextRef"),
                "unitRef": _attr(attrs, "unitRef"),
                "decimals": _attr(attrs, "decimals"),
                "precision": _attr(attrs, "precision"),
                "scale": _attr(attrs, "scale"),
                "sign": _attr(attrs, "sign"),
                "fmt": _attr(attrs, "format"),
                "nil": (_attr(attrs, "nil") or "").lower() == "true",
                "hidden": self._hidden > 0,
                "line": line,
                "col": col,
            }
            self._fact_builders.append(builder)
            self._captures.append({"kind": "fact", "name": name, "buf": [], "builder": builder})
            return
        if uri == IX and local in ("tuple", "fraction", "continuation"):
            self.doc.problems.append(f"行 {line}: ix:{local} 暂不支持，已跳过")
            return
        if (
            uri not in (XBRLI, IX, XBRLDI)
            and _attr(attrs, "contextRef") is not None
            and self._ctx is None
            and self._unit is None
        ):
            # 普通 XBRL instance 的事实节点
            builder = {
                "kind": "item",
                "concept": QName(uri, local),
                "contextRef": _attr(attrs, "contextRef"),
                "unitRef": _attr(attrs, "unitRef"),
                "decimals": _attr(attrs, "decimals"),
                "precision": _attr(attrs, "precision"),
                "scale": None,
                "sign": None,
                "fmt": None,
                "nil": (_attr(attrs, "nil") or "").lower() == "true",
                "hidden": False,
                "line": line,
                "col": col,
            }
            self._fact_builders.append(builder)
            self._captures.append({"kind": "fact", "name": name, "buf": [], "builder": builder})
            return

    # ---- 元素结束 -----------------------------------------------------

    def endElementNS(self, name, qname):
        uri, local = name

        if (uri, local) == (IX, "hidden"):
            self._hidden -= 1
            return
        if (uri, local) in ((XBRLI, "segment"), (XBRLI, "scenario")):
            self._section = None
            return
        if (uri, local) == (XBRLI, "context") and self._ctx is not None:
            self._finish_context()
            return
        if (uri, local) == (XBRLI, "unit") and self._unit is not None:
            self._finish_unit()
            return
        if self._captures and self._captures[-1]["name"] == name:
            cap = self._captures.pop()
            self._dispatch_capture(cap)

    def _dispatch_capture(self, cap: dict):
        kind = cap["kind"]
        text = _collapse("".join(cap["buf"]))
        if kind == "fact":
            cap["builder"]["raw"] = "".join(cap["buf"])
            return
        if self._ctx is not None:
            if kind == "identifier":
                self._ctx["identifier"] = text
                self._ctx["scheme"] = _attr(cap["attrs"], "scheme")
            elif kind in ("instant", "startDate", "endDate"):
                self._ctx[kind] = text
            elif kind == "explicitMember":
                # dimension 属性在文档中通常不带前缀（无命名空间属性）
                dim_attr = _attr(cap["attrs"], "dimension") or ""
                self._ctx["dims"].append(
                    Dimension(
                        axis=self._qname(dim_attr),
                        member=self._qname(text),
                        origin=self._section or "scenario",
                    )
                )
            return
        if self._unit is not None:
            measures = [self._qname(t) for t in text.split() if t]
            if kind in ("measure", "numeratorMeasures"):
                self._unit["measures"].extend(measures)
            elif kind == "denominatorMeasures":
                self._unit["divide"].extend(measures)

    def _finish_context(self):
        c = self._ctx
        self._ctx = None
        try:
            if "instant" in c:
                period = Period(kind="instant", instant=date.fromisoformat(c["instant"]))
            else:
                period = Period(
                    kind="duration",
                    start=date.fromisoformat(c["startDate"]),
                    end=date.fromisoformat(c["endDate"]),
                )
        except (KeyError, ValueError) as exc:
            self.doc.problems.append(f"context {c.get('id')}: 期间解析失败 {exc}")
            return
        entity = Entity(scheme=c.get("scheme"), identifier=c.get("identifier", ""))
        ctx = Context(
            id=c.get("id") or "",
            entity=entity,
            period=period,
            dimensions=frozenset(c["dims"]),
        )
        self.doc.contexts[ctx.id] = ctx

    def _finish_unit(self):
        u = self._unit
        self._unit = None
        unit = Unit(
            id=u.get("id") or "",
            measures=tuple(u["measures"]),
            divide=tuple(u["divide"]),
        )
        self.doc.units[unit.id] = unit


def extract(raw: bytes, source_name: str = "<memory>") -> ExtractedDoc:
    """解析 Inline XBRL 或普通 instance，返回带溯源的事实集合。"""
    doc = ExtractedDoc(
        source_name=source_name,
        source_hash=hashlib.sha256(raw).hexdigest(),
    )
    handler = _Handler(doc)
    parser = make_parser()
    parser.setFeature(feature_namespaces, True)
    parser.setContentHandler(handler)
    parser.parse(io.BytesIO(raw))
    _finalize_facts(doc, handler._fact_builders)
    return doc


def _finalize_facts(doc: ExtractedDoc, builders: list[dict]):
    for b in builders:
        raw_text = b.get("raw", "")
        text = _collapse(raw_text)
        notes: list[str] = []
        problems: list[str] = []

        scale = 0
        if b["scale"] not in (None, ""):
            try:
                scale = int(b["scale"])
            except ValueError:
                problems.append(f"scale 无法解析: {b['scale']!r}")

        context = doc.contexts.get(b["contextRef"] or "")
        if b["contextRef"] and context is None:
            problems.append(f"contextRef 未定义: {b['contextRef']}")
        unit = doc.units.get(b["unitRef"] or "")
        if b["unitRef"] and unit is None:
            problems.append(f"unitRef 未定义: {b['unitRef']}")

        value: Decimal | str | None
        if b["nil"]:
            value = None
            notes.append("nil=true")
        elif b["kind"] == "nonNumeric":
            value = text
        else:
            value, steps, problem = normalize_number(text, b["fmt"], b["sign"], scale)
            notes.extend(steps)
            if problem:
                problems.append(problem)

        for p in problems:
            doc.problems.append(f"{b['concept']} 行 {b['line']}: {p}")
        notes.extend(f"问题: {p}" for p in problems)

        doc.facts.append(
            Fact(
                concept=b["concept"],
                context=context,
                unit=unit,
                kind=b["kind"],
                value_raw=text,
                value=value,
                decimals=b["decimals"],
                precision=b["precision"],
                scale=scale,
                sign=b["sign"],
                fmt=b["fmt"],
                nil=b["nil"],
                hidden=b["hidden"],
                provenance=Provenance(
                    source_hash=doc.source_hash,
                    source_name=doc.source_name,
                    line=b["line"],
                    column=b["col"],
                    parser_version=PARSER_VERSION,
                ),
                normalization=tuple(notes),
            )
        )
