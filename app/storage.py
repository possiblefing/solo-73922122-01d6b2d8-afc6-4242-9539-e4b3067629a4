"""汇交持久化与瓦片拼接核心。

设计要点（与灾后遥感汇交需求一一对应）：
- 状态存 SQLite（WAL 模式 + BEGIN IMMEDIATE 写事务），两个实例并发安全；
- 原件存共享卷内容寻址存储（路径 = objects/sha256 前两位/全哈希），
  写盘采用「临时文件 + fsync + 原子 rename + 目录 fsync」，
  已确认（返回 2xx）的原件不会丢失，且同内容天然去重、可复用；
- 每片登记一个左闭右开整数窗口，上传矩阵形状必须与窗口一致，
  重叠像元分类必须一致，冲突永久标记且禁止发布；
- 发布前强制复核：片齐、无冲突、窗口铺满、重叠一致、原件文件齐全；
  拼接网格原子落盘后再置 published，杜绝「已发布却缺文件」；
- 发布后清单与瓦片不可改；同号同清单、同片同原文重试幂等复用，异内容拒绝。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

# 单实例约束
MAX_TILES = 4
MAX_GRID_ROWS = 32
MAX_GRID_COLS = 32
# 像元三分类（沿用 /assess 的三类值）
CELL_VALUES = ("flooded", "dry", "unknown")

SCHEMA = """
CREATE TABLE IF NOT EXISTS manifests (
    flight_id        TEXT PRIMARY KEY,
    origin_x         REAL NOT NULL,
    origin_y         REAL NOT NULL,
    cell_size        INTEGER NOT NULL,
    grid_rows        INTEGER NOT NULL,
    grid_cols        INTEGER NOT NULL,
    tile_count       INTEGER NOT NULL,
    manifest_json    TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('collecting','blocked','published')),
    grid_object_sha  TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    published_at     TEXT
);
CREATE TABLE IF NOT EXISTS tiles (
    flight_id        TEXT NOT NULL,
    tile_id          TEXT NOT NULL,
    row_start        INTEGER NOT NULL,
    row_end          INTEGER NOT NULL,
    col_start        INTEGER NOT NULL,
    col_end          INTEGER NOT NULL,
    expected_sha256  TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('pending','received','conflict')),
    content_sha256   TEXT,
    matrix_json      TEXT,
    updated_at       TEXT,
    PRIMARY KEY (flight_id, tile_id),
    FOREIGN KEY (flight_id) REFERENCES manifests(flight_id)
);
"""


class StorageError(Exception):
    """存储层业务错误，message 可直接面向调用方。"""


class NotFound(StorageError):
    """航次 / 瓦片不存在。"""


class Conflict(StorageError):
    """与既有状态冲突（异内容重传、发布后修改、状态不允许等）。"""


class Unprocessable(StorageError):
    """语义校验失败（窗口不铺满、矩阵形状不符、重叠不一致、缺片等）。"""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class Storage:
    """SQLite + 共享卷原件存储。线程安全（连接每次新建，关闭即归还）。"""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = os.path.abspath(data_dir)
        self.db_path = os.path.join(self.data_dir, "flood_ingest.sqlite3")
        self.objects_dir = os.path.join(self.data_dir, "objects")
        self.grids_dir = os.path.join(self.data_dir, "grids")
        self._init_lock = threading.Lock()
        self._initialized = False

    # ------------------------------------------------------------ 初始化
    def init(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            os.makedirs(self.objects_dir, exist_ok=True)
            os.makedirs(self.grids_dir, exist_ok=True)
            # 确保目录元数据落盘
            self._fsync_dir(self.data_dir)
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(SCHEMA)
                conn.commit()
            finally:
                conn.close()
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(self.data_dir, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """立即写事务：并发写串行化，锁等待由 busy_timeout 兜底。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _fsync_dir(path: str) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    # ------------------------------------------------------------ 原件对象存储
    def _object_path(self, sha256: str) -> str:
        return os.path.join(self.objects_dir, sha256[:2], sha256)

    def object_exists(self, sha256: str) -> bool:
        return os.path.isfile(self._object_path(sha256))

    def put_object(self, payload: bytes, expected_sha256: str | None = None) -> str:
        """内容寻址写入原件；已存在则复用。返回 sha256。"""
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise Unprocessable(
                f"原件 SHA-256 校验不符：实际 {digest}，登记期望 {expected_sha256}"
            )
        final = self._object_path(digest)
        if os.path.isfile(final):
            return digest  # 同内容幂等复用
        os.makedirs(os.path.dirname(final), exist_ok=True)
        tmp = f"{final}.tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)  # 原子改名
        self._fsync_dir(os.path.dirname(final))
        return digest

    def get_object_path(self, sha256: str) -> str:
        path = self._object_path(sha256)
        if not os.path.isfile(path):
            raise NotFound(f"原件 {sha256} 在共享卷中缺失")
        return path

    def _grid_path(self, flight_id: str) -> str:
        return os.path.join(self.grids_dir, f"{flight_id}.grid.json")

    # ------------------------------------------------------------ 纯函数校验
    @staticmethod
    def validate_windows(
        tiles: list[dict[str, Any]], grid_rows: int, grid_cols: int
    ) -> None:
        """校验窗口：边界合法、片号唯一、窗口铺满目标（重叠允许，须在上传/发布时一致）。"""
        if not (1 <= grid_rows <= MAX_GRID_ROWS and 1 <= grid_cols <= MAX_GRID_COLS):
            raise Unprocessable("目标网格行列数须在 1~32 之间")
        if not (1 <= len(tiles) <= MAX_TILES):
            raise Unprocessable("片数须在 1~4 之间")
        seen: set[str] = set()
        covered = [[False] * grid_cols for _ in range(grid_rows)]
        for t in tiles:
            tid = t["tile_id"]
            if tid in seen:
                raise Unprocessable(f"片号 {tid!r} 重复")
            seen.add(tid)
            rs, re_, cs, ce = (
                t["row_start"], t["row_end"], t["col_start"], t["col_end"]
            )
            if not (0 <= rs < re_ <= grid_rows and 0 <= cs < ce <= grid_cols):
                raise Unprocessable(
                    f"片 {tid!r} 窗口 [r{rs}:{re_}, c{cs}:{ce}] 越界或非左闭右开"
                )
            for r in range(rs, re_):
                for c in range(cs, ce):
                    covered[r][c] = True
        missing = [
            [r, c]
            for r in range(grid_rows)
            for c in range(grid_cols)
            if not covered[r][c]
        ]
        if missing:
            raise Unprocessable(
                f"窗口未铺满目标网格，缺 {len(missing)} 个像元，示例：{missing[:8]}"
            )

    @staticmethod
    def validate_matrix(
        rows: list[list[str]],
        row_start: int,
        row_end: int,
        col_start: int,
        col_end: int,
    ) -> None:
        h, w = row_end - row_start, col_end - col_start
        if len(rows) != h or any(len(r) != w for r in rows):
            raise Unprocessable(
                f"矩阵形状 {len(rows)} 行与窗口 {h}×{w} 不一致"
            )
        for r in rows:
            for v in r:
                if v not in CELL_VALUES:
                    raise Unprocessable(f"非法分类值 {v!r}，仅允许 {CELL_VALUES}")

    @staticmethod
    def stitch(
        grid_rows: int, grid_cols: int, tiles: list[dict[str, Any]]
    ) -> tuple[list[list[str | None]], list[dict[str, Any]]]:
        """把已收齐的片矩阵拼回整幅网格，同时检查重叠一致性。

        tiles 元素需含 row_start/row_end/col_start/col_end/matrix/tile_id。
        返回 (整幅网格（含 None 表示缺像元）, 冲突明细列表)。
        """
        grid: list[list[str | None]] = [
            [None] * grid_cols for _ in range(grid_rows)
        ]
        conflicts: list[dict[str, Any]] = []
        for t in tiles:
            matrix = t["matrix"]
            for i, r in enumerate(range(t["row_start"], t["row_end"])):
                for j, c in enumerate(range(t["col_start"], t["col_end"])):
                    val = matrix[i][j]
                    if grid[r][c] is None:
                        grid[r][c] = val
                    elif grid[r][c] != val:
                        conflicts.append(
                            {
                                "row": r,
                                "col": c,
                                "existing": grid[r][c],
                                "incoming": val,
                                "tile_id": t["tile_id"],
                            }
                        )
        return grid, conflicts

    # ------------------------------------------------------------ 登记
    def register(self, req: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """登记航次清单。返回 (航次状态视图, 是否新建)。"""
        flight_id = req["flight_id"]
        tiles = req["tiles"]
        grid_rows, grid_cols = req["grid_rows"], req["grid_cols"]
        self.validate_windows(tiles, grid_rows, grid_cols)
        canonical = json.dumps(
            req, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        with self.tx() as conn:
            row = conn.execute(
                "SELECT * FROM manifests WHERE flight_id=?", (flight_id,)
            ).fetchone()
            if row is not None:
                if row["manifest_json"] != canonical:
                    raise Conflict(
                        f"航次 {flight_id} 已登记且清单不一致，异内容登记被拒绝"
                    )
                if row["status"] == "published":
                    raise Conflict(f"航次 {flight_id} 已发布，清单不可修改")
                return self._status(conn, flight_id), False
            conn.execute(
                "INSERT INTO manifests (flight_id, origin_x, origin_y, cell_size,"
                " grid_rows, grid_cols, tile_count, manifest_json, status)"
                " VALUES (?,?,?,?,?,?,?,?, 'collecting')",
                (
                    flight_id,
                    req["origin"][0],
                    req["origin"][1],
                    req["cell_size"],
                    grid_rows,
                    grid_cols,
                    len(tiles),
                    canonical,
                ),
            )
            for t in tiles:
                conn.execute(
                    "INSERT INTO tiles (flight_id, tile_id, row_start, row_end,"
                    " col_start, col_end, expected_sha256, status)"
                    " VALUES (?,?,?,?,?,?,?, 'pending')",
                    (
                        flight_id,
                        t["tile_id"],
                        t["row_start"],
                        t["row_end"],
                        t["col_start"],
                        t["col_end"],
                        t["expected_sha256"].lower(),
                    ),
                )
            return self._status(conn, flight_id), True

    # ------------------------------------------------------------ 上传瓦片
    def upload_tile(
        self, flight_id: str, tile_id: str, raw_payload: bytes
    ) -> tuple[dict[str, Any], bool]:
        """上传一片原件（JSON 分类矩阵）。返回 (瓦片视图, 是否本次新收)。

        - 同片同原文（sha256 相同）重试 → 幂等复用；
        - 同片异原文 → 拒绝（已收片不可改）；
        - 与已收片重叠处分类不一致 → 标记 conflict，航次永久 blocked。
        """
        # 先算原件哈希并解析（解析失败不写任何状态）
        digest = hashlib.sha256(raw_payload).hexdigest()
        try:
            body = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Unprocessable(f"原件不是合法 UTF-8 JSON：{exc}")
        if not isinstance(body, dict) or not isinstance(body.get("rows"), list):
            raise Unprocessable("原件 JSON 须为含 rows 字段的对象")
        rows = body["rows"]
        if not rows or not all(isinstance(r, list) for r in rows):
            raise Unprocessable("rows 须为非空二维数组")
        if not all(isinstance(v, str) for r in rows for v in r):
            raise Unprocessable("rows 仅接受字符串分类值")

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            m = conn.execute(
                "SELECT * FROM manifests WHERE flight_id=?", (flight_id,)
            ).fetchone()
            if m is None:
                raise NotFound(f"航次 {flight_id} 未登记")
            tile = conn.execute(
                "SELECT * FROM tiles WHERE flight_id=? AND tile_id=?",
                (flight_id, tile_id),
            ).fetchone()
            if tile is None:
                raise NotFound(f"航次 {flight_id} 清单中不存在片号 {tile_id}")

            if tile["status"] in ("received", "conflict"):
                if tile["content_sha256"] == digest:
                    # 同片同原文一律幂等复用（发布后重传同样允许，内容零变化）
                    return self._tile_view(tile), False
                if m["status"] == "published":
                    raise Conflict(f"航次 {flight_id} 已发布，瓦片不可修改")
                raise Conflict(
                    f"片 {tile_id} 已收录原件 {tile['content_sha256']}，"
                    f"异内容（{digest}）拒绝"
                )

            if m["status"] == "published":
                # 理论不可达：发布要求全部片已收录，留作防御
                raise Conflict(f"航次 {flight_id} 已发布，瓦片不可修改")

            # 登记期望哈希强校验
            if digest != tile["expected_sha256"]:
                raise Unprocessable(
                    f"片 {tile_id} 原件 SHA-256 不符：实际 {digest}，"
                    f"登记期望 {tile['expected_sha256']}"
                )
            self.validate_matrix(
                rows,
                tile["row_start"],
                tile["row_end"],
                tile["col_start"],
                tile["col_end"],
            )

            # 与已收片做重叠一致性检查（不跳过任何已收片）
            received = conn.execute(
                "SELECT * FROM tiles WHERE flight_id=? AND status IN ('received','conflict')",
                (flight_id,),
            ).fetchall()
            incoming = {
                "tile_id": tile_id,
                "row_start": tile["row_start"],
                "row_end": tile["row_end"],
                "col_start": tile["col_start"],
                "col_end": tile["col_end"],
                "matrix": rows,
            }
            prior = [
                {
                    "tile_id": x["tile_id"],
                    "row_start": x["row_start"],
                    "row_end": x["row_end"],
                    "col_start": x["col_start"],
                    "col_end": x["col_end"],
                    "matrix": json.loads(x["matrix_json"]),
                }
                for x in received
            ]
            _, conflicts = self.stitch(
                m["grid_rows"], m["grid_cols"], prior + [incoming]
            )

            # 原件先持久化到共享卷（事务内；若后续步骤失败，对象去重无副作用）
            self.put_object(raw_payload, expected_sha256=digest)

            new_status = "conflict" if conflicts else "received"
            conn.execute(
                "UPDATE tiles SET status=?, content_sha256=?, matrix_json=?,"
                " updated_at=? WHERE flight_id=? AND tile_id=?",
                (
                    new_status,
                    digest,
                    json.dumps(rows, ensure_ascii=False),
                    _now(),
                    flight_id,
                    tile_id,
                ),
            )
            if conflicts:
                conn.execute(
                    "UPDATE manifests SET status='blocked' WHERE flight_id=?",
                    (flight_id,),
                )
            fresh = conn.execute(
                "SELECT * FROM tiles WHERE flight_id=? AND tile_id=?",
                (flight_id, tile_id),
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if conflicts:
            # 冲突状态已提交持久化，再向调用方返回 409
            raise Conflict(
                f"片 {tile_id} 与已收片在 {len(conflicts)} 个重叠像元分类不一致，"
                "航次已标记 blocked，不能发布"
            )
        return self._tile_view(fresh), True

    # ------------------------------------------------------------ 发布
    def publish(self, flight_id: str) -> dict[str, Any]:
        """收齐且校验全部通过后发布；幂等（已发布直接返回状态）。"""
        with self.tx() as conn:
            m = conn.execute(
                "SELECT * FROM manifests WHERE flight_id=?", (flight_id,)
            ).fetchone()
            if m is None:
                raise NotFound(f"航次 {flight_id} 未登记")
            if m["status"] == "published":
                return self._status(conn, flight_id)
            if m["status"] == "blocked":
                raise Unprocessable(f"航次 {flight_id} 存在重叠分类冲突，不能发布")

            tiles = conn.execute(
                "SELECT * FROM tiles WHERE flight_id=?", (flight_id,)
            ).fetchall()
            pending = [t["tile_id"] for t in tiles if t["status"] == "pending"]
            if pending:
                raise Unprocessable(
                    f"尚有 {len(pending)} 片未收齐，不能发布；缺片：{pending}"
                )
            conflicted = [t["tile_id"] for t in tiles if t["status"] == "conflict"]
            if conflicted:
                raise Unprocessable(
                    f"片 {conflicted} 存在重叠分类冲突，不能发布"
                )

            # 原件文件必须在共享卷中真实存在
            missing_objs = [
                t["tile_id"]
                for t in tiles
                if not self.object_exists(t["content_sha256"])
            ]
            if missing_objs:
                raise Unprocessable(
                    f"原件在共享卷缺失，不能发布；涉及片：{missing_objs}"
                )

            tile_payloads = [
                {
                    "tile_id": t["tile_id"],
                    "row_start": t["row_start"],
                    "row_end": t["row_end"],
                    "col_start": t["col_start"],
                    "col_end": t["col_end"],
                    "matrix": json.loads(t["matrix_json"]),
                }
                for t in tiles
            ]
            # 发布前再做一次窗口铺满 + 重叠一致性全量复核
            self.validate_windows(
                [
                    {
                        "tile_id": t["tile_id"],
                        "row_start": t["row_start"],
                        "row_end": t["row_end"],
                        "col_start": t["col_start"],
                        "col_end": t["col_end"],
                    }
                    for t in tiles
                ],
                m["grid_rows"],
                m["grid_cols"],
            )
            grid, conflicts = self.stitch(
                m["grid_rows"], m["grid_cols"], tile_payloads
            )
            if conflicts:
                conn.execute(
                    "UPDATE manifests SET status='blocked' WHERE flight_id=?",
                    (flight_id,),
                )
                raise Unprocessable(
                    f"重叠像元分类不一致 {len(conflicts)} 处，已标记 blocked"
                )
            if any(v is None for row in grid for v in row):
                raise Unprocessable("拼接后仍有像元未被任何窗口覆盖")

            grid_doc = {
                "origin": [m["origin_x"], m["origin_y"]],
                "cell_size": m["cell_size"],
                "rows": grid,
            }
            payload = json.dumps(
                grid_doc, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            # 拼接网格先落共享卷并 fsync，再提交发布状态
            sha = self.put_object(payload)
            grid_file = self._grid_path(flight_id)
            tmp = f"{grid_file}.tmp-{os.getpid()}-{threading.get_ident()}"
            with open(tmp, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, grid_file)
            self._fsync_dir(self.grids_dir)

            conn.execute(
                "UPDATE manifests SET status='published', grid_object_sha=?,"
                " published_at=? WHERE flight_id=?",
                (sha, _now(), flight_id),
            )
            return self._status(conn, flight_id)

    # ------------------------------------------------------------ 查询
    def get_status(self, flight_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM manifests WHERE flight_id=?", (flight_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"航次 {flight_id} 未登记")
            return self._status(conn, flight_id)

    def list_flights(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT flight_id, status FROM manifests ORDER BY flight_id"
            ).fetchall()
            return [dict(r) for r in rows]

    def _tile_view(self, tile: sqlite3.Row) -> dict[str, Any]:
        return {
            "tile_id": tile["tile_id"],
            "row_start": tile["row_start"],
            "row_end": tile["row_end"],
            "col_start": tile["col_start"],
            "col_end": tile["col_end"],
            "expected_sha256": tile["expected_sha256"],
            "status": tile["status"],
            "content_sha256": tile["content_sha256"],
        }

    def _status(self, conn: sqlite3.Connection, flight_id: str) -> dict[str, Any]:
        m = conn.execute(
            "SELECT * FROM manifests WHERE flight_id=?", (flight_id,)
        ).fetchone()
        if m is None:
            raise NotFound(f"航次 {flight_id} 未登记")
        tiles = conn.execute(
            "SELECT * FROM tiles WHERE flight_id=? ORDER BY tile_id", (flight_id,)
        ).fetchall()
        tile_views = [self._tile_view(t) for t in tiles]
        missing = [t["tile_id"] for t in tiles if t["status"] == "pending"]
        conflicts = [t["tile_id"] for t in tiles if t["status"] == "conflict"]
        return {
            "flight_id": flight_id,
            "status": m["status"],
            "origin": [m["origin_x"], m["origin_y"]],
            "cell_size": m["cell_size"],
            "grid_rows": m["grid_rows"],
            "grid_cols": m["grid_cols"],
            "tile_count": m["tile_count"],
            "received_count": sum(
                1 for t in tiles if t["status"] in ("received", "conflict")
            ),
            "missing_tiles": missing,
            "conflict_tiles": conflicts,
            "grid_object_sha256": m["grid_object_sha"],
            "published_at": m["published_at"],
            "tiles": tile_views,
        }

    # ------------------------------------------------------------ 下载
    def get_original(self, flight_id: str, tile_id: str) -> tuple[str, str, bytes]:
        """返回 (文件名, content_type, 原件字节)。"""
        with self._connect() as conn:
            tile = conn.execute(
                "SELECT * FROM tiles WHERE flight_id=? AND tile_id=?",
                (flight_id, tile_id),
            ).fetchone()
            if tile is None:
                raise NotFound(
                    f"航次 {flight_id} 清单中不存在片号 {tile_id}"
                )
            if tile["status"] == "pending":
                raise NotFound(f"片 {tile_id} 尚未上传，无原件可下载")
            sha = tile["content_sha256"]
        path = self.get_object_path(sha)
        with open(path, "rb") as f:
            data = f.read()
        return f"{flight_id}_{tile_id}.json", "application/json", data

    def get_grid(self, flight_id: str) -> tuple[str, bytes]:
        """发布后下载拼接网格（形状与 /assess 的 grid 入参一致）。"""
        with self._connect() as conn:
            m = conn.execute(
                "SELECT status FROM manifests WHERE flight_id=?", (flight_id,)
            ).fetchone()
            if m is None:
                raise NotFound(f"航次 {flight_id} 未登记")
            if m["status"] != "published":
                raise Conflict(f"航次 {flight_id} 尚未发布，拼接网格不可用")
        path = self._grid_path(flight_id)
        if not os.path.isfile(path):
            # 兜底：对象存储里仍有一份
            with self._connect() as conn:
                sha = conn.execute(
                    "SELECT grid_object_sha FROM manifests WHERE flight_id=?",
                    (flight_id,),
                ).fetchone()["grid_object_sha"]
            if sha and self.object_exists(sha):
                with open(self.get_object_path(sha), "rb") as f:
                    data = f.read()
                return f"{flight_id}_grid.json", data
            raise NotFound("拼接网格文件缺失，请联系运维核对共享卷")
        with open(path, "rb") as f:
            data = f.read()
        return f"{flight_id}_grid.json", data

    def reconcile(self) -> dict[str, Any]:
        """重启后对账：检查已收/已发布对象在共享卷是否齐全。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT flight_id, tile_id, content_sha256 FROM tiles"
                " WHERE status IN ('received','conflict')"
            ).fetchall()
            missing = [
                {"flight_id": r["flight_id"], "tile_id": r["tile_id"],
                 "sha256": r["content_sha256"]}
                for r in rows
                if not self.object_exists(r["content_sha256"])
            ]
            published = conn.execute(
                "SELECT flight_id, grid_object_sha FROM manifests"
                " WHERE status='published'"
            ).fetchall()
            missing_grids = [
                r["flight_id"]
                for r in published
                if not os.path.isfile(self._grid_path(r["flight_id"]))
                and not self.object_exists(r["grid_object_sha"])
            ]
        return {
            "checked_tiles": len(rows),
            "missing_objects": missing,
            "published_flights": len(published),
            "missing_grids": missing_grids,
        }
