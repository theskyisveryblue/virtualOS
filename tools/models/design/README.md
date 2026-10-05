---
license: apache-2.0
tags:
- ui-design
- computer-use
- virtualos
---
# virtualOS design principle rankers

Three small image feature models rank alternative graphic designs for human-rated
**alignment**, **overlap**, and **whitespace**. Each artifact is under 10 KB.
Training used the `absolute-human-*` shards from
[GraphicDesignEvaluation](https://huggingface.co/datasets/creative-graphic-design/GraphicDesignEvaluation)
at revision `a503deef9686671615cbd76b653fff5bbc823fb1` (Apache 2.0).

Every perturbation of one source design stayed in one train, validation, or
holdout group. On 22 unseen designs per principle, pairwise agreement with
human mean ratings was 93/114 for alignment, 100/109 for overlap, and 98/108
for whitespace. The pairs exclude rating differences below 0.5. These are
relative comparisons within a design family. They do not establish absolute
design quality, usability, or performance on Xcode screens.

The implementation is `tools/design_ranker.py` in virtualOS. The model files
are compact JSON weights over 64×64 image statistics extracted by macOS
`sips`. `./tools/computer-design.sh --compare LEFT RIGHT` scores local image
files offline with the same extraction path and no image Python package.
Scoring is advisory. No virtualOS private screenshots were used in training.
