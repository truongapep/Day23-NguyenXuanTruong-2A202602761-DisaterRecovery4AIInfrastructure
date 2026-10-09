"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi."""
    url = f"{URL[region]}/readyz"
    try:
        resp = httpx.get(url, timeout=timeout)
        if resp.status_code == 200:
            return True, "ready"
        # try to extract reasons
        try:
            body = resp.json()
            reasons = body.get("reasons", [])
            reason = ",".join(reasons) if reasons else f"status_{resp.status_code}"
        except Exception:
            reason = f"status_{resp.status_code}"
        return False, reason
    except Exception as e:
        return False, f"{type(e).__name__}:{e}"


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL."""
    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Truncate at start to avoid mixing old drill events outside time window
    # (measure.py filters by t0..hi, but clean file makes debugging easier)
    # Keep append behavior if test expects accumulation? We truncate then append.
    with out.open("w") as _:
        pass

    state = {"a": "HEALTHY", "b": "HEALTHY"}
    fails = {"a": 0, "b": 0}

    end = time.time() + duration
    while time.time() < end:
        loop_start = time.time()
        for region in ("a", "b"):
            ok, reason = probe(region, timeout)
            ts = time.time()
            iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))
            if ok:
                fails[region] = 0
                if state[region] == "UNHEALTHY":
                    state[region] = "HEALTHY"
                    rec = {
                        "ts": ts,
                        "iso": iso,
                        "event": "state_change",
                        "region": region,
                        "to": "HEALTHY",
                        "reason": reason,
                        "interval_s": interval,
                        "threshold": threshold,
                        "consecutive_fails": 0,
                    }
                    with out.open("a") as f:
                        f.write(json.dumps(rec) + "\n")
                    print(json.dumps(rec))
            else:
                fails[region] += 1
                if fails[region] >= threshold and state[region] == "HEALTHY":
                    state[region] = "UNHEALTHY"
                    rec = {
                        "ts": ts,
                        "iso": iso,
                        "event": "state_change",
                        "region": region,
                        "to": "UNHEALTHY",
                        "reason": reason,
                        "interval_s": interval,
                        "threshold": threshold,
                        "consecutive_fails": fails[region],
                    }
                    with out.open("a") as f:
                        f.write(json.dumps(rec) + "\n")
                    print(json.dumps(rec))
        elapsed = time.time() - loop_start
        remain = interval - elapsed
        if remain > 0:
            # sleep but not beyond end
            time.sleep(min(remain, max(0, end - time.time())))
        else:
            # if probing took longer than interval, yield a bit
            time.sleep(0.01)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
