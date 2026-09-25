# Address Matcher · 标准地址匹配 Web 应用

一个基于 **FastAPI + SQLite** 的本地 Web 应用，把杂乱的外部地址文本匹配到用户上传的
**标准地址表**。内置中文与英文地址处理，自动统一 Unicode、大小写、常见道路缩写与门牌号
格式，按 **词元相似度** 与 **门牌号相似度** 两条轴线分别打分；最高分过低或与次高分差距
过小的候选会被标记为 **待确认（needs_review）**，并在界面上展示命中词元、冲突字段与
可复核结果。

- 🌐 网页界面：单条/批量匹配、**规则 A/B 对照实验**、上传标准地址 CSV、待确认队列人工复核（含同案合并复核、复核共识记忆）、**地址覆盖缺口清单**
- 🔬 **导入影响预演**：CSV 预览确认前，把本次**有效待导入行**按正式导入的同款行规范化生成临时地址（以 CSV 行号标识、只在内存中），对**按日志时间倒序取出的最近 100 个不同原始查询**，分别用当前地址表与临时合并表沿现有匹配及复核共识逻辑重跑，逐条对比前后状态与自动命中目标，按 **改善 / 退化 / 目标改派 / 不变** 分类汇总；无效及重复行不参与，预演不写入地址和匹配日志，确认导入仍走原有文件校验与事务
- 🕳️ **地址覆盖缺口**：从已保存且状态为 `no_match` 的匹配日志生成缺口清单，按归一化后的**道路词元 + 地区词元 + 门牌号**归组（门牌号不同不算同一缺口），展示出现次数、原始查询示例与最近出现时间；每次打开清单都用**当前标准地址表与默认规则**重跑每组代表查询——现已能自动命中的组标为**已覆盖**并显示命中地址，其余标为**待补录**并显示当前首选候选，避免上传地址后仍把旧拒绝记录当作未解决。支持待补录 / 已覆盖筛选，全程只读、不写日志
- 🩺 **标准地址自匹配巡检**：对当前表每条标准地址，以其全文作为查询、按默认规则在**完整地址表**上重新评分，只列出**未能自动命中自身 ID** 的记录。每条风险给出自身分数与所有得分不低于「自身分数 − 默认分差阈值」的其他地址（阻碍候选，**不受前五候选截断**），并按 **自身分数不足 / 首选偏移 / 分差歧义** 分类汇总；上传新表后刷新即重新计算，全程只读、不写日志
- 🔁 **复核结论回放**：取全部已复核记录，以原查询在**当前标准地址表**上按**默认规则**重新匹配，逐条对照人工结论——确认/改派记录自动命中所选地址为**一致**、命中其他地址为**冲突**、未自动命中为**漏命中**，判无匹配记录出现自动命中为**误命中**。单条复核与同案组复核写入时会**持久保存所选地址的 ID 与全文快照**（普通列、非外键，地址删除也不消失），回放始终展示原 ID、全文与复核得分，并用当前地址表标注所选地址是否已删除；历史记录若在外键置空前已失去可确定的所选 ID，则明确标记为**原选择未知（unknown）**，绝不从候选列表猜测。返回各类数量、原结论、当前首选与得分，支持按类别筛选，全程只读、不写日志也不改动复核记录
- 🧪 **规则 A/B 对照实验**：提交 2～4 个命名规则档案（自动命中/拒绝/分差阈值 + 词元权重）与 1～100 条查询，在同一地址表快照下逐档运行，逐条对比状态/首选/得分/候选差异并标出不一致与触发阈值，结果按实验编号持久化回看
- 📈 **历史回归对比**：任选两次已持久化实验，服务端按档案名称与去首尾空白后的查询逐档对齐（缺失档案 / 重复查询直接拒绝），逐单元分类为 改善 / 退化 / 仅分数变化 / 无变化 并汇总；地址表快照 digest 不同时提示不可直接比较但仍展示逐条差异；页面支持按类别筛选与 CSV 下载
- 🔌 REST API：`/api/match`、`/api/match/batch`、`/api/addresses`、复核接口、A/B 实验接口（含 `/docs` Swagger）
- 🗄️ SQLite 持久化，零外部依赖，数据通过 Docker volume 保存
- 🌍 归一化：NFKC + 变音符折叠 + 繁简中文映射 + 全角字符、道路缩写（St/Rd/Ave…）、门牌号（`#12-B`/`No.12b`/`12号`）
- 👥 同案合并复核：归一化后道路/地区词元与门牌号一致、且候选（ID/顺序/得分）完全相同的待确认记录自动归组，可整组确认首选、改派共同候选或判无匹配；整组写入在单个 SQLite 写事务内完成，组成员或签名有变化即整体冲突回滚
- 🧠 **复核共识记忆**：单条与批量匹配照常评分；当同一归一化道路词元 + 地区词元 + 门牌号下已有 **至少 2 条已复核记录**、且全部一致确认同一个仍然有效的标准地址（无拒绝、无不同结论）时，新的待确认结果自动指向该地址；目标不在本次候选中或历史结论冲突时保持原判并返回原因。新复核即时生效，旧记录不回写；页面展示共识命中状态与证据条数

---

## 一条命令启动（Docker Compose）

```bash
docker compose up --build
```

启动后打开：

| 入口 | 地址 |
| --- | --- |
| 网页界面（默认「单条 / 批量匹配」，标签页内含「复核回放」「地址覆盖缺口」「自匹配巡检」等页面；「标准地址表」页的 CSV 预览面板内含「影响预演」入口） | http://localhost:8000 |
| 复核结论回放 API | http://localhost:8000/api/review-replay |
| 标准地址自匹配巡检 API | http://localhost:8000/api/address-audit |
| 导入影响预演 API（`{token}` 为预览凭证） | http://localhost:8000/api/addresses/preview/{token}/impact |
| REST API 文档（Swagger） | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/api/health |

打开 **http://localhost:8000** 后，点击顶部 **「地址覆盖缺口」** 标签即可查看缺口清单，
点击 **「自匹配巡检」** 标签即可查看标准地址自匹配巡检结果，
点击 **「复核回放」** 标签即可查看复核结论回放对照；
在 **「标准地址表」** 标签页选择 CSV 生成预览后，点击预览面板中的 **「🔬 影响预演」**
即可查看本次导入对最近日志查询的影响。

- **端口**：`8000`（在 `docker-compose.yml` 的 `ports` 中修改）
- 首次启动会自动加载内置示例标准地址（`sample_data/standard_addresses.csv`，共 15 条，含中英文地址）
- SQLite 数据库保存在 Docker volume `address-data`（容器内 `/data/addresses.db`），重启不丢失（A/B 实验记录同样持久化在该库）
- **必要配置**：开箱无需任何配置；`docker-compose.yml` 中的 `AUTO_THRESHOLD` / `MARGIN_THRESHOLD` /
  `REJECT_THRESHOLD` / `TOKEN_WEIGHT` 环境变量仅作为常规匹配的默认值与网页实验表单的预填值，
  A/B 实验中每档参数都可在请求里单独覆盖，无需重建镜像

不用 Docker 时：

```bash
pip install -r requirements.txt
ADDRESS_DB_PATH=./data/addresses.db \
SAMPLE_CSV=./sample_data/standard_addresses.csv \
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 快速体验

网页打开 http://localhost:8000 ，点击 **「填充示例」** 再点 **「开始匹配」**，
或直接用 curl：

```bash
# 1) 自动命中：缩写 + 大小写被统一（123 Main St. -> 123 Main Street）
curl -s -X POST http://localhost:8000/api/match \
  -H 'Content-Type: application/json' \
  -d '{"query": "123 main st., New York, NY 10001"}'

# 2) 待确认（门牌号冲突）：街道词元完全命中，但门牌号 1010 ≠ 1000/100
curl -s -X POST http://localhost:8000/api/match \
  -H 'Content-Type: application/json' \
  -d '{"query": "1010 Lake Dr, Austin TX"}'

# 3) 待确认（歧义）：50 / 52 Hillcrest Rd 且门牌号缺失，前两名分差为 0
curl -s -X POST http://localhost:8000/api/match \
  -H 'Content-Type: application/json' \
  -d '{"query": "Hillcrest Road, Dallas"}'

# 4) 自动命中（中文 + 全角数字 + 繁体字）：北京東路８８號 3楼
curl -s -X POST http://localhost:8000/api/match \
  -H 'Content-Type: application/json' \
  -d '{"query": "北京東路８８號 3楼, 上海"}'

# 5) 自动命中（Tce -> Terrace 缩写归一 + 仅街道无城市也不扣分）
curl -s -X POST http://localhost:8000/api/match \
  -H 'Content-Type: application/json' \
  -d '{"query": "742 Evergreen Tce"}'

# 6) 无匹配
curl -s -X POST http://localhost:8000/api/match \
  -H 'Content-Type: application/json' \
  -d '{"query": "1 Milky Way, Mars Base"}'
```

批量匹配：

```bash
curl -s -X POST http://localhost:8000/api/match/batch \
  -H 'Content-Type: application/json' \
  -d '{"queries": ["123 main st ny", "88 Beijing East Rd Shanghai", "xxxxx"]}'
```

上传自己的标准地址表（直接上传，服务端解析后立即写入并跳过重复）：

```bash
curl -s -X POST http://localhost:8000/api/addresses/upload \
  -F 'file=@sample_data/standard_addresses.csv'
```

**上传前预览确认（推荐）**：先预览有效/重复/无效数量与无效行号，确认时再
**重新提交同一文件**。服务端会在同一个 SQLite 写事务内核对预览凭证绑定的
文件 SHA-256 摘要与地址表快照，任一不符都不会写入，必须重新预览。

```bash
# 1) 预览：不写入任何记录，返回 preview_token 与统计
curl -s -X POST http://localhost:8000/api/addresses/preview \
  -F 'file=@sample_data/standard_addresses.csv'
# => {"preview_token":"...","valid":15,"duplicates":0,"invalid":0,
#     "invalid_lines":[],"total":15,"filename":"..."}

# 2) （可选）导入影响预演：GET，无需重新上传文件
curl -s http://localhost:8000/api/addresses/preview/上一步返回的token/impact

# 3) 确认：必须再次提交文件，并带上预览凭证
curl -s -X POST http://localhost:8000/api/addresses/confirm \
  -F 'file=@sample_data/standard_addresses.csv' \
  -F 'preview_token=上一步返回的 token'
# => {"inserted":15,"duplicates":0,"invalid":0,"invalid_lines":[],...}
```

- 预览只写入一条带 30 分钟有效期的凭证（含文件摘要、地址表快照、有效行内容），
  **不写地址表**；
- 确认时若文件被改动（摘要不符）、地址表在预览后发生变化（快照不符）、
  凭证不存在或已过期，接口分别返回 `409` / `409` / `404`，整笔事务回滚，
  需要重新调用预览接口；
- 确认成功后凭证立即失效（一次性），且仅写入该次预览中分类为「有效」的记录；
- 「标准地址表」页默认勾选「上传前先预览确认」；取消勾选则继续使用直接上传接口。

---

## 导入影响预演

预览之后、确认之前，可以先回答一个问题：**这批新地址写进去，会改变哪些已有查询的
判定？**「导入影响预演」用预览凭证中的**有效待导入行**做一次纯内存干跑：

- **临时合并表**：有效行按**与正式导入完全相同的行规范化**（`ingest.normalize_for_insert`）
  生成临时地址，追加在当前地址表快照之后组成合并表；临时地址不写入数据库，
  以 **CSV 行号**标识（如「CSV 第 2 行」）。**无效及重复行不参与**。
- **预演对象**：匹配日志中**按日志时间倒序取出的最近 100 个不同原始查询**
  （同一查询的重复日志只保留最近一次）。
- **双跑对比**：每条查询分别用**当前地址表**与**临时合并表**，沿现有匹配 +
  **复核共识**逻辑（与 `/api/match` 相同）各重跑一遍，对比前后状态与自动命中目标：

  | 分类 | 判定 |
  | --- | --- |
  | 改善 `improved` | 状态上升（按 无匹配 < 待确认 < 自动命中 的等级比较） |
  | 退化 `regressed` | 状态下降 |
  | 目标改派 `retargeted` | 两次均自动命中，但自动命中目标不同 |
  | 不变 `unchanged` | 其余情况 |

- **校验**：令牌不存在或已过期返回 `404`，地址表在预览后发生变化返回 `409`；
  预演**不消耗凭证**，之后仍可正常确认导入（确认仍走原有的文件摘要/快照校验与
  单事务写入）。
- **只读**：预演只在内存中重跑评分与共识判定，**不写入地址和匹配日志**。
- **页面入口**：「标准地址表」标签页上传 CSV 生成预览后，点击预览面板中的
  **「🔬 影响预演」**；结果按 退化 → 目标改派 → 改善 → 不变 排序（风险优先），
  顶部汇总四类数量与参与预演的新地址清单。

```bash
# 影响预演（GET，token 来自 POST /api/addresses/preview）
curl -s http://localhost:8000/api/addresses/preview/<preview_token>/impact
# => {"filename":"s.csv","valid_rows":2,"query_limit":100,"query_count":100,
#     "new_addresses":[{"csv_line":2,"full_text":"76 Impossible Crescent",
#                       "house":"76","external_id":"SMK-1"}],
#     "summary":{"total":100,"improved":3,"regressed":1,"retargeted":0,"unchanged":96},
#     "items":[{
#       "query":"76 Impossible Crescent, Nowhereville","latest_match_id":87,
#       "before":{"status":"no_match","score":0.21,"target":null,
#                 "consensus_applied":false},
#       "after":{"status":"auto_matched","score":1.0,
#                "target":{"kind":"new","csv_line":2,
#                          "full_text":"76 Impossible Crescent"},
#                "consensus_applied":false},
#       "category":"improved","category_label":"改善",
#       "reason":"状态改善：无匹配 → 自动命中（导入后目标：CSV 第 2 行「…」）"}]}
```

自动命中目标中，`kind` 为 `existing` 时表示现有地址（带数据库 `id`），为 `new`
时表示本次待导入的新地址（带 `csv_line`）；非自动命中的状态没有自动命中目标，
`target` 为 `null`。

---

## 规则 A/B 对照实验

在同一标准地址表快照下，用 **2～4 个命名规则档案** 对 **1～100 条查询** 逐档运行现有匹配器，
用来比较不同阈值 / 权重组合的判定差异。实验是**纯评分对照**：不查询复核共识记忆、
不写入匹配日志与待确认队列；匹配器本身完全不变。

网页打开 http://localhost:8000 后切换到 **「规则 A/B 实验」** 标签：默认预置「基线A / 宽松B」两档，
可增删到 4 档、逐档调整 4 个参数，粘贴查询后点击「开始对照实验」；下方「历史实验」可按编号回看。

每个档案包含 4 个参数（均为 0～1 的数值）：

| 参数 | 含义 |
| --- | --- |
| `auto_threshold` | 自动命中阈值，首选得分达到该值且领先次选足够明显才自动命中 |
| `reject_threshold` | 拒绝阈值，首选得分低于该值判 `no_match`（必须严格小于自动命中阈值） |
| `margin_threshold` | 前两名分差小于该值判 `needs_review`（歧义） |
| `token_weight` | 词元相似度权重，门牌号权重为 `1 - token_weight` |

返回内容（逐条查询 × 逐档）：

- **各档结论**：状态（`auto_matched` / `needs_review` / `no_match`）、首选地址 ID 与得分
  （以及相对第一档的分差）、次高分、判定原因；
- **触发阈值**：自动命中 / 拒绝 / 分差三个条件分别给出阈值、实测值（首选分或前两名分差）与
  是否触发，并标出**决定性阈值**（最终决定结论的那条规则）；
- **结论不一致标记**：各档状态或首选地址任一不同即标「⚠ 结论不一致」，并按状态分组展示；
- **候选差异**：所有档候选取并集，按地址给出各档的排名与得分（未进入该档 top-5 标 `—`），
  并可展开查看每档完整候选（词元命中、冲突等明细）；
- **汇总**：每档 自动命中 / 待确认 / 无匹配 计数、不一致查询总数，以及实验所用地址表快照
  （行数、最大 ID、摘要 digest）。

**逐项校验、不执行**：档案数量不是 2～4、查询数量不是 1～100、参数非数值 / 越界、
拒绝阈值 ≥ 自动命中阈值、档案重名、查询为空或**完全重复**（去空白后逐条比较）等问题会
一次性按 `档案/查询 + 序号 + 字段` 列在 `errors` 中返回 `400`，只要存在任何一项错误就
**不运行、不持久化**。

**持久化与稳定性**：实验结果（档案、查询、快照、全部逐档结果、汇总）写入 SQLite，
可按实验编号（`EXP-<UTC时间>-<随机>`）随时回看。匹配是确定性的，同一输入顺序、
同一地址表快照下重复提交得到完全一致的结果（编号与时间戳除外）；地址表变化后重跑会得到
新的快照 digest，但旧实验仍按其保存的快照原文回放。

```bash
# 提交一次两档对照实验
curl -s -X POST http://localhost:8000/api/experiments \
  -H 'Content-Type: application/json' -d '{
    "profiles": [
      {"name": "基线A", "auto_threshold": 0.80, "reject_threshold": 0.35,
       "margin_threshold": 0.06, "token_weight": 0.70},
      {"name": "激进B", "auto_threshold": 0.70, "reject_threshold": 0.20,
       "margin_threshold": 0.02, "token_weight": 0.80}
    ],
    "queries": ["1010 Lake Dr, Austin TX", "Hillcrest Road, Dallas",
                "1 Milky Way, Mars Base"]
  }'

# 历史列表 / 按编号回看完整结果
curl -s http://localhost:8000/api/experiments
curl -s http://localhost:8000/api/experiments/EXP-20260924T075404-358e3e3e

# 参数非法 + 查询重复 -> 400，errors 逐项列出，实验不执行
curl -s -X POST http://localhost:8000/api/experiments \
  -H 'Content-Type: application/json' -d '{
    "profiles": [
      {"name": "X", "auto_threshold": 1.5, "reject_threshold": 0.3,
       "margin_threshold": 0.1, "token_weight": 0.7},
      {"name": "X", "auto_threshold": 0.4, "reject_threshold": 0.5,
       "margin_threshold": 0.1, "token_weight": 2}
    ],
    "queries": ["abc", "abc"]
  }'
# => {"detail":"请求参数校验失败","errors":[
#      {"type":"profile","index":0,"field":"auto_threshold","message":"auto_threshold=1.5 超出允许范围 [0, 1]"},
#      {"type":"profile","index":1,"field":"name","message":"档案名称与第 1 档重复：…"},
#      {"type":"query","index":1,"field":null,"message":"查询与第 1 条完全重复：…"}, ...]}
```

---

## 历史回归对比

在「规则 A/B 实验」标签页底部的 **「历史回归对比」** 卡片中，从下拉框选择两次已持久化的实验
（历史实验列表每行也有「→基准 / →对比」快捷按钮），点击「开始对比」即可得到逐档回归报告；
历史列表刷新后下拉框自动更新，默认选中「次新 → 最新」两次实验。

**对齐规则（服务端强制执行）**：

- **档案按名称对齐**：两次实验的档案集合必须完全一致，任一方缺失档案（如基准有 `P2`
  而对比没有）会逐项列出并以 `400` 拒绝；档案顺序以基准实验为准；
- **查询按去首尾空白后的文本对齐**：只有两次实验都存在的查询参与对比；仅一方存在的
  查询会在结果中单独列出、不计入统计；任一实验内存在（去空白后）重复查询时同样
  逐项报错并拒绝；
- 两次实验编号相同、编号不存在分别返回 `400` / `404`；对比是**纯推导**，不写任何表。

**逐单元分类**（每个「查询 × 档案」单元，状态质量按 `no_match < needs_review < auto_matched`
排序）：

| 分类 | 判定 |
| --- | --- |
| 改善 `improved` | 状态上升（如 待确认 → 自动命中） |
| 退化 `regressed` | 状态下降；或状态相同但**首选地址变化**（无 ground truth，保守计入退化） |
| 仅分数变化 `score_only` | 状态与首选地址不变，仅首选得分变化 |
| 无变化 `unchanged` | 状态、首选地址、得分完全一致 |

**快照提示**：两次实验的地址表快照 digest 不同时，结果顶部显示红色警告
「不可直接比较」（差异可能来自地址表本身的变化），但逐条差异仍完整展示。

**页面功能**：四个分类的汇总计数、按类别筛选（点击筛选按钮只显示该类别）、
**CSV 下载**（下载当前筛选结果，UTF-8 带 BOM，Excel 可直接打开）。

```bash
# 对比两次已持久化的实验（编号来自 GET /api/experiments）
curl -s -X POST http://localhost:8000/api/experiments/compare \
  -H 'Content-Type: application/json' -d '{
    "base_id":   "EXP-20260924T075404-358e3e3e",
    "target_id": "EXP-20260924T081227-9a1c04f0"
  }'
# => {"base": {...}, "target": {...}, "aligned_profiles": ["基线A", "激进B"],
#     "queries": {"common": 3, "only_in_base": [], "only_in_target": []},
#     "snapshot": {"comparable": true, "base_digest": "…", "target_digest": "…", "note": null},
#     "summary": {"total_cells": 6, "improved": 1, "regressed": 0,
#                 "score_only": 2, "unchanged": 3},
#     "cells": [{"query": "…", "profile": "基线A", "category": "improved",
#                "category_label": "改善", "reason": "状态改善：待确认 → 自动命中",
#                "base": {"status": "needs_review", "best_id": 5, "best_score": 0.7864},
#                "target": {"status": "auto_matched", "best_id": 5, "best_score": 0.7864},
#                "score_delta": 0.0}, ...]}

# 档案对不齐 -> 400，errors 逐项列出，不执行对比
curl -s -X POST http://localhost:8000/api/experiments/compare \
  -H 'Content-Type: application/json' \
  -d '{"base_id": "EXP-...", "target_id": "EXP-..."}'
# => {"detail":"对比请求校验失败","errors":[
#      {"type":"profiles","index":null,"field":null,"message":"对比实验缺少档案：“P2”"}, ...]}
```

CSV 支持的列名（均可识别别名，表头大小写不敏感）：

| 标准列 | 可识别别名 |
| --- | --- |
| `address` | `full_address`、`formatted_address`、`addr` |
| `line1` | `street`、`address_line1` |
| `city` | `town` |
| `state` | `province`、`region` |
| `postal_code` | `zip`、`zipcode`、`postcode` |
| `country` | — |
| `external_id` | `id`、`code` |

没有 `address` 列时会自动用 `line1, city, state, postal_code, country` 拼接。

---

## 匹配逻辑说明

1. **归一化**（`app/normalization.py`）
   - Unicode NFKC（全角字母/数字→半角）、变音符折叠（`Müller`→`muller`）、
     常用繁体字→简体字；
   - 统一小写、去除多余标点；
   - 道路缩写归一：`street/str/st.`→`st`、`road/rd.`→`rd`、`avenue/av`→`ave` 等，
     方向词 `north/south/east/west`→`n/s/e/w`；
   - 门牌号单独抽取并规范化：`#12-B`、`No. 12b`、`12号`、`１２Ｂ` → `12b`
     （5 位孤立数字识别为邮编，`Apt/Suite/Unit/栋/室` 后的数字不会误当门牌号）。

2. **词元化与词元相似度**（`app/matcher.py`）
   - 地址先按逗号/结构化字段拆成 **道路行**（street）与 **地区**（city/state/postcode）两个部分；
   - 英文按词切分；中文切单字并叠加相邻二元组（bigram），兼容轻微表述差异；
   - 以「查询词元 ↔ 候选词元」贪心配对（完全相等优先，其次编辑距离比率 ≥ 0.72，
     或 3 字符以上的一方包含另一方；单字/短缩写必须精确相等，避免 `ter` 误命中 `water`），
     分别计算道路与地区的 **精确率 / 召回率 / F1**；
   - 查询未提供地区时，地区部分不参与扣分（只写街道名的查询仍是完整输入）；
     查询提供了地区则按 道路 0.75 / 地区 0.25 加权；
   - 结果中输出每个命中词元及其相似度，查询独有、候选独有的词元分别标出，
     地区冲突（如查询写了城市但候选缺失/不一致）单列在 `conflicts` 中。

3. **门牌号相似度**
   - 完全相同 = 1.0；数字相同仅字母不同（`12b` vs `12`）= 0.7；
   - 数字不同按数值接近度给**极低**的部分分（上限约 0.3，门牌号错了通常就是另一处房产）；
   - 两边都没有门牌号 = 0.5（中性），仅一方有 = 0.15。

4. **综合得分与判定**
   - `score = TOKEN_WEIGHT × 词元F1 + (1−TOKEN_WEIGHT) × 门牌号分`（默认权重 0.7 / 0.3）
   - `score ≥ 0.80` 且领先第二名 ≥ `0.06` → **自动命中 `auto_matched`**
   - `0.35 ≤ score < 0.80`，或前两名分差 < `0.06` → **待确认 `needs_review`**
   - `score < 0.35` → **无匹配 `no_match`**

   阈值均可通过环境变量调整：`AUTO_THRESHOLD`、`MARGIN_THRESHOLD`、
   `REJECT_THRESHOLD`、`TOKEN_WEIGHT`。

5. **人工复核**：待确认记录可在网页「待确认队列」或通过
   `POST /api/matches/{id}/review` 执行 `confirmed`（确认首选）、
   `overridden`（改派为其他候选，带 `selected_id`）、`rejected`（标记无匹配）。

6. **同案合并复核**（`app/review_groups.py`）
   - **归组规则**：待确认记录按查询归一化后的 **道路词元 + 地区词元 + 门牌号**
     归组；同一归一化桶内，只有 **候选地址 ID、顺序及得分完全相同** 的记录才进入
     同组（组签名 = 归一化键 + 有序 `[候选ID, 得分]` 序列的 SHA-256）。匹配是确定性的，
     因此同一标准地址表下写法不同的同案查询自然同组；若两次匹配之间标准地址表
     发生变化导致候选不同，则自动拆分为不同组，不会误并。
   - **网页展示**：「待确认队列」页顶部列出成员数 ≥ 2 的同案组，展示组内
     **原始查询及数量**、归一化键与共同候选；原有单条复核在下方列表中保留。
   - **整组复核**：`GET /api/review-groups` 查看分组，
     `POST /api/review-groups/review` 携带 `group_key` 与客户端看到的
     `match_ids` 执行整组 `confirmed` / `overridden`（改派共同候选，带
     `selected_id`，必须是组内共同候选之一）/ `rejected`。
   - **事务保证**：服务端在 **同一个 SQLite 写事务**（`BEGIN IMMEDIATE`）内
     重新归组并核对：每位成员仍为待确认、组签名与成员集合均未变化，才逐条写入
     复核决定与对应得分（确认首选记各成员自己的首选及得分，改派记共同候选的得分，
     判无匹配清空选中与得分）；任一核对失败即返回 `409` 冲突且 **一条也不写**，
     客户端刷新后重试。

```bash
# 查看同案分组（仅含待确认记录）
curl -s http://localhost:8000/api/review-groups

# 整组确认首选 / 改派共同候选 / 判无匹配
curl -s -X POST http://localhost:8000/api/review-groups/review \
  -H 'Content-Type: application/json' \
  -d '{"group_key": "<上一步返回的 group_key>", "match_ids": [1,2,3],
       "action": "confirmed"}'
```

7. **复核共识记忆**（`app/consensus.py`）

   评分逻辑完全不变；每次匹配（`/api/match` 与 `/api/match/batch`）评分后，
   都会实时查询该查询 **归一化道路词元 + 地区词元 + 门牌号** 下的已复核历史：

   - **形成共识（hit）**：至少 **2 条** 已复核记录（`confirmed` /
     `overridden`），且全部指向 **同一个仍然存在的标准地址** ——
     不能有任何一条 `rejected`，也不能有指向其他地址的不同结论；
   - **自动指向**：仅当本次结果原本为 **待确认**、且共识地址在本次候选中时，
     才把结果改为 `auto_matched` 并指向共识地址（候选原得分保持不变）；
     `auto_matched` / `no_match` 的评分原判不会被改写；
   - **保持原判并返回原因**：
     - 证据不足（`insufficient`，同向记录 < 2）；
     - 历史结论冲突（`conflict`，存在拒绝结论，或确认了不同地址）；
     - 共识地址已删除（`target_gone`）；
     - 共识地址不在本次候选中（`not_candidate`）。
   - **即时生效、不回写**：共识历史每次实时读取，刚提交的复核下一次匹配
     即可用；旧的匹配/复核记录一律不被修改。命中共识而自动确认的新记录
     会在 `match_logs.consensus_json` 中保存当时的共识判定（含证据条数、
     目标地址、原判原因）。
   - **页面展示**：命中时结果卡片显示绿色「✅ 复核共识命中」横幅与
     **证据条数**，共识候选带「共识指向」标记；未命中时以琥珀色横幅
     说明原因（冲突/证据不足/目标失效/不在候选）。匹配结果 JSON 中对应
     `consensus` 字段：

   ```json
   {
     "status": "auto_matched",
     "best_id": 5,
     "consensus": {
       "state": "hit",
       "applied": true,
       "target_id": 5,
       "target": {"id": 5, "full_text": "1000 Lake Drive"},
       "evidence_count": 2,
       "rejected_count": 0,
       "required": 2,
       "original_status": "needs_review",
       "original_reason": "最高得分低于自动确认阈值……",
       "reason": "命中复核共识：2 条已复核记录一致确认标准地址 #5，自动指向该地址（原判：……）",
       "evidence": [
         {"match_id": 12, "action": "confirmed", "selected_id": 5, "reviewed_at": "…"}
       ]
     }
   }
   ```

---

## 地址覆盖缺口

历史上被判为**无匹配**的外部地址，本质上是标准地址表里缺失的条目；但缺口清单不能只看
旧日志的状态——把对应地址补录进标准地址表后，旧的拒绝记录其实已经被「覆盖」。
「地址覆盖缺口」页（顶部标签，入口 http://localhost:8000）解决这个问题：

- **来源与归组**：读取已保存且 `status = no_match` 的匹配日志，按查询归一化后的
  **道路词元 + 地区词元 + 门牌号**归组（复用复核共识同款归一化键）。同键无论出现多少次
  都只算 **1 个缺口**；**门牌号不同永远不会合并**为同一缺口。每组展示：
  - **出现次数**（组内日志条数）与最多 5 条不同写法的**原始查询示例**（超出部分只显示数量）；
  - **最近出现时间 / 首次出现时间**与代表查询（组内最早一条）。
- **每次打开都实时重跑**：调用接口或点击「重跑并刷新」时，服务端用**当前标准地址表**与
  **默认规则**（配置的默认阈值，`app/config.py`；即普通匹配的同款评分）对每组代表查询
  重新执行一次匹配：
  - 重跑结果为 **自动命中 `auto_matched`** → 该组标记为 **✅ 已覆盖**，显示命中的
    标准地址（ID + 全文，候选明细可展开）；
  - 其余情况（`no_match` / `needs_review`，如门牌号冲突）→ 标记为 **⛏ 待补录**，
    显示**当前首选候选**及得分、判定原因，候选明细可展开。
- **纯评分、只读**：重跑只调用匹配器评分，**不查询复核共识记忆**，也**不写入任何
  匹配日志**；原始 `no_match` 日志的状态、得分与候选一律不被改写，评分规则完全不变。
- **筛选**：下拉框支持「仅待补录（默认）/ 仅已覆盖 / 全部」；标签上的角标显示当前
  待补录缺口数。汇总行始终给出全部缺口数、待补录 / 已覆盖计数、源日志条数与重跑时间。

```bash
# 查看缺口清单（默认全部；status=open|covered 可筛选）
curl -s http://localhost:8000/api/coverage-gaps
curl -s 'http://localhost:8000/api/coverage-gaps?status=open'
# => {"generated_at":"…","total_groups":2,"open":1,"covered":1,
#     "source_records":4,"groups":[{
#       "group_key":"…","street_tokens":["…"],"locality_tokens":["…"],
#       "house":"76","count":3,"examples":[{"id":12,"query":"…"}],
#       "first_seen_at":"…","last_seen_at":"…",
#       "representative_match_id":12,"representative_query":"…",
#       "coverage":"covered",
#       "rerun":{"status":"auto_matched","best_id":24,
#                "candidates":[{"address":{"id":24,"full_text":"76 …"}}]}}]}
```

> 上传地址后重新打开本页即可看到对应组从「待补录」变为「已覆盖」；若只补了相似门牌号
> （如表里是 76 号而历史拒绝的是 71 号），首选会指向该地址但因门牌号冲突仍为
> 「待补录」，不会被误标为已覆盖。

---

## 标准地址自匹配巡检

一张健康的标准地址表应当「认识自己」：把每条记录的全文当作查询打回全表，它应当
**自动命中自己的 ID**。命中不了的记录就是表内的隐患——重复登记、写法冲突或残缺数据。
「自匹配巡检」页（顶部标签，入口 http://localhost:8000 ）做的就是这件事：

- **巡检方式**：对当前标准地址表的**每一条记录**，以其 `full_text` 全文作为查询、
  按**默认规则**（`app/config.py` 的默认阈值与权重，普通匹配同款评分）在**完整
  地址表**上重新评分；只列出**未自动命中自身 ID**（状态非 `auto_matched` 或首选
  不是自身）的记录，其余记录不出现在清单中。
- **每条风险展示**：原地址（ID + 全文）、**自身分数**、判定原因，以及所有得分
  不低于 **自身分数 − 默认分差阈值** 的其他地址（**阻碍候选**）。阻碍候选取自完整
  评分结果，**不受常规匹配的前五候选截断**——多条近重复记录并列时，记录自身甚至
  可能排不进自己的前五，巡检依然完整列出。
- **三类判定**（优先级自上而下，各类数量在页面顶部汇总）：

  | 分类 | 判定 |
  | --- | --- |
  | 自身分数不足 `self_score_low` | 自身得分低于自动命中阈值（默认 0.80），无论如何都无法自动命中自身 |
  | 首选偏移 `best_drifted` | 自身得分已达阈值，但首选是其他地址（近重复/重复登记的典型信号） |
  | 分差歧义 `margin_ambiguous` | 首选是自身，但领先次高分的分差小于默认分差阈值（默认 0.06） |

- **刷新即重算**：每次打开页面或点击「重新巡检」都用**当前标准地址表**整体重算，
  上传新表后刷新即可看到最新结果；支持按分类筛选（汇总计数不受筛选影响）。
- **只读**：巡检只调用匹配器评分，**不查询复核共识记忆**，也**不生成任何匹配日志**；
  标准地址表与历史日志一律不被改写。

```bash
# 查看巡检清单（默认全部风险；category 可筛选）
curl -s http://localhost:8000/api/address-audit
curl -s 'http://localhost:8000/api/address-audit?category=best_drifted'
# => {"generated_at":"…","total_addresses":18,"auto_hit_self":15,"risk_count":3,
#     "self_score_low":1,"best_drifted":1,"margin_ambiguous":1,
#     "thresholds":{"auto_threshold":0.8,"margin_threshold":0.06,...},
#     "items":[{
#       "address":{"id":17,"full_text":"100 Zxqwv Pine Road, Tstville 99001",...},
#       "category":"best_drifted","category_label":"首选偏移",
#       "audit_reason":"自身得分 100.0% 已达自动命中阈值 80.0%，但首选为 #16（100.0%）…",
#       "own_score":1.0,"blocker_floor":0.94,
#       "match_status":"needs_review","match_reason":"最高分与次高分差距过小…",
#       "best_id":16,"best_score":1.0,"best_full_text":"100 Zxqwv Pine Rd, Tstville 99001",
#       "blockers":[{"address":{"id":16,...},"score":1.0,...}]}]}
```

---

## 复核结论回放

人工复核的结论是基于「当时的地址表与规则」做出的；地址表或默认阈值变化后，旧结论可能与
当前自动行为不再一致。「复核回放」页（顶部标签，入口 http://localhost:8000 ）把每条
**已复核**记录的原查询放到**当前标准地址表**上按**默认规则**重新匹配（纯评分，不查询
复核共识记忆），逐条对照人工结论并分类：

| 人工结论 | 当前回放 | 分类 |
| --- | --- | --- |
| 确认首选 / 改派 | 自动命中**所选地址** | ✅ 一致 `consistent` |
| 确认首选 / 改派 | 自动命中**其他地址** | ⛔ 冲突 `conflict` |
| 确认首选 / 改派 | 未自动命中 | ⚠ 漏命中 `missed` |
| 判无匹配 | 出现自动命中 | ⛔ 误命中 `false_hit` |
| 判无匹配 | 仍未自动命中 | ✅ 一致 `consistent` |
| 历史正向结论、所选 ID 已不可确定 | 无法对照 | ❔ 原选择未知 `unknown` |

- **所选地址快照（地址删除也不丢失原结论）**：单条复核与同案组复核写入时，
  除外键列 `selected_id`（`ON DELETE SET NULL`）外，还会把人工所选地址的
  **ID 与全文**持久保存到 `match_logs.selected_address_id` /
  `selected_address_text`（普通列，不随地址行删除而置空），复核得分保存在
  `score`。回放时原结论只从这份快照还原，始终显示**原 ID、全文、复核得分**；
  当前地址表仅用于判定所选地址**是否已删除**（`selected_missing`），删除后
  回放自动命中其他地址记为冲突、未自动命中记为漏命中，不会退化成泛化的
  「地址已删除」提示。程序启动时会为快照功能上线前复核、且所选地址仍存在的
  历史记录**一次性回填**快照。
- **原选择未知（不从候选猜测）**：若历史正向记录在外键置空前就已失去可确定的
  所选 ID（既无快照、外键也为 `NULL`），原结论无法与当前回放对照，统一标记为
  **原选择未知 `unknown`**，页面/API 明确提示原选择的 ID 与全文无法确定，
  **不会**从候选列表推断；该类计入风险数，可单独筛选。
- **每条记录展示**：原始查询与记录号、复核时间、**原结论**（动作 + 所选地址 + 复核得分；
  所选地址已被删除时明确标注）、**当前回放**（状态、当前首选及得分、判定原因）、分类判定
  说明，以及可展开的本次回放候选明细（人工所选地址带标记）。
- **汇总与筛选**：页面顶部与 API 均返回各类数量（一致 / 冲突 / 漏命中 / 误命中 /
  原选择未知）与已复核总数；标签角标显示非一致记录数（冲突 + 漏命中 + 误命中 +
  原选择未知）。下拉框支持按类别筛选，汇总计数不受筛选影响；列表按
  冲突 → 误命中 → 漏命中 → 原选择未知 → 一致 排序，风险优先。
- **刷新即重算**：每次打开页面或点击「重新回放」都用当前地址表与默认阈值整体重算，
  便于定位规则或地址表变化带来的风险。
- **只读**：回放只调用匹配器评分，**不写入任何匹配日志**，也**不改动任何复核记录**。

```bash
# 查看回放清单（默认全部；category 可筛选 consistent|conflict|missed|false_hit|unknown）
curl -s http://localhost:8000/api/review-replay
curl -s 'http://localhost:8000/api/review-replay?category=conflict'
# => {"generated_at":"…","total_reviewed":4,"consistent":2,"conflict":1,
#     "missed":1,"false_hit":0,"unknown":0,"risk_count":2,
#     "thresholds":{"auto_threshold":0.8,...},
#     "items":[{
#       "match_id":2,"query":"100 Lake Dr, Austin TX",
#       "review_action":"overridden","review_action_label":"改派",
#       "selected_id":5,"selected_full_text":"1000 Lake Drive",
#       "selected_known":true,"selected_missing":false,"review_score":0.7,
#       "category":"conflict","category_label":"冲突",
#       "verdict_reason":"当前回放自动命中 #4（100.0%），与人工所选地址 #5 不同",
#       "replay_status":"auto_matched","replay_best_id":4,
#       "replay_best_score":1.0,"replay_best_full_text":"100 Lake Drive",
#       "replay":{"status":"auto_matched","best_id":4,"candidates":[...]}}]}
```

所选地址删除后，`selected_id` 外键会被置空，但回放仍从快照返回原 ID / 全文：

```json
{
  "review_action": "confirmed",
  "selected_id": 17,
  "selected_full_text": "555 Zxqwv Replaydel Avenue, Delton 90001",
  "selected_known": true,
  "selected_missing": true,
  "review_score": 0.91,
  "category": "missed",
  "verdict_reason": "当前回放未自动命中（状态：待确认），人工所选地址 #17 按当前规则无法自动复现；人工所选地址已删除（不在当前标准地址表中）"
}
```

历史记录若已失去可确定的所选 ID：

```json
{
  "review_action": "confirmed",
  "selected_id": null,
  "selected_full_text": null,
  "selected_known": false,
  "selected_missing": false,
  "category": "unknown",
  "category_label": "原选择未知",
  "verdict_reason": "该记录复核于所选地址快照功能上线之前，且人工所选地址已被删除，原选择的地址 ID 与全文均无法确定，无法按当前规则对照；不会从候选列表猜测原选择"
}
```

---

## API 速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 + 标准地址/待确认计数 |
| GET | `/api/addresses` | 列出标准地址 |
| POST | `/api/addresses` | JSON 批量写入标准地址 |
| POST | `/api/addresses/upload` | 上传 CSV（multipart，直接写入，跳过重复） |
| POST | `/api/addresses/preview` | 上传 CSV 生成预览凭证：有效/重复/无效数量 + 无效行号（不写入） |
| GET | `/api/addresses/preview/{token}/impact` | 导入影响预演：校验凭证（不存在/过期 404、地址表已变化 409），用有效行按正式导入同款规范化生成临时地址（CSV 行号标识），对按日志时间倒序的最近 100 个不同原始查询，分别用当前表与临时合并表沿匹配 + 复核共识逻辑重跑，按 改善(improved)/退化(regressed)/目标改派(retargeted)/不变(unchanged) 分类汇总；无效及重复行不参与，只读不写 |
| POST | `/api/addresses/confirm` | 重新提交文件 + `preview_token`，事务内校验摘要/快照后仅写入有效记录 |
| POST | `/api/addresses/load-sample` | 重新加载内置示例数据 |
| POST | `/api/match` | 单条地址匹配（评分后应用复核共识记忆，返回 `consensus` 判定） |
| POST | `/api/match/batch` | 批量地址匹配（每条独立应用复核共识记忆） |
| POST | `/api/experiments` | 提交规则 A/B 对照实验（2~4 档案 × 1~100 查询，同一快照逐档运行；非法参数/重复查询逐项报错且不执行） |
| GET | `/api/experiments` | 历史实验列表（编号、档案、查询数、各档计数与不一致数） |
| GET | `/api/experiments/{id}` | 按实验编号回看完整结果（档案、查询、快照、逐档结果与候选差异） |
| POST | `/api/experiments/compare` | 历史回归对比：两次已持久化实验按档案名 + 去空白查询对齐，逐单元分类 改善/退化/仅分数变化/无变化 并汇总；缺失档案/重复查询逐项报错（400），实验不存在 404；快照 digest 不同标记不可直接比较但仍返回逐条差异 |
| GET | `/api/matches?status=needs_review` | 匹配记录/待确认队列 |
| GET | `/api/matches/{id}` | 单条匹配详情 |
| POST | `/api/matches/{id}/review` | 单条复核：confirmed / overridden / rejected |
| GET | `/api/review-groups` | 待确认记录的同案分组（归一化键 + 候选签名归组，含组内原始查询与数量） |
| POST | `/api/review-groups/review` | 整组复核：单事务核对组签名与成员集合，冲突返回 409 且一条不写 |
| GET | `/api/review-replay?category=…` | 复核结论回放：已复核记录以原查询在当前地址表上按默认规则重跑并对照人工结论，分为 一致(consistent)/冲突(conflict)/漏命中(missed)/误命中(false_hit)/原选择未知(unknown)；原 ID、全文与复核得分取自复核写入时持久保存的地址快照（不随地址删除消失），按当前地址表标注是否已删除；只读不写 |
| GET | `/api/coverage-gaps?status=open` | 地址覆盖缺口：`no_match` 日志按归一化道路/地区词元 + 门牌号归组，每次调用用当前地址表与默认规则重跑代表查询，标记 待补录(open)/已覆盖(covered)；只读不写 |
| GET | `/api/address-audit?category=…` | 标准地址自匹配巡检：每条标准地址以其全文按默认规则在完整地址表上评分，只列出未自动命中自身 ID 的记录，含自身分数与得分 ≥ 自身分数 − 默认分差阈值的全部阻碍候选（不受前五截断），按 自身分数不足(self_score_low)/首选偏移(best_drifted)/分差歧义(margin_ambiguous) 分类汇总；只读不写 |

匹配结果示例（节选）：

```json
{
  "query": "1010 Lake Dr, Austin TX",
  "status": "needs_review",
  "reason": "最高得分低于自动确认阈值",
  "best_id": 5,
  "best_score": 0.786,
  "runner_up_id": 4,
  "runner_up_score": 0.7,
  "candidates": [
    {
      "address": {"id": 5, "full_text": "1000 Lake Drive"},
      "score": 0.786,
      "street_f1": 1.0,
      "locality_f1": 1.0,
      "house_score": 0.288,
      "matched_tokens": [{"query": "lake", "candidate": "lake", "similarity": 1.0}],
      "conflicts": [
        {"field": "house_number", "query": "1010", "candidate": "1000",
         "similarity": 0.288, "message": "门牌号不一致"}
      ]
    }
  ]
}
```

---

## 项目结构

```
.
├── app/
│   ├── main.py            # FastAPI 路由（网页 + REST API）
│   ├── config.py          # 阈值/路径等环境变量配置
│   ├── db.py              # SQLite 表结构与读写
│   ├── normalization.py   # Unicode/缩写/门牌号归一化与分词
│   ├── matcher.py         # 词元与门牌号相似度、候选判定
│   ├── experiments.py     # 规则 A/B 对照实验：逐档运行、差异分析与持久化；历史回归对比（对齐/分类/汇总）
│   ├── consensus.py       # 复核共识记忆：历史复核聚合与自动指向
│   ├── review_groups.py   # 同案合并复核：归组与整组事务写入
│   ├── review_replay.py   # 复核结论回放：已复核记录按当前表/默认规则重跑并对照分类（只读）
│   ├── coverage_gaps.py   # 地址覆盖缺口：no_match 日志归组 + 当前表/默认规则实时重跑（只读）
│   ├── address_audit.py   # 标准地址自匹配巡检：每条记录以自身全文重评全表，列出未命中自身的风险（只读）
│   ├── ingest.py          # CSV / JSON 标准地址导入（含预览凭证与确认事务）
│   ├── import_impact.py   # 导入影响预演：有效待导入行对最近日志查询的内存干跑（只读）
│   ├── schemas.py         # Pydantic 模型
│   ├── templates/         # 网页（Jinja2，单页）
│   └── static/            # 前端 CSS / JS（原生 JS，无构建步骤）
├── sample_data/
│   └── standard_addresses.csv
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 运行测试

```bash
pip install -r requirements.txt
python -m pytest -q
```
