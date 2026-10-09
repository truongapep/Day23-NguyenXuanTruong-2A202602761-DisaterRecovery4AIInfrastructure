"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    ts = time.time()
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))
    rec = {"ts": ts, "iso": iso, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec))
    return rec


def state_of(region: str) -> dict:
    """Helper nội bộ: lấy state của region (dùng cho test patch)."""
    try:
        r = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
        return r.json()
    except Exception as e:
        return {"region": region, "error": str(e)}


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước ở trên, đúng thứ tự."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    other = "a" if target == "b" else "b"

    # --- 1_verify_target ---
    try:
        st = state_of(target)
        # st contains pool_state, weights, count etc
        pool = st.get("pool_state")
        weights = st.get("weights")
        count = st.get("count")
        emit(step="1_verify_target", target=target, state=st, pool_state=pool, weights=weights, count=count)
    except Exception as e:
        emit(step="1_verify_target", target=target, error=str(e))
        st = {}

    # --- 2_restore_snapshot ---
    try:
        meta = snapshot.get(target, backend)
        # meta contains snapshot_at, latest_doc_ts, embed_model_version, restored_at
        # Compute RPO: data in primary that restored copy doesn't have
        primary_db = pathlib.Path(f"state/region-{other}/vectors.sqlite")
        # fallback: if other DB doesn't exist, use region-a as primary (lab convention)
        if not primary_db.exists():
            primary_db = pathlib.Path("state/region-a/vectors.sqlite")
        restored_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")
        rpo_info = snapshot.rpo(primary_db, restored_db)
        rpo_seconds = rpo_info.get("rpo_seconds")
        docs_lost = rpo_info.get("docs_lost")
        embed_version = meta.get("embed_model_version")
        emit(step="2_restore_snapshot", target=target, backend=backend,
             rpo_seconds=rpo_seconds, docs_lost=docs_lost,
             embed_model_version=embed_version,
             snapshot_at=meta.get("snapshot_at"), restored_at=meta.get("restored_at"),
             latest_doc_ts=meta.get("latest_doc_ts"))
    except SystemExit as e:
        # snapshot.get raises SystemExit when no snapshot ever put
        emit(step="2_restore_snapshot", target=target, backend=backend, error=str(e), ok=False)
        return {"ok": False, "error": str(e), "step_failed": "2_restore_snapshot"}
    except Exception as e:
        emit(step="2_restore_snapshot", target=target, backend=backend, error=str(e), ok=False)
        return {"ok": False, "error": str(e), "step_failed": "2_restore_snapshot"}

    # --- 3_scale_pool ---
    try:
        ps_path = pathlib.Path(f"state/region-{target}/pool_state")
        ps_path.parent.mkdir(parents=True, exist_ok=True)
        before = ps_path.read_text().strip() if ps_path.exists() else "unknown"
        ps_path.write_text("full")
        emit(step="3_scale_pool", target=target, before=before, after="full")
    except Exception as e:
        emit(step="3_scale_pool", target=target, error=str(e), ok=False)
        return {"ok": False, "error": str(e), "step_failed": "3_scale_pool"}

    # --- 4_wait_ready ---
    t_start = time.time()
    waited_s = None
    ready = False
    last_reason = None
    while time.time() - t_start < wait:
        try:
            resp = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            if resp.status_code == 200:
                body = resp.json() if resp.headers.get("content-type","").startswith("application/json") else {}
                ready = True
                waited_s = round(time.time() - t_start, 2)
                emit(step="4_wait_ready", target=target, ready=True, waited_s=waited_s, body=body)
                break
            else:
                try:
                    body = resp.json()
                    last_reason = body.get("reasons", str(body))
                except Exception:
                    last_reason = f"status_{resp.status_code}"
        except Exception as e:
            last_reason = f"{type(e).__name__}:{e}"
        time.sleep(0.5)
    if not ready:
        waited_s = round(time.time() - t_start, 2)
        emit(step="4_wait_ready", target=target, ready=False, waited_s=waited_s, reason=last_reason, ok=False, timeout=wait)
        # ABORT — không cutover
        return {"ok": False, "error": f"target {target} not ready after {wait}s: {last_reason}",
                "step_failed": "4_wait_ready", "waited_s": waited_s,
                "rpo_seconds": rpo_seconds if 'rpo_seconds' in locals() else None,
                "docs_lost": docs_lost if 'docs_lost' in locals() else None,
                "embed_model_version": embed_version if 'embed_version' in locals() else None}

    # --- 5_dns_cutover ---
    try:
        af = pathlib.Path("edge/active_region")
        before = af.read_text().strip() if af.exists() else None
        af.parent.mkdir(parents=True, exist_ok=True)
        af.write_text(target)
        emit(step="5_dns_cutover", target=target, before=before, after=target, waited_s=waited_s)
    except Exception as e:
        emit(step="5_dns_cutover", target=target, error=str(e), ok=False)
        return {"ok": False, "error": str(e), "step_failed": "5_dns_cutover"}

    return {"ok": True, "target": target, "backend": backend,
            "rpo_seconds": rpo_seconds, "docs_lost": docs_lost,
            "embed_model_version": embed_version,
            "waited_s": waited_s, "state": st}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
