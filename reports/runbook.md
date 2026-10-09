# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

Điều kiện tiên quyết (phải đang chạy): `python3 dr/health_checker.py --interval 5 --threshold 3 --duration 300 --out reports/health-events.jsonl &` và `python3 state/replicate.py --every 30 --duration 300 --backend fs &`

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | `"a": {"alive": false}` hoặc `"ready": false` 3 lần liên tiếp | on-call |
| 2 | Mở incident + bấm giờ RTO | `grep UNHEALTHY reports/health-events.jsonl` | có dòng `region:a, to:UNHEALTHY`; ts ghi vào `reports/runbook-run.jsonl` | on-call |
| 3 | Restore state ở region phụ | `python3 dr/runbook.py --primary a --target b --backend fs` (trả lời `y`; bước này gọi `python3 state/snapshot.py get --region b --backend fs`) | `reports/failover-events.jsonl` có `2_restore_snapshot` với `rpo_seconds`, `docs_lost` | DR operator |
| 4 | Scale pool warm→full | `curl --max-time 3 localhost:8002/readyz` | `/readyz` của b trả 200 | DR operator |
| 5 | DNS/LB cutover | `curl localhost:8080/edge/state` | `curl localhost:8080/edge/state` cho `active_region=b` | DR operator |
| 6 | Verify golden signals | `curl localhost:8080/v1/infer` (chờ ≥ 6s cho edge TTL 5s) | p95 < 500ms, error rate < 20% (≤ 2/10 request lỗi). Drill: 0/10 lỗi, p95 307.0ms (`reports/runbook-run.jsonl:6`) | on-call |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict` != null (drill: PASS, 23.5s) | incident commander |

**Rollback (failover ngược):** trả traffic về region A chỉ khi đủ 3 điều kiện: (1) `curl --max-time 3 localhost:8001/readyz` trả 200 liên tục ≥ 10 phút, (2) dữ liệu mới của B đã đồng bộ ngược về A, (3) có cửa sổ ít tải và đã báo stakeholders. **Incident commander** quyết định (on-call chỉ đề xuất). Không full-auto vì thiếu circuit breaker làm 2 region flap qua lại (§4 Anti-Patterns), nên `--auto` chỉ dùng cho drill/CI; outage thật luôn hỏi `y/N`.