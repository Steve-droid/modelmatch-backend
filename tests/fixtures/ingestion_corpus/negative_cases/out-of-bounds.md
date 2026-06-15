<!-- FIXTURE / DEMO SOURCE — synthetic text for ModelMatch ingestion tests. NOT a real cited benchmark; all figures are invented. -->

# Corrupted scrape (FIXTURE, out-of-bounds)

Numbers that overflow the catalog's column bounds — a score far above the column
ceiling and a negative price. Both rows must be DROPPED (never clamped/stored).
