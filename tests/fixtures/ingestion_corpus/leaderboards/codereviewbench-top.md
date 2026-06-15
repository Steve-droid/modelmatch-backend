<!-- FIXTURE / DEMO SOURCE — synthetic text for ModelMatch ingestion tests. NOT a real cited benchmark; all figures are invented. -->

# CodeReviewBench — public leaderboard snapshot (FIXTURE)

CodeReviewBench scores how well a model reviews real pull-request diffs
(catches bugs + security issues without noise). Higher review_score_percent is
better. Snapshot below is invented demo data measured 2026-03-01.

| Model              | Vendor    | Review score % | $ in / out per Mtok |
|--------------------|-----------|----------------|---------------------|
| Claude Sonnet 4.5  | Anthropic | 88.0           | 3.00 / 15.00        |
| Claude Haiku 4.5   | Anthropic | 82.0           | 0.80 / 4.00         |
| Gemini 2.0 Flash   | Google    | 79.0           | 0.10 / 0.40         |
| Amazon Nova 2 Lite | Amazon    | 77.0           | 0.07 / 0.28         |
| Amazon Nova Lite   | Amazon    | 74.0           | 0.06 / 0.24         |
