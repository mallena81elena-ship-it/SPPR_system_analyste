# -*- coding: utf-8 -*-
"""
Метрики и графики для гл. 4 магистерской диссертации (СППР).

Запуск:
  python diploma_metrics.py
  python diploma_metrics.py --charts-only

Результат:
  diploma_results/metrics_orders.csv
  diploma_results/summary_for_thesis.md
  diploma_results/charts/*.png
"""
from __future__ import annotations

import argparse
import csv
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(r"c:\Users\kholina\Desktop\Диплом\Примеры")
CSV = ROOT / "Ошибки_1.csv"
MATERIALS = Path(__file__).parent
OUT = MATERIALS / "diploma_results"
CHARTS = OUT / "charts"

SYNTH = frozenset(
    {
        "DEMO_ORDER_001",
        "DEMO_ORDER_001",
        "DEMO_ORDER_001",
        "DEMO_ORDER_001",
        "DEMO_ORDER_001",
        "DEMO_ORDER_001",
        "DEMO_ORDER_001",
    }
)

# импорт движка СППР
import sppr_diagnose as sd  # noqa: E402


def read_errors_csv() -> list[dict]:
    text = CSV.read_bytes().decode("utf-8-sig")
    raw = list(csv.reader(text.splitlines(), delimiter=";"))[1:]
    out = []
    for row in raw:
        if len(row) < 3:
            continue
        oid = row[1].strip()
        err = row[2].strip()
        if not oid:
            continue
        # Корпус только по order_id: CSV с «;» внутри Ошибки ломает колонку Корпус
        corpus = "SYNTH" if oid in SYNTH else "LIVE"
        out.append({"order_id": oid, "error": err, "corpus": corpus})
    return out


def typeid_prefix(tid: str) -> str:
    if not tid:
        return ""
    return tid.split("(")[0].strip()


def analyze_order(oid: str, err: str, corpus: str) -> dict:
    folder = sd.find_folder(oid)
    rec = {
        "order_id": oid,
        "corpus": corpus,
        "folder_found": bool(folder),
        "csv_error": err[:120],
    }
    if not folder:
        rec["status"] = "NO_FOLDER"
        return rec

    t0 = time.perf_counter()
    csv_cls, csv_tids = sd.classify_csv_label(err)
    lat = sd.extract_latest_2010(folder)
    sppr_cls, _ = sd.resolve_class(csv_cls, err, lat, folder)
    origin, cpt = sd.detect_order_origin(folder)
    rec["diagnose_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    rec["csv_class"] = csv_cls
    rec["sppr_class"] = sppr_cls
    rec["origin"] = origin
    rec["cpt"] = cpt

    json_roots = []
    if lat and lat.get("items"):
        json_roots = [x["typeID"] for x in lat["items"]]
    rec["typeid_json"] = json_roots[0] if json_roots else ""
    rec["typeid_csv"] = csv_tids[0] if csv_tids else ""

    if corpus == "SYNTH" or oid in SYNTH:
        rec["typeid_match"] = "SYNTH"
        rec["status"] = "SYNTH_EXPECTED"
    elif csv_cls == "A":
        if not lat:
            rec["typeid_match"] = "no"
            rec["status"] = "NO_2010"
        else:
            ok = not csv_tids or any(
                typeid_prefix(cr) == typeid_prefix(jr)
                for cr in csv_tids
                for jr in json_roots
            )
            rec["typeid_match"] = "yes" if ok else "no"
            rec["status"] = "OK" if ok else "MISMATCH"
    elif csv_cls in ("B_2010", "B_0993"):
        rec["typeid_match"] = "n/a"
        rec["status"] = "B_LAYER"
        if sppr_cls == "B->A" and lat and lat.get("items"):
            rec["typeid_json"] = lat["items"][0].get("typeID") or ""
    else:
        rec["typeid_match"] = "n/a"
        rec["status"] = csv_cls

    rec["has_0993"] = len(list(folder.glob("*0993*"))) > 0
    rec["has_0973"] = len(list(folder.glob("*0973*"))) > 0
    rec["n_2010"] = len(
        [p for p in folder.glob("*2010*") if "SYNTH" not in p.name]
    )
    return rec


def write_csv(rows: list[dict]) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "metrics_orders.csv"
    if not rows:
        return path
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";")
        w.writeheader()
        w.writerows(rows)
    return path


def compute_summary(rows: list[dict]) -> dict:
    live = [r for r in rows if r.get("corpus") == "LIVE"]
    a_live = [r for r in live if r.get("csv_class") == "A"]
    ok = [r for r in a_live if r.get("status") == "OK"]
    b2010 = [r for r in live if r.get("csv_class") == "B_2010"]
    b2010_ba = [r for r in b2010 if r.get("sppr_class") == "B->A"]
    b0993 = [r for r in live if r.get("csv_class") == "B_0993"]
    b0993_ba = [r for r in b0993 if r.get("sppr_class") == "B->A"]
    times = [float(r["diagnose_ms"]) for r in live if r.get("diagnose_ms")]

    tid_ok = len(ok)
    tid_total = len(a_live)
    acc = (tid_ok / tid_total * 100) if tid_total else 0.0

    return {
        "n_orders_total": len(rows),
        "n_live": len(live),
        "n_synth": len(rows) - len(live),
        "typeid_accuracy_pct": round(acc, 1),
        "typeid_ok": tid_ok,
        "typeid_denominator": tid_total,
        "b2010_reclassified_pct": round(
            len(b2010_ba) / len(b2010) * 100, 1
        )
        if b2010
        else 0.0,
        "b2010_n": len(b2010),
        "b2010_ba_n": len(b2010_ba),
        "b0993_reclassified_pct": round(
            len(b0993_ba) / len(b0993) * 100, 1
        )
        if b0993
        else 0.0,
        "b0993_n": len(b0993),
        "b0993_ba_n": len(b0993_ba),
        "avg_diagnose_ms": round(sum(times) / len(times), 1) if times else 0,
        "p95_diagnose_ms": round(sorted(times)[int(len(times) * 0.95)], 1)
        if len(times) > 1
        else (times[0] if times else 0),
    }


def write_summary_md(summary: dict, rows: list[dict]) -> Path:
    path = OUT / "summary_for_thesis.md"
    by_class = Counter(r.get("sppr_class") for r in rows if r.get("corpus") == "LIVE")
    by_origin = Counter(r.get("origin") for r in rows if r.get("corpus") == "LIVE")
    tid_freq = Counter(
        typeid_prefix(r.get("typeid_json") or r.get("typeid_csv") or "?")
        for r in rows
        if r.get("corpus") == "LIVE" and (r.get("typeid_json") or r.get("typeid_csv"))
    )
    top_tid = ", ".join(f"**{k}** ({v})" for k, v in tid_freq.most_common(8))

    body = f"""# Сводка метрик для магистерской (автогенерация)

## Ключевые показатели (вставка в гл. 4)

| Показатель | Значение |
|------------|----------|
| Заказов в корпусе (всего) | {summary['n_orders_total']} |
| Живых кейсов (LIVE) | {summary['n_live']} |
| Учебных SYNTH_TRAIN | {summary['n_synth']} |
| **Точность определения typeID** (класс A, LIVE) | **{summary['typeid_accuracy_pct']}%** ({summary['typeid_ok']}/{summary['typeid_denominator']}) |
| Переклассификация «Не пришел 2010» → B→A | {summary['b2010_reclassified_pct']}% ({summary['b2010_ba_n']}/{summary['b2010_n']}) |
| Переклассификация «Не пришел 0993» → B→A | {summary['b0993_reclassified_pct']}% ({summary['b0993_ba_n']}/{summary['b0993_n']}) |
| Среднее время отчёта движка | {summary['avg_diagnose_ms']} мс |
| P95 время отчёта | {summary['p95_diagnose_ms']} мс |

## Распределение классов СППР (LIVE)

{chr(10).join(f'- {k}: {v}' for k, v in by_class.most_common())}

## CRM vs ИМК (LIVE)

{chr(10).join(f'- {k}: {v}' for k, v in by_origin.most_common())}

## Частые typeID (LIVE)

{top_tid}

## Файлы графиков

См. папку `diploma_results/charts/` — рисунки для ВКР.

---
*Сгенерировано `diploma_metrics.py`*
"""
    path.write_text(body, encoding="utf-8")
    return path


def _setup_matplotlib_ru():
    """Шрифт с кириллицей (Windows). Подписи править в make_charts ниже."""
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = [
        "Segoe UI",
        "Arial",
        "DejaVu Sans",
        "Tahoma",
        "sans-serif",
    ]
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def _annotate_bars(ax, bars) -> None:
    for bar in bars:
        h = bar.get_height()
        if h <= 0:
            continue
        ax.annotate(
            f"{int(h)}",
            xy=(bar.get_x() + bar.get_width() / 2, h),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=10,
        )


def make_charts(rows: list[dict], summary: dict) -> None:
    try:
        plt = _setup_matplotlib_ru()
    except ImportError:
        print("matplotlib не установлен: pip install matplotlib")
        return

    CHARTS.mkdir(parents=True, exist_ok=True)
    DPI = 200  # для вставки в Word / печати

    live = [r for r in rows if r.get("corpus") == "LIVE"]

    # --- Рис. 4.x: правьте подписи здесь ---
    # 1. Классы СППР
    cls = Counter(r.get("sppr_class") or "?" for r in live)
    fig, ax = plt.subplots(figsize=(7, 4))
    bars = ax.bar(list(cls.keys()), list(cls.values()), color="#4C72B0")
    _annotate_bars(ax, bars)
    ax.set_title("Распределение классов СППР (живой корпус)")
    ax.set_ylabel("Число заказов")
    ax.set_xlabel("Класс")
    plt.xticks(rotation=20, ha="right")
    fig.tight_layout()
    fig.savefig(CHARTS / "01_sppr_classes.png", dpi=DPI)
    plt.close()

    # 2. Точность typeID — файл 02_typeid_accuracy.png
    ok = summary["typeid_ok"]
    bad = summary["typeid_denominator"] - ok
    fig, ax = plt.subplots(figsize=(6, 4.5))
    labels = ["Верно", "Ошибка"]
    values = [ok, bad]
    colors = ["#2E7D32", "#C62828"] if bad else ["#2E7D32", "#E0E0E0"]
    bars = ax.bar(labels, values, color=colors, width=0.55)
    _annotate_bars(ax, bars)
    ax.set_ylabel("Число заказов (класс A)")
    ax.set_title(
        f"Точность определения typeID: {summary['typeid_accuracy_pct']}% "
        f"({ok} из {summary['typeid_denominator']})"
    )
    ax.set_ylim(0, max(values) * 1.15 if max(values) else 1)
    if bad == 0:
        ax.text(
            0.5,
            0.02,
            "Ошибок сверки с JSON нет",
            transform=ax.transAxes,
            ha="center",
            fontsize=9,
            color="#555",
        )
    fig.tight_layout()
    fig.savefig(CHARTS / "02_typeid_accuracy.png", dpi=DPI)
    plt.close()

    # 3. Частота typeID
    tid = Counter(
        typeid_prefix(r.get("typeid_json") or r.get("typeid_csv") or "")
        for r in live
    )
    tid.pop("", None)
    top = tid.most_common(12)
    if top:
        fig, ax = plt.subplots(figsize=(8, 4.5))
        ax.barh(
            [t[0] for t in reversed(top)],
            [t[1] for t in reversed(top)],
            color="#5C6BC0",
        )
        ax.set_title("Частота кодов typeID в INT-2010 (LIVE)")
        ax.set_xlabel("Число заказов")
        fig.tight_layout()
        fig.savefig(CHARTS / "03_typeid_frequency.png", dpi=DPI)
        plt.close()

    # 4. CRM vs ИМК
    orig = Counter(r.get("origin") for r in live)
    label_map = {"CRM": "CRM (9000…)", "IMK": "ИМК (5005…)"}
    pie_labels = [label_map.get(k, k) for k in orig.keys()]
    fig, ax = plt.subplots(figsize=(5.5, 4.5))
    ax.pie(
        orig.values(),
        labels=pie_labels,
        autopct="%1.0f%%",
        colors=["#42A5F5", "#FF7043"],
        startangle=90,
    )
    ax.set_title("Источник заказа в корпусе")
    fig.savefig(CHARTS / "04_crm_imk.png", dpi=DPI)
    plt.close()

    # 5. Время движка
    times = [float(r["diagnose_ms"]) for r in live if r.get("diagnose_ms")]
    if times:
        fig, ax = plt.subplots(figsize=(6.5, 4))
        ax.hist(
            times,
            bins=min(20, max(5, len(times) // 3)),
            color="#4C72B0",
            edgecolor="white",
        )
        ax.axvline(
            summary["avg_diagnose_ms"],
            color="#C62828",
            linestyle="--",
            linewidth=2,
            label=f"Среднее: {summary['avg_diagnose_ms']} мс",
        )
        ax.set_title("Время построения отчёта sppr_diagnose")
        ax.set_xlabel("мс")
        ax.set_ylabel("Число заказов")
        ax.legend()
        fig.tight_layout()
        fig.savefig(CHARTS / "05_diagnose_latency.png", dpi=DPI)
        plt.close()

    print(f"Charts: {CHARTS}")
    print("Подписи: diploma_metrics.py, функция make_charts(); пересборка: python diploma_metrics.py --charts-only")


def load_metrics_csv() -> list[dict]:
    path = OUT / "metrics_orders.csv"
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter=";"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--charts-only", action="store_true")
    args = ap.parse_args()

    if not args.charts_only:
        orders = read_errors_csv()
        rows = [analyze_order(o["order_id"], o["error"], o["corpus"]) for o in orders]
        write_csv(rows)
        summary = compute_summary(rows)
        write_summary_md(summary, rows)
    else:
        rows = load_metrics_csv()
        summary = compute_summary(rows)

    make_charts(rows, summary)
    print(f"OK: {OUT / 'summary_for_thesis.md'}")


if __name__ == "__main__":
    main()
