"""摄取管线与演示入口。

``ingest_filing`` 把一份报告目录（filing.json + 报告文件）变为不可变 Filing：
提取事实 → 固定申报时可见的 taxonomy 版本 → 构建该版本的单位注册表 → 重复判定。

``python3 -m app.pipeline`` 用 fixtures 下的合成数据演示：
多单位换算、维度、重复精度判定、重述双时态视图、追溯、差异、重新投影与 extension 环。
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app import extract as extractor
from app.dedupe import classify_facts
from app.model import QName, Unit
from app.query import compare_concept, reproject_view
from app.store import FactStore, Filing
from app.taxonomy import TaxonomyRegistry
from app.units import default_registry

FIXTURES = Path("fixtures")


def load_taxonomies(directory: Path = FIXTURES / "taxonomy") -> TaxonomyRegistry:
    reg = TaxonomyRegistry()
    reg.load_dir(directory)
    # 兼容旧样例
    legacy = FIXTURES / "taxonomy.json"
    if legacy.exists():
        reg.load(legacy)
    return reg


def ingest_filing(store: FactStore, taxonomies: TaxonomyRegistry, directory: Path) -> Filing:
    meta = json.loads((directory / "filing.json").read_text(encoding="utf-8"))
    doc_path = directory / meta["document"]
    raw = doc_path.read_bytes()
    doc = extractor.extract(raw, source_name=str(doc_path))

    filed_at = datetime.fromisoformat(meta["filed_at"])
    taxonomy_version = meta.get("taxonomy")
    if taxonomy_version is None:
        tax = taxonomies.version_for_date(filed_at.date())
        taxonomy_version = tax.version if tax else "unknown"

    # 单位注册表随该 filing 的 taxonomy 版本固定
    registry = default_registry()
    try:
        tax = taxonomies.get(taxonomy_version)
        registry.load_taxonomy_units(tax.unit_namespace, tax.units)
    except KeyError:
        doc.problems.append(f"taxonomy 版本未登记: {taxonomy_version}")

    groups = classify_facts(doc.facts, registry)
    filing = Filing(
        id=meta["filing_id"],
        entity=meta["entity"],
        filed_at=filed_at,
        source_name=str(doc_path),
        source_hash=doc.source_hash,
        taxonomy_version=taxonomy_version,
        parser_version=extractor.PARSER_VERSION,
        supersedes=meta.get("supersedes"),
        facts=tuple(doc.facts),
        groups=tuple(groups),
        problems=tuple(doc.problems),
    )
    store.add_filing(filing)
    return filing


def _jsonable(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(str(x) for x in obj)
    return str(obj)


def main() -> None:
    taxonomies = load_taxonomies()
    store = FactStore(taxonomies)

    print("== taxonomy 版本与局部问题 ==")
    for version in taxonomies.versions():
        tax = taxonomies.get(version)
        print(f"  {version}: 概念 {len(tax.concepts)} 个，链 {' <- '.join(tax.lineage)}")
        for problem in tax.problems:
            print(f"    [局部标记] {problem}")

    print("\n== 摄取 filings ==")
    filings = []
    for directory in sorted((FIXTURES / "filings").iterdir()):
        if (directory / "filing.json").exists():
            filing = ingest_filing(store, taxonomies, directory)
            filings.append(filing)
            print(
                f"  {filing.id} 实体 {filing.entity} 申报 {filing.filed_at.date()} "
                f"taxonomy={filing.taxonomy_version} 事实 {len(filing.facts)} 条"
            )
            for problem in filing.problems:
                print(f"    [解析问题] {problem}")

    registry = default_registry()

    print("\n== 当前视图（SYNTH-01，重述后） ==")
    for entry in store.view(entity="SYNTH-01").entries.values():
        g = entry.group
        chosen = g.chosen
        value = f"{chosen.value} {chosen.unit}" if chosen and chosen.unit else (
            str(chosen.value) if chosen else "（未决冲突）"
        )
        print(
            f"  {entry.key[0].local:20s} {str(entry.key[2]):38s} "
            f"{value:24s} [{g.classification.value}] 来自 {entry.filing_id}"
        )

    as_of = datetime(2026, 5, 1)
    print(f"\n== as_of {as_of.date()} 视图（重述前仍可查） ==")
    for entry in store.view(entity="SYNTH-01", as_of=as_of).entries.values():
        g = entry.group
        chosen = g.chosen
        value = f"{chosen.value} {chosen.unit}" if chosen and chosen.unit else (
            str(chosen.value) if chosen else "（未决冲突）"
        )
        print(f"  {entry.key[0].local:20s} {value:24s} 来自 {entry.filing_id}")

    print("\n== 追溯：重述后的 Waste 事实 ==")
    target = None
    for entry in store.view(entity="SYNTH-01").entries.values():
        if entry.key[0].local == "Waste":
            target = entry.group.chosen
    if target is not None:
        print(store.trace(target.fact_id).explain())

    print("\n== 差异：f-001 → f-002（直接比较） ==")
    diff = store.diff("f-001", "f-002", registry)
    for e in diff.entries:
        if e.status != "unchanged":
            print(f"  [{e.status}] {e.key[0]} {e.note} delta={e.delta}")
    print("== 差异：f-001 → f-002（先投影到 base-2026，更名与重述分开） ==")
    diff_p = store.diff("f-001", "f-002", registry, project_to="base-2026")
    for e in diff_p.entries:
        if e.status != "unchanged":
            print(f"  [{e.status}] {e.key[0]} {e.note} delta={e.delta}")

    print("\n== 重新投影：当前视图 → base-2026（旧结果保留，可并存查询） ==")
    projection = reproject_view(store, taxonomies, "base-2026", "view-2026", entity="SYNTH-01")
    for entry in projection.entries:
        print(f"  {entry.concept_from} → {entry.concept_to} [{entry.status}]")

    print("\n== 跨企业比较：Scope1 排放 → tCO2e ==")
    scope1 = None
    for entry in store.view(entity="SYNTH-01").entries.values():
        if entry.key[0].local == "Emissions" and entry.key[3]:
            scope1 = entry.key[3]
            break
    rows = compare_concept(
        store, registry, "Emissions", ["SYNTH-01", "SYNTH-02"],
        target=Unit(id="", measures=(QName("https://example.invalid/units", "tCO2e"),)),
        dimensions=scope1,
    )
    for row in rows:
        if row.error:
            print(f"  {row.entity}: 错误 {row.error}")
        else:
            print(
                f"  {row.entity}: {row.value} {row.unit} → {row.converted} tCO2e "
                f"（因子 {row.factor}，taxonomy {row.taxonomy_version}，判定 {row.judgment}）"
            )

    print("\n== 量纲不兼容示例：Workers → tCO2e ==")
    rows = compare_concept(
        store, registry, "Workers", ["SYNTH-01"],
        target=Unit(id="", measures=(QName("https://example.invalid/units", "tCO2e"),)),
    )
    for row in rows:
        print(f"  {row.entity}: {row.error}")

    out_dir = Path("runtime")
    out_dir.mkdir(exist_ok=True)
    summary = {
        "filings": [f.id for f in filings],
        "taxonomy_problems": {
            v: taxonomies.get(v).problems for v in taxonomies.versions()
        },
        "diff_f001_f002": [
            {"key": str(e.key[0]), "status": e.status, "delta": e.delta, "note": e.note}
            for e in diff_p.entries
        ],
        "projection_view_2026": [
            {"from": e.concept_from, "to": e.concept_to, "status": e.status}
            for e in projection.entries
        ],
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=_jsonable),
        encoding="utf-8",
    )
    print("\n已写出 runtime/summary.json")


if __name__ == "__main__":
    main()
