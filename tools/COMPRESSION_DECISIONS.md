# Compression experiment decisions

All trials were local. They did not train models, change API responses, read the
production VM RAW, or export private guest data. Every reported size was
verified by an exact decompression and SHA-256 roundtrip; the weight-precision
rows additionally quantify their bounded numeric effect on model output.

| Rank | Candidate | Measured benefit | Cost and decision |
| --- | --- | --- | --- |
| 1, highest measured | Binary float packing + Brotli for model bundles | 118,383 B of model JSON became 11,663 B (9.9%) with int16 fixed 12 + Brotli 11, versus 25,365 B for Brotli on text alone; ~90% off, ~40% better than the best text codec | Needs a small binary loader and re-run of the API tests. Output change is ≤0.035 on design `score_delta`, ≤0.015 on router logits, ≤0.0004 on ratings. [Weight-precision experiment](WEIGHT_QUANT_EXPERIMENT.md). |
| 2, highest potential | zstd 1 for a future VM archive | On three disposable 32 MiB disk shapes, 7–8× faster encoding than gzip 1 and equal or smaller output; every restore SHA matched | Multi-GiB potential, but synthetic. Keep current production path until a complete backup receipt and restore test exist. [Disk experiment](DISK_COMPRESSION_EXPERIMENT.md). |
| 3, ready for packaging | Brotli 5 or zstd 9 for model bundles (text only) | 119,099 B of model/metric JSON became 30,820 B or 30,883 B; exact roundtrip | Superseded by rank 1 when a loader is added. Keep plain local JSON for offline speed meanwhile. [Model experiment](MODEL_COMPRESSION.md). |
| 4, already useful live | Uncompressed VA batches for symbolic facts | In a varied 64-record fixture, 543 B versus 10,121 B compact JSON | Keep live batches uncompressed: one record grows under zstd/Brotli, and eight records save only 8–13 B. Consider compression for cold reviewed batches of 64+. [Trace experiment](TRACE_COMPRESSION.md). |
| 5, conditional | Binary framing for image-pair API requests | Removes base64 JSON's roughly 33% expansion on image bytes | The VM bridge is local. gzip JSON recovered the expansion on noisy fixtures but spent 20–52 ms CPU; a new binary protocol has validation cost. Keep bounded JSON now. [API payload experiment](API_PAYLOAD_EXPERIMENT.md). |

The model and symbolic artifacts are measured in kilobytes. They do not explain
the Mini's multi-gigabyte swap use. The largest possible disk saving is in VM
images and retained screenshots, where representative data, recovery, and
privacy checks matter more than a synthetic ratio. None of these measurements
justifies deleting the live RAW or its local overlay.
