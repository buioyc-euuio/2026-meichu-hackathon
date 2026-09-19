---
description: Preserve the LLM's ASR-correction role while enforcing deterministic motion limits.
applyTo: "robot_control/**/*.py"
---

The local LLM normalizes plausible ASR errors before classifying motion intent; do not require direction keywords to appear verbatim in the raw transcript, which defeats repairs such as the homophone "turn write" to "turn right".
Retain deterministic action allowlists, duration bounds, explicit negation/stop handling, stale-result cancellation, and rejection of contradictions with an unambiguous direction.
Distinguish plausible transcription repair from genuinely unspecified requests such as "turn a little"; log the raw transcript and the model's correction/decision.
Regression tests must cover both successful ASR repairs and refusals, including the original imperfect transcription rather than only cleaner replacement utterances.
