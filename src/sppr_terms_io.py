# -*- coding: utf-8 -*-
"""Чтение/запись терминов L2 и обратной связи для Streamlit."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

MAT = Path(__file__).parent
L2_TERMS = MAT / "sppr_l2_terms.json"
L2_FEEDBACK = MAT / "sppr_l2_feedback.jsonl"
L2_EVENTS = MAT / "sppr_l2_events.jsonl"
CODE_USER = MAT / "json_code_lookups_user.json"


def load_l2_terms() -> dict[str, Any]:
    if not L2_TERMS.is_file():
        return {"version": 1, "terms": []}
    return json.loads(L2_TERMS.read_text(encoding="utf-8"))


def save_l2_terms(data: dict[str, Any]) -> None:
    L2_TERMS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_l2_term(entry: dict[str, Any]) -> None:
    data = load_l2_terms()
    terms: list[dict] = list(data.get("terms") or [])
    tid = entry.get("id") or _slug(entry.get("user_terms") or ["term"])
    entry["id"] = tid
    entry.setdefault("added", date.today().isoformat())
    terms = [t for t in terms if t.get("id") != tid]
    terms.append(entry)
    data["terms"] = terms
    save_l2_terms(data)


def delete_l2_term(term_id: str) -> None:
    data = load_l2_terms()
    data["terms"] = [t for t in (data.get("terms") or []) if t.get("id") != term_id]
    save_l2_terms(data)


def _slug(words: list[str]) -> str:
    base = re.sub(r"[^\w]+", "_", (words[0] if words else "term").lower())[:40]
    return base or "term"


def load_code_user() -> dict[str, Any]:
    if not CODE_USER.is_file():
        return {}
    return json.loads(CODE_USER.read_text(encoding="utf-8"))


def save_code_user(data: dict[str, Any]) -> None:
    CODE_USER.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_feedback() -> list[dict[str, Any]]:
    if not L2_FEEDBACK.is_file():
        return []
    rows = []
    for line in L2_FEEDBACK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def append_feedback(entry: dict[str, Any]) -> None:
    entry.setdefault("date", date.today().isoformat())
    with L2_FEEDBACK.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def save_answer_feedback(
    *,
    mode: str,
    order_id: str,
    question: str,
    answer: str,
    rating: str,
    l2_etalon_answer: str = "",
    msg_id: str = "",
    use_llm: bool | None = None,
) -> None:
    """rating: good | bad (смайлы); legacy: helpful, not_helpful, neutral→bad"""
    row: dict[str, Any] = {
        "type": "answer_rating",
        "mode": mode,
        "order_id": order_id,
        "question": question,
        "answer_preview": (answer or "")[:2000],
        "rating": rating,
        "l2_etalon_answer": (l2_etalon_answer or "").strip()[:8000],
        "msg_id": msg_id,
    }
    if use_llm is not None:
        row["use_llm"] = bool(use_llm)
    append_feedback(row)


def _normalize_rating(raw: str | None) -> str:
    legacy = {"helpful": "good", "not_helpful": "bad", "corrected": "bad"}
    key = legacy.get(str(raw or "").strip(), str(raw or "").strip())
    if key in ("good", "bad"):
        return key
    if key == "neutral":
        return "bad"
    return "other"


def save_feedback_rows(rows: list[dict[str, Any]]) -> None:
    with L2_FEEDBACK.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def delete_feedback_indices(indices: set[int]) -> int:
    rows = load_feedback()
    keep = [r for i, r in enumerate(rows) if i not in indices]
    removed = len(rows) - len(keep)
    if removed:
        save_feedback_rows(keep)
    return removed


def clear_all_feedback() -> int:
    n = len(load_feedback())
    if n:
        save_feedback_rows([])
    return n


def feedback_row_label(index: int, row: dict[str, Any]) -> str:
    oid = row.get("order_id") or "—"
    mode = row.get("mode") or row.get("type") or "—"
    rating = _normalize_rating(row.get("rating"))
    if rating == "good":
        mark = "😊"
    elif rating == "bad":
        mark = "😞"
    else:
        mark = "·"
    q = (row.get("question") or row.get("context") or "")[:50]
    return f"#{index + 1} {mark} | {oid} | {mode} | {q}"


def feedback_summary() -> dict[str, int]:
    rows = load_feedback()
    s = {"good": 0, "bad": 0, "other": 0, "total": len(rows)}
    for r in rows:
        if r.get("type") != "answer_rating":
            s["other"] += 1
            continue
        key = _normalize_rating(r.get("rating"))
        if key in s:
            s[key] += 1
        else:
            s["other"] += 1
    return s


def load_events() -> list[dict[str, Any]]:
    if not L2_EVENTS.is_file():
        return []
    rows = []
    for line in L2_EVENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def append_event(
    event: str,
    *,
    mode: str = "",
    order_id: str = "",
    ok: bool = True,
    total_ms: float | None = None,
    use_llm: bool | None = None,
    detail: str = "",
) -> None:
    """Журнал онлайн-действий L2 (пилот Streamlit)."""
    row: dict[str, Any] = {
        "type": "sppr_event",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        "mode": mode,
        "order_id": (order_id or "").strip(),
        "ok": bool(ok),
    }
    if total_ms is not None:
        row["total_ms"] = round(float(total_ms), 1)
    if use_llm is not None:
        row["use_llm"] = bool(use_llm)
    if detail:
        row["detail"] = detail[:500]
    with L2_EVENTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round(0.95 * (len(s) - 1)))))
    return s[idx]


_REQUEST_EVENTS = frozenset(
    {
        "analyze_brief",
        "analyze_llm",
        "analyze_full",
        "qa_question",
        "kb_question",
    }
)


def online_metrics_summary() -> dict[str, Any]:
    """Топ-5 онлайн-метрик по журналу событий и обратной связи."""
    events = load_events()
    fb = [r for r in load_feedback() if r.get("type") == "answer_rating"]
    good = sum(1 for r in fb if _normalize_rating(r.get("rating")) == "good")
    bad = sum(1 for r in fb if _normalize_rating(r.get("rating")) == "bad")
    rated = good + bad

    by_mode_fb: dict[str, dict[str, int]] = {}
    for r in fb:
        m = str(r.get("mode") or "—")
        by_mode_fb.setdefault(m, {"good": 0, "bad": 0})
        key = _normalize_rating(r.get("rating"))
        if key in ("good", "bad"):
            by_mode_fb[m][key] += 1

    timed = [
        float(e["total_ms"])
        for e in events
        if e.get("total_ms") is not None and float(e["total_ms"]) >= 0
    ]
    timed_diag = [
        float(e["total_ms"])
        for e in events
        if e.get("event", "").startswith("analyze_")
        and e.get("total_ms") is not None
    ]
    timed_llm = [
        float(e["total_ms"])
        for e in events
        if e.get("event") == "analyze_llm" and e.get("total_ms") is not None
    ]
    timed_no_llm = [
        float(e["total_ms"])
        for e in events
        if e.get("event") == "analyze_brief" and e.get("total_ms") is not None
    ]

    requests = [e for e in events if e.get("event") in _REQUEST_EVENTS]
    ok_n = sum(1 for e in requests if e.get("ok"))
    analyze = [e for e in events if e.get("event", "").startswith("analyze_")]
    analyze_llm = [e for e in events if e.get("event") == "analyze_llm"]
    analyze_all = [e for e in events if e.get("event") in ("analyze_brief", "analyze_llm", "analyze_full")]

    per_order: dict[str, int] = {}
    for e in events:
        oid = (e.get("order_id") or "").strip()
        if oid:
            per_order[oid] = per_order.get(oid, 0) + 1
    per_order_counts = list(per_order.values())
    avg_events = (
        sum(per_order_counts) / len(per_order_counts) if per_order_counts else 0.0
    )

    fb_llm = [r for r in fb if r.get("use_llm") is True]
    fb_no_llm = [r for r in fb if r.get("use_llm") is False]

    return {
        "events_total": len(events),
        "feedback_rated": rated,
        "helpfulness_pct": round(100.0 * good / rated, 1) if rated else None,
        "good": good,
        "bad": bad,
        "by_mode_feedback": by_mode_fb,
        "p95_total_ms": round(_p95(timed), 1) if timed else None,
        "p95_diag_ms": round(_p95(timed_diag), 1) if timed_diag else None,
        "p95_analyze_llm_ms": round(_p95(timed_llm), 1) if timed_llm else None,
        "p95_analyze_brief_ms": round(_p95(timed_no_llm), 1) if timed_no_llm else None,
        "success_rate_pct": round(100.0 * ok_n / len(requests), 1) if requests else None,
        "requests": len(requests),
        "requests_ok": ok_n,
        "llm_adoption_pct": (
            round(100.0 * len(analyze_llm) / len(analyze_all), 1) if analyze_all else None
        ),
        "analyze_total": len(analyze_all),
        "analyze_llm": len(analyze_llm),
        "orders_with_events": len(per_order),
        "avg_events_per_order": round(avg_events, 2),
        "feedback_with_llm": len(fb_llm),
        "feedback_without_llm": len(fb_no_llm),
    }


def events_to_csv() -> str:
    import csv
    import io

    rows = load_events()
    if not rows:
        return "ts,event,mode,order_id,ok,total_ms,use_llm\n"
    buf = io.StringIO()
    keys = sorted({k for r in rows for k in r.keys()})
    w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def clear_all_events() -> int:
    n = len(load_events())
    if n:
        L2_EVENTS.write_text("", encoding="utf-8")
    return n


def feedback_to_csv() -> str:
    """Таблица для Excel / анализа в ВКР."""
    import csv
    import io

    rows = load_feedback()
    if not rows:
        return "order_id,rating,question,mode,date\n"
    buf = io.StringIO()
    keys = sorted({k for r in rows for k in r.keys()})
    w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def run_rebuild_catalog() -> tuple[int, str, str]:
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(MAT / "build_json_field_catalog.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(MAT),
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""
