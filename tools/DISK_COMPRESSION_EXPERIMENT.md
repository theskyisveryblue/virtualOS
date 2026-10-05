# Disposable disk compression experiment

Run `./tools/computer-disk-compression.sh --measure`. The script creates three
32 MiB RAW-shaped fixtures under a temporary directory, measures gzip and
zstd archive sizes and wall time, verifies every decompressed SHA-256, then
deletes all fixtures. It never opens the production VM disk or a private
guest file.

| Fixture | gzip 1 | gzip 6 | zstd 1 | zstd 3 | zstd 1 encode vs gzip 1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 8 MiB random blocks with holes | 8,506,645 B | 8,421,612 B | 8,389,670 B | 8,389,582 B | 20 vs 152 ms |
| Dense zero bytes | 146,442 B | 32,635 B | 1,152 B | 1,068 B | 7 vs 31 ms |
| 4 MiB random, 4 MiB repeated, holes | 4,336,484 B | 4,234,409 B | 4,195,732 B | 4,195,630 B | 11 vs 90 ms |

**Promising:** zstd level 1 won size and encode time in all three synthetic
cases. The gain is mostly speed when data is incompressible; its dramatic zero
ratio depends on actual zero-heavy content. macOS APFS reported 32 MiB of
allocated blocks for these fixtures after writes, so the `st_blocks` figure
was not used to claim sparse-file savings. This experiment does not establish
the live VM disk's compression ratio, R2 restore time, or macOS boot behavior.
The Mini has `/opt/homebrew/bin/zstd`, although its noninteractive SSH path
does not include that directory. Changing the VM archive workflow requires a
verified receipt and a separate restore
test first. The production RAW remains untouched.
