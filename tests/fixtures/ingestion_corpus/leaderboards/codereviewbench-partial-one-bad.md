<!-- FIXTURE / DEMO SOURCE — synthetic text for ModelMatch ingestion tests. NOT a real cited benchmark; all figures are invented. -->

# CodeReviewBench — messy scrape (FIXTURE, partial)

A deliberately messy snapshot: one clean row and one row whose vendor column was
lost in scraping. The validator must keep the good row and DROP the broken one —
never invent the missing vendor. Invented figures, measured 2026-03-03.

- Claude Haiku 4.5 (Anthropic): review_score_percent 82.0, $0.80/Mtok.
- "Mystery Model" (vendor missing): review_score_percent 65.0, $0.20/Mtok.
