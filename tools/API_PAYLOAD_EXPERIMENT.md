# Model API payload experiment

Run `./tools/computer-api-payload.sh --measure`. It creates two distinct,
deterministic 512×512 images for each fixture, converts both to JPEG quality 80
with macOS `sips`, measures pair transfer shapes, checks exact payload
roundtrips, then deletes the images. It uses synthetic pixels only and makes no
network request.

| Image pair | Binary frame | Current base64 JSON | gzip JSON | gzip encode median |
| --- | ---: | ---: | ---: | ---: |
| UI-like PNG | 5,420 B | 7,268 B | 708 B | 0.021 ms |
| UI-like JPEG 80 | 17,652 B | 23,576 B | 3,720 B | 0.071 ms |
| Random PNG | 1,574,512 B | 2,099,392 B | 1,590,292 B | 51.6 ms |
| Random JPEG 80 | 544,796 B | 726,436 B | 548,021 B | 19.5 ms |

Base64 JSON adds about one-third to already compressed image bytes. gzip can
remove that expansion, but on random-like screenshots it offers almost no gain
over a small binary frame while spending 20–52 ms of CPU for this fixture.
The UI-like fixture compresses unusually well because it has large flat areas
and the two variants share most pixels. Actual guest screenshots may behave
differently. The existing VM bridge is local, so a binary upload path would
save bandwidth but adds protocol and validation work; it is a lower-priority
change than reducing stored image copies and improving model evidence.

The live API still accepts bounded base64 JSON. These results do not measure
network latency, image decoding in `sips`, or UI prediction quality.
