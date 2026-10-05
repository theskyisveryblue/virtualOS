# Local model compression experiments

Run `./tools/computer-model-compression.sh --output /private/tmp/virtualos-model-compression-results.json` from any directory. This reads the checked-in local JSON artifacts only. It does not alter the models, inference code, or remote repositories. It caps each input at 2 MB, the total at 8 MB, subprocess output at 16 MB, and each codec process at 25 seconds. It requires Python 3 and the `gzip`, `zstd`, `brotli`, and `xz` command line tools for the full matrix. If a codec is missing, the command exits nonzero after reporting the completed trials.

The experiment tests **60 combinations**: model payloads alone (5 JSON files) and model plus metric files (10 JSON files); separate file streams and one deterministic length-prefixed bundle; gzip levels 1/6/9, zstd 1/3/9/19, Brotli 1/5/9/11, and xz 0/3/6/9. Every result was decompressed and checked against the original SHA-256 bytes. Bundles were also parsed back into the original file names and byte content. The output JSON includes a source SHA-256 manifest and every trial.

## Results on this Mac (2026-10-05)

For the **10-file, 119,099-byte JSON set**, selected bundle results were:

| Codec and level | Compressed | Reduction | Encode | Decode |
| --- | ---: | ---: | ---: | ---: |
| gzip 6 | 31,671 B | 73.4% | 4.77 ms | 2.25 ms |
| zstd 9 | 30,883 B | 74.1% | 5.32 ms | 3.66 ms |
| Brotli 5 | 30,820 B | 74.1% | 4.91 ms | 3.13 ms |
| xz 6 | 26,404 B | 77.8% | 19.60 ms | 5.46 ms |
| zstd 19 | 27,288 B | 77.1% | 38.80 ms | 3.77 ms |
| Brotli 11 | **25,365 B** | **78.7%** | 98.01 ms | 3.58 ms |

For the **5-file, 115,833-byte model-only set**, Brotli 11 bundled to **24,685 B** (78.7% reduction); xz 6/9 tied at 25,656 B; zstd 19 was 26,452 B. The 10-file metrics added only 3,266 source bytes and 680 compressed bytes to the Brotli 11 bundle.

Bundling helped the highest-compression options. With all 10 JSON files, separate Brotli 11 streams totaled 27,567 B, versus 25,365 B bundled. Separate streams allow independent fetches and updates, so the 2,202 B bundle savings alone may not justify changing distribution.

**Promising next choices:** Brotli 11 is the smallest artifact for infrequent build-time packaging. Brotli 5 or zstd 9 give about 31 KB with roughly 5 ms encode time in this single run. gzip 6 is a useful compatibility baseline. xz 9 offered no size gain over xz 6 here. These models are already small: compression would save at most about 94 KB of the local JSON payload. It would not shrink the separate API service runtime by megabytes, and a compressed file needs a decompression step before the current JSON reader can use it.

Timings are one wall-clock sample per trial, include command launch, and are useful only as rough local comparisons. Peak memory was not measured; xz and high Brotli levels can have larger transient memory needs, so deployment decisions need a representative runtime benchmark. No compressed format was promoted into live inference.
