# Operational scripts

Run scripts from the repository root so the `camellia` package resolves.

- `validate_schemas.py`: validate versioned menu and Q&A data.
- `simulate_bot.py`: local interactive simulation.
- `conversation_benchmark.py`: deterministic regression benchmark.
- `judge_conversation_benchmark.py`: independent point-wise/pairwise judge.
- `export_anonymized_conversations.py`: local review sample with direct phone
  numbers and emails removed.
- `generate_pdf.py`: operator PDF generation.

Generated output belongs under `scratch/`, which is ignored by Git.
