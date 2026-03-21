#!/usr/bin/env python3
"""HN post comment monitor — checks for new comments and prints alerts."""

import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

STORY_ID = 47465550
STATE_FILE = Path(__file__).parent / ".hn_monitor_state.json"
API_BASE = "https://hacker-news.firebaseio.com/v0"


def _fetch(path: str) -> dict | list | None:
    try:
        req = urllib.request.Request(
            f"{API_BASE}/{path}", headers={"User-Agent": "Python"}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        print(f"  [error] {e}")
        return None


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_ids": [], "last_check": None}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _collect_all_comment_ids(item_id: int, depth: int = 0) -> list[dict]:
    """Recursively collect all comments under an item."""
    item = _fetch(f"item/{item_id}.json")
    if not item:
        return []
    results = []
    if item.get("type") == "comment" and not item.get("dead") and not item.get("deleted"):
        results.append({
            "id": item["id"],
            "by": item.get("by", "[deleted]"),
            "text": item.get("text", "")[:200],
            "time": item.get("time", 0),
            "depth": depth,
            "parent": item.get("parent"),
        })
    for kid_id in item.get("kids", []):
        results.extend(_collect_all_comment_ids(kid_id, depth + 1))
    return results


def check_once() -> list[dict]:
    """Check for new comments. Returns list of new comments."""
    state = _load_state()
    seen = set(state.get("seen_ids", []))

    story = _fetch(f"item/{STORY_ID}.json")
    if not story:
        print("[error] Could not fetch story")
        return []

    print(f"Post: {story.get('title')}")
    print(f"Score: {story.get('score')} | Comments: {story.get('descendants')}")
    print(f"Checking for new comments...")

    all_comments = _collect_all_comment_ids(STORY_ID)
    new_comments = [c for c in all_comments if c["id"] not in seen]

    if new_comments:
        print(f"\n{'='*60}")
        print(f"NEW COMMENTS: {len(new_comments)}")
        print(f"{'='*60}")
        for c in sorted(new_comments, key=lambda x: x["time"]):
            ts = datetime.fromtimestamp(c["time"]).strftime("%H:%M:%S")
            indent = "  " * c["depth"]
            text_preview = c["text"].replace("\n", " ")[:150]
            print(f"\n{indent}[{ts}] {c['by']}:")
            print(f"{indent}  {text_preview}")
    else:
        print("No new comments.")

    # Update state
    state["seen_ids"] = [c["id"] for c in all_comments]
    state["last_check"] = datetime.now().isoformat()
    _save_state(state)

    return new_comments


def monitor_loop(interval_minutes: int = 5):
    """Continuously monitor for new comments."""
    print(f"Monitoring HN post {STORY_ID} every {interval_minutes} min...")
    print(f"Press Ctrl+C to stop.\n")
    while True:
        print(f"\n--- Check at {datetime.now().strftime('%H:%M:%S')} ---")
        check_once()
        time.sleep(interval_minutes * 60)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        mins = 5
        for i, arg in enumerate(sys.argv):
            if arg == "--interval" and i + 1 < len(sys.argv):
                mins = int(sys.argv[i + 1])
        monitor_loop(mins)
    else:
        check_once()
