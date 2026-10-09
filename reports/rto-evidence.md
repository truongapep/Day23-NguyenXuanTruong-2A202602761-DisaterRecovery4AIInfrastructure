# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T10:46:06` | chaos kill | `chaos/chaos-events.jsonl:3` |
| Request fail đầu tiên | `+0.2s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:34` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json:9` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json:23` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:5` |
| User thấy lỗi đầu tiên | 0.2 | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:26` |
| Health check phát hiện | 14.5 | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 14.9 | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | 21.3 | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | 21.3 | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | 23.5 | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:37` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `23.5s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `2.0s` / `1` doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 14.5 | `interval_s × threshold` = 5 × 3 = 15s cấu hình, thực tế phát hiện ở +14.5s trong `reports/health-events.jsonl:2` | Hạ interval (5s → 2s thì floor còn 6s), đánh đổi là dễ flapping |
| Snapshot restore | 0.3 | 2_restore → 3_scale, từ lúc detect đến khi restore xong (`reports/failover-events.jsonl:2`) | Đã rất nhỏ (snapshot ít dữ liệu), không đáng tối ưu |
| GPU pool warm-up | 6.4 | `waited_s` ở `4_wait_ready` (`reports/failover-events.jsonl:4`) | Giữ pool region phụ ở trạng thái pre-warm |
| DNS/LB TTL cache | 2.3 | t_recovered − t_cutover (`reports/drill-2-withdr.jsonl:37`) | Giảm `EDGE_TTL_SECONDS` (đang 5s) |
| **Tổng** | **23.5** | khớp RTO đo được | |