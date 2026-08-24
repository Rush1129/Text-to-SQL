# Full evaluation (all 170 queries, ~20-25 min)
.localvenv\Scripts\python.exe evaluation/auto_eval.py --delay 3

# Quick test (first 30 queries)
.localvenv\Scripts\python.exe evaluation/auto_eval.py --limit 30 --delay 2

# Resume from crash
.localvenv\Scripts\python.exe evaluation/auto_eval.py --resume --delay 3

# Skip guardrail tests
.localvenv\Scripts\python.exe evaluation/auto_eval.py --skip-guardrails
