---
license: apache-2.0
tags:
- tool-routing
- computer-use
- virtualos
---
# virtualOS compact interface router

This 46 KB text classifier chooses an interface mode: CLI, structured API,
DOM click, or form. It was trained on the 50 human-written training prompts
from [Agent UI Human](https://huggingface.co/datasets/akashnaren/agent-ui-human)
at revision `bff66198177024eeed2a4914cc3df03adc99fe35` (Apache 2.0).
The untouched 10-prompt test split scored 7/10 against a 5/10 majority
baseline. It is a virtualOS-specific count model, not a Transformers checkpoint.

The model does not navigate apps, inspect screenshots, judge design, or execute
actions. Confidence values are uncalibrated. `./tools/computer-route.sh
--suggest "TASK"` offers offline advice and marks low vocabulary overlap as out
of domain. App actions still need grounding and independent verification.

`model.json`, `metrics.json`, and `train.py` reproduce the artifact from the
pinned public dataset. The private virtualOS trace journal was not used.
