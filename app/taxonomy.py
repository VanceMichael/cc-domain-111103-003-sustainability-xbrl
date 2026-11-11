"""可版本化 taxonomy 解析。

- taxonomy 文档为 JSON：``version`` / ``namespace`` / ``issued`` / ``extends`` /
  ``concepts`` / ``aliases`` / ``units``。兼容旧的简写形式（概念值直接写量纲字符串）。
- extension 通过 ``extends`` 引用基础 taxonomy，概念可用 ``ref`` 引用同链上的基础概念。
- 循环引用（extends 环、概念 ref 环、别名环）与缺失定义都只局部标记：
  受影响的概念/别名记入 ``status`` 与 ``problems``，其余概念正常可用。
- 概念映射（旧名 → 新名）由目标版本的 ``aliases`` 与 ``replaces`` 共同推出，
  用于把旧 filing 重新投影到新 taxonomy，而不改写已存事实。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path


@dataclass
class ConceptDef:
    name: str
    dimension: str | None = None      # 量纲标签（与单位注册表交叉核对用）
    datatype: str | None = None
    period_type: str | None = None
    ref: str | None = None            # 引用同链上的基础概念
    replaces: str | None = None       # 本概念替代的旧概念名
    deprecated: bool = False
    source: str = ""                  # 声明它的 taxonomy 版本
    status: str = "ok"                # ok | cycle | missing
    problem: str | None = None


@dataclass
class Mapping:
    """单个概念名到目标版本的映射结果。"""

    source: str
    target: str | None
    status: str                       # unchanged | ok | unmapped | broken
    chain: tuple[str, ...] = ()
    reason: str | None = None


@dataclass
class Taxonomy:
    version: str
    namespace: str | None
    issued: date | None
    concepts: dict[str, ConceptDef]
    aliases: dict[str, str]           # 旧本地名 → 新本地名（含 replaces 推出的）
    units: dict
    unit_namespace: str | None
    problems: list[str]
    lineage: list[str]                # 解析后的 extends 链（父在前）

    def concept(self, name: str) -> ConceptDef | None:
        return self.concepts.get(name)

    def map_name(self, name: str) -> Mapping:
        """把本地名映射到本版本。未定义且无别名 → unmapped；断链 → broken。"""
        if name in self.concepts and name not in self.aliases:
            return Mapping(source=name, target=name, status="unchanged")
        chain: list[str] = []
        seen = {name}
        current = name
        while current in self.aliases:
            nxt = self.aliases[current]
            chain.append(nxt)
            if nxt in seen:
                return Mapping(name, None, "broken", tuple(chain), f"别名环: {' → '.join(chain)}")
            if nxt in self.concepts:
                return Mapping(name, nxt, "ok", tuple(chain))
            seen.add(nxt)
            current = nxt
        if not chain:
            return Mapping(name, None, "unmapped", reason="目标版本未定义该概念且无别名")
        return Mapping(name, None, "broken", tuple(chain), f"映射目标未定义: {current}")


def _parse_concept(name: str, spec, source: str) -> ConceptDef:
    if isinstance(spec, str):  # 旧简写：{"Waste": "mass"}
        spec = {"dimension": spec}
    spec = spec or {}
    return ConceptDef(
        name=name,
        dimension=spec.get("dimension"),
        datatype=spec.get("datatype"),
        period_type=spec.get("period_type"),
        ref=spec.get("ref"),
        replaces=spec.get("replaces"),
        deprecated=bool(spec.get("deprecated", False)),
        source=source,
    )


class TaxonomyRegistry:
    """按版本登记 taxonomy 文档并解析 extension 链。"""

    def __init__(self):
        self._docs: dict[str, dict] = {}
        self._resolved: dict[str, Taxonomy] = {}

    def add_doc(self, doc: dict) -> None:
        version = doc.get("version")
        if not version:
            raise ValueError("taxonomy 文档缺少 version")
        self._docs[version] = doc
        self._resolved.pop(version, None)

    def load(self, path: str | Path) -> None:
        self.add_doc(json.loads(Path(path).read_text(encoding="utf-8")))

    def load_dir(self, directory: str | Path) -> None:
        for path in sorted(Path(directory).glob("*.json")):
            self.load(path)

    def versions(self) -> list[str]:
        return sorted(self._docs)

    def get(self, version: str) -> Taxonomy:
        if version not in self._docs:
            raise KeyError(f"未登记的 taxonomy 版本: {version}")
        if version not in self._resolved:
            self._resolved[version] = self._resolve(version, [])
        return self._resolved[version]

    def version_for_date(self, day: date) -> Taxonomy | None:
        """申报日可见的最新版本：issued 不晚于 day 中 issued 最大者。"""
        candidates = []
        for v in self._docs:
            tax = self.get(v)
            if tax.issued is not None and tax.issued <= day:
                candidates.append((tax.issued, v))
        if not candidates:
            return None
        return self.get(max(candidates)[1])

    # ---- 解析 ---------------------------------------------------------

    def _resolve(self, version: str, stack: list[str]) -> Taxonomy:
        if version in self._resolved:
            return self._resolved[version]
        doc = self._docs[version]
        problems: list[str] = []
        lineage: list[str] = []
        concepts: dict[str, ConceptDef] = {}
        aliases: dict[str, str] = {}
        units: dict = {}

        for parent_ref in doc.get("extends", []) or []:
            if parent_ref in stack or parent_ref == version:
                problems.append(
                    f"extends 环: {' → '.join(stack + [version, parent_ref])}，该边已忽略"
                )
                continue
            if parent_ref not in self._docs:
                problems.append(f"extends 引用的 taxonomy 缺失: {parent_ref}，该边已忽略")
                continue
            parent = self._resolve(parent_ref, stack + [version])
            lineage.extend(x for x in parent.lineage if x not in lineage)
            # 拷贝概念定义：本版本的 ref/状态解析不回写父版本
            concepts.update({n: replace(c) for n, c in parent.concepts.items()})
            aliases.update(parent.aliases)
            units.update(parent.units)

        for name, spec in (doc.get("concepts") or {}).items():
            concepts[name] = _parse_concept(name, spec, source=version)
        aliases.update(doc.get("aliases") or {})
        units.update(doc.get("units") or {})

        issued = doc.get("issued")
        tax = Taxonomy(
            version=version,
            namespace=doc.get("namespace"),
            issued=date.fromisoformat(issued) if issued else None,
            concepts=concepts,
            aliases=aliases,
            units=units,
            unit_namespace=doc.get("unit_namespace"),
            problems=problems,
            lineage=lineage + [version],
        )
        # 只缓存顶层请求的版本：环中的中间结果与解析入口相关，不复用
        if not stack:
            self._resolved[version] = tax
        self._resolve_concept_refs(tax)
        self._resolve_aliases(tax)
        return tax

    def _resolve_concept_refs(self, tax: Taxonomy) -> None:
        """解析概念级 ref 链；环与缺失只标记链上的概念。"""
        state: dict[str, str] = {}

        def visit(name: str, trail: list[str]) -> str:
            if name in state:
                return state[name]
            concept = tax.concepts[name]
            if not concept.ref:
                state[name] = "ok"
                return "ok"
            if concept.ref not in tax.concepts:
                concept.status = "missing"
                concept.problem = f"ref 引用的概念未定义: {concept.ref}"
                tax.problems.append(f"概念 {name}: {concept.problem}")
                state[name] = "missing"
                return "missing"
            if concept.ref in trail:
                cycle = trail[trail.index(concept.ref):] + [concept.ref]
                for member in cycle:
                    c = tax.concepts[member]
                    c.status = "cycle"
                    c.problem = f"ref 环: {' → '.join(cycle)}"
                    state[member] = "cycle"
                tax.problems.append(f"概念 ref 环: {' → '.join(cycle)}")
                return "cycle"
            result = visit(concept.ref, trail + [concept.ref])
            if result != "ok":
                concept.status = result
                concept.problem = f"ref 链断裂于 {concept.ref}"
            else:
                base = tax.concepts[concept.ref]
                # 继承基础概念未覆盖的属性
                concept.dimension = concept.dimension or base.dimension
                concept.datatype = concept.datatype or base.datatype
                concept.period_type = concept.period_type or base.period_type
            state[name] = concept.status
            return concept.status

        for name in list(tax.concepts):
            visit(name, [name])

    def _resolve_aliases(self, tax: Taxonomy) -> None:
        """合并 replaces 推出的隐式别名，并校验别名目标存在。"""
        for name, concept in tax.concepts.items():
            if concept.replaces and concept.replaces not in tax.aliases:
                tax.aliases[concept.replaces] = name
        for old, new in list(tax.aliases.items()):
            if new not in tax.concepts:
                tax.problems.append(f"别名 {old} → {new} 的目标未定义，该映射已标记为断链")

    # ---- 概念映射 -----------------------------------------------------

    def map_concept(self, name: str, target_version: str) -> Mapping:
        return self.get(target_version).map_name(name)
