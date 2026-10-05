---
license: cc-by-4.0
tags:
- ui-design
- usability
- virtualos
---
# virtualOS human critique interpreter

This 43 KB text model estimates usability and aesthetics ratings from a
human-written critique of a UI. It does **not** inspect a screenshot or judge
the design on its own. It was trained on the human-sourced comments and
ratings in [UICrit](https://github.com/google-research-datasets/uicrit),
licensed CC BY 4.0. Eighty-three rows with mismatched comment/source arrays
were excluded. The screen ID determines the train, validation, and holdout
split; all ratings for one screen stay together.

On 547 ratings from unseen screens, mean absolute error was 0.616 for
usability and 0.633 for aesthetics. A training-mean predictor scored 0.781
and 0.814 respectively. The ratings are on a 1–10 scale. The source contains
older mobile UI screens and feedback; generalization to current websites,
macOS apps, or Xcode is unmeasured. This model is advisory only.

`model.json` and `metrics.json` are reproducible with `train.py` and the
pinned source checksum. No virtualOS private guest trace was used.
