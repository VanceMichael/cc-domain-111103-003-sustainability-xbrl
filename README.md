# 可持续披露报告的 XBRL 事实采集

这是一个 Python 数据采集项目，从 Inline XBRL 与普通 XBRL instance 中提取温室气体、
员工保障、安全生产等披露事实，并提供可版本化 taxonomy 解析、重复事实判定、
重述双时态视图、事实追溯与差异查询。仅依赖标准库，验收不依赖商业 XBRL 服务。

所有 fixtures 均为合成披露数据：发行主体、日期、数值均为虚构，
不构成财务或可持续性结论，也不代表真实企业、机构或设备数据。

## 模块

| 模块 | 职责 |
| --- | --- |
| `app/model.py` | 不可变数据模型：QName、Context、Unit、Fact、Provenance |
| `app/extract.py` | SAX 提取器：Inline XBRL 与普通 instance，记录行/列原文位置 |
| `app/units.py` | 单位注册表：量纲向量、兼容检查、换算因子 |
| `app/taxonomy.py` | 版本化 taxonomy：extension 链、环/缺失局部标记、概念映射 |
| `app/dedupe.py` | 重复事实分类与判定（保留全部来源） |
| `app/store.py` | filing 存储、双时态视图、追溯、差异 |
| `app/query.py` | 跨企业可比值、按新 taxonomy 重新投影 |
| `app/pipeline.py` | 摄取管线与演示入口 |
| `app/xbrl.py` / `app/inspect.py` | 早期基础读取入口（保留兼容） |

## 语义规则

- **概念身份是 QName**。同名标签在不同命名空间下不是同一概念，模型全程保留命名空间。
- **量纲兼容是换算前提**。每个度量声明量纲向量（`mass`、`mass_co2e`、`energy`、
  `count` 等）与基准单位因子；向量不一致即抛 `IncompatibleUnits`。
  tCO2e 与 tonne 分属不同量纲，不能自动换算。
- **重复事实不静默取舍**。同概念同上下文的多条事实分类为：
  格式重复（值/单位/精度一致）、精度变体（同值不同精度，取精度最高者）、
  单位变体（换算后一致）、同上下文冲突（不自动取舍，标记复核）。
  全部成员始终保留，判定只给出代表值与理由。
- **重述不改写历史**。filing 不可变存储，申报时固定 taxonomy 版本与解析器版本；
  当前视图按申报时间取最新，as_of 视图回到任一时点，旧 filing 始终可查。
  后发布的 taxonomy 不会改写当时可见的事实。
- **taxonomy 问题局部标记**。extension 环、概念 ref 环、缺失定义只标记受影响的
  概念/别名，同一报告中无关概念正常可用。
- **重新投影只增不改**。按目标 taxonomy 版本的概念映射（aliases + replaces）
  生成命名投影，原始视图与事实保持不变，多个投影可并存。

## 本地运行

项目使用 Python 3.12 或更新版本，仅依赖标准库。从项目根目录执行：

```sh
python3 -m app.pipeline     # 合成数据端到端演示，结果写入 runtime/
python3 -m app.inspect      # 早期基础读取入口
python3 -m unittest discover -s tests -v
python3 -m compileall -q app tests
```

## 来源材料

- `fixtures/report.xml`、`fixtures/taxonomy.json`、`fixtures/report-inline.xhtml`：
  早期基础样例（保留，旧测试仍覆盖）。
- `fixtures/taxonomy/`：版本化 taxonomy。`base-2025` → `base-2026` 演示准则更名
  （`WasteGenerated` → `Waste`）；`ext-acme-2026` 演示 extension 引用基础概念；
  `ext-cycle-a/b` 演示 extends 环与概念 ref 环；`ext-missing` 演示缺失引用。
- `fixtures/filings/`：三份合成 Inline XBRL 申报，覆盖多单位（tCO2e/kgCO2e、
  MWh/kWh）、维度（Scope1/Scope2、地区）、重复精度（decimals 0/1、隐藏区重复）、
  同上下文冲突（员工人数 35/36）、重述（f-002 更正 f-001）与同名异命名空间标签。
  每份目录的 `filing.json` 记录申报时间与申报时固定的 taxonomy 版本。

## 提取范围与限制

已支持：`ix:nonFraction`（format/scale/sign/decimals/precision）、`ix:nonNumeric`、
`ix:hidden`、普通 instance item、explicitMember 维度（segment/scenario）、
divide 复合单位、ixt 的 numdotdecimal/numcommadecimal/numdash/zerodash。

暂不支持（遇到时记录问题并跳过，不中断提取）：`ix:tuple`、`ix:fraction`、
`ix:continuation` 链、typedMember 维度。

处理结果放在 runtime 目录，源文件应保持只读。若连接真实来源，
先按来源方授权范围配置读取权限。
