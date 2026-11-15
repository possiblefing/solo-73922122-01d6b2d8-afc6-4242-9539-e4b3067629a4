# 洪水受灾耕地评估与灾后瓦片汇交服务

两部分能力：

1. **受灾评估（原有）**：将洪水分类栅格（淹水 / 未淹 / 未知）按 **2~4 组候选整幅平移**
   叠加到 **1~8 个耕地地块** 上，按地块与像元的**真实相交面积**计算受淹占比上下界，
   给出「确定达标 / 确定未达 / 待核」判定。
2. **瓦片汇交（新增）**：登记航次清单，逐片上传 JSON 分类矩阵原件，校验铺满、
   重叠一致与 SHA-256，收齐后发布；发布的拼接网格可直接送 `/assess` 判读。

技术栈：Python 3.12 · FastAPI · Pydantic v2 · Uvicorn · SQLite(WAL) · Docker Compose。
几何计算为纯 Python 实现（鞋带公式 + Sutherland–Hodgman 矩形裁剪），无重型 GIS 依赖。

## 快速开始（双实例 + 共享卷）

```bash
docker compose up --build
```

启动两个对等实例，共用同一命名卷（SQLite 状态 + 原件/网格文件）：

- 实例 A（Swagger UI）: <http://localhost:8001/docs>
- 实例 B（Swagger UI）: <http://localhost:8002/docs>
- OpenAPI：`/openapi.json`；健康检查：`/healthz`

## 汇交流程与可靠性约定

```
登记航次 PUT /submissions/{flight_id}
   │  航次号、网格原点、像元边长、≤32×32 行列、≤4 片（片号/左闭右开窗口/原件 SHA-256）
   │  窗口必须铺满目标网格；重叠允许，但重叠像元分类必须一致
   ▼
逐片上传 PUT /submissions/{flight_id}/tiles/{tile_id}      （失败可重传）
   │  原件 = JSON 分类矩阵原始字节；服务端重算 SHA-256，与登记值强校验
   │  同片同原文重试 → 幂等复用；同片异原文 → 409 拒绝
   │  重叠处分类不一致 → 该航次永久 blocked，不能发布
   ▼
查缺片/状态 GET /submissions/{flight_id}
   ▼
发布 POST /submissions/{flight_id}/publish
   │  未收齐 / 有冲突 / 共享卷缺原件 → 422 拒绝；发布幂等
   │  拼接网格先 fsync 落盘，再提交 published 状态（杜绝「已发布却缺文件」）
   ▼
下载原件 GET .../tiles/{tile_id}/original；下载拼接网格 GET .../grid（可直送 /assess）
发布后清单与瓦片不可改（同原文重传除外，内容零变化）
```

持久化与并发：

- **状态**：SQLite，WAL 模式 + `synchronous=FULL` + `BEGIN IMMEDIATE` 写事务，
  双实例并发写串行化，忙等待 `busy_timeout=10s`；
- **原件**：共享卷内容寻址存储 `objects/<sha256前两位>/<sha256>`，
  「临时文件 + fsync + 原子 rename + 目录 fsync」，同内容去重复用，已确认原件不丢；
- **重启续交**：进程退出后重启，SQLite 与共享卷均在，可继续上传/发布；
  启动时执行对账（`Storage.reconcile`），核对已收原件与已发布网格是否齐全；
- 状态机：`collecting`（收集中）→ `published`（已发布）；
  一旦出现重叠分类冲突即转 `blocked`（永久不可发布，需重新登记新航次）。

## 汇交接口

### `PUT /submissions/{flight_id}` 登记（同号同清单幂等，异内容 409）

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `flight_id` | 1~64 位 `[A-Za-z0-9._-]` | 航次号，路径与请求体须一致 |
| `origin` | `[x, y]` 有限浮点 | 网格左上角（西北角）米制坐标 |
| `cell_size` | 正整数 | 像元边长（米） |
| `grid_rows` / `grid_cols` | 1~32 | 目标网格行列数 |
| `tiles` | 1~4 片 | 每片：`tile_id`、`[row_start,row_end)`×`[col_start,col_end)` 左闭右开整数窗口、`expected_sha256` |

窗口须完全落在目标网格内且**铺满全部像元**；片号同航次唯一。

### `PUT /submissions/{flight_id}/tiles/{tile_id}` 上传原件

请求体即原件原始字节（`application/json`）：`{"rows": [["flooded", ...], ...]}`，
沿用 `flooded / dry / unknown` 三类值；矩阵形状必须与登记窗口一致。
服务端对**原始字节**重算 SHA-256 并与登记值比对。返回 `reused` 标识是否命中幂等复用。

### 其余接口

| 方法/路径 | 说明 |
| --- | --- |
| `GET /submissions` | 列出全部航次及状态 |
| `GET /submissions/{flight_id}` | 状态、逐片状态、`missing_tiles` 缺片、`conflict_tiles` 冲突片 |
| `POST /submissions/{flight_id}/publish` | 收齐且校验通过后发布（幂等） |
| `GET /submissions/{flight_id}/tiles/{tile_id}/original` | 下载已收录原件（字节级一致） |
| `GET /submissions/{flight_id}/grid` | 发布后下载拼接网格 `{origin, cell_size, rows}` |

`grid` 响应体与 `POST /assess` 的 `grid` 入参形状完全一致，可直接嵌入评估请求。

### 端到端示例

```bash
# 1) 登记（4x4，左右两片无重叠铺满）
SHA_L=$(printf '{"rows":[["flooded","dry"],["flooded","dry"],["flooded","dry"],["flooded","dry"]]}' | sha256sum | cut -d' ' -f1)
SHA_R=$(printf '{"rows":[["dry","unknown"],["dry","unknown"],["dry","unknown"],["dry","unknown"]]}' | sha256sum | cut -d' ' -f1)
curl -X PUT http://localhost:8001/submissions/FLIGHT-1 -H 'Content-Type: application/json' -d "{
  \"flight_id\":\"FLIGHT-1\",\"origin\":[500000.0,3456040.0],\"cell_size\":10,
  \"grid_rows\":4,\"grid_cols\":4,
  \"tiles\":[
    {\"tile_id\":\"L\",\"row_start\":0,\"row_end\":4,\"col_start\":0,\"col_end\":2,\"expected_sha256\":\"$SHA_L\"},
    {\"tile_id\":\"R\",\"row_start\":0,\"row_end\":4,\"col_start\":2,\"col_end\":4,\"expected_sha256\":\"$SHA_R\"}]}"

# 2) 逐片上传（失败重传同一原文即可，幂等）
printf '{"rows":[["flooded","dry"],["flooded","dry"],["flooded","dry"],["flooded","dry"]]}' \
  | curl -X PUT http://localhost:8001/submissions/FLIGHT-1/tiles/L --data-binary @- -H 'Content-Type: application/json'
printf '{"rows":[["dry","unknown"],["dry","unknown"],["dry","unknown"],["dry","unknown"]]}' \
  | curl -X PUT http://localhost:8002/submissions/FLIGHT-1/tiles/R --data-binary @- -H 'Content-Type: application/json'

# 3) 发布并取拼接网格
curl -X POST http://localhost:8001/submissions/FLIGHT-1/publish
curl http://localhost:8002/submissions/FLIGHT-1/grid
```

## 评估接口 `POST /assess`（原有能力，未改动语义）

请求体：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `grid.origin` | `[x, y]` | 有限浮点 | 网格**左上角（西北角）**米制坐标 |
| `grid.cell_size` | int | 正整数 | 像元边长（米） |
| `grid.rows` | string[][] | 1~32 行 × 1~32 列，矩形 | `flooded` / `dry` / `unknown`；`rows[0]` 为最北一行 |
| `translations` | object[] | 2~4 组 | 候选整幅平移量 `{dx, dy}`（米） |
| `parcels` | object[] | 1~8 个 | 地块：`id` + `vertices`（3~16 顶点，首尾可重复闭合） |
| `threshold` | float | 0~1 | 受淹占比阈值 |

坐标约定：y 轴向北为正；像元 `(r, c)` 覆盖
`x ∈ [ox + c·s, ox + (c+1)·s]`，`y ∈ [oy − (r+1)·s, oy − r·s]`。

计算规则：每组平移独立计算（下界=淹水像元真实相交面积，上界=下界+未知像元相交面积）；
全局下界取各组最小值、上界取最大值；下界占比 ≥ 阈值 → `confirmed`，
上界占比 < 阈值 → `not_met`，其余 → `review`。任一地块非法则整次请求 422 拒绝。

## 本地开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r app/requirements.txt pytest httpx
python -m pytest tests/ -q          # 47 项单元 / 集成 / 双进程重启测试
FLOOD_DATA_DIR=./.data uvicorn main:app --reload --app-dir app
```

测试覆盖要点：登记约束与幂等、SHA-256/形状/取值校验、同原文复用与异内容拒绝、
重叠一致与冲突 blocked、缺片不可发布、发布幂等与发布后不可改、原件/网格下载、
拼接网格直送 `/assess`、线程级并发上传、双 uvicorn 进程共享卷并发发布与退出重启续交。

> 测试/沙箱钩子：设置环境变量 `FLOOD_ENABLE_SHUTDOWN_HOOK=1` 会注册
> `POST /shutdown`（不显示在 OpenAPI），供禁止进程信号的环境让实例自行退出；
> 生产 Compose 不设置该变量，钩子不存在。

## 项目结构

```
├── docker-compose.yml      # 双实例 api-a(8001)/api-b(8002) + 共享命名卷 flood-shared
├── app/
│   ├── Dockerfile          # python:3.12-slim，FLOOD_DATA_DIR=/data
│   ├── requirements.txt
│   ├── main.py             # FastAPI 入口：/docs、/assess、/submissions/*、/healthz
│   ├── models.py           # 评估请求/响应模型与约束（Pydantic v2）
│   ├── ingest_models.py    # 汇交请求/响应模型与约束
│   ├── service.py          # 评估核心：逐组上下界、汇总、判定、地块校验
│   ├── storage.py          # SQLite(WAL) 状态机 + 共享卷内容寻址原件 + 窗口拼接/一致性
│   └── geometry.py         # 纯 Python 几何：面积、简单多边形校验、矩形裁剪
└── tests/
    ├── conftest.py
    ├── test_assessment.py  # 原有评估测试（31 项）
    └── test_ingest.py      # 汇交测试（16 项，含双进程重启续交）
```
