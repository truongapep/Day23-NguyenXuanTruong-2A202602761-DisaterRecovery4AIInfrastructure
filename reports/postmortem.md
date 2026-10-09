# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T11:05:16 | outage bắt đầu | `chaos/chaos-events.jsonl:5` |
| 2026-10-09T11:05:16 (+0.2s) | user đầu tiên bị ảnh hưởng | `reports/drill-2-withdr.jsonl:26` |
| 2026-10-09T11:05:30 (+14.5s) | health check alert | `reports/health-events.jsonl:2` |
| 2026-10-09T11:05:30 (+14.7s) | operator confirm cutover (`--auto`, incident declared) | `reports/runbook-run.jsonl:2` |
| 2026-10-09T11:05:37 (+21.3s) | region B ready, DNS cutover | `reports/failover-events.jsonl:5` |
| 2026-10-09T11:05:39 (+23.5s) | resolved (request đầu tiên OK từ region phụ) | `reports/drill-2-withdr.jsonl:37` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `23.5s` · gap: `276.5s` (còn dư)
- RPO mục tiêu: 300s · đo được: `2.0s` (`1` doc bị mất) · gap: `298.0s` (còn dư)
- **Bước tốn nhiều giây nhất:** `health-check detection (14.5s, ~62% RTO)` — vì phải chờ 3 lần probe fail liên tiếp ở chu kỳ 5s mới kết luận outage (`reports/health-events.jsonl:2`). Bước lớn thứ hai là GPU warm-up 6.4s (`reports/failover-events.jsonl:4`).

## 3. Root cause (5 whys)

Không phải "vì tôi chạy chaos script". Câu hỏi: *nếu đây là outage thật, bước nào
trong runbook của tôi sẽ thất bại?*

1. Vì sao user lỗi? Region A mất kết nối mà edge vẫn trỏ về A cho đến khi cutover (`reports/drill-2-withdr.jsonl:26`).
2. Vì sao lỗi kéo dài ~23.5s? Không có gì tự chuyển traffic trước khi health check phát hiện (14.5s), rồi còn restore + warm-up + TTL.
3. Vì sao region B không serve ngay? B khởi đầu rỗng (không weights, không vector) và ở trạng thái `warm`, phải restore snapshot rồi chờ warm-up 6.4s.
4. Vì sao có mất dữ liệu? Replication chạy theo chu kỳ 30s nên doc ghi sau snapshot cuối không có ở B (`docs_lost=1`, `reports/failover-events.jsonl:2`).
5. Bước nào của runbook sẽ thất bại trong outage thật? Health checker là tiến trình đơn lẻ (nó chết thì runbook chờ 60s mà không thấy UNHEALTHY); `--auto` chỉ an toàn trong drill; không có cảnh báo khi replication lag tăng.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Hạ health-check interval 5s → 2s (giữ threshold 3), thêm test chống flapping | Reliability engineer | 2026-10-16 | RTO giảm ~9s (floor 15s → 6s) |
| 2 | Rút chu kỳ replication 30s → 10s, cảnh báo khi snapshot lag > 60s | DR on-call | 2026-10-23 | RPO tối đa giảm ~20s |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?
   → 5 × 3 = **15s** cấu hình (`reports/measure-drill-2.json:15`); thực tế phát hiện ở 14.5s, chiếm **~62%** RTO 23.5s (14.5 / 23.5).
2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?
   → Floor còn 3s, RTO giảm khoảng **11.5s** (14.5s → ~3s, RTO còn ~12s). Đánh đổi: nhiều probe hơn và dễ flapping, một đợt jitter ngắn có thể kích failover rồi failback liên tục giữa 2 region.
3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của
   bạn có nghĩa gì với khách hàng?
   → `docs_lost=1` nghĩa là 1 document khách hàng ghi trong ~2s trước outage mất vĩnh viễn và khách phải nhập lại. Vì replication theo chu kỳ, con số này có thể lớn hơn ở lần khác nên phải giám sát replication lag.