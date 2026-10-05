# Weight-precision experiment

Run `./tools/computer-weight-quant.sh --measure`. It reads the checked-in model
JSON only, re-encodes every float as fixed-width binary, and reports packed size
and the effect on each model's arithmetic. It writes nothing to the repo and
uses no network. `weight_quant_experiment.py` sizes the formats;
`quant_codec_experiment.py` runs the packed bytes through real gzip, zstd, and
Brotli with a SHA-256 roundtrip.

## Why precision dominates codec choice

The 10 JSON files hold 10,077 floats but 118,383 bytes of decimal text. The
numbers are stored as decimal strings, so codecs must compress digits. Packing
floats as fixed-width binary first, then compressing, stacks two independent
wins.

Decimal JSON is 118,383 B. Binary packing alone:

| Format | Bytes | Ratio |
| --- | ---: | ---: |
| int8 fixed 6 | 71,028 | 0.600 |
| float16 / bfloat16 / int16 fixed 12 | 81,105 | 0.685 |
| float32 | 101,259 | 0.855 |
| float64 | 141,567 | 1.196 |

The prior encoder-only best was 25,365 B (Brotli 11 on text). Packing floats
**then** compressing beats it:

| Format + codec | Bytes | Ratio vs decimal |
| --- | ---: | ---: |
| int16 fixed 12 + Brotli 11 | 11,663 | 0.099 |
| int16 fixed 12 + zstd 19 | 12,530 | 0.106 |
| float16 + Brotli 11 | 12,849 | 0.109 |
| float16 + zstd 19 | 13,701 | 0.116 |
| (any) + none | 20,154 | 0.170 |

## Prediction impact

Monte-Carlo re-scoring of each model's real inference path (6,000 design
comparisons, 2,000 router feature sets, 2,000 critique feature sets) gives
maximum absolute output change for int16 fixed 12:

| Model | Quantity | Max abs error |
| --- | --- | ---: |
| design rankers | `score_delta` | 0.035 |
| interaction router | logits | 0.015 |
| critique interpreter | ratings (1–10) | 0.0004 |

The signed preference direction and the rounded critique ratings are unchanged
in every sampled case. float16 binary packing round-trips byte-exact for all 10
files after a single host-side quantize step.

## Shipped

`model_bundle.py` packs the ten JSON files as a self-checking int16 bundle
(`VOSB1`, SHA-256 header), and `build_model_bundle.py` writes the Brotli form.
`./tools/computer-model-api.sh --bundle-mini` builds
`~/Library/Application Support/virtualOS/model-api/models.bundle.br` (15,342 B
on the Mini) and `--serve-mini` passes it as `--models-bundle`; without the file
the server reads plain JSON unchanged. The bundle decodes to the exact model
dicts the existing inference code already consumes, so endpoints and inference
math are unchanged.

All 11 tests in `test_model_api.py` pass, including the four API integration
tests running from the bundle via `BundledApiTest`. On the Mini the live server
loads the `.br` bundle and `--self-test-mini` returns HTTP 200 from route,
critique, and design and HTTP 401 without a token. `model_bundle.decode`
rejects any byte-flip via its checksum.
