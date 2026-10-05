# Symbolic trace compression experiment

Run `./tools/computer-trace-compression.sh --repeats 3 --json` for the full
machine-readable matrix. It uses only deterministic synthetic records from the
reviewed `VA/v1` registry. No guest screenshot, private trace, URL, or model
feedback is read. It tests 1, 8, 64, and 256 records in steady, mixed, and
varied sequences across compact JSON, JSONL, individually framed VA records,
and batches of up to 64 VA records. Every format and compressed payload must
decode back to the exact source records.

Representative **varied** results on this Mac:

| Records | Compact JSON | VA batch | VA batch + zstd 3 | VA batch + Brotli 5 |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 165 B | 24 B | 37 B | 28 B |
| 8 | 1,262 B | 81 B | 73 B | 68 B |
| 64 | 10,121 B | 543 B | 152 B | 126 B |
| 256 | 40,486 B | 2,172 B | 259 B | 238 B |

The full 192-case matrix passed exact roundtrips. CLI decompression and
parsing took roughly 2–4 ms for compressed cases, including process startup;
uncompressed VA decoding took under 1 ms even for 256 records. These are
local synthetic timings, not VM transport measurements. The finite fixture
vocabulary creates strong repetition, especially at 256 records, so the
archive ratios should not be extrapolated to screenshots or free-form UI text.

**Decision:** keep the existing uncompressed VA batches for live one-shot
queries. Compression adds bytes for one record and saves only 13 bytes for an
eight-record varied batch. A cold archive of reviewed symbolic records may
benefit from Brotli or zstd once it has at least 64 records, but the archive
must still retain the transport's authentication, sequence, and freshness
metadata. No live trace format or private journal was changed.
