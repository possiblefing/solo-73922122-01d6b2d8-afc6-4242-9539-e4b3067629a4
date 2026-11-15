"""汇交 API 集成测试。

覆盖：
- 登记约束（铺满/越界/重复片号/数量上限）、同清单幂等、异清单拒绝；
- 上传校验（SHA-256、矩阵形状、三类值）、同原文复用、异内容拒绝、
  重叠一致/冲突 blocked、缺片不可发布、发布幂等、发布后不可改；
- 原件与拼接网格下载，拼接网格直接送 /assess；
- 线程级并发上传；
- 双 uvicorn 进程共享卷/SQLite 并发 + 进程退出重启续交。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app, storage

client = TestClient(app)


# --------------------------------------------------------------- 构造工具

def new_flight(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def tile_body(matrix: list[list[str]]) -> tuple[bytes, str]:
    raw = json.dumps({"rows": matrix}, separators=(",", ":")).encode("utf-8")
    return raw, hashlib.sha256(raw).hexdigest()


def spec(tile_id: str, rs: int, re_: int, cs: int, ce: int, sha: str) -> dict:
    return {
        "tile_id": tile_id,
        "row_start": rs,
        "row_end": re_,
        "col_start": cs,
        "col_end": ce,
        "expected_sha256": sha,
    }


def register(flight: str, specs: list[dict], rows: int = 4, cols: int = 4,
             origin=(0.0, 40.0), cell_size: int = 10):
    payload = {
        "flight_id": flight,
        "origin": list(origin),
        "cell_size": cell_size,
        "grid_rows": rows,
        "grid_cols": cols,
        "tiles": specs,
    }
    return client.put(f"/submissions/{flight}", json=payload)


def two_tile_halves(flight: str, left: list[list[str]], right: list[list[str]]):
    """4x4 网格，左右两片无重叠铺满。返回 (flight, [(tile_id, raw)...])。"""
    raw_l, sha_l = tile_body(left)
    raw_r, sha_r = tile_body(right)
    r = register(flight, [
        spec("L", 0, 4, 0, 2, sha_l),
        spec("R", 0, 4, 2, 4, sha_r),
    ])
    assert r.status_code == 200, r.text
    return flight, [("L", raw_l), ("R", raw_r)]


GRID_LEFT = [["flooded", "dry"]] * 4
GRID_RIGHT = [["dry", "unknown"]] * 4
FULL_GRID = [["flooded", "dry", "dry", "unknown"]] * 4


def upload(flight: str, tile_id: str, raw: bytes):
    return client.put(
        f"/submissions/{flight}/tiles/{tile_id}",
        content=raw,
        headers={"Content-Type": "application/json"},
    )


# --------------------------------------------------------------- 登记

def test_register_happy_and_missing_query():
    f = new_flight("reg")
    _, sha = tile_body([["dry"]])
    r = register(f, [spec("T1", 0, 4, 0, 4, sha)])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "collecting"
    assert body["missing_tiles"] == ["T1"]
    assert body["received_count"] == 0
    assert body["tiles"][0]["status"] == "pending"

    r2 = client.get(f"/submissions/{f}")
    assert r2.json()["missing_tiles"] == ["T1"]


def test_register_rejects_non_covering_windows():
    f = new_flight("cover")
    _, sha = tile_body([["dry", "dry"]] * 4)
    # 只覆盖左 2 列，右 2 列缺失
    r = register(f, [spec("T1", 0, 4, 0, 2, sha)])
    assert r.status_code == 422
    assert "铺满" in r.text


def test_register_rejects_out_of_bounds_and_overlap_window_ok():
    f = new_flight("bounds")
    _, sha = tile_body([["dry"]])
    r = register(f, [spec("T1", 0, 5, 0, 1, sha)])  # row_end 越界
    assert r.status_code == 422
    assert "越界" in r.text


def test_register_rejects_duplicate_tile_id():
    f = new_flight("dup")
    _, sha = tile_body([["dry", "dry"]] * 2)
    r = register(f, [
        spec("T1", 0, 2, 0, 4, sha),
        spec("T1", 2, 4, 0, 4, sha),
    ])
    assert r.status_code == 422 and "重复" in r.text


def test_register_limits_and_id_safety():
    f = new_flight("limit")
    _, sha = tile_body([["dry"]])
    too_many = [spec(f"T{i}", 0, 1, 0, 1, sha) for i in range(5)]
    r = register(f, too_many)
    assert r.status_code == 422  # 超过 4 片

    f2 = new_flight("big")
    r2 = register(
        f2, [spec("T", 0, 1, 0, 1, sha)], rows=33, cols=1
    )
    assert r2.status_code == 422  # 行数超 32

    f3 = "bad_id!"
    r3 = register(f3, [spec("T", 0, 1, 0, 1, sha)])
    assert r3.status_code == 422  # 航次号非法字符


def test_register_idempotent_same_manifest_conflict_on_change():
    f = new_flight("idem")
    _, sha = tile_body([["dry"]] * 4)
    specs = [spec("T1", 0, 4, 0, 1, sha)]
    assert register(f, specs, cols=1).json()["reused"] is False
    again = register(f, specs, cols=1)
    assert again.status_code == 200 and again.json()["reused"] is True

    # 异内容（不同期望哈希）拒绝
    other_specs = [spec("T1", 0, 4, 0, 1, "a" * 64)]
    r = register(f, other_specs, cols=1)
    assert r.status_code == 409 and "不一致" in r.text


# --------------------------------------------------------------- 上传与校验

def test_upload_happy_hash_mismatch_and_shape():
    f = new_flight("up")
    two_tile_halves(f, GRID_LEFT, GRID_RIGHT)

    raw, sha = tile_body(GRID_LEFT)
    r = upload(f, "L", raw)
    assert r.status_code == 200, r.text
    ack = r.json()
    assert ack["status"] == "received" and ack["reused"] is False
    assert ack["content_sha256"] == sha

    # 原件确实落共享卷，内容寻址
    assert storage.object_exists(sha)

    # 哈希不符：篡改一个字节
    tampered = raw.replace(b"dry", b"wet", 1)
    assert upload(f, "L", tampered).status_code == 422

    # 形状与窗口不符（L 应为 4x2）
    bad_shape, _ = tile_body([["dry", "dry", "dry"]] * 4)
    # 重新按登记哈希构造一个形状错误但需先算出其哈希的登记新航次
    f2 = new_flight("shape")
    raw_bad, sha_bad = tile_body([["dry", "dry", "dry"]] * 4)
    register(f2, [spec("T", 0, 4, 0, 3, sha_bad)], cols=3)
    r2 = upload(f2, "T", raw_bad)  # 登记窗口 4x3，矩阵 4x3 其实匹配
    assert r2.status_code == 200
    # 真正的形状不符：另一个航次登记 4x2，上传 4x3 原件
    f3 = new_flight("shape2")
    register(f3, [spec("T", 0, 4, 0, 2, sha_bad)], cols=2)
    r3 = upload(f3, "T", raw_bad)
    assert r3.status_code == 422 and "形状" in r3.text


def test_upload_unknown_flight_and_tile():
    assert client.put("/submissions/nope/tiles/T", content=b"{}").status_code == 404
    f = new_flight("unk")
    _, sha = tile_body([["dry"]])
    register(f, [spec("T", 0, 1, 0, 1, sha)], rows=1, cols=1)
    raw, _ = tile_body([["dry"]])
    assert upload(f, "X", raw).status_code == 404


def test_same_bytes_retry_reused_different_bytes_rejected():
    f = new_flight("retry")
    two_tile_halves(f, GRID_LEFT, GRID_RIGHT)
    raw, _ = tile_body(GRID_LEFT)
    assert upload(f, "L", raw).json()["reused"] is False
    r = upload(f, "L", raw)
    assert r.status_code == 200 and r.json()["reused"] is True

    changed = [row.copy() for row in GRID_LEFT]
    changed[0] = ["flooded", "flooded"]
    raw2, _ = tile_body(changed)
    r2 = upload(f, "L", raw2)
    assert r2.status_code == 409 and "异内容" in r2.text
    # 原件未被替换
    status = client.get(f"/submissions/{f}").json()
    assert status["tiles"][0]["content_sha256"] == hashlib.sha256(raw).hexdigest()


def test_overlap_consistent_accepted_inconsistent_blocks():
    # 一致场景：两片在 col=2 重叠，取值相同
    f_ok = new_flight("ov-ok")
    a = [["flooded", "dry", "dry"]] * 4          # 窗口 cols [0,3)
    b = [["dry", "unknown"]] * 4                 # 窗口 cols [2,4)，重叠列值 dry
    raw_a, sha_a = tile_body(a)
    raw_b, sha_b = tile_body(b)
    r = register(f_ok, [
        spec("A", 0, 4, 0, 3, sha_a),
        spec("B", 0, 4, 2, 4, sha_b),
    ])
    assert r.status_code == 200, r.text
    assert upload(f_ok, "A", raw_a).status_code == 200
    assert upload(f_ok, "B", raw_b).status_code == 200
    assert client.post(f"/submissions/{f_ok}/publish").status_code == 200

    # 冲突场景：重叠列取值不同
    f_bad = new_flight("ov-bad")
    b_conflict = [["flooded", "unknown"]] * 4    # 重叠列变成 flooded
    raw_c, sha_c = tile_body(b_conflict)
    register(f_bad, [
        spec("A", 0, 4, 0, 3, sha_a),
        spec("B", 0, 4, 2, 4, sha_c),
    ])
    upload(f_bad, "A", raw_a)
    r = upload(f_bad, "B", raw_c)
    assert r.status_code == 409 and "不一致" in r.text
    status = client.get(f"/submissions/{f_bad}").json()
    assert status["status"] == "blocked"
    assert status["conflict_tiles"] == ["B"]
    # blocked 永远不能发布
    pub = client.post(f"/submissions/{f_bad}/publish")
    assert pub.status_code == 422


# --------------------------------------------------------------- 发布与下载

def test_publish_requires_all_tiles_and_is_idempotent():
    f = new_flight("pub")
    two_tile_halves(f, GRID_LEFT, GRID_RIGHT)
    raw_l, _ = tile_body(GRID_LEFT)
    upload(f, "L", raw_l)
    r = client.post(f"/submissions/{f}/publish")
    assert r.status_code == 422 and "未收齐" in r.text
    assert r.json()  # 错误体可解析

    raw_r, _ = tile_body(GRID_RIGHT)
    upload(f, "R", raw_r)
    pub1 = client.post(f"/submissions/{f}/publish")
    assert pub1.status_code == 200 and pub1.json()["status"] == "published"
    pub2 = client.post(f"/submissions/{f}/publish")
    assert pub2.status_code == 200 and pub2.json()["status"] == "published"


def test_published_manifest_and_tiles_immutable():
    f = new_flight("imm")
    two_tile_halves(f, GRID_LEFT, GRID_RIGHT)
    raw_l, sha_l = tile_body(GRID_LEFT)
    raw_r, _ = tile_body(GRID_RIGHT)
    upload(f, "L", raw_l)
    upload(f, "R", raw_r)
    client.post(f"/submissions/{f}/publish")

    # 清单不可改（同号异清单）
    r = register(f, [spec("L", 0, 4, 0, 2, "a" * 64),
                     spec("R", 0, 4, 2, 4, "b" * 64)])
    assert r.status_code == 409
    # 瓦片不可改
    assert upload(f, "L", raw_l).status_code == 200  # 同原文仍幂等
    changed, _ = tile_body([["dry", "dry"]] * 4)
    assert upload(f, "L", changed).status_code == 409


def test_download_original_and_grid_grid_feeds_assess():
    f = new_flight("dl")
    two_tile_halves(f, GRID_LEFT, GRID_RIGHT)
    raw_l, _ = tile_body(GRID_LEFT)
    raw_r, _ = tile_body(GRID_RIGHT)
    upload(f, "L", raw_l)
    # 未发布也可下载已收原件
    d = client.get(f"/submissions/{f}/tiles/L/original")
    assert d.status_code == 200 and d.content == raw_l
    # 未上传的片无原件
    assert client.get(f"/submissions/{f}/tiles/R/original").status_code == 404
    # 未发布无网格
    assert client.get(f"/submissions/{f}/grid").status_code == 409

    upload(f, "R", raw_r)
    client.post(f"/submissions/{f}/publish")

    g = client.get(f"/submissions/{f}/grid")
    assert g.status_code == 200
    grid_doc = g.json()
    assert grid_doc["rows"] == FULL_GRID
    assert grid_doc["origin"] == [0.0, 40.0] and grid_doc["cell_size"] == 10

    # 拼接网格直接送 /assess（地块完全落在网格内，两组平移）
    assess_req = {
        "grid": grid_doc,
        "translations": [{"dx": 0.0, "dy": 0.0}, {"dx": 1.0, "dy": 1.0}],
        "parcels": [{
            "id": "P1",
            "vertices": [[2, 2], [8, 2], [8, 8], [2, 8]],
        }],
        "threshold": 0.5,
    }
    r = client.post("/assess", json=assess_req)
    assert r.status_code == 200, r.text
    (p,) = r.json()["parcels"]
    assert p["verdict"] in {"confirmed", "review", "not_met"}

    # 原件字节级一致
    d2 = client.get(f"/submissions/{f}/tiles/R/original")
    assert d2.content == raw_r


def test_list_flights_and_reconcile():
    f = new_flight("list")
    two_tile_halves(f, GRID_LEFT, GRID_RIGHT)
    flights = client.get("/submissions").json()["flights"]
    assert any(x["flight_id"] == f for x in flights)
    report = storage.reconcile()
    assert report["missing_objects"] == []
    assert isinstance(report["checked_tiles"], int)


# --------------------------------------------------------------- 存储层纯函数 / 防御约束

def test_storage_defensive_limits_and_window_rules():
    from storage import Storage, Unprocessable as SUnprocessable

    with pytest.raises(SUnprocessable):
        Storage.validate_windows([], 4, 4)  # 0 片
    with pytest.raises(SUnprocessable):
        _, sha = tile_body([["dry"]])
        Storage.validate_windows(
            [spec(f"T{i}", 0, 1, 0, 1, sha) for i in range(5)], 4, 4
        )  # 5 片
    with pytest.raises(SUnprocessable):
        Storage.validate_windows([spec("T", 0, 1, 0, 1, sha)], 33, 1)  # 行数越界
    with pytest.raises(SUnprocessable):
        Storage.validate_windows(
            [spec("T", 2, 2, 0, 1, sha)], 4, 1
        )  # 空窗口（start == end）


def test_stitch_detects_conflicts_and_fills_grid():
    from storage import Storage

    tiles = [
        {"tile_id": "A", "row_start": 0, "row_end": 2, "col_start": 0,
         "col_end": 2, "matrix": [["flooded", "dry"], ["dry", "dry"]]},
        {"tile_id": "B", "row_start": 1, "row_end": 3, "col_start": 0,
         "col_end": 2, "matrix": [["dry", "dry"], ["unknown", "unknown"]]},
    ]
    # 重叠区域为行 1：A 行1=[dry,dry]，B 首行=[dry,dry]，一致
    grid, conflicts = Storage.stitch(3, 2, tiles)
    assert conflicts == []
    assert grid[0] == ["flooded", "dry"]
    assert grid[2] == ["unknown", "unknown"]

    # B 首行改成 flooded → 与 A 在 (1,0) 冲突
    tiles[1]["matrix"][0] = ["flooded", "dry"]
    _, conflicts = Storage.stitch(3, 2, tiles)
    assert len(conflicts) == 1 and conflicts[0]["row"] == 1 and conflicts[0]["col"] == 0


# --------------------------------------------------------------- 线程并发

def test_concurrent_uploads_different_tiles_and_duplicate_retries():
    import threading

    f = new_flight("conc")
    two_tile_halves(f, GRID_LEFT, GRID_RIGHT)
    raw_l, _ = tile_body(GRID_LEFT)
    raw_r, _ = tile_body(GRID_RIGHT)
    errors: list[Exception] = []

    def worker(tile_id: str, raw: bytes):
        try:
            for _ in range(3):  # 同片重复重试并发
                r = upload(f, tile_id, raw)
                assert r.status_code == 200, r.text
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("L", raw_l)),
        threading.Thread(target=worker, args=("R", raw_r)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    status = client.get(f"/submissions/{f}").json()
    assert sorted(status["missing_tiles"]) == []
    pub = client.post(f"/submissions/{f}/publish")
    assert pub.status_code == 200, pub.text


# --------------------------------------------------------------- 双进程 + 重启续交

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http(method: str, url: str, body: bytes | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _wait_ready(port: int) -> None:
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=2
            ) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"uvicorn on {port} did not become ready")


@pytest.mark.skipif(
    importlib.util.find_spec("uvicorn") is None,
    reason="uvicorn 不可用时跳过跨进程测试")
def test_two_processes_shared_volume_and_restart_resume():
    app_dir = str(Path(__file__).resolve().parent.parent / "app")
    data_dir = tempfile.mkdtemp(prefix="flood-multiproc-")
    port1, port2, port3 = _free_port(), _free_port(), _free_port()

    extra_paths = [p for p in sys.path if p and ("packages" in p or "pylibs" in p)]
    env = os.environ.copy()
    env["FLOOD_DATA_DIR"] = data_dir
    env["FLOOD_ENABLE_SHUTDOWN_HOOK"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [app_dir, env.get("PYTHONPATH")] + extra_paths))
    procs: list[subprocess.Popen] = []

    def start(port: int) -> subprocess.Popen:
        # 不用 -I：依赖位于 PYTHONPATH 指向的目录，隔离模式会将其忽略；
        # cwd 固定为应用目录，不解释任何外部下载文件
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=app_dir, env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        procs.append(proc)
        _wait_ready(port)
        return proc

    def stop(proc: subprocess.Popen, port: int) -> None:
        # 沙箱 seccomp 禁止 kill 信号，改用进程自带的 shutdown 钩子自行退出
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/shutdown", method="POST"
            )
            urllib.request.urlopen(req, timeout=5).read()
        except OSError:
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.wait(timeout=10)

    base = f"http://127.0.0.1"
    try:
        p1 = start(port1)
        p2 = start(port2)  # 双实例共享同一 SQLite + 共享卷

        flight = f"multi-{uuid.uuid4().hex[:8]}"
        raw_l, sha_l = tile_body(GRID_LEFT)
        raw_r, sha_r = tile_body(GRID_RIGHT)
        manifest = {
            "flight_id": flight,
            "origin": [0.0, 40.0],
            "cell_size": 10,
            "grid_rows": 4,
            "grid_cols": 4,
            "tiles": [
                spec("L", 0, 4, 0, 2, sha_l),
                spec("R", 0, 4, 2, 4, sha_r),
            ],
        }
        code, body = _http("PUT", f"{base}:{port1}/submissions/{flight}",
                           json.dumps(manifest).encode())
        assert code == 200, body

        # 实例 2 立刻可见实例 1 的登记（共享 SQLite）
        code, body = _http("GET", f"{base}:{port2}/submissions/{flight}")
        assert code == 200 and json.loads(body)["missing_tiles"] == ["L", "R"]

        # 两片分别上传到不同实例
        code, _ = _http("PUT", f"{base}:{port1}/submissions/{flight}/tiles/L", raw_l)
        assert code == 200
        code, _ = _http("PUT", f"{base}:{port2}/submissions/{flight}/tiles/R", raw_r)
        assert code == 200

        # 实例 1 发布，实例 2 下载网格与原件
        code, body = _http("POST", f"{base}:{port1}/submissions/{flight}/publish")
        assert code == 200 and json.loads(body)["status"] == "published"
        code, body = _http("GET", f"{base}:{port2}/submissions/{flight}/grid")
        assert code == 200 and json.loads(body)["rows"] == FULL_GRID
        code, body = _http(
            "GET", f"{base}:{port2}/submissions/{flight}/tiles/L/original"
        )
        assert code == 200 and body == raw_l

        # 落盘后进程退出，重启续交：状态与文件均在
        stop(p1, port1)
        stop(p2, port2)

        p3 = start(port3)
        try:
            code, body = _http("GET", f"{base}:{port3}/submissions/{flight}")
            assert code == 200
            st = json.loads(body)
            assert st["status"] == "published" and st["missing_tiles"] == []
            code, body = _http("GET", f"{base}:{port3}/submissions/{flight}/grid")
            assert code == 200 and json.loads(body)["rows"] == FULL_GRID
            # 发布后不可改
            code, _ = _http(
                "PUT", f"{base}:{port3}/submissions/{flight}/tiles/L",
                json.dumps({"rows": [["dry", "dry"]] * 4}).encode(),
            )
            assert code == 409
        finally:
            stop(p3, port3)
    finally:
        # 兜底：记录仍存活的进程（正常路径已通过 shutdown 钩子退出）
        leaked = [p.pid for p in procs if p.poll() is None]
        assert not leaked, f"仍有子进程未退出: {leaked}"
