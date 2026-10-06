---
name: medical-ai-job-screening
description: Apply the job_market evidence rules when screening medical or pharmaceutical roles for visual AI/deep-learning and Agent/LLM targets. Use for candidate-JD analysis, incremental target refreshes, and screening audits in this project.
---

# Medical AI Job Screening

Read this skill before classifying candidate roles or updating target-company and target-JD outputs.

## Target Categories

Keep the established priority order:

1. Visual AI or deep-learning algorithm roles.
2. Agent or LLM development roles.
3. Agent or LLM algorithm roles as the secondary category.

## Visual AI Evidence Rule

Treat generic image processing as outside category 1 by default. The phrase `图像处理` alone is not evidence of visual AI or a deep-learning algorithm role.

Do not admit a role solely because it mentions image enhancement, registration, reconstruction, stitching, denoising, compression, rendering, graphics, ISP, OpenCV, medical-image processing, microscopy-image processing, or similar conventional processing work.

Admit it only when the title or JD also provides affirmative algorithm evidence, such as:

- computer vision, machine vision, visual AI, or AI algorithm ownership;
- training, fine-tuning, evaluating, deploying, or optimizing deep-learning or machine-learning models;
- CNN, Transformer, ViT, multimodal vision models, or comparable model architectures;
- learned detection, segmentation, classification, recognition, tracking, pose estimation, or image-generation models.

Using a pretrained model or calling an image API is not sufficient by itself when the role is primarily application integration. Require evidence that the candidate develops or materially optimizes the model or algorithm.

When excluding a role for this distinction, record `image_processing_without_ai_evidence` and retain the quoted title/JD evidence used for the decision. When admitting it, record the specific model or algorithm evidence; do not infer it from the company industry.

## Decision Discipline

- Evaluate each JD independently. Do not promote every role at a company because one role qualifies.
- CSV-declared target roles may remain candidates under the established broad policy, but generic `图像处理` is not a declared visual-AI target without affirmative evidence.
- Page, structured-job, or audit evidence may raise confidence; a conservative fetch status must not erase explicit qualifying evidence.
- Keep borderline image-processing roles in an adjacent or review list instead of the strict target list.
- Leave the established Agent/LLM and location rules unchanged unless the user updates them.

## Limited-Source AI Mention Rule

Use a separate low-evidence admission path only for sources whose job coverage cannot be
proved complete, including WeChat articles, questionnaires, application forms, and static
documents without a reliable pagination or terminal condition.

For these limited sources, admit a role to the candidate list when either the role title or
the role's own local text explicitly mentions an AI signal such as `AI`, `人工智能`,
`机器学习`, `深度学习`, `大模型`, `LLM`, `Agent`, `CV`, or `NLP`.

This exception does not apply to ordinary complete sources, which must continue to satisfy
the three strict target-category rules above. Apply these safeguards:

- The AI mention must be bound to an identifiable role title or that role's adjacent duties,
  requirements, or qualifications. Company introductions, product descriptions, industry
  labels, news headlines, and unrelated roles do not qualify.
- Never invent a role from a company-level AI statement. If no identifiable role exists,
  keep the company pending.
- Record `evidence_level=limited_source_ai_mention`, the source URL, a concise verbatim
  evidence excerpt, and the unresolved coverage or detail uncertainty.
- Set `details_verified=false`; do not include these rows in verified company or verified JD
  outputs, even when an older audit labeled the page sufficient for semantic review.
- Keep strict duplicates out of the low-evidence set. If the same company and normalized role
  already has stronger, complete-source evidence, retain the stronger row only.
- A list that is demonstrably complete is not fetch-partial merely because some duties or
  requirements are absent. Record list acquisition as successful, preserve
  `list_complete=true` and `jd_complete=false`, and keep any detail fetch as optional.

