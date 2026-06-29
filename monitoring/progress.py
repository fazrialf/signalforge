"""
SignalForge Progress Tracker
Updates build_status.json and sends progress updates to Telegram.
"""
import json
import asyncio
import aiohttp
from datetime import datetime
from pathlib import Path
import sys
import os

ROOT = Path(__file__).parent.parent
STATUS_PATH = ROOT / "config" / "build_status.json"

# Load token/chat from settings
sys.path.insert(0, str(ROOT))
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def load_status() -> dict:
    with open(STATUS_PATH) as f:
        return json.load(f)


def save_status(status: dict):
    with open(STATUS_PATH, "w") as f:
        json.dump(status, f, indent=2)


def calc_progress(status: dict) -> tuple[int, int]:
    """Returns (sprint_progress_pct, overall_progress_pct)"""
    tasks = status.get("tasks", [])
    total_tasks = len(tasks)
    done_tasks = sum(1 for t in tasks if t.get("status") in ("done", "completed"))
    sprint_pct = int((done_tasks / total_tasks) * 100) if total_tasks else 100

    # Use sprint_progress if available, else calculate from tasks
    if "sprint_progress" in status:
        sprint_pct = status["sprint_progress"]

    overall_pct = status.get("overall_progress", sprint_pct)
    return sprint_pct, overall_pct


def progress_bar(pct: int, width: int = 12) -> str:
    """Returns a text progress bar like [========----] 67%"""
    filled = int(width * pct / 100)
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    return f"[{bar}] {pct}%"


async def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                txt = await resp.text()
                print(f"[Telegram ERROR] {resp.status}: {txt}")


def mark_task_done(task_id: str):
    """Mark a task as done and save."""
    status = load_status()
    tasks = status.get("tasks", [])
    for t in tasks:
        if t["id"] == task_id:
            t["status"] = "done"
            break
    sprint_pct, overall_pct = calc_progress(status)
    status["sprint_progress"] = sprint_pct
    status["overall_progress"] = overall_pct
    save_status(status)
    return status, sprint_pct, overall_pct


def mark_task_inprogress(task_id: str):
    """Mark a task as in_progress and save."""
    status = load_status()
    tasks = status.get("tasks", [])
    for t in tasks:
        if t["id"] == task_id:
            t["status"] = "in_progress"
            break
    save_status(status)
    return status


async def send_progress_update(task_name: str, task_id: str, done: bool = True):
    """Mark task done/in_progress and send a Telegram update."""
    if done:
        status, sprint_pct, overall_pct = mark_task_done(task_id)
    else:
        status = mark_task_inprogress(task_id)
        sprint_pct, overall_pct = calc_progress(status)

    sprint_bar   = progress_bar(sprint_pct)
    overall_bar  = progress_bar(overall_pct)
    sprint_name  = status.get("sprint_name", "Sprint ?")
    sprint_num   = status.get("current_sprint", 0)
    total_sprints = status.get("total_sprints", 9)
    tasklist     = status.get("tasks", [])
    done_count   = sum(1 for t in tasklist if t.get("status") == "done")
    total_tasks  = len(tasklist)

    # Build task list
    task_lines = []
    for t in tasklist:
        if t.get("status") == "done":
            icon = "\u2705"
        elif t.get("status") == "in_progress":
            icon = "\u23f3"
        else:
            icon = "\u25ab\ufe0f"
        task_lines.append(f"{icon} {t.get('name', t.get('id', '?'))}")

    task_list = "\n".join(task_lines)

    if done:
        action_line = f"\u2705 <b>Done:</b> {task_name}"
    else:
        action_line = f"\u23f3 <b>Started:</b> {task_name}"

    msg = (
        f"\U0001f528 <b>SignalForge Build Update</b>\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"{action_line}\n\n"
        f"\U0001f4e6 <b>Sprint {sprint_num}/{total_sprints} \u2014 {sprint_name}</b>\n"
        f"<code>{sprint_bar}</code> ({done_count}/{total_tasks} tasks)\n\n"
        f"\U0001f30d <b>Overall Progress</b>\n"
        f"<code>{overall_bar}</code>\n\n"
        f"<b>Tasks:</b>\n{task_list}"
    )
    await send_telegram(msg)


async def send_sprint_complete(sprint_num: int, sprint_name: str, next_sprint: str):
    """Send a sprint completion summary."""
    status = load_status()
    _, overall_pct = calc_progress(status)
    overall_bar = progress_bar(overall_pct)

    msg = (
        f"\U0001f389 <b>Sprint {sprint_num} Complete!</b>\n"
        f"<b>{sprint_name}</b> is done.\n\n"
        f"\U0001f30d <b>Overall Progress</b>\n"
        f"<code>{overall_bar}</code>\n\n"
        f"\u27a1\ufe0f Next: <b>Sprint {sprint_num + 1} \u2014 {next_sprint}</b>"
    )
    await send_telegram(msg)


if __name__ == "__main__":
    # Quick test
    asyncio.run(send_telegram(
        "\U0001f528 <b>SignalForge Build Started!</b>\n\n"
        "Sprint 1/9 \u2014 Foundation is underway.\n"
        "You will receive progress updates here as each task completes."
    ))
    print("Test message sent.")
