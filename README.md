# 林芯树轮年表 API

FastAPI + SQLite 的树轮年表服务：按树号、芯号报送逐年**整数**轮宽指数，按年份查询
参考值，并可在查询时**临时剔除**一棵待核树。

## 快速开始（Docker Compose）

```bash
docker compose up --build -d
open http://127.0.0.1:8000/docs        # Linux 用 xdg-open
docker compose logs -f api
docker compose down                    # 加 -v 连数据卷一起删
```

数据位于命名卷 `treering-data` 的 `/data/treering/chronology.sqlite3`（WAL 模式）。
服务以非 root 用户运行，`/health` 同时用作 Compose healthcheck。

本地不装 Docker 时可直接跑：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
TREERING_DB=./chronology.sqlite3 uvicorn app.main:app --port 8000
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/reports` | 报送/重报若干根芯的逐年指数，**整根替换**，单事务 |
| GET | `/chronology/{year}?exclude_tree=N` | 查参考值，可临时剔除树 N |
| POST | `/chronology/query` | 同上，JSON 请求体 `{"year":…, "exclude_tree":…}` |
| GET | `/common-sample?start_year=…&end_year=…&min_trees=…` | 共同样本：范围内最长连续年段 |
| POST | `/common-sample` | 同上，JSON 请求体 |
| GET | `/cores/{tree}/{core}` | 查看单根芯当前已提交的序列和修订号 |
| GET | `/trees` | 已报送的树号 |
| GET | `/health` | 健康检查 |

报送示例（`years` 的键在 JSON 里是字符串，服务端转成整数年份）：

```bash
curl -X POST http://127.0.0.1:8000/reports -H 'Content-Type: application/json' -d '{
  "cores": [
    {"tree_no": 1, "core_no": 1, "years": {"1975": 90, "1976": 92}},
    {"tree_no": 1, "core_no": 2, "years": {"1975": 96, "1976": 94}}
  ]
}'
```

**整根替换语义**：同一 `(tree_no, core_no)` 再次报送时，旧序列整体丢弃；
新序列里没有的年份，该芯就退出那些年份的计算；新序列为空对象则该芯不再有任何年份数据（芯记录保留、修订号 +1，序列为空）。

## 计算规则

1. 当年样本树：该年至少有一根已报芯有值的树。剔除树只在该次查询中生效；
   若被剔除的树当年本来就没有数据，则不计入剔除（响应里 `excluded_tree_present=false`）。
2. 株内中位数：对该树当年**所有有值的芯**取中位数（偶数个芯时为两中值平均）。
3. 参考值：对所有样本树的株内中位数再取中位数（偶数棵时为两中值平均）。
4. 样本树数 = 剔除后实际参与计算的树数；≥ 3 才返回精确参考值
   （`status=ok`、`enough_samples=true`），否则 `status=insufficient_samples`、
   `value=null`，仍返回 `sample_trees` 供判断。
5. 全程用整数做精确运算：`value_x4` 是参考值的 4 倍整数，`value = value_x4/4`
   （粒度 0.25），避免浮点误差。

## 共同样本查询（同一批树比较历年轮宽）

`GET /common-sample?start_year=1950&end_year=2000&min_trees=5`
（或 `POST /common-sample`，请求体 `{"start_year":1950,"end_year":2000,"min_trees":5}`）

在 `[start_year, end_year]`（**两端均含**）内找**最长连续年段** `[s, e]`，使得
**至少有 `min_trees` 棵树在该段每一年的有值**；一棵树只有在整个 `[s, e]`
每年都有值才算“完整覆盖”，才出现在返回的 `trees` 里。

* **有值**：该树当年只要有任一根已报芯报了指数就算有值，**零值也计入**；
  某年所有芯都没有值即为断档。
* **并列**：年段同长时取**起始年较早**的一段。
* **无解**：范围内不存在这样的年段时，返回 HTTP 404，响应体仍为完整结构：
  `status="no_solution"`、`found=false`、`segment_start/segment_end=null`、
  `segment_length=0`、`trees=[]`，并附带 `data_version`。
* **同快照**：`segment_*`、`trees` 与 `data_version` 都取自同一个已提交读事务。

示例响应：

```json
{
  "status": "ok",
  "found": true,
  "start_year": 1950, "end_year": 2000, "min_trees": 5,
  "segment_start": 1962, "segment_end": 1988, "segment_length": 27,
  "tree_count": 7, "trees": [3, 4, 7, 11, 12, 19, 25],
  "data_version": 42,
  "read_cost": {"rows_read": 1841, "statements": 2, "slot_probes": 0}
}
```

### 索引与复杂度

写入时按树维护 `tree_span(tree_no, start_year, end_year)`：每棵树每段**极大连续有值年段**一行
（不是每年一行）。整根重报时只重算受影响的树（同一事务内），并从 `tree_year` 派生，
所以一棵树有 1000 年连续数据也只占 1 行。

查询用一次按 `start_year` 的**流式扫描**（覆盖索引，按序号顺序，不排序、不逐年展开），
维护一个大小恰为 `min_trees` 的最小堆：扫描到候选起点 `s` 时，堆顶就是“至少有
`min_trees` 棵树能到的最远年份”，即该起点的最佳终点。随后用区间刺探索引
（`tree_span_by_end`）一次性取出完整覆盖该段的树号。

* 读取行数只随**区间数**增长，不随 `年数 × 树数` 增长；不逐年展开。
* 除返回值外的工作内存只随 `min_trees` 增长（堆 + 常数）。
* 实测（5,000 棵树、约 16.3 万个区间、200 年窗口）：

  | min_trees | 读取索引行数 | 峰值额外内存 | 结果 |
  | --- | --- | --- | --- |
  | 1 | 163,083 | 4 KB | 57 年段、1 棵完整覆盖树 |
  | 5,000 | 163,082 | 201 KB | 无解 |

  查询连接由 SQLite authorizer 限制为**只能读** `tree_span` 与 `meta`；
  `tree_year`/`year_stat`/`year_value_cum` 以及原始 `measurement`/`core`
  在该连接上直接报 "access ... is prohibited"。

### 已有数据卷升级

`init_db` 启动时若发现 `meta.span_index_version` 缺失，会在一个事务里从
`tree_year` 回填 `tree_span` 并写入标记；旧的命名卷 `docker compose up` 后
无需手工迁移即可直接查询。回填中断（崩溃）则下次启动重试，不会留下半成品。

## 为什么查询是次线性的

写入时维护三张按年聚合的表（`tree_year`、`year_value_cum`、`year_stat`）：

* `tree_year(year, tree_no)` 存该树当年的株内中位数（二倍整数，保证偶数芯也精确）；
* `year_value_cum(year, slot, value_x2, cnt, cum)` 是当年株内中位数的**累计直方图**：
  每个不同取值一行，`cum` 为 ≤ 该值的树数；
* `year_stat(year, tree_count, slot_count)` 存当年树数和直方图行数。

查询只做：

1. 读 `year_stat` 一行；
2. 若带 `exclude_tree`，按主键读 `tree_year` 一行拿到该树中位数；
3. 在累计直方图上二分做 1～2 次次序统计（中位数），每次 O(log 不同取值数) 次主键点查；
   剔除的那棵树只在读到的 `cum` 上做 ±1 修正，**不为任何树预存剔除后的参考值**。

所以一次查询读取的行数是 `O(log 当年不同中位值数)`，与样本树数次线性；
且查询连接由 SQLite authorizer 限制为**只能读**上述聚合表，
`measurement`/`core` 原始取芯表在查询连接上直接报 "access ... is prohibited"。
节点上全部是主键/索引 SEARCH（测试用 `EXPLAIN QUERY PLAN` 断言无 SCAN）。
响应里的 `read_cost.rows_read / statements / slot_probes` 是本次查询实际读的行数等，
可以直接观察。

实测（单年，全部树各 1 芯）：

| 样本树数 | 不同中位值 | rows_read（不剔除 / 剔除） | slot_probes |
| --- | --- | --- | --- |
| 100 | 100 | 17 / 12 | 15 / 9 |
| 1,000 | 1,000 | 23 / 14 | 21 / 11 |
| 10,000 | 10,000 | 31 / 18 | 29 / 15 |
| 50,000 | 50,000 | 35 / 20 | 33 / 17 |

## 一致性与失败回滚

* 报送的取芯明细和年表聚合在**同一个** `BEGIN IMMEDIATE` 事务里改完才 COMMIT；
  失败（含校验失败、注入的异常、磁盘错误）整体 ROLLBACK，旧数据原样保留。
* 读连接使用只读 URI + 显式 `BEGIN`（deferred）读事务，一次请求只看到一个已提交快照；
  写事务进行中或提交瞬间都不会读到半更新的年表。
* WAL 模式允许读不阻塞写；`busy_timeout` + 指数退避只对 `SQLITE_BUSY` 重试。
* `meta.data_version` 每次成功报送 +1 并在响应中返回，可用来判断两次查询之间是否有写入。

并发/回滚/快照一致性由 `tests/test_concurrency.py` 与
`tests/test_common_sample.py` 覆盖。

## 测试与试跑

```bash
pip install -r requirements-dev.txt
pytest -q                                     # 45 个用例
bash scripts/smoke.sh http://127.0.0.1:8000    # 需要已启动的服务
python scripts/demo.py http://127.0.0.1:8000   # 打印完整示例响应
```

测试覆盖：中位数（奇数/偶数、.5 精度）、逐树剔除、不足 3 树、整根替换丢年份、
被剔除树当年无数据、校验失败、注入异常回滚、并发报送后聚合与原始明细一致、
读期间快照版本一致、真实随机数据与暴力解对拍、原始表在查询连接上不可读、
只读表结构中没有“逐树剔除后答案”列、查询计划无 SCAN、读取行数次线性。

共同样本另有：最长年段与并列取早起、零值计入、任一芯有值即算覆盖、
无解显式返回、整根重报后年段分裂、仅重算受影响树的索引、索引随报送事务回滚、
旧数据卷自动回填、共同样本连接不可读年表/原始表、随机数据与暴力解对拍、
读取行数随区间数而非年数×树数增长、额外内存随 `min_trees` 增长。

## 从 /docs 试查

1. 打开 `http://127.0.0.1:8000/docs`。
2. 展开 `POST /reports` → *Try it out*，粘贴上面的报送示例 → *Execute*（201）。
3. 展开 `GET /chronology/{year}` → year 填 `1975` → *Execute*，记下 `value`。
4. 同一接口 `exclude_tree` 填 `4` → *Execute*，对比 `sample_trees`、`value`、
   `excluded_tree_present` 和 `read_cost`。
5. 再次 `POST /reports` 只报 `{"tree_no":3,"core_no":1,"years":{"1975":121}}`（缺 1976），
   然后查 `1976`：该芯 1976 的数据已被替换掉。
6. 把 `exclude_tree` 调到只剩 2 棵样本树，观察 `insufficient_samples`。
7. 展开 `GET /common-sample` → `start_year=1975`、`end_year=1976`、`min_trees=2`
   → *Execute*，记下 `segment_*` 与 `trees`。
8. 再次 `POST /reports` 整根重报一棵树、去掉某一年（如
   `{"tree_no":4,"core_no":1,"years":{"1975":140}}`），重复第 7 步：
   共同样本年段/树号随之变化，`data_version` 递增。
