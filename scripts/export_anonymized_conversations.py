"""Export local review samples without customer IDs, phone numbers, or emails.

Run: python scripts/export_anonymized_conversations.py --output scratch/review.jsonl --limit 200
The script never calls Meta or an LLM and never changes database rows.
"""
import argparse
import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from camellia.database import db


def anonymize(text: str) -> str:
    value = text or ""
    value = re.sub(r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", "[EMAIL]", value)
    return re.sub(r"(?<!\d)(?:\+?91[\s-]?)?(?:0[\s-]?)?[6-9]\d(?:[\s-]?\d){8}(?!\d)", "[PHONE]", value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, user_message, bot_response, was_matched, template_id "
            "FROM conversations ORDER BY id DESC LIMIT ?", (args.limit,)
        ).fetchall()
    rows = list(reversed(rows))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in rows:
            item = {
                "sample_id": f"conversation-{row['id']}",
                "customer_message": anonymize(row["user_message"]),
                "bot_reply": anonymize(row["bot_response"]),
                "matched": bool(row["was_matched"]),
                "template_id": row["template_id"],
                "human_overall": None,
                "human_notes": "",
            }
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"Exported {len(rows)} anonymized samples to {args.output}")


if __name__ == "__main__":
    main()
