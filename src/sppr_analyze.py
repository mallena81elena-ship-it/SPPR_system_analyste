# -*- coding: utf-8 -*-
"""
Оркестратор СППР: diagnose (факты) + опционально LLM API (оформление ответа L2).

Без API-ключа печатает report из sppr_diagnose — это уже валидный вывод движка.

Примеры:
  python sppr_analyze.py --order DEMO_ORDER_001
  python sppr_analyze.py --order DEMO_ORDER_001 --full
  python sppr_analyze.py --order DEMO_ORDER_001 --llm
  set SPPR_LLM_API_KEY=... & set SPPR_LLM_BASE_URL=https://api.openai.com/v1 & python sppr_analyze.py --order DEMO_ORDER_001 --llm

Переменные: см. `.env.example` и `НАСТРОЙКА_GIGACHAT.md` (GigaChat / OpenAI-compatible provider).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from sppr_llm import call_llm, load_env

ROOT = Path(r"c:\Users\kholina\Desktop\Диплом\Примеры")
MATERIALS = Path(__file__).parent
RULES = MATERIALS / "СППР_ПРАВИЛА_LLM.md"
SYSTEM_PROMPT = MATERIALS / "СППР_СИСТЕМНЫЙ_ПРОМПТ.md"
KB = MATERIALS / "kb_typeid_index.json"
DIAGNOSE = MATERIALS / "sppr_diagnose.py"
ANALIZ = Path(r"c:\Users\kholina\Desktop\Анализ")
FEW_SHOT = [
    ANALIZ / "итого_DEMO_ORDER_002.md",
    ANALIZ / "итого_DEMO_ORDER_003.md",
    ANALIZ / "итого_DEMO_ORDER_004.md",
]


def find_folder(order_id: str) -> Path | None:
    for p in ROOT.iterdir():
        if p.is_dir() and order_id in p.name:
            return p
    return None


def is_clean_integration_report(report: str) -> bool:
    return "✅ Ошибок в интеграционных сценариях" in (report or "")


def run_diagnose(order_id: str, folder: Path | None, out: Path, *, full: bool = False) -> str:
    cmd = [
        sys.executable,
        str(DIAGNOSE),
        "--order",
        order_id,
        "--out",
        str(out),
        "--quiet",
    ]
    if full:
        cmd.append("--full")
    if folder:
        cmd.extend(["--folder", str(folder)])
    subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(MATERIALS),
    )
    return out.read_text(encoding="utf-8")


def kb_hints_for_report(report: str) -> str:
    if not KB.is_file():
        return ""
    data = json.loads(KB.read_text(encoding="utf-8"))
    hints = []
    for ent in data.get("entries") or []:
        tid = ent.get("typeID") or ""
        if tid and tid.split("(")[0] in report:
            hints.append(
                f"- {tid}: {ent.get('l2_action') or ent.get('crm') or ''}"
            )
    return "\n".join(hints[:8])


def load_system_prompt() -> str:
    if not SYSTEM_PROMPT.is_file():
        return "Ты аналитик L2 CRM по ошибкам интеграции заказов."
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    if "```" in text:
        parts = text.split("```")
        for i, p in enumerate(parts):
            if i % 2 == 1 and p.strip():
                return p.strip()
    return text[:12000]


def few_shot_block() -> str:
    blocks = []
    for p in FEW_SHOT:
        if p.is_file():
            blocks.append(f"### Пример ({p.name})\n{p.read_text(encoding='utf-8')[:3500]}")
    if not blocks:
        return ""
    return "## Эталоны формата ответа (не копировать факты другого заказа)\n\n" + "\n\n".join(blocks)


def _parse_folder_from_report(report: str) -> Path | None:
    m = re.search(r"^Папка:\s*(.+)$", report, re.M)
    if not m:
        return None
    p = Path(m.group(1).strip())
    return p if p.is_dir() else None


def _meta_from_latest_2010_json(folder: Path) -> dict:
    """INT-2010 из JSON папки заказа (когда в report только краткий блок)."""
    meta = {
        "date_time": "",
        "message_id": "",
        "filename": "",
        "file_uri": "",
        "folder": str(folder),
        "items": [],
    }
    try:
        from sppr_diagnose import extract_latest_2010
    except ImportError:
        return meta
    lat = extract_latest_2010(folder)
    if not lat:
        return meta
    meta["date_time"] = (lat.get("dateTime") or "").strip()
    meta["filename"] = (lat.get("file") or "").strip()
    if meta["filename"]:
        fp = folder / meta["filename"]
        if fp.is_file():
            meta["file_uri"] = fp.resolve().as_uri()
    mid = lat.get("messageId") or ""
    if mid:
        meta["message_id"] = str(mid).strip()
    for it in lat.get("items") or []:
        tid = (it.get("typeID") or "").strip()
        note = (it.get("note") or "").strip()
        if tid and note:
            meta["items"].append(
                {
                    "type_id": tid,
                    "note": note,
                    "date_time": meta["date_time"],
                }
            )
    return meta


def _meta_from_brief_2010_line(report: str) -> dict:
    """Краткий отчёт: **INT-2010** (dt): **note**."""
    meta = {
        "date_time": "",
        "message_id": "",
        "filename": "",
        "file_uri": "",
        "folder": "",
        "items": [],
    }
    m = re.search(
        r"\*\*INT-2010\*\*\s*\(([^)]+)\):\s*\*{0,2}(.+?)\*{0,2}\s*$",
        report,
        re.M | re.I,
    )
    if not m:
        return meta
    dt = m.group(1).strip()
    note = re.sub(r"\*+", "", m.group(2)).strip()
    meta["date_time"] = dt
    tid = "143"
    rm = re.search(r"корень:\s*(\d+)", report, re.I)
    if rm:
        tid = rm.group(1)
    else:
        tm = re.search(r"(\d{3})\(ZSD_AIF_SDSLS_IN\)", report, re.I)
        if tm:
            tid = tm.group(1)
    if note:
        meta["items"].append(
            {"type_id": tid, "note": note, "date_time": dt}
        )
    return meta


def extract_2010_errors(report: str, folder: Path | None = None) -> dict:
    """
    Метаданные корневого INT-2010 + список typeID/note для шапки тикета.
  folder — папка JSON заказа (для file:// ссылки на выгрузку).
    """
    folder = folder or _parse_folder_from_report(report)
    m = re.search(
        r"【Корень из INT-2010.*?\】(.*?)(?=\n【|\Z)",
        report,
        re.S | re.I,
    )
    meta = {
        "date_time": "",
        "message_id": "",
        "filename": "",
        "file_uri": "",
        "folder": str(folder) if folder else "",
        "items": [],
    }
    if not m:
        brief = _meta_from_brief_2010_line(report)
        if brief.get("items"):
            meta = {**meta, **brief, "folder": meta["folder"]}
            if folder and not meta.get("filename"):
                fj = _meta_from_latest_2010_json(folder)
                for key in ("filename", "file_uri", "message_id"):
                    if fj.get(key) and not meta.get(key):
                        meta[key] = fj[key]
        if folder and not meta.get("items"):
            meta = _meta_from_latest_2010_json(folder)
        return _merge_2010_meta_from_folder(meta, folder)

    block = m.group(1)
    head = block[:800]
    dm = re.search(
        r"INT-2010,\s*([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+)",
        head,
    )
    if dm:
        meta["date_time"] = dm.group(1).strip()
    mm = re.search(r"messageId=([0-9a-fA-F]+)", head)
    if mm:
        meta["message_id"] = mm.group(1).strip()
    fm = re.search(r"файл\s+(\S+\.json)", head, re.I)
    if fm:
        meta["filename"] = fm.group(1).strip()
        if folder:
            fp = folder / meta["filename"]
            if fp.is_file():
                meta["file_uri"] = fp.resolve().as_uri()

    cur_tid = ""
    cur_dt = meta["date_time"]
    for line in block.splitlines():
        dtm = re.search(r"\[([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+)\]", line)
        if dtm:
            cur_dt = dtm.group(1)
        tm = re.search(r"typeID:\s*(\S+)", line)
        if tm:
            cur_tid = tm.group(1).strip()
            continue
        nm = re.search(r"note:\s*(.+)", line, re.I)
        if nm and cur_tid:
            meta["items"].append(
                {
                    "type_id": cur_tid,
                    "note": nm.group(1).strip(),
                    "date_time": cur_dt or meta["date_time"],
                }
            )
            cur_tid = ""

    seen: set[tuple[str, str]] = set()
    unique = []
    for it in meta["items"]:
        key = (it["type_id"], it["note"])
        if key not in seen:
            seen.add(key)
            unique.append(it)
    meta["items"] = unique
    return _merge_2010_meta_from_folder(meta, folder)


def extract_znd_ticket_line(report: str) -> str:
    """Явная строка про номер ЗНД для тикета (есть / нет в JSON)."""
    if re.search(r"ЗНД в JSON:.*не найден", report, re.I):
        hints = []
        for m in re.finditer(
            r"поз\.\s*(\d+).*ZZStatusUserItem=(E\d+).*?(E0010|ЗНД создано)",
            report,
            re.I,
        ):
            hints.append(f"поз. {m.group(1)} — {m.group(2)} («ЗНД создано»)")
        hint_txt = (
            "; ".join(dict.fromkeys(hints))
            if hints
            else "по 0992/2010 — признак ЗНД в note и статусах"
        )
        return (
            "**Номер ЗНД:** **отсутствует** — ни в одном JSON обмене по заказу "
            "(INT-0973, 0993, CrmOfdChanged / Ofd) **не найден** идентификатор ЗНД "
            "(`4000…`). Это не означает отсутствие ЗНД в процессе.\n"
            f"**Признак в обменах:** {hint_txt}.\n"
            "**Действие:** номер ЗНД взять из **CRM Web UI** (не придумывать)."
        )

    znds = re.findall(r"\b(4000\d{7,})\b", report)
    if znds:
        uniq = list(dict.fromkeys(znds))
        return f"**Номер ЗНД (из JSON):** {', '.join(uniq)}"
    return "**Номер ЗНД:** уточнить по trace INT в отчёте движка."


def sanitize_report_for_llm(report: str) -> str:
    """Убрать служебные строки движка, которые LLM тащит в тикет."""
    drop = (
        "Охват MVP:",
        "Метка CSV (MSP):",
        "Пояснение:",
        "【L2 S/4",
        "§7.9:",
    )
    lines = [
        ln
        for ln in report.splitlines()
        if not any(ln.strip().startswith(p) for p in drop)
    ]
    return "\n".join(lines)


def format_2010_header_for_ticket(err: dict) -> str:
    """Готовый блок шапки для тикета (движок → LLM копирует структуру)."""
    if not err.get("items") and not err.get("date_time"):
        return (
            "**Ошибка из INT-2010 (JSON):**\n"
            "- см. выгрузку JSON заказа (INT-2010) в папке заказа"
        )

    lines = ["**Ошибка из INT-2010 (JSON):**"]
    if err.get("date_time"):
        mid = err.get("message_id") or ""
        mid_part = f" | messageId: `{mid}`" if mid else ""
        lines.append(
            f"- **dateTime:** `{err['date_time']}` | **INT:** 2010{mid_part}"
        )
    if err.get("filename"):
        if err.get("file_uri"):
            lines.append(
                f"- **Выгрузка JSON:** [{err['filename']}]({err['file_uri']})"
            )
        elif err.get("folder"):
            fp = Path(err["folder"]) / err["filename"]
            lines.append(f"- **Выгрузка JSON:** `{fp}`")
        else:
            lines.append(f"- **Файл JSON:** `{err['filename']}`")
    for it in err["items"]:
        dt = it.get("date_time") or err.get("date_time") or ""
        lines.append("")
        lines.append(f"- **typeID {it['type_id']}**" + (f" | `{dt}`" if dt else ""))
        lines.append(f"  — {it['note']}")
    return "\n".join(lines).strip()


def build_user_message(
    order_id: str, report: str, kb: str, few: str, folder: Path | None = None
) -> str:
    err = extract_2010_errors(report, folder)
    err_block = format_2010_header_for_ticket(err)
    znd_block = extract_znd_ticket_line(report)
    branch_005 = ""
    l2_005_rules = ""
    branch_143 = ""
    l2_143_rules = ""
    escalation_hint = (
        "При необходимости эскалации — **одной фразой в конце 【L2 CRM】**:\n"
        "«Если в CRM Web UI причину не определить — рекомендуется проверить данные на стороне S/4»."
    )
    branch_201 = ""
    l2_201_rules = ""
    if is_201_report(report) and not is_143_report(report):
        ctx201 = extract_201_context(report)
        branch_201 = "\n\n" + format_201_prompt_block(ctx201)
        escalation_hint = (
            "**Для typeID 201(R1)** — отсутствие BP в S/4 (note 2010). "
            "**Запрещено:** 143, ZPRI, ЗНД, таблица позиций, «пустой Partner[]» если в 0992 роли есть.\n"
            "**【L2 CRM】** — **сначала найти ДП в CRM Web UI**; если ДП **есть в CRM** — проверить S/4; "
            "загрузка BP в ERP — **только** при отсутствии в S/4."
        )
        l2_201_rules = """
**Для typeID 201(R1) — 【L2 CRM】 (§7.11), строго по порядку:**
1) **CRM Web UI:** найти делового партнёра из note **201(R1)**; карточка BP, КЛ; сверить роли с `Partner[]` в **INT-0992**.
2) Если ДП **найден в CRM** и согласован с **0992** — проверить, есть ли BP в **S/4**.
3) При корректных данных в **CRM**, но **отсутствии BP в S/4** — загрузка/создание в **ERP**, повтор **0992→2010** (одна фраза, без блока 【L2 S/4】).
**Не писать:** «сначала S/4», 143, ZPRI, ЗНД, VA03 как первый шаг CRM.
"""
    elif is_143_152_report(report, folder):
        branch_143 = "\n\n" + format_143_152_prompt_block(report)
        escalation_hint = (
            "**Составной кейс 143+152:** оба typeID в ## Итого; таблица позиций из 0992; "
            "Confluence — markdown-таблица (не табы); **【L2 CRM】** — отдельно 143 и 152. "
            "**Не** «причина не ясна»."
        )
        l2_143_rules = """
**Для 143+152 — структура как в эталоне `итого_DEMO_ORDER_002.md`:**
- ## Итого: оба кода 143 (поз. из note) и 152 (поз. 910);
- таблица | Поз. | typeID | Статус | Технология | ZPRI |;
- ## Confluence — только markdown-таблица | Назначение | Ссылка |;
- ## 【L2 CRM】: 143 (ZPRI/отмена ЗНД), 152 (технология при ЗНД), связь одной ЗНД.
"""
    elif is_143_report(report):
        ctx143 = extract_143_context(report)
        branch_143 = "\n\n" + format_143_prompt_block(ctx143)
        escalation_hint = (
            "**Для typeID 143** причина **известна** из note — **запрещено** писать "
            "«причина не ясна», «если причину не определить», дублировать эскалацию на S/4.\n"
            "**【L2 CRM】** — чеклист из ветки 143: ЗНД в CRM → при отсутствии в CRM проверка в S/4 → "
            "**отмена ЗНД** → изменение ZPRI (DEMO_PAGE_ID)."
        )
        l2_143_rules = """
**Для typeID 143 (ZPRI после ЗНД) — 【L2 CRM】:**
1) **Причина:** ЗНД на позиции — S/4 отклоняет изменение ZPRI (note INT-2010).
2) **CRM Web UI:** есть ли ЗНД (E0010)? Если **да** — отменить ЗНД → изменить ZPRI (DEMO_PAGE_ID).
3) Если ЗНД в CRM **нет** — **переадресовать L2 S/4** (VA03).
4) **Не** указывать «было→стало» без INT-0993/S/4; ZPRI только из INT-0992 (факты движка).
"""
    branch_004 = ""
    if is_004_report(report):
        branch_004 = "\n\n" + format_004_prompt_block(
            extract_004_context(report, folder)
        )
    if is_005_report(report):
        ctx005 = extract_005_context(report)
        branch_005 = "\n\n" + format_005_prompt_block(ctx005)
        l2_005_rules = """
**Для typeID 005 (рассинхрон версии) — 【L2 CRM】 минимум 4 пункта:**
1) Факт **005** из INT-2010 (note дословно) + **dateTime** 2010.
2) Последний **0992**: **ExternalDocLastChangeDateTime** (значение из фактов движка), позиции **actionCode=04** при наличии.
3) **0993** в выгрузке: есть/нет (число файлов из движка).
4) При **E0020 (рекламация)** на позициях в 0992 — рекомендуется обратиться к **S/4** для обновления рекламации в заказе.
5) Если в CRM Web UI причину не определить — рекомендуется проверить данные на стороне S/4 (одна фраза, без блока 【L2 S/4】).

В `## Итого` — таблица или список позиций из 0992 (если есть в фактах движка). **Confluence** — URL из отчёта.
"""
    return f"""Проанализируй заказ **{order_id}**.

## Шапка: ошибки INT-2010 (скопируй в ответ под guid, допускается лёгкая правка формулировок note)
{err_block}

## ЗНД (обязательно отдельной строкой под шапкой INT-2010 или в ## Итого)
{znd_block}

## Факты из движка СППР (единственный источник typeID, dateTime, статусов)
```
{sanitize_report_for_llm(report)[:14000]}
```

## Подсказки из KB по typeID в отчёте
{kb or '(нет совпадений в KB)'}

{few}

## Задача
Если в фактах движка уже есть строка **«✅ Ошибок в интеграционных сценариях … не найдено»** —
**не** оформляй тикет об ошибке: кратко повтори этот статус (✅), guid, INT-2010 dateTime, ЗНД из фактов.
**Не** пиши «ЗНД отсутствует», «typeID 2010» как код ошибки, чеклист L2 по несуществующей ошибке.

Иначе сформируй **только текст для тикета L2 CRM** (§10).

**Шапка (обязательно сразу после заголовка с номером заказа):**
- **guid заказа**
- блок **«Ошибка из INT-2010 (JSON):»** — **dateTime**, ссылка на **выгрузку JSON** (file), затем typeID + **note** дословно (как в шаблоне выше).

**Структура:**
1) `# Итого: заказ …` — в заголовке кратко typeID (напр. 143+152)
2) guid + **Ошибка из INT-2010** (typeID + note из JSON)
3) `## Итого` — факты, dateTime; **«Номер ЗНД: отсутствует во всех JSON обменах»**
   (не писать «ЗНД нет» — только **номера** нет в 0973/0993/Ofd); таблица 0992
4) `## Confluence` — только URL (опционально)
5) `## 【L2 CRM】` — для **201** / **143** / **143+152** / **005** — см. отдельные правила ниже.
   Не писать «запросить у L2 S/4»; **номер ЗНД только в шапке** (не дублировать в ## Итого).
{l2_143_rules}
{l2_005_rules}

**Не добавлять** в конце блок **ИТОГО**, строки «охват MVP», «тип ошибки A» — это служебное из движка, не для тикета.
{branch_201}
{branch_143}
{branch_004}
{branch_005}
{l2_201_rules}

**Блок 【L2 S/4】 / 【L2 S/4 (ERP LO)】 НЕ СОЗДАВАТЬ** — L2 CRM закрывает тикет.
{escalation_hint}

Не выдумывай ЗНД, цены. Без report_*.txt и файлов проекта.

**Объём:** только текст тикета — **не повторяй** блоки «Факты из движка», «Задача», code fence ```.
**Не добавляй** секцию «Итоговый номер ЗНД» в конце — номер ЗНД внутри ## Итого.

Структура: guid → (шапка INT-2010 скопируешь из шаблона) → ## Итого → Confluence → 【L2 CRM】.
"""


CRM_ESCALATION_LINE = (
    "Если в CRM Web UI номер ЗНД (и причину ошибки) определить не удаётся — "
    "рекомендуется проверить данные на стороне S/4."
)

CRM_005_RECLAMATION_S4_LINE = (
    "При позициях в статусе **рекламация (E0020)** в последнем **INT-0992** — "
    "рекомендуется обратиться к **S/4** для обновления рекламации в заказе."
)

CONF_143_PRICES = (
    "https://example.local/confluence?pageId=DEMO_PAGE_ID"
)


def _report_root_2010_section(report: str) -> str:
    """Только блок корня INT-2010 (без «охват MVP: typeID 143» и аналогов)."""
    m = re.search(
        r"【Корень из INT-2010.*?\】(.*?)(?=\n【|\Z)",
        report,
        re.S | re.I,
    )
    return m.group(1) if m else ""


def _root_has_typeid(report: str, base: str) -> bool:
    root = _report_root_2010_section(report)
    if not root:
        return False
    return bool(re.search(rf"typeID:\s*{re.escape(base)}(?:\(|\b)", root, re.I))


def is_143_report(report: str) -> bool:
    if _root_has_typeid(report, "143"):
        return True
    return bool(
        re.search(
            r"143\(ZSD_AIF_SDSLS_IN\)|\*\*корень:\s*143|typeID:\s*143|"
            r"цена\s+ZPRI\s+меняется\s+после\s+созданного\s+ЗНД",
            report,
            re.I,
        )
    )


def is_201_report(report: str) -> bool:
    if _root_has_typeid(report, "201"):
        return True
    return bool(
        re.search(
            r"201\(R1\)|\*\*корень:\s*201|корень:\s*201\(R1\)|typeID:\s*201|"
            r"Деловой партнер\s+\d+\s+отсутствует",
            report,
            re.I,
        )
    )


def _merge_2010_meta_from_folder(meta: dict, folder: Path | None) -> dict:
    """Для 143+152 подставить оба typeID из JSON, если в brief только корень 143."""
    if not folder:
        return meta
    fj = _meta_from_latest_2010_json(folder)
    if lat_meta_has_143_and_152(fj):
        for key in ("date_time", "message_id", "filename", "file_uri"):
            if fj.get(key) and not meta.get(key):
                meta[key] = fj[key]
        if fj.get("items"):
            meta["items"] = fj["items"]
    elif not meta.get("items") and fj.get("items"):
        for key in ("date_time", "message_id", "filename", "file_uri"):
            if fj.get(key) and not meta.get(key):
                meta[key] = fj[key]
        meta["items"] = fj["items"]
    return meta


def lat_meta_has_143_and_152(meta: dict) -> bool:
    """В INT-2010 одновременно severity 3: 143 и 152."""
    bases: set[str] = set()
    for it in meta.get("items") or []:
        tid = str(it.get("type_id") or it.get("typeID") or "")
        m = re.match(r"(\d+)", tid)
        if m:
            bases.add(m.group(1))
    return "143" in bases and "152" in bases


def is_143_152_report(report: str, folder: Path | None = None) -> bool:
    folder = folder or _parse_folder_from_report(report)
    if not is_143_report(report):
        return False
    if (
        _root_has_typeid(report, "152")
        or bool(re.search(r"【Ветка 143 \+ 152|корень:\s*143\+152", report, re.I))
    ):
        return True
    if folder:
        return lat_meta_has_143_and_152(_meta_from_latest_2010_json(folder))
    return False


def _resolve_152_position(report: str, folder: Path | None) -> str:
    m = re.search(r"Позиция\s+(\d+).*технолог", report, re.I)
    if m:
        return _norm_crm_pos(m.group(1))
    if folder:
        try:
            from sppr_diagnose import extract_latest_2010, _parse_152_position

            lat = extract_latest_2010(folder)
            if lat:
                for it in lat.get("items") or []:
                    if str(it.get("typeID") or "").startswith("152"):
                        p = _parse_152_position(it.get("note") or "")
                        if p:
                            return _norm_crm_pos(p)
        except ImportError:
            pass
    return "910"


def _norm_crm_pos(pos: str) -> str:
    p = (pos or "").strip().lstrip("0")
    return p or "0"


def extract_201_context(report: str) -> dict:
    """Факты ветки 201(R1) из отчёта движка."""
    ctx: dict = {
        "bp": "",
        "note": "",
        "dt2010": "",
        "dt0992": "",
        "partner_line": "",
        "roles": [],
        "origin_imk": bool(
            re.search(
                r"Источник заказа:\s*IMK|\|\s*IMK\s*\||Класс:\s*A\s*\|\s*IMK",
                report,
                re.I,
            )
        ),
    }
    root = _report_root_2010_section(report)
    nm = re.search(r"note:\s*(.+)", root, re.I)
    if nm:
        ctx["note"] = nm.group(1).strip()
    if not ctx["note"]:
        bm2 = re.search(
            r"\*?\*?INT-2010\*?\*?\s*\([^)]*\):\s*\*?\*?(.+?)\*?\*?(?:\n|$)",
            report,
            re.I,
        )
        if bm2:
            ctx["note"] = bm2.group(1).strip().strip("*")
    bm = re.search(r"Деловой партнер\s+(\d+)", ctx["note"] or report, re.I)
    if bm:
        ctx["bp"] = bm.group(1)
    dm = re.search(
        r"\*?\*?INT-2010\*?\*?\s*\(([0-9T:.\-]+)\)", report, re.I
    )
    if not dm:
        dm = re.search(r"INT-2010,\s*([0-9T:.\-]+)", report)
    if dm:
        ctx["dt2010"] = dm.group(1).strip()
    dm2 = re.search(
        r"\*?\*?INT-0992\*?\*?\s*\(([0-9T:.\-]+)\)", report, re.I
    )
    if not dm2:
        dm2 = re.search(r"INT-0992,\s*([0-9T:.\-]+)", report)
    if dm2:
        ctx["dt0992"] = dm2.group(1).strip()
    pl = re.search(r"Partner\[\]:\s*([^\n]+)", report, re.I)
    if pl:
        ctx["partner_line"] = pl.group(1).strip()
    for m in re.finditer(
        r"роль (\w+):\s*BusinessPartnerID=([^;\n]+)",
        report,
        re.I,
    ):
        ctx["roles"].append(f"**{m.group(1)}**=`{m.group(2).strip()}`")
    return ctx


def format_201_prompt_block(ctx: dict) -> str:
    lines = [
        "## Ветка 201(R1) (обязательно)",
        f"- **note INT-2010:** {ctx.get('note') or 'Деловой партнер … отсутствует.'}",
        f"- **ДП из note:** `{ctx.get('bp') or '?'}`",
    ]
    if ctx.get("partner_line"):
        lines.append(f"- **Partner[] (0992):** `{ctx['partner_line']}`")
    if ctx.get("roles"):
        lines.append("- **Роли в 0992:** " + ", ".join(ctx["roles"][:8]))
    lines.append(
        "- **Запрещено в ответе:** typeID **143**, ZPRI, ЗНД, таблица позиций; "
        "не утверждать «пустой Partner[]», если роли в отчёте есть."
    )
    return "\n".join(lines)


def build_201_itogo_block(ctx: dict) -> str:
    bp = ctx.get("bp") or "(из note)"
    dt2010 = ctx.get("dt2010") or "…"
    dt0992 = ctx.get("dt0992") or "…"
    origin = "ИМК" if ctx.get("origin_imk") else "CRM"
    lines = [
        "## Итого",
        "",
        f"При обработке в S/4 **INT-2010 от {dt2010}** — **201(R1)** — "
        f"«{ctx.get('note') or f'Деловой партнер {bp} отсутствует.'}».",
        "",
        f"В **INT-0992 от {dt0992}**:",
    ]
    if ctx.get("partner_line"):
        lines.append(f"- `Partner[]`: {ctx['partner_line']}")
    if ctx.get("roles"):
        lines.append("- Роли: " + ", ".join(ctx["roles"]))
    lines.append("")
    lines.append(
        f"**Смысл:** CRM передала ДП **{bp}** в обмен; в **S/4** контрагент **отсутствует** — "
        "заказ не принят. Это **не** 143 (ZPRI/ЗНД), **не** 127."
    )
    lines.append("")
    lines.append(f"Класс **A**. Источник заказа: **{origin}**.")
    return "\n".join(lines)


def build_201_confluence_block() -> str:
    return "\n".join(
        [
            "## Confluence",
            "",
            "| Назначение | Ссылка |",
            "| --- | --- |",
            "| П. **10.41** — партнёр отсутствует в S/4 | "
            "https://example.local/confluence?pageId=DEMO_PAGE_ID |",
            "| Строка **201(R1)** в таблице INT-2010 | "
            "https://example.local/confluence?pageId=DEMO_PAGE_ID |",
            "| Цепочка INT 0989→0992→2010→2026 | "
            "https://example.local/confluence?pageId=DEMO_PAGE_ID |",
        ]
    )


def lines_201_l2_crm_text(bp: str, partner_line: str = "", *, numbered: bool = True) -> list[str]:
    """L2 CRM для 201(R1): найти ДП в CRM → если есть в CRM — проверить S/4 → загрузка BP."""
    bp = bp or "(из note)"
    p0992 = f" (`{partner_line}`)" if partner_line else ""
    if numbered:
        return [
            f"1. **CRM Web UI:** найти делового партнёра **{bp}** (карточка BP, контактные лица); "
            f"сверить роли на заказе с `Partner[]` в **INT-0992**{p0992}.",
            f"2. Если ДП **найден в CRM** и данные на заказе согласованы с **0992** — проверить, "
            f"есть ли бизнес-партнёр **{bp}** в **S/4**.",
            "3. При корректных данных в **CRM**, но **отсутствии BP в S/4** — загрузка/создание "
            f"контрагента **{bp}** на стороне **ERP** (L2 S/4), затем повтор **0992→2010**.",
        ]
    return [
        f"  Найти делового партнёра **{bp}** в **CRM Web UI** (карточка BP, КЛ); "
        f"сверить роли с `Partner[]` в **0992**{p0992}.",
        f"  Если ДП **есть в CRM** — проверить наличие **{bp}** в **S/4**; "
        "при отсутствии в **S/4** — загрузка BP в **ERP**, затем повтор **0992→2010**.",
    ]


def build_201_l2_brief_block(report: str, ctx: dict | None = None) -> str:
    """L2 CRM для 201(R1): CRM (найти ДП) → при наличии в CRM — проверка S/4."""
    ctx = ctx or extract_201_context(report)
    bp = ctx.get("bp") or "(из note)"
    lines = ["## 【L2 CRM】", ""]
    lines.extend(lines_201_l2_crm_text(bp, ctx.get("partner_line") or "", numbered=True))
    return "\n".join(lines)


def build_201_l2_crm_block(ctx: dict, report: str = "") -> str:
    return build_201_l2_brief_block(report, ctx)


def build_201_full_ticket_answer(
    report: str, order_id: str, folder: Path | None = None
) -> str:
    """Полный тикет 201(R1) из движка без вызова LLM."""
    ctx = extract_201_context(report)
    head = build_201_ticket_header(order_id, report, folder)
    body = (
        f"{build_201_itogo_block(ctx)}\n\n"
        f"{build_201_confluence_block()}\n\n"
        f"{build_201_l2_brief_block(report, ctx)}\n"
    )
    out = f"{head}\n\n{body}".strip()
    out = _strip_vague_escalation_lines(out)
    out = _fix_origin_in_answer(out, report)
    try:
        from sppr_markdown_fix import finalize_display_markdown, strip_html_link_artifacts

        out = strip_html_link_artifacts(finalize_display_markdown(out))
    except ImportError:
        pass
    return out


def build_201_ticket_header(
    order_id: str, report: str, folder: Path | None = None
) -> str:
    guid = ""
    gm = re.search(
        r"guid[^\n`]*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        report,
        re.I,
    )
    if gm:
        guid = gm.group(1)
    lines = [f"**Заказ {order_id}** (201 — ДП отсутствует в S/4)", ""]
    if guid:
        lines.append(f"**guid заказа:** `{guid}`")
        lines.append("")
    lines.append(format_2010_header_for_ticket(extract_2010_errors(report, folder)))
    return "\n".join(lines).strip()


def enrich_201_ticket(
    text: str,
    report: str,
    folder: Path | None = None,
    order_id: str = "",
) -> str:
    """Подставить чеклист 201(R1) — без смешения с 143."""
    if not is_201_report(report) or is_143_report(report):
        return text
    ctx = extract_201_context(report)
    itogo = build_201_itogo_block(ctx)
    conf = build_201_confluence_block()
    l2 = build_201_l2_brief_block(report, ctx)
    body = f"{itogo}\n\n{conf}\n\n{l2}\n"
    oid = order_id or ""
    if not oid:
        om = re.search(r"заказ[а]?\s*(\d{10})", report, re.I) or re.search(
            r"\*\*Заказ\s+(\d{10})\*\*", text, re.I
        )
        if om:
            oid = om.group(1)
    head = _ticket_head_before_structured_tail(_strip_vague_escalation_lines(text))
    head = re.sub(r"\*\*Номер ЗНД:\*\*[^\n]+\n?", "", head, flags=re.I)
    head = re.sub(r"^#+\s*Номер ЗНД:?\s*[^\n]*\n?", "", head, flags=re.M | re.I)
    if oid:
        head = build_201_ticket_header(oid, report, folder)
    out = f"{head}\n\n{body}".strip()
    out = _strip_vague_escalation_lines(out)
    out = _fix_origin_in_answer(out, report)
    try:
        from sppr_markdown_fix import fix_ticket_section_glue

        out = fix_ticket_section_glue(out)
    except ImportError:
        pass
    return out


def extract_143_context(report: str) -> dict:
    """Позиции и note 143 из отчёта движка."""
    ctx: dict = {
        "positions": [],
        "notes": [],
        "znd_missing_in_json": bool(
            re.search(r"ЗНД в JSON:.*не найден", report, re.I)
        ),
        "znd_ids": list(dict.fromkeys(re.findall(r"\b(4000\d{7,})\b", report))),
        "origin_imk": bool(
            re.search(r"Источник заказа:\s*IMK|\|\s*IMK\s*\|", report, re.I)
        ),
    }
    for m in re.finditer(
        r"note:\s*((?:В заказе \d+ )?поз\.\s*\d+[^\n]*ZPRI[^\n]*ЗНД[^\n]*)",
        report,
        re.I,
    ):
        note = m.group(1).strip()
        if note not in ctx["notes"]:
            ctx["notes"].append(note)
        pm = re.search(r"поз\.\s*0*(\d+)", note, re.I)
        if pm:
            pos = _norm_crm_pos(pm.group(1))
            if pos not in ctx["positions"]:
                ctx["positions"].append(pos)
    for m in re.finditer(
        r"INT-2010[^:\n]*:\s*\*{0,2}(В заказе \d+ поз\.\s*\d+[^*\n]+ZPRI[^*\n]+ЗНД[^*\n]+)\*{0,2}",
        report,
        re.I,
    ):
        note = re.sub(r"\*+", "", m.group(1)).strip()
        if note and note not in ctx["notes"]:
            ctx["notes"].append(note)
        pm = re.search(r"поз\.\s*0*(\d+)", note, re.I)
        if pm:
            pos = _norm_crm_pos(pm.group(1))
            if pos not in ctx["positions"]:
                ctx["positions"].append(pos)
    rm = re.search(
        r"поз\.\s*\*\*(\d+)\*\*[^:\n]*\(позиция из INT-2010\)",
        report,
        re.I,
    )
    if rm:
        pos = _norm_crm_pos(rm.group(1))
        if pos not in ctx["positions"]:
            ctx["positions"].insert(0, pos)
    bm = re.search(r"【Ветка 143】[^\n]*поз\.\s*([0-9,\s]+)", report, re.I)
    if bm:
        for p in re.findall(r"\d+", bm.group(1)):
            pos = _norm_crm_pos(p)
            if pos not in ctx["positions"]:
                ctx["positions"].append(pos)
    for m in re.finditer(r"поз\.\s*\*\*(\d+)\*\*[^\n]+", report, re.I):
        line = m.group(0)
        if re.search(r"—\s*\*\*152\*\*", line):
            continue
        if "Material=" in line or "ZPRI=" in line:
            pos = _norm_crm_pos(m.group(1))
            if pos not in ctx["positions"]:
                ctx["positions"].append(pos)
    return ctx


def extract_143_0992_facts(report: str) -> dict:
    """dateTime INT-0992 и ZPRI/статус по позициям из отчёта движка."""
    facts: dict = {"dt0992": "", "items": {}, "n0993": None}
    dm = re.search(r"INT-0992,\s*([0-9T:.\-]+)", report)
    if dm:
        facts["dt0992"] = dm.group(1)[:19]
    dm2 = re.search(r"\*\*INT-0992\*\*\s*\(([^)]+)\)", report)
    if dm2 and not facts["dt0992"]:
        facts["dt0992"] = dm2.group(1)[:19]
    dm3 = re.search(r"\*\*ZPRI из INT-0992\*\*\s*\(([^)]+)\)", report, re.I)
    if dm3 and not facts["dt0992"]:
        facts["dt0992"] = dm3.group(1)[:19]
    for m in re.finditer(
        r"поз\.\s*(\d+):\s*Material=(\d+);\s*ZPRI=([\d.]+);\s*"
        r"ZZStatusUserItem=(\w+)",
        report,
        re.I,
    ):
        pos = _norm_crm_pos(m.group(1))
        facts["items"][pos] = {
            "material": m.group(2),
            "zpri": m.group(3),
            "status": m.group(4),
        }
    for m in re.finditer(
        r"поз\.\s*\*\*(\d+)\*\*[^:]*:\s*Material=(\d+);\s*"
        r"ZPRI=([\d.]+)(?:;\s*ZZ1_SPTECH=(\w+))?;\s*статус\s+(\S+)",
        report,
        re.I,
    ):
        pos = _norm_crm_pos(m.group(1))
        entry = {
            "material": m.group(2),
            "zpri": m.group(3),
            "status": m.group(5),
        }
        if m.group(4):
            entry["sptech"] = m.group(4)
        facts["items"][pos] = entry
    for m in re.finditer(
        r"поз\.\s*(\d+):\s*ZZ1_SPTECH=(\w+);\s*ZZStatusUserItem=(\w+)",
        report,
        re.I,
    ):
        pos = _norm_crm_pos(m.group(1))
        if pos not in facts["items"]:
            facts["items"][pos] = {
                "sptech": m.group(2),
                "status": m.group(3),
                "zpri": None,
            }
    nm = re.search(r"Файлов \*0993\*[^:]*:\s*(\d+)", report)
    if nm:
        facts["n0993"] = int(nm.group(1))
    elif re.search(r"0973/0993/Ofd.*не найден|0993/Ofd\) не найден", report, re.I):
        facts["n0993"] = 0
    return facts


def format_143_prompt_block(ctx: dict) -> str:
    lines = ["## Ветка 143 (обязательно в тикете)"]
    if ctx.get("notes"):
        lines.append("- **note INT-2010 (дословно):**")
        for n in ctx["notes"][:6]:
            lines.append(f"  - {n}")
    if ctx.get("positions"):
        lines.append(f"- **Позиции:** {', '.join(ctx['positions'][:12])}")
    if ctx.get("znd_missing_in_json"):
        lines.append(
            "- **ЗНД в JSON (0973/0993/Ofd):** номер **не найден** — не выдумывать; "
            "проверить ЗНД в **CRM Web UI**; если в CRM нет — проверить в **S/4**."
        )
    elif ctx.get("znd_ids"):
        lines.append(f"- **ЗНД в JSON:** {', '.join(ctx['znd_ids'][:3])}")
    lines.append(
        "- **Причина 143 ясна из note:** после создания ЗНД нельзя менять **ZPRI**. "
        "**Не** писать «причина не ясна» / «если причину не определить»."
    )
    lines.append(
        "- **【L2 CRM】:** (1) ЗНД в CRM Web UI (E0010)? → если есть, **отменить ЗНД**, "
        f"затем ZPRI ([DEMO_PAGE_ID]({CONF_143_PRICES})); (2) если ЗНД в CRM **нет** — "
        "**переадресовать L2 S/4** (VA03); (3) **не** указывать «было→стало» без INT-0993/S/4."
    )
    return "\n".join(lines)


def build_143_itogo_block(report: str, ctx: dict) -> str:
    dt2010 = ""
    dm = re.search(r"INT-2010,\s*([0-9T:.\-]+)", report)
    if dm:
        dt2010 = dm.group(1)
    if not dt2010:
        dm = re.search(r"\*\*INT-2010\*\*\s*\(([^)]+)\)", report)
        if dm:
            dt2010 = dm.group(1)
    facts = extract_143_0992_facts(report)
    root_pos = ""
    if ctx.get("notes"):
        m = re.search(r"поз\.\s*0*(\d+)", ctx["notes"][0], re.I)
        if m:
            root_pos = _norm_crm_pos(m.group(1))
    if not root_pos:
        rm = re.search(
            r"поз\.\s*\*\*(\d+)\*\*[^:\n]*\(позиция из INT-2010\)",
            report,
            re.I,
        )
        if rm:
            root_pos = _norm_crm_pos(rm.group(1))
    order_pos: list[str] = []
    if root_pos:
        order_pos.append(root_pos)
    for p in ctx.get("positions") or []:
        pk = _norm_crm_pos(p)
        if pk not in order_pos:
            order_pos.append(pk)
    if not order_pos and facts.get("items"):
        for key in sorted(
            facts["items"].keys(),
            key=lambda x: int(x) if str(x).isdigit() else 0,
        ):
            order_pos.append(key)
        if root_pos and root_pos in facts["items"]:
            order_pos = [root_pos] + [p for p in order_pos if p != root_pos]
    pos_txt = ", ".join(order_pos) if order_pos else "(из note INT-2010)"
    sppr_cls = "**A**"
    if re.search(r"Класс:\s*B\s*->\s*A|Класс СППР:\s*B", report, re.I):
        sppr_cls = "**B→A**"
    msp = ""
    if re.search(r"Не пришел 2010|Не пришёл 2010", report, re.I):
        msp = (
            " Метка CSV (MSP) «Не пришёл 2010» — **слой 2** (незамкнутый шаг); "
            "в JSON есть **INT-2010** с **143** — корень в отказе S/4, не «файла нет»."
        )
    lines = [
        "## Итого",
        "",
        "**Смысл 143:** после создания **ЗНД** нельзя менять **ZPRI**; "
        "для исправления — **отмена ЗНД**, затем изменение цены в **CRM Web UI**.",
        "",
        f"При обработке в S/4 **INT-2010 от {dt2010 or '…'}** — **143(ZSD_AIF_SDSLS_IN)** "
        f"по позициям **{pos_txt}**: цена **ZPRI** меняется после созданного **ЗНД**.",
        "",
    ]
    if root_pos and facts.get("items", {}).get(root_pos, {}).get("zpri"):
        it = facts["items"][root_pos]
        lines.append(
            f"**Позиция из note (корень):** **{root_pos}** (S/4 {root_pos.zfill(6)}), "
            f"Material={it.get('material') or '?'}, **ZPRI={it['zpri']}**, "
            f"статус **{it.get('status') or '?'}**."
        )
        lines.append("")
    if facts.get("items"):
        dt0992 = facts.get("dt0992") or dt2010 or "…"
        table_pos = list(order_pos)
        for key in sorted(
            facts["items"].keys(),
            key=lambda x: int(x) if str(x).isdigit() else 0,
        ):
            if key not in table_pos:
                table_pos.append(key)
        if root_pos and root_pos in facts["items"]:
            table_pos = [root_pos] + [p for p in table_pos if p != root_pos]
        lines.append(f"**INT-0992** ({dt0992}) — сверка позиций 143:")
        lines.append("")
        lines.append("| Поз. | typeID | Статус | Технология | ZPRI |")
        lines.append("| --- | --- | --- | --- | --- |")
        for p in table_pos[:12]:
            it = facts["items"].get(p, {})
            zpri = it.get("zpri") or "—"
            lines.append(
                f"| **{p}** | 143 | {it.get('status') or '?'} | "
                f"{it.get('sptech') or '—'} | {zpri} |"
            )
        lines.append("")
    if ctx.get("notes"):
        lines.append("**Цитаты note (все в одном 2010):**")
        for n in ctx["notes"][:8]:
            lines.append(f"- {n}")
        lines.append("")
    lines.append("")
    lines.append("**Номер ЗНД:** отсутствует во всех JSON обменах (0973/0993/Ofd).")
    if ctx.get("znd_missing_in_json"):
        lines.append(
            "Проверить ЗНД в **CRM Web UI**; если в CRM нет — **L2 S/4** (VA03)."
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    origin = "**ИМК**" if ctx.get("origin_imk") else "**CRM**"
    if re.search(
        r"Источник заказа:\s*IMK|CustomerPurchaseOrderType\s*=\s*2|\|\s*IMK\s*\|",
        report,
        re.I,
    ):
        origin = "**ИМК**"
    elif re.search(r"CustomerPurchaseOrderType\s*=\s*1", report, re.I):
        origin = "**CRM**"
    lines.append(f"Класс {sppr_cls}.{msp} Источник заказа: {origin}.")
    return "\n".join(lines)


def build_143_confluence_block() -> str:
    return "\n".join(
        [
            "## Confluence",
            "",
            "| Назначение | Ссылка |",
            "| --- | --- |",
            "| Ошибка **143** (ZPRI после ЗНД), маршрутизация L2 | "
            "https://example.local/confluence?pageId=DEMO_PAGE_ID |",
            "| Строка **143** в таблице INT-2010 | "
            "https://example.local/confluence?pageId=DEMO_PAGE_ID |",
            "| Регламент исправления **ZPRI** в CRM | "
            "https://example.local/confluence?pageId=DEMO_PAGE_ID |",
            "| Цепочка INT 0989→0992→2010→2026 | "
            "https://example.local/confluence?pageId=DEMO_PAGE_ID |",
        ]
    )


def build_143_l2_crm_block(ctx: dict, report: str = "") -> str:
    facts = extract_143_0992_facts(report) if report else {}
    positions = list(ctx.get("positions") or [])
    root_pos = ""
    if ctx.get("notes"):
        m = re.search(r"поз\.\s*0*(\d+)", ctx["notes"][0], re.I)
        if m:
            root_pos = _norm_crm_pos(m.group(1))
    order_pos: list[str] = []
    if root_pos and root_pos in {_norm_crm_pos(p) for p in positions}:
        order_pos.append(root_pos)
    for p in positions:
        pk = _norm_crm_pos(p)
        if pk not in order_pos:
            order_pos.append(pk)
    pos_txt = ", ".join(order_pos) if order_pos else "(из note INT-2010)"
    lines = [
        "## 【L2 CRM】",
        "",
        "**Причина (S/4, INT-2010, typeID 143):** на позиции уже создана **ЗНД** — "
        "S/4 отклоняет изменение **ZPRI** («цена ZPRI меняется после созданного ЗНД»).",
    ]
    if facts.get("dt0992") and order_pos:
        details: list[str] = []
        for p in order_pos[:8]:
            it = facts.get("items", {}).get(p, {})
            if it.get("zpri"):
                mat = it.get("material") or "?"
                details.append(
                    f"поз. **{p}** (материал {mat}): ZPRI **{it['zpri']}**, "
                    f"статус **{it.get('status', '?')}**"
                )
        if details:
            lines.append("")
            lines.append(
                f"**INT-0992 от {facts['dt0992']}** (текущая цена в CRM→S/4): "
                + "; ".join(details) + "."
            )
    lines.extend(
        [
            "",
            f"1. **Позиции из note:** {pos_txt}. Проверить в **CRM Web UI**, есть ли **ЗНД** "
            "(список ЗНД / статус **E0010** — «Передано в доставку. ЗНД создано»).",
            "   • **Если ЗНД в CRM есть** — изменить **ZPRI** можно **только после отмены ЗНД** "
            f"в CRM, затем правка цены по регламенту [DEMO_PAGE_ID]({CONF_143_PRICES}).",
            "   • **Если ЗНД в CRM нет** — **переадресовать на L2 S/4:** проверить наличие ЗНД "
            "на позиции в **S/4** (VA03); S/4 уже считает ЗНД созданной (ошибка **143**).",
        ]
    )
    lines.append("")
    if facts.get("n0993") == 0 or ctx.get("znd_missing_in_json"):
        lines.append(
            "2. **«Было → стало» по цене** из этой выгрузки **не восстановить**: "
            "**INT-0993** в JSON нет — цена на момент создания ЗНД только в **S/4** или CRM UI. "
            "Не указывать произвольные значения ZPRI — только из **INT-0992**."
        )
    else:
        lines.append(
            "2. **Сверка цены:** сравнить ZPRI в CRM с ценой в **INT-0993** (ЗНД); "
            "без 0993 — не указывать «было → стало»."
        )
    if ctx.get("notes"):
        lines.extend(["", "**Цитаты note из INT-2010:**"])
        for n in ctx["notes"][:4]:
            lines.append(f"- {n}")
    return "\n".join(lines)


def _strip_vague_escalation_lines(text: str) -> str:
    """Убрать «причина не ясна» и дубли эскалации на S/4."""
    vague = (
        r"Если в CRM Web UI (?:номер ЗНД \(и причину ошибки\) )?определить не удаётся",
        r"Если в CRM Web UI номер ЗНД",
        r"Если в CRM Web UI причину не определить",
        r"Если причина не ясна в CRM Web UI",
        r"причину ошибки\) определить не удаётся",
        r"рекомендуется проверить данные на стороне S/4",
        r"рекомендуется проверить данные на стороне s/4",
        r"убедиться, что цена ZPRI действительно изменилась",
        r"Регламент исправления цен ZPRI описан в документе DEMO_PAGE_ID",
        r"обновить ZPRI до актуального значения",
        r"переадресовать задачу на L2 S/4",
        r"отменить её через интерфейс CRM, затем обновить",
    )
    out: list[str] = []
    seen_s4 = False
    for line in text.splitlines():
        low = line.lower()
        if any(re.search(p, line, re.I) for p in vague):
            if "рекомендуется проверить данные на стороне s/4" in low:
                if seen_s4:
                    continue
                seen_s4 = True
            elif "причин" in low or "не ясна" in low or "не удаётся" in low:
                continue
            elif "убедиться" in low or "регламент исправления" in low:
                continue
        out.append(line)
    return "\n".join(out)


def is_004_report(report: str) -> bool:
    return bool(
        re.search(
            r"корень:\s*004|typeID:\s*004|004\(ZSD_AIF_SDSLS_IN\)|"
            r"Снять резерв невозможно.*ЗНД",
            report,
            re.I,
        )
    )


def extract_004_context(report: str, folder: Path | None = None) -> dict:
    """Факты ветки 004 (резерв при ЗНД) из отчёта движка и JSON."""
    ctx: dict = {
        "dt2010": "",
        "dt0992": "",
        "note": "",
        "origin_imk": bool(
            re.search(
                r"Источник заказа:\s*IMK|\|\s*IMK\s*\||CustomerPurchaseOrderType\s*=\s*2",
                report,
                re.I,
            )
        ),
        "e0010_count": 0,
        "e0020_pos": "",
        "e0020_material": "",
        "e0020_sptech": "",
        "order_status": "",
        "action_code": "",
        "znd_missing_in_json": bool(
            re.search(r"ЗНД в JSON:.*не найден|0973/0993.*нет", report, re.I)
        ),
    }
    m = re.search(
        r"\*\*INT-2010\*\*\s*\([^)]+\):\s*\*{0,2}(.+?)\*{0,2}\s*$",
        report,
        re.M | re.I,
    )
    if m:
        ctx["note"] = re.sub(r"\*+", "", m.group(1)).strip()
    nm = re.search(r"note:\s*(.+)", report, re.I)
    if nm and not ctx["note"]:
        ctx["note"] = nm.group(1).strip()[:220]
    dm = re.search(r"INT-2010,\s*([0-9T:.\-]+)", report)
    if dm:
        ctx["dt2010"] = dm.group(1)
    if not ctx["dt2010"]:
        dm2 = re.search(r"\*\*INT-2010\*\*\s*\(([^)]+)\)", report, re.I)
        if dm2:
            ctx["dt2010"] = dm2.group(1).strip()
    dm3 = re.search(r"INT-0992,\s*([0-9T:.\-]+)", report)
    if dm3:
        ctx["dt0992"] = dm3.group(1)
    if not ctx["dt0992"]:
        dm4 = re.search(r"\*\*INT-0992\*\*\s*\(([^)]+)\)", report, re.I)
        if dm4:
            ctx["dt0992"] = dm4.group(1).strip()
    ac = re.search(r"actionCode=(\d+)", report, re.I)
    if ac:
        ctx["action_code"] = ac.group(1)
    m60 = re.search(
        r"поз\.\s*(\d+)[^\n]{0,120}(?:E0020|рекламац)",
        report,
        re.I,
    )
    if m60:
        ctx["e0020_pos"] = _norm_crm_pos(m60.group(1))
    mm = re.search(r"материал\s+(\d+)|Material=(\d+)", report, re.I)
    if mm:
        ctx["e0020_material"] = mm.group(1) or mm.group(2) or ""
    if folder:
        try:
            from sppr_diagnose import extract_latest_0992_snapshot, extract_latest_2010

            snap = extract_latest_0992_snapshot(folder)
            if snap:
                if not ctx["dt0992"]:
                    ctx["dt0992"] = (snap.get("dateTime") or "")[:26]
                ctx["order_status"] = str(snap.get("order_status") or "")
                for it in snap.get("items") or []:
                    st = str(it.get("status") or "")
                    if st == "E0010":
                        ctx["e0010_count"] += 1
                    elif st == "E0020":
                        pos = _norm_crm_pos(str(it.get("pos") or ""))
                        if not ctx["e0020_pos"] or pos == ctx["e0020_pos"]:
                            ctx["e0020_pos"] = pos or ctx["e0020_pos"]
                            ctx["e0020_material"] = str(
                                it.get("material") or ctx["e0020_material"]
                            )
                            ctx["e0020_sptech"] = str(
                                it.get("sptech") or ctx["e0020_sptech"]
                            )
            lat = extract_latest_2010(folder)
            if lat:
                if not ctx["dt2010"]:
                    ctx["dt2010"] = (lat.get("dateTime") or "")[:26]
                if not ctx["note"]:
                    for it in lat.get("items") or []:
                        if str(it.get("typeID") or "").startswith("004"):
                            ctx["note"] = (it.get("note") or "")[:220]
            if not list(folder.glob("*0993*")) and not list(folder.glob("*0973*")):
                ctx["znd_missing_in_json"] = True
        except ImportError:
            pass
    if not ctx["e0020_pos"]:
        ctx["e0020_pos"] = "60"
    return ctx


def build_004_znd_line(report: str, folder: Path | None) -> str:
    if re.search(r"ЗНД в JSON:.*не найден", report, re.I):
        return extract_znd_ticket_line(report)
    folder = folder or _parse_folder_from_report(report)
    if folder and not list(folder.glob("*0993*")) and not list(folder.glob("*0973*")):
        return (
            "**Номер ЗНД:** **отсутствует** — в архиве **0973/0993** по заказу нет. "
            "Это не означает отсутствие ЗНД в процессе.\n"
            "**Действие:** номер ЗНД указать после **CRM Web UI** или ответа **L2 S/4 (VA03)**."
        )
    return extract_znd_ticket_line(report)


def format_004_prompt_block(ctx: dict) -> str:
    lines = [
        "## Ветка 004 (обязательно)",
        "- **004:** снятие резерва при **неотменённой ЗНД**; **не** путать с **003** и **152**.",
        "- Источник **ИМК** — указать в ## Итого (`CustomerPurchaseOrderType=2`).",
    ]
    if ctx.get("e0020_pos"):
        lines.append(
            f"- **0992:** поз. **{ctx['e0020_pos']}** — **E0020** (рекламация); "
            f"остальные позиции — **E0010** (ЗНД создана)."
        )
    lines.append(
        "- **Номер ЗНД:** отсутствует в JSON — одна формулировка в шапке, без «trace INT»."
    )
    return "\n".join(lines)


def is_005_report(report: str) -> bool:
    return bool(
        re.search(
            r"typeID:\s*005|005\(SOA_SD\)|【Ветка 005|§7\.9 typeID 005",
            report,
            re.I,
        )
    )


def extract_005_context(report: str) -> dict:
    """Факты ветки 005 из отчёта движка (для промпта и постобработки)."""
    ctx: dict = {
        "ext_dt": "",
        "snap_0992_dt": "",
        "snap_0992_mid": "",
        "n0993": "",
        "n005_hist": "",
        "reclamation_positions": [],
    }
    m = re.search(r"ExternalDocLastChangeDateTime:\s*(\S+)", report)
    if m:
        ctx["ext_dt"] = m.group(1).strip()
    m = re.search(
        r"INT-0992,\s*([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+)",
        report,
    )
    if m:
        ctx["snap_0992_dt"] = m.group(1).strip()
    m = re.search(
        r"INT-0992,[^\n]*messageId:\s*([0-9a-fA-F]+)",
        report,
        re.I,
    )
    if m:
        ctx["snap_0992_mid"] = m.group(1).strip()
    m = re.search(r"Файлов \*0993\*[^:]*:\s*(\d+)", report)
    if m:
        ctx["n0993"] = m.group(1)
    m = re.search(r"Эпизодов 005[^:]*:\s*(\d+)", report)
    if m:
        ctx["n005_hist"] = m.group(1)
    for pm in re.finditer(
        r"поз\.\s*(\d+):[^;\n]*E0020",
        report,
        re.I,
    ):
        ctx["reclamation_positions"].append(pm.group(1))
    ctx["reclamation_positions"] = list(dict.fromkeys(ctx["reclamation_positions"]))
    return ctx


def format_005_prompt_block(ctx: dict) -> str:
    lines = ["## Ветка 005 (обязательно отразить в тикете)"]
    if ctx.get("ext_dt"):
        lines.append(
            f"- **ExternalDocLastChangeDateTime** в последнем **0992:** `{ctx['ext_dt']}`"
        )
    if ctx.get("snap_0992_dt"):
        mid = f", messageId `{ctx['snap_0992_mid']}`" if ctx.get("snap_0992_mid") else ""
        lines.append(f"- Последний **INT-0992:** `{ctx['snap_0992_dt']}`{mid}")
    if ctx.get("n0993") is not None and ctx.get("n0993") != "":
        lines.append(f"- Файлов **0993** в выгрузке: **{ctx['n0993']}**")
    if ctx.get("n005_hist"):
        lines.append(f"- Повторов **005** в истории **2010:** {ctx['n005_hist']}")
    if ctx.get("reclamation_positions"):
        lines.append(
            "- Позиции с **E0020 (рекламация)** в 0992: "
            + ", ".join(ctx["reclamation_positions"][:8])
        )
    lines.append(
        f"- В **【L2 CRM】** обязательно пункт: {CRM_005_RECLAMATION_S4_LINE}"
    )
    lines.append(
        "- Не писать «OMS виноват», «репликация восстановлена», «пересохранить заказ» — "
        "только факты из JSON и проверки CRM; эскалация на S/4 — одной фразой (п.4)."
    )
    return "\n".join(lines)


def remove_duplicate_znd_block(text: str) -> str:
    """Оставить один блок **Номер ЗНД:** в шапке (первый, полный)."""
    first = text.find("**Номер ЗНД:**")
    if first < 0:
        return text
    while True:
        second = text.find("**Номер ЗНД:**", first + 14)
        if second < 0:
            break
        end = second
        for line in text[second:].splitlines():
            if not line.strip():
                end += len(line) + 1
                break
            if line.startswith("## ") or re.match(r"^Класс \*\*A\*\*", line):
                break
            end += len(line) + 1
        text = text[:second].rstrip() + "\n\n" + text[end:].lstrip()
    return text


def normalize_l2_crm_escalation(text: str, report: str = "") -> str:
    """П.3 — одна ЗНД; п.4 — эскалация на S/4 без отдельного блока L2 S/4."""
    if report and is_143_report(report):
        return _strip_vague_escalation_lines(text)
    if report and is_201_report(report) and not is_143_report(report):
        return _strip_vague_escalation_lines(text)
    text = re.sub(
        r"\d+\.\s*Оба симптома[^\n]*(?:запросить|уточнить|получить)[^\n]*L2\s*S/?4[^\n]*",
        f"3. Оба симптома связать одной ЗНД в тикете (номер — см. шапку).\n4. {CRM_ESCALATION_LINE}",
        text,
        count=1,
        flags=re.I,
    )
    text = re.sub(
        r",?\s*которую нужно уточнить через CRM Web UI или запросить у L2 S/4\.?",
        " (номер — см. шапку).",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\s*—\s*уточнить его в CRM Web UI или запросить у L2 S/4\.?",
        " — номер ЗНД см. в шапке тикета.",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\s*или\s+запросить\s+у\s+L2\s*S/?4\.?",
        " (номер — см. шапку).",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\d+\.\s*Проверить наличие номера ЗНД[^\n]*(?:L2\s*S/?4|получить)[^\n]*\n?",
        "2. Номер ЗНД — см. блок **Номер ЗНД** в шапке.\n",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\d+\.\s*[^\n]*(?:получить\s+ответ|запросить|обратиться)[^\n]*L2\s*S/?4[^\n]*\n?",
        "2. Номер ЗНД — см. блок **Номер ЗНД** в шапке.\n",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\d+\.\s*[^\n]*уточнить[^\n]*в CRM Web UI[^\n]*L2\s*S/?4[^\n]*\n?",
        "2. Номер ЗНД — см. блок **Номер ЗНД** в шапке.\n",
        text,
        flags=re.I,
    )
    # Один пункт эскалации (убрать дубли 3./4. с одним смыслом)
    esc = re.escape(CRM_ESCALATION_LINE)
    dup = re.findall(esc, text, flags=re.I)
    if len(dup) > 1:
        seen = False
        def _one_esc(m: re.Match) -> str:
            nonlocal seen
            if seen:
                return ""
            seen = True
            return m.group(0)
        text = re.sub(esc, _one_esc, text, flags=re.I)
    if CRM_ESCALATION_LINE not in text and "【L2 CRM】" in text:
        text = re.sub(
            r"(##\s*【L2 CRM】\s*\n(?:\d+\.\s*[^\n]+\n)+)",
            lambda m: m.group(1).rstrip() + f"\n4. {CRM_ESCALATION_LINE}\n",
            text,
            count=1,
            flags=re.I,
        )
    return text


def finalize_ticket_answer(
    answer: str, order_id: str, report: str, folder: Path | None
) -> str:
    """Очистка утечки промпта + подстановка шапки INT-2010 и формулировки ЗНД из движка."""
    from sppr_llm import clean_l2_output

    text = clean_l2_output(answer)
    err_hdr = format_2010_header_for_ticket(extract_2010_errors(report, folder))
    only_201 = is_201_report(report) and not is_143_report(report)
    znd = "" if only_201 else extract_znd_ticket_line(report)

    text = re.sub(
        r"\*\*0993/0973\*\*[^\n]*",
        "*(номер ЗНД — см. блок **Номер ЗНД** в шапке.)*",
        text,
        count=1,
        flags=re.I,
    )

    if "Ошибка из INT-2010" not in text and not is_143_report(report):
        guid_m = re.search(
            r"(\*\*guid заказа:\*\*[^\n]+\n)",
            text,
            re.I,
        )
        insert = f"\n{err_hdr}\n\n"
        if znd:
            insert += f"{znd}\n\n"
        if guid_m:
            pos = guid_m.end()
            text = text[:pos] + insert + text[pos:]
        else:
            lines = text.splitlines()
            if lines:
                text = lines[0] + "\n" + insert + "\n".join(lines[1:])
    elif "Номер ЗНД:" not in text:
        guid_m = re.search(r"(\*\*guid заказа:\*\*[^\n]+\n)", text, re.I)
        if guid_m:
            pos = guid_m.end()
            text = text[:pos] + f"\n{znd}\n\n" + text[pos:]

    text = remove_duplicate_znd_block(text)
    is_143 = is_143_report(report) and not is_143_152_report(report, folder)
    is_004 = is_004_report(report)
    if only_201:
        text = re.sub(
            r"^#+\s*Номер ЗНД:?\s*\n(?:.*\n)*?(?=^#+\s|\Z)",
            "",
            text,
            flags=re.I | re.M,
        )
        text = re.sub(r"\*\*Номер ЗНД:\*\*[^\n]+\n?", "", text, flags=re.I)
        text = re.sub(
            r"^Номер ЗНД:\s*уточнить по trace INT[^\n]*\n?",
            "",
            text,
            flags=re.I | re.M,
        )
        text = re.sub(
            r"\*\*Не требуется уточнение номера ЗНД[^\n]*\n?",
            "",
            text,
            flags=re.I,
        )
    if is_143:
        text = re.sub(
            r"\(см\.\s*блок\s*【Корень из INT-2010】[^)]*\)",
            "",
            text,
            flags=re.I,
        )
        text = re.sub(
            r"guid\s+заказа:\s*([0-9a-f\-]{36})\s*\(см\.[^\n]*\)",
            r"**guid заказа:** `\1`",
            text,
            flags=re.I,
        )
    if not is_143 and not is_004:
        text = normalize_l2_crm_escalation(text, report)
    text = enrich_005_ticket(text, report)
    if only_201:
        text = enrich_201_ticket(text, report, folder, order_id)
    elif is_004:
        text = enrich_004_ticket(text, report, folder, order_id)
    elif is_143_152_report(report, folder):
        text = enrich_143_152_ticket(text, report, folder, order_id)
    elif is_143_report(report):
        text = enrich_143_ticket(text, report, folder, order_id)
        text = _dedupe_143_l2_crm_lines(text)
        try:
            from sppr_markdown_fix import fix_ticket_section_glue

            text = fix_ticket_section_glue(text)
        except ImportError:
            pass
    text = _fix_origin_in_answer(text, report)
    text = _strip_spurious_sections(text)
    if is_005_report(report):
        text = _renumber_l2_crm_block(text)
        text = _dedupe_005_l2_crm_lines(text)
    structured_enrich = (
        is_004_report(report)
        or is_143_report(report)
        or is_143_152_report(report, folder)
        or only_201
    )
    try:
        from sppr_markdown_fix import (
            finalize_display_markdown,
            polish_llm_answer,
            strip_html_link_artifacts,
        )

        text = strip_html_link_artifacts(text)
        if structured_enrich:
            text = finalize_display_markdown(text)
        else:
            text = polish_llm_answer(text)
        text = strip_html_link_artifacts(text)
    except ImportError:
        pass
    return text.strip()


def extract_143_152_0992_rows(
    report: str, folder: Path | None = None
) -> list[dict]:
    """Строки позиций 143+152 из краткого/полного отчёта движка."""
    folder = folder or _parse_folder_from_report(report)
    pos_152 = _resolve_152_position(report, folder)
    pos_143 = set(extract_143_context(report).get("positions", []))
    rows: list[dict] = []
    for m in re.finditer(
        r"поз\.\s*\*\*(\d+)\*\*[^:]*:\s*Material=(\d+);\s*"
        r"(?:ZPRI=([\d.]+);\s*)?ZZ1_SPTECH=(\w+);\s*статус\s+(\S+)",
        report,
        re.I,
    ):
        pos = _norm_crm_pos(m.group(1))
        tid = "152" if pos == pos_152 else "143"
        rows.append(
            {
                "pos": pos,
                "typeid": tid,
                "status": m.group(5),
                "sptech": m.group(4),
                "zpri": m.group(3) or "",
            }
        )
    for m in re.finditer(
        r"поз\.\s*(\d+):\s*Material=(\d+);\s*ZPRI=([\d.]+);\s*"
        r"ZZStatusUserItem=(\w+)(?:;\s*ZZ1_SPTECH=(\w+))?",
        report,
        re.I,
    ):
        pos = _norm_crm_pos(m.group(1))
        if pos == pos_152:
            tid = "152"
        elif pos in pos_143:
            tid = "143"
        else:
            tid = "143" if pos in ("870", "780") else "?"
        rows.append(
            {
                "pos": pos,
                "typeid": tid,
                "status": m.group(4),
                "sptech": m.group(5) or "—",
                "zpri": m.group(3),
            }
        )
    for m in re.finditer(
        r"поз\.\s*(\d+):\s*ZZ1_SPTECH=(\w+);\s*ZZStatusUserItem=(\w+).*?Material=(\d+)",
        report,
        re.S | re.I,
    ):
        pos = _norm_crm_pos(m.group(1))
        if any(r["pos"] == pos for r in rows):
            continue
        if pos == pos_152:
            tid = "152"
        elif pos in pos_143:
            tid = "143"
        else:
            tid = "143" if pos in ("870", "780") else "?"
        rows.append(
            {
                "pos": pos,
                "typeid": tid,
                "status": m.group(3),
                "sptech": m.group(2),
                "zpri": "",
            }
        )
    for p in pos_143:
        if not any(r["pos"] == p for r in rows):
            rows.append(
                {"pos": p, "typeid": "143", "status": "?", "sptech": "?", "zpri": ""}
            )
    if pos_152 and not any(r["pos"] == pos_152 for r in rows) and folder:
        try:
            from sppr_diagnose import extract_latest_0992_snapshot, _crm_pos_key

            snap = extract_latest_0992_snapshot(folder)
            key = _crm_pos_key(pos_152)
            for it in (snap or {}).get("items") or []:
                if _crm_pos_key(str(it.get("pos") or "")) != key:
                    continue
                rows.append(
                    {
                        "pos": key,
                        "typeid": "152",
                        "status": it.get("status") or "?",
                        "sptech": it.get("sptech") or "?",
                        "zpri": str(it.get("zpri") or ""),
                    }
                )
                break
        except ImportError:
            rows.append(
                {
                    "pos": pos_152,
                    "typeid": "152",
                    "status": "?",
                    "sptech": "?",
                    "zpri": "",
                }
            )
    return sorted(rows, key=lambda x: int(x["pos"]) if x["pos"].isdigit() else 0)


def format_143_152_prompt_block(report: str) -> str:
    lines = ["## Ветка 143+152 (обязательно)"]
    lines.append(format_143_prompt_block(extract_143_context(report)))
    lines.append(
        "- **152:** технология при ЗНД; в 0992 на поз. 910 часто **E0020** (рекламация) — "
        "указать в 【L2 CRM】; не менять **ZTRN** при активной ЗНД."
    )
    lines.append(
        "- **Вёрстка:** таблицы только markdown (`| col |`), пустая строка перед `##`."
    )
    return "\n".join(lines)


def build_143_152_itogo_block(
    report: str, folder: Path | None = None
) -> str:
    folder = folder or _parse_folder_from_report(report)
    ctx = extract_143_context(report)
    pos_143 = ", ".join(ctx.get("positions") or ["870", "780"])
    pos_152 = _resolve_152_position(report, folder)
    dt2010 = ""
    dm = re.search(r"INT-2010,\s*([0-9T:.\-]+)", report)
    if dm:
        dt2010 = dm.group(1)
    if not dt2010:
        dm_b = re.search(r"\*\*INT-2010\*\*\s*\(([^)]+)\)", report, re.I)
        if dm_b:
            dt2010 = dm_b.group(1).strip()
    dt0992 = ""
    dm2 = re.search(r"INT-0992,\s*([0-9T:.\-]+)", report)
    if dm2:
        dt0992 = dm2.group(1)
    lines = [
        "## Итого",
        "",
        f"При обработке в S/4 **INT-2010 от {dt2010 or '…'}** зафиксированы **две** корневые ошибки:",
        "",
        f"1. **143** — поз. **{pos_143}**: цена **ZPRI** меняется после созданной **ЗНД**.",
        f"2. **152** — поз. **{pos_152 or '910'}**: смена технологии отгрузки невозможна при существующей **ЗНД**.",
        "",
    ]
    row_152 = _152_row_from_report(report, folder, pos_152)
    st_152 = str(row_152.get("status") or "")
    if st_152 == "E0020":
        lines.append(
            f"На поз. **{pos_152 or '910'}** в INT-0992: **E0020** — в CRM Web UI «Рекламация» "
            "(сверить с UI, не только код в JSON)."
        )
        lines.append("")
    if dt0992:
        lines.append(f"В **INT-0992 от {dt0992}**:")
        lines.append("")
    lines.append("| Поз. | typeID | Статус | Технология | ZPRI |")
    lines.append("| --- | --- | --- | --- | --- |")
    for r in extract_143_152_0992_rows(report, folder):
        zpri = r.get("zpri") or "—"
        st = str(r.get("status") or "—")
        lines.append(
            f"| **{r['pos']}** | {r['typeid']} | {st} | {r.get('sptech') or '—'} | {zpri} |"
        )
    lines.append("")
    if re.search(r"ЗНД в JSON:.*не найден", report, re.I):
        lines.append(
            "**0973/0993** в выгрузке **нет** — номер ЗНД только после **CRM Web UI** "
            "или проверки в **S/4**."
        )
    lines.append("")
    origin = "CRM"
    if re.search(r"Источник заказа:\s*IMK", report, re.I):
        origin = "CRM (ИМК в метаданных)"
    lines.append(f"Класс **A**. Источник заказа: **{origin}**.")
    return "\n".join(lines)


def build_143_152_confluence_block(report: str) -> str:
    links = [
        (
            "Маршрутизация **143** (Ошибка 2/3), **152** (п. 10.53)",
            "https://example.local/confluence?pageId=DEMO_PAGE_ID",
        ),
        (
            "Коды **INT-2010** (строки **143**, **152**)",
            "https://example.local/confluence?pageId=DEMO_PAGE_ID",
        ),
        (
            "Шаги исправления цен **ZPRI** в CRM",
            "https://example.local/confluence?pageId=DEMO_PAGE_ID",
        ),
        (
            "Цепочка INT",
            "https://example.local/confluence?pageId=DEMO_PAGE_ID",
        ),
    ]
    lines = ["## Confluence", "", "| Назначение | Ссылка |", "| --- | --- |"]
    for title, url in links:
        if url in report or title.split()[0] in report:
            lines.append(f"| {title} | {url} |")
    if len(lines) <= 4:
        for title, url in links:
            lines.append(f"| {title} | {url} |")
    return "\n".join(lines)


def _crm_status_hint(code: str) -> str:
    """Подпись статуса позиции для тикета (E0020 → рекламация)."""
    if not code or code == "?":
        return ""
    try:
        from sppr_diagnose import crm_status_label

        lbl = crm_status_label(code)
    except ImportError:
        lbl = ""
    if code == "E0020" or (lbl and "екламац" in lbl.lower()):
        return "; статус **E0020** (рекламация в CRM Web UI)"
    if lbl and lbl != code:
        return f"; статус **{code}** ({lbl})"
    return f"; статус **{code}**"


def _152_row_from_report(report: str, folder: Path | None, pos_152: str) -> dict:
    for r in extract_143_152_0992_rows(report, folder):
        if r.get("typeid") == "152" or r.get("pos") == pos_152:
            return r
    return {}


def build_143_152_l2_crm_block(
    report: str, folder: Path | None = None
) -> str:
    folder = folder or _parse_folder_from_report(report)
    ctx = extract_143_context(report)
    pos_143 = ", ".join(ctx.get("positions") or ["870", "780"])
    pos_152 = _resolve_152_position(report, folder)
    row_152 = _152_row_from_report(report, folder, pos_152)
    sptech = row_152.get("sptech") or "ZTRN"
    if sptech in ("?", "—", ""):
        sm = re.search(
            rf"поз\.\s*\*\*{re.escape(pos_152)}\*\*[^\n]*ZZ1_SPTECH=(\w+)",
            report,
            re.I,
        )
        if sm:
            sptech = sm.group(1)
    st_hint = _crm_status_hint(str(row_152.get("status") or ""))
    lines = [
        "## 【L2 CRM】",
        "",
        f"1. **143 (ZPRI):** поз. **{pos_143}** — после ЗНД цену менять нельзя; "
        f"при подтверждённой ЗНД — **отменить ЗНД**, затем ZPRI "
        f"([DEMO_PAGE_ID]({CONF_143_PRICES})).",
        f"2. **152 (технология):** поз. **{pos_152}** — при активной ЗНД не менять технологию "
        f"(**{sptech}** и др.); проверить **ЗНД** в CRM Web UI{st_hint}.",
        "3. **Номер ЗНД в тикете:** **143** (поз. "
        f"{pos_143}) и **152** (поз. **{pos_152}**) — следствие одной **ЗНД** "
        "на заказе (S/4 блокирует ZPRI и смену технологии после её создания). "
        "Номер ЗНД — из **CRM Web UI**; в JSON по заказу **0993/0973 нет**.",
    ]
    if re.search(r"ЗНД в JSON:.*не найден", report, re.I):
        lines.append(
            "4. Если в CRM Web UI ЗНД не видна — уточнить блокировку в **S/4** (VA03) "
            "и указать номер ЗНД в ответе L2."
        )
    return "\n".join(lines)


def build_143_152_ticket_header(
    order_id: str, report: str, folder: Path | None
) -> str:
    guid = ""
    gm = re.search(
        r"guid[^\n`]*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        report,
        re.I,
    )
    if gm:
        guid = gm.group(1)
    lines = [f"**Заказ {order_id}** (143+152)", ""]
    if guid:
        lines.append(f"**guid заказа:** `{guid}`")
        lines.append("")
    err_block = format_2010_header_for_ticket(extract_2010_errors(report, folder))
    lines.append(err_block)
    return "\n".join(lines).strip()


def enrich_143_152_ticket(
    text: str,
    report: str,
    folder: Path | None = None,
    order_id: str = "",
) -> str:
    """Эталонная вёрстка и чеклист для составного 143+152 (DEMO_ORDER_001)."""
    if not is_143_152_report(report, folder):
        return text
    itogo = build_143_152_itogo_block(report, folder)
    conf = build_143_152_confluence_block(report)
    l2 = build_143_152_l2_crm_block(report, folder)
    body = f"{itogo}\n\n{conf}\n\n{l2}\n"
    head = _normalize_143_ticket_head(_ticket_head_before_structured_tail(text))
    head = re.sub(r"\*\*Номер ЗНД:\*\*[^\n]+\n?", "", head, flags=re.I)
    oid = order_id or ""
    if not oid:
        om = re.search(r"\*\*Заказ\s+(\d{10})\*\*", head) or re.search(
            r"заказ[а]?\s*(\d{10})", report, re.I
        )
        if om:
            oid = om.group(1)
    if oid:
        head = build_143_152_ticket_header(oid, report, folder)
    out = f"{head}\n\n{body}".strip()
    out = _strip_vague_escalation_lines(out)
    out = _fix_origin_in_answer(out, report)
    return out


def _clean_llm_head_garbage(head: str) -> str:
    """Убрать кривые inline-таблицы и дубли «Итого» из шапки LLM."""
    out: list[str] = []
    for line in head.splitlines():
        if re.search(r"\|\s*Поз\.\s*\|", line) and not line.strip().startswith("|"):
            line = re.split(r"\s*\|\s*Поз\.\s*\|", line, maxsplit=1)[0].rstrip(" |—")
        if re.match(r"^\s*\d{1,3}\s*\|\s*\d{3}\s*\|", line.strip()):
            continue
        if re.search(r"^Итого\s+Заказ\s+\d", line, re.I):
            continue
        if re.search(r"^##\s*Итого\b", line, re.I):
            continue
        if re.search(r"^##\s*Confluence\b", line, re.I):
            continue
        if re.search(r"^【L2 CRM】", line):
            continue
        if re.match(r"^\|\s*(?:Назначение|Маршрутизация|Ссылка)\b", line.strip(), re.I):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _ticket_head_before_structured_tail(text: str) -> str:
    """Шапка тикета до ## Итого / 【L2 CRM】 (для подстановки эталона 143)."""
    for pat in (
        r"^##\s*Итого\b",
        r"^#\s*Итого\b",
        r"^##\s*Номер ЗНД",
        r"^【L2 CRM】",
        r"^##\s*【L2 CRM】",
        r"^##\s*Confluence\b",
        r"^Итого\s+Заказ\s+\d",
        r"^#*\s*Итого:\s*заказ",
        r"^Итого\s+Смысл\s+143",
    ):
        m = re.search(pat, text, re.I | re.M)
        if m:
            return _clean_llm_head_garbage(text[: m.start()])
    return _clean_llm_head_garbage(text)


def _fix_origin_in_answer(text: str, report: str) -> str:
    """Исправить источник заказа, если LLM подставил неверный CustomerPurchaseOrderType."""
    if re.search(r"Источник заказа:\s*IMK|CustomerPurchaseOrderType\s*=\s*2|\|\s*IMK\s*\|", report, re.I):
        text = re.sub(
            r"CustomerPurchaseOrderType\s*=\s*1",
            "CustomerPurchaseOrderType=2",
            text,
            flags=re.I,
        )
        text = re.sub(
            r"Источник заказа:\s*\*{0,2}CRM\*{0,2}",
            "Источник заказа: **ИМК**",
            text,
            flags=re.I,
        )
    return text


def build_143_ticket_header(order_id: str, report: str, folder: Path | None) -> str:
    """Единая шапка тикета 143 (без дубля «Итого: заказ…»)."""
    guid = ""
    gm = re.search(
        r"guid[^\n`]*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        report,
        re.I,
    )
    if gm:
        guid = gm.group(1)
    lines = [f"**Заказ {order_id}** (143)", ""]
    if guid:
        lines.append(f"**guid заказа:** `{guid}`")
        lines.append("")
    err_block = format_2010_header_for_ticket(extract_2010_errors(report, folder))
    lines.append(err_block)
    return "\n".join(lines).strip()


def _normalize_143_ticket_head(head: str) -> str:
    """Убрать из шапки дубли Итого/таблицы/Confluence до подстановки эталона."""
    out: list[str] = []
    for line in head.splitlines():
        if re.match(r"^#*\s*Итого:\s*заказ", line, re.I):
            continue
        if re.match(r"^#*\s*Итого\b", line, re.I):
            continue
        if re.match(r"^Итого\s+Смысл\s+143", line, re.I):
            continue
        if re.match(r"^\|?\s*\d{1,3}\s*\|", line.strip()):
            continue
        if re.match(r"^\|\s*Поз\.", line.strip(), re.I):
            continue
        if re.match(r"^##\s*Confluence\b", line, re.I):
            break
        if re.match(r"^Confluence\b", line, re.I):
            break
        out.append(line)
    return "\n".join(out).strip()


def enrich_143_ticket(
    text: str, report: str, folder: Path | None = None, order_id: str = ""
) -> str:
    """Подставить чеклист 143 вместо размытых рекомендаций LLM."""
    if not is_143_report(report) or is_143_152_report(report, folder):
        return text
    ctx = extract_143_context(report)
    itogo = build_143_itogo_block(report, ctx)
    conf = build_143_confluence_block()
    l2 = build_143_l2_crm_block(ctx, report)
    body = f"{itogo}\n\n{conf}\n\n{l2}\n"
    head = _normalize_143_ticket_head(_ticket_head_before_structured_tail(text))
    head = re.sub(r"\*\*Номер ЗНД:\*\*[^\n]+\n?", "", head, flags=re.I)
    head = re.sub(r"^Номер ЗНД:[^\n]+\n?", "", head, flags=re.M | re.I)
    oid = order_id or ""
    if not oid:
        om = re.search(r"\*\*Заказ\s+(\d{10})\*\*", head) or re.search(
            r"заказ[а]?\s*(\d{10})", report, re.I
        )
        if om:
            oid = om.group(1)
    if oid:
        head = build_143_ticket_header(oid, report, folder)
    out = f"{head}\n\n{body}".strip()
    out = _strip_vague_escalation_lines(out)
    out = _fix_origin_in_answer(out, report)
    return out


def _dedupe_143_l2_crm_lines(text: str) -> str:
    """Убрать остатки дублей эскалации и «причина не ясна» после подстановки блока 143."""
    return _strip_vague_escalation_lines(text)


def build_004_itogo_block(
    report: str, order_id: str, folder: Path | None = None
) -> str:
    ctx = extract_004_context(report, folder)
    guid_m = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        report,
        re.I,
    )
    guid = guid_m.group(1) if guid_m else ""
    origin = "**ИМК** (`CustomerPurchaseOrderType=2`)" if ctx.get("origin_imk") else "**CRM**"
    note = ctx.get("note") or "Снять резерв невозможно, так как существует неотмененная ЗНД"
    dt2010 = ctx.get("dt2010") or "…"
    dt0992 = ctx.get("dt0992") or "…"
    ac = ctx.get("action_code") or "02"
    e10 = ctx.get("e0010_count")
    e10_txt = f" — **{e10}** строк" if e10 else ""
    pos = ctx.get("e0020_pos") or "60"
    mat = ctx.get("e0020_material") or "2242013"
    spt = ctx.get("e0020_sptech") or "ZIPT"
    hdr_st = ctx.get("order_status") or "E0001"
    lines = [
        "## Итого",
        "",
        f"Заказ **{order_id}**"
        + (f" (guid **{guid}**)" if guid else "")
        + f", {origin}: при обработке в S/4 **INT-2010 от {dt2010}** — "
        f"**004(ZSD_AIF_SDSLS_IN)** — «{note}».",
        "",
        f"Предшествующий запрос: **INT-0992 от {dt0992}** "
        f"(`actionCode={ac}`, изменение заказа).",
        "",
        f"В **0992** на большинстве позиций **`ZZStatusUserItem=E0010`** "
        f"(передано в доставку, ЗНД создано){e10_txt}; на поз. **{pos}** "
        f"(материал **{mat}**, **{spt}**): **`E0020`** (рекламация). "
        f"Заголовок заказа: **`ZZStatusUser={hdr_st}`**.",
        "",
        "**0973/0993** в архиве **нет** — номер ЗНД в тикете только после "
        "**CRM Web UI** или ответа **L2 S/4 (VA03)**.",
        "",
        "Класс **A**. Это **004** (ЗНД блокирует снятие резерва), **не 003** "
        "(в note нет текста про вид заказа/технологию без ЗНД).",
    ]
    return "\n".join(lines)


def build_004_confluence_block() -> str:
    links = [
        (
            "Строка **004** в таблице INT-2010 (резерв при активной ЗНД; репликация 0993 по ЗНД)",
            "https://example.local/confluence?pageId=DEMO_PAGE_ID",
        ),
        (
            "Маршрутизация L2 CRM по интеграции заказов",
            "https://example.local/confluence?pageId=DEMO_PAGE_ID",
        ),
        (
            "Цепочка INT 0989→0992→2010→2026",
            "https://example.local/confluence?pageId=DEMO_PAGE_ID",
        ),
        (
            "Технологии ZTAN / ZTRN / ZIPT (контекст ИМК)",
            "https://example.local/confluence?pageId=DEMO_PAGE_ID",
        ),
    ]
    lines = ["## Confluence", "", "| Назначение | Ссылка |", "| --- | --- |"]
    for title, url in links:
        lines.append(f"| {title} | {url} |")
    return "\n".join(lines)


def build_004_l2_crm_block(report: str, folder: Path | None = None) -> str:
    ctx = extract_004_context(report, folder)
    pos = ctx.get("e0020_pos") or "60"
    mat = ctx.get("e0020_material") or "2242013"
    dt2010 = ctx.get("dt2010") or "…"
    dt0992 = ctx.get("dt0992") or "…"
    items = [
        f"1. Указать в тикете **INT-2010** и **INT-0992** с **dateTime** "
        f"({dt2010}; {dt0992}).",
        "2. **CRM Web UI:** активные **ЗНД** по заказу; что сделал оператор "
        f"(на **0992** — рекламация на поз. **{pos}**, арт. **{mat}**, см. инструкцию в заказе).",
        "3. Сверить: позиции с **E0010** — с ЗНД; поз. "
        f"**{pos}** (**E0020**) — попытка изменения/снятия резерва при неотменённой "
        "ЗНД на заказе → ожидаемый отказ S/4 **004**.",
        "4. **Не** путать с **003** (другой текст в **2010**); **не** с **152** "
        "(смена технологии при ЗНД) — здесь корень именно **снятие резерва**.",
        "5. Если по процессу нужна отмена/закрытие ЗНД — действия по регламенту CRM/ЗНД; "
        "после отмены ЗНД в UI — повтор интеграции (не «снять INT»).",
    ]
    lines = ["## 【L2 CRM】", ""]
    for item in items:
        lines.append(item)
        lines.append("")
    return "\n".join(lines).rstrip()


def build_004_ticket_header(
    order_id: str, report: str, folder: Path | None
) -> str:
    guid = ""
    gm = re.search(
        r"guid[^\n`]*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        report,
        re.I,
    )
    if gm:
        guid = gm.group(1)
    lines = [f"**Заказ {order_id}** (004 — резерв и ЗНД)", ""]
    if guid:
        lines.append(f"**guid заказа:** `{guid}`")
        lines.append("")
    lines.append(format_2010_header_for_ticket(extract_2010_errors(report, folder)))
    lines.append("")
    lines.append(build_004_znd_line(report, folder))
    return "\n".join(lines).strip()


def enrich_004_ticket(
    text: str,
    report: str,
    folder: Path | None = None,
    order_id: str = "",
) -> str:
    """Эталонная вёрстка для 004 (DEMO_ORDER_001)."""
    if not is_004_report(report):
        return text
    oid = order_id or ""
    if not oid:
        om = re.search(r"заказ[а]?\s*(\d{10})", report, re.I) or re.search(
            r"\*\*Заказ\s+(\d{10})\*\*", text, re.I
        )
        if om:
            oid = om.group(1)
    itogo = build_004_itogo_block(report, oid, folder)
    conf = build_004_confluence_block()
    l2 = build_004_l2_crm_block(report, folder)
    body = f"{itogo}\n\n{conf}\n\n{l2}\n"
    head = _normalize_143_ticket_head(_ticket_head_before_structured_tail(text))
    head = re.sub(r"\*\*Номер ЗНД:\*\*[^\n]+\n?", "", head, flags=re.I)
    head = re.sub(r"^Номер ЗНД:[^\n]+\n?", "", head, flags=re.M | re.I)
    head = re.sub(r"^#+\s*Итого:?\s*заказ[^\n]*\n?", "", head, flags=re.I | re.M)
    head = re.sub(r"^Итого:?\s*заказ[^\n]*\n?", "", head, flags=re.I | re.M)
    if oid:
        head = build_004_ticket_header(oid, report, folder)
    out = f"{head}\n\n{body}".strip()
    out = _strip_vague_escalation_lines(out)
    out = _fix_origin_in_answer(out, report)
    try:
        from sppr_markdown_fix import fix_ticket_section_glue

        out = fix_ticket_section_glue(out)
    except ImportError:
        pass
    return out


def enrich_005_ticket(text: str, report: str) -> str:
    """Для 005: подставить факты 0992 и рекомендацию по рекламации → S/4."""
    if not is_005_report(report):
        return text
    ctx = extract_005_context(report)

    # Факт 0992 в ## Итого, если LLM не вывел
    if ctx.get("ext_dt") and ctx["ext_dt"] not in text:
        fact = (
            f"\n\n**INT-0992 (снимок):** `ExternalDocLastChangeDateTime` = `{ctx['ext_dt']}`"
        )
        if ctx.get("snap_0992_dt"):
            fact += f", dateTime 0992 = `{ctx['snap_0992_dt']}`"
        if ctx.get("n0993") != "":
            fact += f"; файлов **0993** в выгрузке: **{ctx['n0993']}**"
        if "## Итого" in text:
            text = re.sub(
                r"(## Итого\s*\n)",
                r"\1" + fact.lstrip() + "\n",
                text,
                count=1,
                flags=re.I,
            )
        else:
            text += fact

    if "【L2 CRM】" not in text:
        text += f"\n\n## 【L2 CRM】\n"
    has_reclamation_s4 = bool(
        re.search(r"обновления рекламации в заказе", text, re.I)
    )
    if not has_reclamation_s4 and (
        ctx.get("reclamation_positions")
        or "E0020" in report
        or "рекламац" in report.lower()
    ):
        bullet = f"4. {CRM_005_RECLAMATION_S4_LINE}"
        if re.search(r"##\s*【L2 CRM】", text, re.I):
            text = re.sub(
                r"(##\s*【L2 CRM】\s*\n(?:\d+\.\s*[^\n]+\n)*)",
                lambda m: m.group(1) + bullet + "\n",
                text,
                count=1,
                flags=re.I,
            )
        else:
            text += f"\n\n## 【L2 CRM】\n{bullet}\n"

    text = re.sub(
        r"(##\s*Ошибка из INT-2010[^\n]*\n+)\s*\*\*Ошибка из INT-2010",
        r"**Ошибка из INT-2010",
        text,
        count=1,
        flags=re.I,
    )
    text = re.sub(
        r"^4\.\s*Если в CRM Web UI номер ЗНД",
        "5. Если в CRM Web UI номер ЗНД",
        text,
        count=1,
        flags=re.M | re.I,
    )
    if re.search(r"обновления рекламации в заказе", text, re.I):
        text = re.sub(
            r"(?<!\d\.\s)(При наличии позиций[^\n]*обновления рекламации в заказе\.?)",
            r"4. \1",
            text,
            count=1,
            flags=re.I,
        )

    # Confluence из отчёта движка
    if "## Confluence" not in text:
        urls = re.findall(
            r"(https://example\.local/pages/viewpage\.action\?pageId=\d+)",
            report,
        )
        if urls:
            uniq = list(dict.fromkeys(urls))[:6]
            conf = "## Confluence\n\n" + "\n".join(f"- {u}" for u in uniq)
            if re.search(r"##\s*【L2 CRM】", text, re.I):
                text = re.sub(
                    r"(##\s*【L2 CRM】)",
                    conf + "\n\n\\1",
                    text,
                    count=1,
                    flags=re.I,
                )
            else:
                text += "\n\n" + conf

    return text


def _dedupe_005_l2_crm_lines(text: str) -> str:
    """Убрать дубли п. про рекламацию→S/4 и эскалации в хвосте 【L2 CRM】."""
    seen_rec = False
    seen_esc = False
    out: list[str] = []
    for line in text.splitlines():
        if re.search(r"обновлени[ея]\s+рекламации", line, re.I):
            if seen_rec:
                continue
            seen_rec = True
        if re.search(
            r"(проверить данные на стороне S/4|причину не удалось установить|причину не определить)",
            line,
            re.I,
        ):
            if seen_esc:
                continue
            seen_esc = True
        out.append(line)
    return "\n".join(out)


def _renumber_l2_crm_block(text: str) -> str:
    m = re.search(r"(##\s*【L2 CRM】\s*\n)(.*?)(?=\n##|\Z)", text, re.S | re.I)
    if not m:
        return text
    items: list[str] = []
    for ln in m.group(2).splitlines():
        ln = ln.strip()
        if not ln:
            continue
        items.append(re.sub(r"^\d+\.\s*", "", ln))
    if not items:
        return text
    body = "\n".join(f"{i + 1}. {it}" for i, it in enumerate(items))
    return text[: m.start(2)] + body + text[m.end(2) :]


def _strip_spurious_sections(text: str) -> str:
    text = re.sub(
        r"\n##\s*Итого\s*\(без[^\n]*\)[\s\S]*?(?=\n##\s*(?:Confluence|【L2 CRM】)|\Z)",
        "\n",
        text,
        flags=re.I,
    )
    return re.sub(r"\n{3,}", "\n\n", text)


def main() -> None:
    load_env()
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="СППР: diagnose + опционально LLM")
    ap.add_argument("--order", required=True)
    ap.add_argument("--folder", default="")
    ap.add_argument(
        "--full",
        action="store_true",
        help="Полный отчёт sppr_diagnose (таймлайн INT, ветки KB, Confluence); иначе краткий",
    )
    ap.add_argument("--llm", action="store_true", help="Вызвать LLM API после diagnose")
    ap.add_argument("--out", default="", help="Файл итогового ответа")
    args = ap.parse_args()

    folder = Path(args.folder) if args.folder else find_folder(args.order)
    if not folder:
        print(f"Папка заказа не найдена под {ROOT}", file=sys.stderr)

    report_path = MATERIALS / f"report_{args.order}.txt"
    report = run_diagnose(args.order, folder, report_path, full=bool(args.full))
    kb = kb_hints_for_report(report)
    few = few_shot_block()

    if not args.llm:
        if args.out:
            Path(args.out).write_text(report, encoding="utf-8")
        print(report)
        print("\n---\n[Без LLM: только движок. Для оформления: --llm и SPPR_LLM_API_KEY]\n")
        return

    if is_clean_integration_report(report):
        if args.out:
            Path(args.out).write_text(report, encoding="utf-8")
        print(report)
        print(
            "\n---\n[LLM не вызывался: ошибок в интеграционных сценариях не найдено — "
            "оставлен вывод движка с ✅]\n"
        )
        return

    is_201_only = is_201_report(report) and not is_143_report(report)
    if is_201_only:
        answer = build_201_full_ticket_answer(report, args.order, folder)
        if args.out:
            Path(args.out).write_text(answer, encoding="utf-8")
        print(answer)
        print(
            "\n---\n[Для **201(R1)**: оформление по эталону движка (CRM Web UI → S/4), "
            "LLM не вызывался — без лишних шагов в 【L2 CRM】]\n"
        )
        return

    try:
        raw = call_llm(
            load_system_prompt(),
            build_user_message(args.order, report, kb, few, folder),
        )
        answer = finalize_ticket_answer(raw, args.order, report, folder)
    except RuntimeError as e:
        print(report)
        print(f"\n--- LLM не вызван: {e}\n", file=sys.stderr)
        sys.exit(1)

    if args.out:
        Path(args.out).write_text(answer, encoding="utf-8")
    print(answer)


if __name__ == "__main__":
    main()
