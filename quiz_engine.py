import json
from pathlib import Path


def load_questions(path):
    """Load and validate quiz questions from questions.json."""
    path = Path(path)

    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not isinstance(data, list):
        return []

    valid = []

    for item in data:
        if not isinstance(item, dict):
            continue

        question = str(item.get("question", "")).strip()
        options = item.get("options")
        correct = item.get("correct")

        if not question:
            continue

        if not isinstance(options, list) or len(options) != 4:
            continue

        if not isinstance(correct, int) or not 0 <= correct < 4:
            continue

        options = [str(x).strip() for x in options]

        if any(not x for x in options):
            continue

        valid.append({
            "question": question,
            "options": options,
            "correct": correct
        })

    return valid[:100]


def score_result(session, stopped=False):
    """Create final result text."""
    total = len(session.get("questions", []))
    correct = int(session.get("correct", 0))
    wrong = int(session.get("wrong", 0))
    skipped = int(session.get("skipped", 0))

    attempted = correct + wrong + skipped

    percentage = (correct / attempted * 100) if attempted else 0

    if stopped:
        title = "🛑 Quiz बीच में बंद की गई"
    else:
        title = "🏁 Quiz पूरी हो गई"

    return (
        f"<b>{title}</b>\n\n"
        f"📚 कुल प्रश्न: {total}\n"
        f"📝 किए गए: {attempted}\n"
        f"✅ सही: {correct}\n"
        f"❌ गलत: {wrong}\n"
        f"⏭️ छोड़े: {skipped}\n"
        f"🎯 प्रतिशत: {percentage:.2f}%"
    )
