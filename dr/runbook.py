"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    ts = time.time()
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))
    rec = {"ts": ts, "iso": iso, "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    try:
        ans = input(f"{msg} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _probe_many(region: str, attempts: int = 3, timeout: float = 2.0):
    """Probe region nhiều lần để tránh false positive từ 1 lần fail."""
    results = []
    for _ in range(attempts):
        try:
            from dr import health_checker as hc
            ok, reason = hc.probe(region, timeout)
        except Exception:
            # fallback if health_checker not yet implemented or fails
            try:
                r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
                ok = r.status_code == 200
                reason = "ready" if ok else f"status_{r.status_code}"
            except Exception as e2:
                ok = False
                reason = f"{type(e2).__name__}:{e2}"
        results.append((ok, reason))
        time.sleep(0.3)
    return results


def _wait_detected(region: str, t_outage: float, timeout: float = 60.0):
    """Wait until health_checker records the outage transition for this region."""
    health_log = pathlib.Path("reports/health-events.jsonl")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for line in health_log.read_text().splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if (event.get("event") == "state_change"
                        and event.get("region") == region
                        and event.get("to") == "UNHEALTHY"
                        and event.get("ts", 0) >= t_outage):
                    return event.get("ts")
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    return None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước ở trên."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    t0_wall = time.time()

    # Load t_outage from chaos events if available (last kill for primary)
    t_outage = None
    chaos_path = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos_path.exists():
        try:
            for line in chaos_path.read_text().splitlines():
                if not line.strip():
                    continue
                e = json.loads(line)
                if e.get("action") == "kill" and e.get("region") == primary:
                    t_outage = e["ts"]
        except Exception:
            pass

    # === Step 1: xac_nhan_outage ===
    primary_probes = _probe_many(primary, attempts=3)
    target_probes = _probe_many(target, attempts=3)
    primary_ok_count = sum(1 for ok, _ in primary_probes if ok)
    target_ok_count = sum(1 for ok, _ in target_probes if ok)
    primary_unhealthy = primary_ok_count == 0
    step(1, "xac_nhan_outage",
         primary=primary, target=target,
         primary_probes=primary_probes, target_probes=target_probes,
         primary_ok_count=primary_ok_count, target_ok_count=target_ok_count,
         primary_unhealthy=primary_unhealthy,
         t_outage=t_outage)

    # Do not start failover before the health checker has detected this outage.
    t_detect = _wait_detected(primary, t_outage or t0_wall)

    # === Step 2: thong_bao_incident ===
    # Always ensure this ts is AFTER t_outage (operator cannot know before outage)
    if t_outage is not None:
        while time.time() <= t_outage:
            time.sleep(0.05)
    t_incident = time.time()
    notification_delay_s = round(t_incident - t_outage, 2) if t_outage else None
    step(2, "thong_bao_incident",
         t_outage=t_outage,
         t_detect=t_detect,
         t_incident=t_incident,
         notification_delay_s=notification_delay_s,
         message=f"INCIDENT declared: region-{primary} outage confirmed, failing over to region-{target}")

    if not confirm(auto, f"Xac nhan failover {primary} -> {target} (backend={backend})?"):
        rec = step(7, "post_incident",
                   status="aborted_by_operator",
                   elapsed_s=round(time.time() - t0_wall, 2),
                   measure_cmd="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
        return {"ok": False, "reason": "operator_aborted", "runbook": rec}

    # === Step 3: scale_gpu_pool — gọi failover.failover ĐÚNG 1 LẦN ===
    try:
        fo_result = fo.failover(target, backend, wait=60)
    except SystemExit as e:
        fo_result = {"ok": False, "error": str(e), "step_failed": "exception"}
    except Exception as e:
        fo_result = {"ok": False, "error": str(e), "step_failed": "exception"}
    step(3, "scale_gpu_pool",
         target=target, backend=backend,
         fo_ok=fo_result.get("ok", False), fo_result=fo_result)

    # === Step 4: verify_state_replica — chỉ đọc lại từ fo_result ===
    try:
        r = httpx.get(f"{URL[target]}/v1/state", timeout=3.0)
        target_state = r.json() if r.status_code == 200 else {"error": r.text[:200]}
    except Exception as e:
        target_state = {"error": f"{type(e).__name__}:{e}"}
    step(4, "verify_state_replica",
         target=target,
         fo_rpo_seconds=fo_result.get("rpo_seconds"),
         fo_docs_lost=fo_result.get("docs_lost"),
         fo_embed_model_version=fo_result.get("embed_model_version"),
         target_state=target_state,
         vector_count=target_state.get("count"),
         weights=target_state.get("weights"))

    # === Step 5: dns_cutover — chỉ đọc lại kết quả ===
    try:
        af = pathlib.Path("edge/active_region")
        active = af.read_text().strip() if af.exists() else None
    except Exception:
        active = None
    cutover_ok = (active == target) and fo_result.get("ok", False)
    step(5, "dns_cutover",
         expected=target, active_region=active, ok=cutover_ok,
         fo_ok=fo_result.get("ok"))

    # If failover failed, skip golden signals and go to post_incident
    if not fo_result.get("ok"):
        rec = step(7, "post_incident",
                   status="failover_failed",
                   fo_error=fo_result.get("error"),
                   step_failed=fo_result.get("step_failed"),
                   elapsed_s=round(time.time() - t0_wall, 2),
                   active_region=active,
                   measure_cmd="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
        return {"ok": False, "fo_result": fo_result, "active_region": active, "runbook": rec}

    # === Step 6: verify_golden_signals — 10 request thật ===
    # Allow the edge's 5s DNS cache TTL to expire before checking traffic.
    time.sleep(6)
    latencies = []
    errors = 0
    edge_url = "http://127.0.0.1:8080/v1/infer"
    for i in range(10):
        t0 = time.time()
        try:
            resp = httpx.get(edge_url, params={"q": f"health check {i}"}, timeout=5.0)
            lat = (time.time() - t0) * 1000
            latencies.append(lat)
            body = resp.json() if resp.headers.get("content-type","").startswith("application/json") else {}
            if resp.status_code != 200 or body.get("error"):
                errors += 1
        except Exception:
            lat = (time.time() - t0) * 1000
            latencies.append(lat)
            errors += 1
        time.sleep(0.2)
    latencies_sorted = sorted(latencies)
    if latencies_sorted:
        import math
        idx = max(0, math.ceil(0.95 * len(latencies_sorted)) - 1)
        p95 = round(latencies_sorted[idx], 1)
        p50 = round(latencies_sorted[len(latencies_sorted)//2], 1)
    else:
        p95 = None
        p50 = None
    error_rate = round(errors / 10, 2) if latencies else 1.0
    golden_ok = (errors <= 2)
    step(6, "verify_golden_signals",
         requests=10, errors=errors, error_rate=error_rate,
         p50_ms=p50, p95_ms=p95, latencies_ms=[round(x, 1) for x in latencies],
         ok=golden_ok)

    # === Step 7: post_incident ===
    elapsed_s = round(time.time() - t0_wall, 2)
    rec = step(7, "post_incident",
               status="completed" if cutover_ok else "cutover_mismatch",
               elapsed_s=elapsed_s,
               active_region=active,
               fo_ok=fo_result.get("ok"),
               golden_ok=golden_ok,
               p95_ms=p95, error_rate=error_rate,
               measure_cmd="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    return {"ok": cutover_ok and golden_ok, "elapsed_s": elapsed_s,
            "active_region": active, "fo_result": fo_result,
            "p95_ms": p95, "error_rate": error_rate}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
