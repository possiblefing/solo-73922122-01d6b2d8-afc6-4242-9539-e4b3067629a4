# 洪水受灾耕地评估服务

灾后核损场景：将洪水分类栅格（淹水 / 未淹 / 未知）按 **2~4 组候选整幅平移** 叠加到
**1~8 个耕地地块** 上，按地块与像元的**真实相交面积**计算受淹占比上下界，
给出「确定达标 / 确定未达 / 待核」判定，筛出需要现场复核的地块。

技术栈：Python 3.12 · FastAPI · Pydantic v2 · Uvicorn · Docker Compose。
几何计算为纯 Python 实现（鞋带公式 + Sutherland–Hodgman 矩形裁剪），无重型 GIS 依赖。

## 快速开始

```bash
docker compose up --build
```

启动后访问交互式文档（可直接在线调用）：

- Swagger UI: <http://localhost:8000/docs>
- OpenAPI 规格: <http://localhost:8000/openapi.json>
- 健康检查: <http://localhost:8000/healthz>

## 接口

### `POST /assess`

请求体：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `grid.origin` | `[x, y]` | 有限浮点 | 网格**左上角（西北角）**米制坐标 |
| `grid.cell_size` | int | 正整数 | 像元边长（米） |
| `grid.rows` | string[][] | 1~32 行 × 1~32 列，矩形 | 像元值：`flooded` 淹水 / `dry` 未淹 / `unknown` 未知；`rows[0]` 为**最北一行** |
| `translations` | object[] | 2~4 组 | 候选整幅平移量 `{dx, dy}`（米），平移后原点为 `(ox+dx, oy+dy)` |
| `parcels` | object[] | 1~8 个 | 地块：`id` + `vertices`（3~16 顶点，首尾可重复闭合） |
| `threshold` | float | 0~1 | 受淹占比阈值 |

坐标约定：y 轴向北为正；像元 `(r, c)` 覆盖
`x ∈ [ox + c·s, ox + (c+1)·s]`，`y ∈ [oy − (r+1)·s, oy − r·s]`。

响应（逐地块）：

| 字段 | 说明 |
| --- | --- |
| `area` | 地块面积（平方米） |
| `flooded_area_lower / _upper` | 受淹面积全局下界 / 上界（平方米） |
| `ratio_lower / ratio_upper` | 受淹占比下界 / 上界 |
| `verdict` | `confirmed` 确定达标 / `not_met` 确定未达 / `review` 待核 |
| `translations[]` | 逐平移组明细：`flooded_area`（该组下界）、`potential_area`（该组上界） |

### 计算规则

1. **每组平移独立计算**：下界 = 地块与淹水像元真实相交面积之和；
   上界 = 下界 + 地块与**未知**像元相交面积（未知像元只计入上界）。
2. **全局汇总**：下界取各组最小值，上界取各组最大值；
   不跨平移组拼接像元（每组明细单独返回，可审计）。
3. **判定**：下界占比 ≥ 阈值 → `confirmed`；上界占比 < 阈值 → `not_met`；
   其余 → `review`（转现场复核）。
4. **地块合法性**：各地块须为无洞简单多边形（无自交、无重复顶点、面积非零、
   3~16 顶点），且**位于所有候选平移后的网格范围内**。
   任一地块非法 → 整次请求以 `422` 拒绝，不返回部分结果。

### 调用示例

```bash
curl -X POST http://localhost:8000/assess \
  -H 'Content-Type: application/json' \
  -d '{
    "grid": {
      "origin": [500000.0, 3456020.0],
      "cell_size": 10,
      "rows": [["flooded", "unknown"], ["dry", "flooded"]]
    },
    "translations": [{"dx": 0.0, "dy": 0.0}, {"dx": 2.0, "dy": 1.0}],
    "parcels": [{
      "id": "A-01",
      "vertices": [[500010.0, 3456002.0], [500020.0, 3456002.0],
                   [500020.0, 3456012.0], [500010.0, 3456012.0]]
    }],
    "threshold": 0.8
  }'
```

响应摘录：

```json
{
  "threshold": 0.8,
  "parcels": [{
    "id": "A-01",
    "area": 100.0,
    "flooded_area_lower": 74.0,
    "flooded_area_upper": 100.0,
    "ratio_lower": 0.74,
    "ratio_upper": 1.0,
    "verdict": "review",
    "translations": [
      {"index": 0, "dx": 0.0, "dy": 0.0, "flooded_area": 80.0, "potential_area": 100.0},
      {"index": 1, "dx": 2.0, "dy": 1.0, "flooded_area": 74.0, "potential_area": 82.0}
    ]
  }]
}
```

该地块占比真值介于 74%~100% 之间，阈值 80% 落在区间内 → `review`，转现场核损。

## 本地开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r app/requirements.txt pytest httpx
python -m pytest tests/ -q          # 31 项单元 / 集成测试
uvicorn main:app --reload --app-dir app
```

## 项目结构

```
├── docker-compose.yml      # 一键启动（构建 + 端口映射 + 健康检查）
├── app/
│   ├── Dockerfile          # python:3.12-slim 镜像
│   ├── requirements.txt
│   ├── main.py             # FastAPI 入口（/docs、/assess、/healthz）
│   ├── models.py           # 请求 / 响应模型与约束（Pydantic v2）
│   ├── service.py          # 评估核心：逐组上下界、汇总、判定、地块校验
│   └── geometry.py         # 纯 Python 几何：面积、简单多边形校验、矩形裁剪
└── tests/
    └── test_assessment.py  # 手算数值核对 + 判定分支 + 非法输入整次拒绝
```
