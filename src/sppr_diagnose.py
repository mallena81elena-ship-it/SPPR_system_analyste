# -*- coding: utf-8 -*-
"""MVP СППР: диагностика заказа по папке JSON + строка CSV + kb_typeid_index."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(r"c:\Users\kholina\Desktop\Диплом\Примеры")
MAT = Path(__file__).parent
CSV = ROOT / "Ошибки_1.csv"
KB = MAT / "kb_typeid_index.json"

TID = re.compile(r"(\d{2,3})\(([A-Za-z0-9_/]+)\)")
SKIP_ROOT = {
    "017(SOA_SD)",
    "003(SOA_SD)",
    "154(ZSD_AIF_SDSLS_IN)",
    "158(/AIF/MES)",
    "158(ZSD_AIF_SDSLS_IN)",
    "099(SLS_LORD)",
    "013(V1)",
    "002(/AIF/ALERT)",
    "081(/AIF/MES)",
}

INT_MARKERS = [
    ("0989", "CRM->OMS", "Отправка заказа из CRM"),
    ("0992", "OMS->S/4", "Передача в S/4"),
    ("2010", "S/4->OMS", "Статус/ошибка обработки S/4"),
    ("2026", "OMS->CRM", "Доставка статуса в CRM"),
    ("0993", "S/4->OMS->CRM", "ЗНД / подтверждение"),
    ("0973", "CRM->OMS", "Создание/изменение ЗНД (OfdCreated)"),
]

CONFLUENCE_BASE = "https://example.local/confluence?pageId="
ORIGIN_MAP = {"1": "CRM", "2": "IMK", "crm_sapcrm": "CRM", "eshop_hybris": "IMK"}

# Тех. код → наименование статуса позиции в CRM (Confluence DEMO_PAGE_ID, §8.2 СППР_ПРАВИЛА_LLM)
CRM_STATUS_POS: dict[str, str] = {
    "E0002": "Отменена оператором",
    "E0006": "Отказ в поставке. ЗНД отменено",
    "E0008": "Передана на КК. ЗНД начальный",
    "E0009": "Отказ КК. ЗНД отменено",
    "E0010": "Передано в доставку. ЗНД создано",
    "E0011": "Принята в ОД. ЗНД принято ОД",
    "E0013": "В пути. ЗНД в пути",
    "E0014": "Доставлено успешно. ЗНД выполнено",
    "E0015": "Не доставлено. ЗНД не доставлено",
    "E0016": "В пути. Отказ на месте",
    "E0017": "Раскомплект. ЗНД отменено",
    "E0019": "Отмена по вычерку",
    "E0020": "Рекламация",
    "E0021": "Снят резерв",
    "E0022": "Передана в ТЭК",
    "E0023": "Готова к отгрузке",
    "E0043": "Удалена",
    "E0320": "Удаление позиции (финальный)",
}

# Статусы позиции: по JSON 0992 часто сопровождают отказ S/4 по qty (ORDER_QTY) — §6.8 СППР
STATUS_LIKELY_BLOCKS_QTY: frozenset[str] = frozenset(
    {"E0020", "E0002", "E0006", "E0016", "E0019", "E0320", "E0043"}
)
CRM_STATUS_HDR: dict[str, str] = {
    "E0001": "Создана",
    "E0017": "Передан в ТЭК (заголовок)",
    "E0018": "Готов к отгрузке (заголовок)",
}


def crm_status_label(code: str | None, header: bool = False) -> str:
    if not code:
        return "(код не указан)"
    table = CRM_STATUS_HDR if header else CRM_STATUS_POS
    name = table.get(str(code).strip())
    return f"{name} ({code})" if name else str(code)


def read_csv_row(order_id: str) -> str | None:
    if not CSV.exists():
        return None
    text = CSV.read_bytes().decode("utf-8-sig")
    for row in list(csv.reader(text.splitlines(), delimiter=";"))[1:]:
        if len(row) >= 3 and row[1].strip() == order_id:
            return row[2].strip()
    return None


def find_folder(order_id: str) -> Path | None:
    for p in ROOT.iterdir():
        if p.is_dir() and order_id in p.name:
            return p
    return None


def classify_csv_label(err: str) -> tuple[str, list[str]]:
    e = (err or "").strip()
    if e in ("Ок", "Ok", ""):
        return "ok", []
    if "Не пришел" in e or "Не пришёл" in e:
        if "2010" in e:
            return "B_2010", []
        if "0993" in e:
            return "B_0993", []
        return "B_other", []
    return "A", [f"{t}({c})" for t, c in TID.findall(e)]


def scan_timeline(folder: Path) -> list[dict]:
    events: list[dict] = []
    for fp in folder.glob("*.json"):
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        dt = d.get("dateTime") or ""
        name = fp.name
        for code, direction, desc in INT_MARKERS:
            if code in name:
                events.append(
                    {
                        "dateTime": dt,
                        "int": code,
                        "direction": direction,
                        "file": name,
                        "desc": desc,
                    }
                )
                break
    events.sort(key=lambda x: x["dateTime"])
    return events


def extract_latest_2010(folder: Path, include_synth: bool = False):
    files = list(folder.glob("*2010*OmsReceivedFromS4*.json"))
    if not include_synth:
        files = [p for p in files if "SYNTH" not in p.name]
    if not files:
        return None
    best = None
    for p in files:
        d = json.loads(p.read_text(encoding="utf-8"))
        dt = d.get("dateTime") or ""
        if best is not None and dt <= best["dateTime"]:
            continue
        data = (d.get("object") or {}).get("data") or {}
        st_list = data.get("orderStatus") or [{}]
        st0 = st_list[0] if st_list else {}
        log = (st0.get("log") or [{}])[0]
        items: list[dict] = []
        info_items: list[dict] = []
        for i in log.get("item") or []:
            tid = i.get("typeID") or ""
            if tid in SKIP_ROOT:
                continue
            sev = str(i.get("severityCode") or "")
            entry = {
                "typeID": tid,
                "note": (i.get("note") or "").strip(),
                "severityCode": sev,
            }
            if sev == "3":
                items.append(entry)
            else:
                info_items.append(entry)
        obj = d.get("object") or {}
        best = {
            "dateTime": dt,
            "file": p.name,
            "messageId": obj.get("messageId") or obj.get("sourceMessageId"),
            "resultCode": log.get("businessDocumentProcessingResultCode"),
            "items": items,
            "info_items": info_items,
        }
    return best


def _2010_has_integration_error(lat: dict | None) -> bool:
    """Ошибка интеграции в последнем INT-2010: severity 3 или resultCode 5."""
    if not lat:
        return False
    if lat.get("resultCode") in (5, "5"):
        return True
    return bool(lat.get("items"))


def is_integration_scenario_clean(
    csv_cls: str, sppr_cls: str, lat: dict | None
) -> bool:
    """MSP «Ок» и в последнем 2010 нет отказа S/4 (severity 3 / rc=5)."""
    if csv_cls != "ok" or sppr_cls != "ok":
        return False
    if not lat:
        return False
    return not _2010_has_integration_error(lat)


def _result_code_label(rc: object) -> str:
    labels = {
        3: "обработка завершена",
        4: "успешно",
        5: "ошибка обработки",
    }
    if rc in labels:
        return f"{labels[rc]} (код {rc})"
    return f"код {rc}"


def format_integration_ok_banner(
    order_id: str,
    *,
    csv_err: str | None,
    cls_note: str,
    origin: str,
    lat: dict,
    znd_summary: str,
) -> list[str]:
    """Явный статус «ошибок интеграции нет» для UI и тикета."""
    dt = lat.get("dateTime") or "?"
    rc = lat.get("resultCode")
    lines = [
        f"✅ Ошибок в интеграционных сценариях по заказу **{order_id}** не найдено.",
        "",
        "| Проверка | Результат |",
        "| --- | --- |",
        f"| Метка MSP / CSV | {csv_err or 'Ок'} |",
        f"| Класс СППР | ok — {cls_note} |",
        f"| Источник заказа | {origin} |",
        f"| Последний INT-2010 | `{dt}` — businessDocumentProcessingResultCode **{rc}** ({_result_code_label(rc)}) |",
        "| typeID severity 3 в 2010 | **нет** |",
        f"| ЗНД в JSON | {znd_summary} |",
    ]
    info = lat.get("info_items") or []
    if info:
        lines.extend(
            [
                "",
                "### Справочно (не ошибка L2)",
                "",
                "Информационные сообщения в последнем INT-2010:",
            ]
        )
        for it in info[:6]:
            lines.append(
                f"- **{it.get('typeID', '')}** (severity {it.get('severityCode', '?')}): "
                f"{(it.get('note') or '')[:160]}"
            )
    lines.extend(
        [
            "",
            "**Действие L2 CRM:** ошибка интеграции не выявлена; разбор не требуется. "
            "При обращении партнёра — сверка статуса заказа в **CRM Web UI**.",
            "",
        ]
    )
    return lines


def format_integration_error_banner(order_id: str) -> list[str]:
    """Явный статус «ошибка интеграции найдена» для UI и тикета."""
    return [
        f"⚠️ Найдена ошибка в интеграционных сценариях по заказу **{order_id}**.",
        "",
    ]


def detect_order_origin(folder: Path) -> tuple[str, str]:
    """CRM / IMK / ? и CustomerPurchaseOrderType из последнего 0992."""
    best_dt = ""
    cpt = None
    for fp in folder.glob("*0992*OmsReceivedFromCRM*.json"):
        if "SYNTH" in fp.name:
            continue
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        dt = d.get("dateTime") or ""
        if dt < best_dt:
            continue
        best_dt = dt
        order = (d.get("object") or {}).get("data", {}).get("Order") or {}
        cpt = order.get("CustomerPurchaseOrderType")
    if cpt is None:
        return "?", "нет 0992"
    label = ORIGIN_MAP.get(str(cpt).strip(), f"type={cpt}")
    return label, str(cpt)


def _norm_guid(val: str | None) -> str | None:
    if not val or not str(val).strip():
        return None
    return str(val).strip()


def _guid_from_order_block(order: dict) -> str | None:
    if not isinstance(order, dict):
        return None
    for key in ("ExternalDocumentID", "ZZExtDocID", "externalDocumentId"):
        g = _norm_guid(order.get(key))
        if g:
            return g
    return None


def extract_crm_order_guid(folder: Path, order_id: str) -> str | None:
    """GUID заказа в CRM/интеграции: ExternalDocumentID / orderGuid из JSON папки."""
    found: list[tuple[str, str]] = []

    def _scan_file(fp: Path, dt: str) -> None:
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        obj = d.get("object") or {}
        data = obj.get("data") or {}

        order = data.get("Order") or data.get("order")
        if isinstance(order, list):
            order = order[0] if order else {}
        g = _guid_from_order_block(order) if isinstance(order, dict) else None
        if g:
            found.append((dt, g))

        for key in ("orderGuid", "OrderGuid"):
            g = _norm_guid(data.get(key))
            if g:
                found.append((dt, g))

        for block in data.get("salesOrder") or []:
            if not isinstance(block, dict):
                continue
            g = _norm_guid(block.get("ExternalDocumentID"))
            if g and order_id in str(block.get("ZZSalesOrdID") or block.get("ReferenceSDDocument") or ""):
                found.append((dt, g))

        jo = data.get("jsonObject")
        if isinstance(jo, str) and order_id in jo:
            try:
                inner = json.loads(jo)
            except json.JSONDecodeError:
                inner = None
            if isinstance(inner, dict):
                hdr = inner.get("header") or {}
                g = _norm_guid(hdr.get("crmOrderGuid") or hdr.get("hybrisOrderGuid"))
                if g:
                    found.append((dt, g))

    for fp in folder.glob("*.json"):
        if "SYNTH" in fp.name:
            continue
        name = fp.name.lower()
        if not any(
            x in name
            for x in (
                "0992",
                "0989",
                "2010",
                "0991",
                "crmorderchanged",
                "2025",
                "2026",
            )
        ):
            continue
        try:
            dt = json.loads(fp.read_text(encoding="utf-8")).get("dateTime") or fp.name
        except (json.JSONDecodeError, OSError):
            dt = fp.name
        _scan_file(fp, dt)

    if not found:
        return None
    found.sort(key=lambda x: x[0])
    return found[-1][1]


def format_order_header(order_id: str, crm_guid: str | None) -> str:
    if crm_guid:
        return f"Заказ {order_id} (guid заказа {crm_guid})"
    return f"Заказ {order_id} (guid заказа: не найден в JSON 0992/0989/2010)"


def int_moment_label(spec: str, date_time: str | None, filename: str, message_id: str | None = None) -> str:
    """Ссылка на сообщение INT с dateTime для поиска XML в CRM/мониторинге."""
    parts = [spec]
    if date_time:
        parts.append(date_time)
    if message_id:
        parts.append(f"messageId={message_id}")
    parts.append(f"файл {filename}")
    return ", ".join(parts)


def build_int_search_anchor(
    lat: dict | None, snap: dict | None, folder: Path
) -> list[str]:
    """Блок «когда искать XML» — дата/время из JSON INT."""
    lines = [
        "  Для поиска исходящего/входящего XML в CRM и мониторинге INT — ориентир по dateTime из JSON:",
    ]
    if lat and lat.get("dateTime"):
        lines.append(
            f"  • Ошибка S/4 (корень): **INT-2010**, **{lat['dateTime']}**, {lat.get('file', '')}"
        )
        if lat.get("messageId"):
            lines.append(f"    messageId: {lat['messageId']}")
    else:
        lines.append("  • INT-2010: нет файла с ошибкой в папке")
    if snap and snap.get("dateTime"):
        lines.append(
            f"  • Последний запрос в S/4 перед 2010: **INT-0992**, **{snap['dateTime']}**, {snap.get('file', '')}"
        )
        if snap.get("messageId"):
            lines.append(f"    messageId: {snap['messageId']}")
    dt2026 = _latest_int_datetime(folder, "2026")
    if dt2026:
        lines.append(f"  • Ответ в CRM после 2010: **INT-2026**, **{dt2026[0]}**, {dt2026[1]}")
    lines.append(
        "  В тезисе и при цитировании ошибки указывать **INT-спецификацию + dateTime** (как в файле)."
    )
    return lines


def _latest_int_datetime(folder: Path, int_code: str) -> tuple[str, str] | None:
    """Последний файл INT по коду в имени (-2010-, -0992-, -2026-)."""
    best_dt, best_name = "", ""
    marker = f"-{int_code}-"
    for fp in folder.glob("*.json"):
        if "SYNTH" in fp.name or marker not in fp.name:
            continue
        try:
            dt = json.loads(fp.read_text(encoding="utf-8")).get("dateTime") or ""
        except (json.JSONDecodeError, OSError):
            continue
        if dt >= best_dt:
            best_dt, best_name = dt, fp.name
    if best_dt:
        return best_dt, best_name
    return None


def _classify_0993_files(folder: Path) -> str:
    sales, znd, other = 0, 0, 0
    for fp in folder.glob("*0993*"):
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            other += 1
            continue
        for so in (d.get("object") or {}).get("data", {}).get("salesOrder") or []:
            zt = so.get("ZZObjectType") or ""
            sid = str(so.get("SalesOrderID") or "")
            if zt == "DELIV_REQ" or sid.startswith("4000"):
                znd += 1
            elif zt == "SALES_ORD":
                sales += 1
            else:
                other += 1
    parts = []
    if sales:
        parts.append(f"заказ(SALES_ORD)={sales}")
    if znd:
        parts.append(f"ЗНД(DELIV_REQ/4000)={znd}")
    if other:
        parts.append(f"прочее={other}")
    return ", ".join(parts) if parts else "не разобрано"


def _json_file_dt(fp: Path) -> str:
    try:
        return json.loads(fp.read_text(encoding="utf-8")).get("dateTime") or fp.name
    except (json.JSONDecodeError, OSError):
        return fp.name


_ZND_CANCEL_ITEM_STATUSES = frozenset({"E0004", "E0006", "E0002"})


def collect_znd_trace(folder: Path, order_id: str) -> list[dict]:
    """События ЗНД из INT (0973, 0993, CrmOfdChanged) для блока 152 и шапки отчёта."""
    events: list[dict] = []
    oid = str(order_id)

    for fp in sorted(folder.glob("*0973*"), key=_json_file_dt):
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        data = (d.get("object") or {}).get("data") or {}
        if str(data.get("orderId")) != oid and oid not in fp.name:
            continue
        spec = (d.get("object") or {}).get("specificationNumber") or "INT-0973"
        dt = d.get("dateTime") or ""
        mid = (d.get("object") or {}).get("messageId")
        for block in data.get("deliveryOrders") or []:
            zid = block.get("deliveryOrderId")
            if not zid:
                continue
            cancel_bits: list[str] = []
            for it in block.get("items") or []:
                st = str(it.get("status") or "")
                cr = it.get("cancelReason")
                if cr or st in _ZND_CANCEL_ITEM_STATUSES:
                    cancel_bits.append(
                        f"строка ЗНД: status={st or '?'}"
                        + (f", cancelReason={cr}" if cr else "")
                    )
            events.append(
                {
                    "spec": spec,
                    "dateTime": dt,
                    "file": fp.name,
                    "messageId": mid,
                    "znd_id": str(zid),
                    "delivery_status": str(block.get("deliveryStatus") or ""),
                    "cancelled": cancel_bits,
                }
            )

    for fp in sorted(folder.glob("*0993*OmsReceivedFromS4*.json"), key=_json_file_dt):
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for so in (d.get("object") or {}).get("data", {}).get("salesOrder") or []:
            if so.get("ZZObjectType") != "DELIV_REQ":
                continue
            zid = str(so.get("SalesOrderID") or "")
            ref = str(so.get("ZZSalesOrdID") or so.get("ReferenceSDDocument") or "")
            if not zid.startswith("4000"):
                continue
            if oid not in ref and oid not in fp.name:
                continue
            events.append(
                {
                    "spec": "INT-0993",
                    "dateTime": d.get("dateTime") or "",
                    "file": fp.name,
                    "messageId": (d.get("object") or {}).get("messageId"),
                    "znd_id": zid,
                    "delivery_status": str(so.get("ZZStatusUserCode") or ""),
                    "cancelled": [],
                }
            )

    for fp in sorted(folder.glob("*CrmOfdChanged*.json"), key=_json_file_dt):
        if oid not in fp.name:
            continue
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        data = (d.get("object") or {}).get("data") or {}
        raw = data.get("jsonObject") or ""
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        header = payload.get("header") or {}
        zid = str(header.get("s4Id") or "")
        if not zid.startswith("4000"):
            continue
        cancel_bits = []
        for it in payload.get("item") or []:
            rr = it.get("rejectionReason")
            st = str(it.get("s4ItemStatus") or "")
            if rr or st in _ZND_CANCEL_ITEM_STATUSES:
                cancel_bits.append(
                    f"строка: s4ItemStatus={st or '?'}"
                    + (f", rejectionReason={rr}" if rr else "")
                )
        events.append(
            {
                "spec": "CrmOfdChanged",
                "dateTime": d.get("dateTime") or "",
                "file": fp.name,
                "messageId": (d.get("object") or {}).get("messageId"),
                "znd_id": zid,
                "delivery_status": str(header.get("s4Status") or ""),
                "cancelled": cancel_bits,
            }
        )

    events.sort(key=lambda e: e.get("dateTime") or "")
    return events


def format_znd_trace_summary(trace: list[dict]) -> str:
    if not trace:
        return "в JSON (0973/0993/Ofd) не найдена — проверить ЗНД в CRM Web UI"
    ids = sorted({e["znd_id"] for e in trace if e.get("znd_id")})
    return ", ".join(ids[:5]) + (f" (+{len(ids) - 5})" if len(ids) > 5 else "")


def format_znd_trace_lines(trace: list[dict], indent: str = "    ") -> list[str]:
    if not trace:
        return [
            f"{indent}ЗНД в выгрузке JSON (0973/0993/CrmOfdChanged) не найдена.",
            f"{indent}Проверить наличие ЗНД в CRM Web UI; при отсутствии — запросить у коллег L2 S/4 (VA03).",
        ]
    lines: list[str] = [f"{indent}【ЗНД в INT (хронология)】"]
    for ev in trace:
        zid = ev.get("znd_id") or "?"
        dt = ev.get("dateTime") or "?"
        spec = ev.get("spec") or "INT"
        extra = ""
        if ev.get("delivery_status"):
            extra += f", статус={ev['delivery_status']}"
        lines.append(f"{indent}  • {spec} от {dt}, ЗНД {zid}{extra}, файл {ev.get('file', '')}")
        for c in ev.get("cancelled") or []:
            lines.append(f"{indent}    отмена/отказ в том же сообщении: {c}")
    return lines


def find_znd_ids(folder: Path, order_id: str) -> list[str]:
    trace = collect_znd_trace(folder, order_id)
    return sorted({e["znd_id"] for e in trace if e.get("znd_id")})


def extract_143_positions(lat: dict | None) -> list[str]:
    if not lat:
        return []
    out = []
    for it in lat.get("items") or []:
        tid = it.get("typeID") or ""
        if not tid.startswith("143"):
            continue
        m = re.search(r"поз\.\s*(\d+)", it.get("note") or "")
        if m:
            out.append(m.group(1).zfill(6))
    return sorted(set(out))


def _crm_pos_key(pos: str) -> str:
    return (pos or "").strip().lstrip("0") or "0"


def _pos_from_143_note(note: str) -> str:
    m = re.search(r"поз\.\s*0*(\d+)", note or "", re.I)
    return _crm_pos_key(m.group(1)) if m else ""


def format_143_zpri_brief_lines(
    snap: dict | None,
    positions_s4: list[str],
    *,
    root_crm_pos: str = "",
    dt0992: str = "",
) -> list[str]:
    """Строки ZPRI по позициям 143 для краткого отчёта (корневая позиция — первой)."""
    if not snap or not positions_s4:
        return []
    want = {_crm_pos_key(p) for p in positions_s4}
    by_pos: dict[str, dict] = {}
    for it in snap.get("items") or []:
        key = _crm_pos_key(str(it.get("pos") or ""))
        if key in want:
            by_pos[key] = it
    order: list[str] = []
    root_key = _crm_pos_key(root_crm_pos)
    if root_key and root_key in by_pos:
        order.append(root_key)
    for key in sorted(by_pos.keys(), key=lambda x: int(x) if x.isdigit() else 0):
        if key not in order:
            order.append(key)
    lines: list[str] = []
    hdr = f"**ZPRI из INT-0992** ({dt0992 or '?'}):" if dt0992 else "**ZPRI из INT-0992:**"
    lines.append(hdr)
    for key in order:
        it = by_pos[key]
        zpri = it.get("zpri")
        if zpri is None:
            continue
        tag = " (позиция из INT-2010)" if key == root_key else ""
        sptech = it.get("sptech") or "?"
        lines.append(
            f"  поз. **{key}** (S/4 {key.zfill(6)}){tag}: Material={it.get('material')}; "
            f"ZPRI={zpri}; ZZ1_SPTECH={sptech}; статус {it.get('status') or '?'}"
        )
    return lines


def format_152_zpri_brief_line(
    snap: dict | None,
    pos_152: str,
    *,
    dt0992: str = "",
) -> list[str]:
    """Строка позиции 152 в кратком отчёте (технология при ЗНД)."""
    if not snap or not pos_152:
        return []
    key = _crm_pos_key(pos_152)
    for it in snap.get("items") or []:
        if _crm_pos_key(str(it.get("pos") or "")) != key:
            continue
        zpri = it.get("zpri")
        zpri_txt = f"ZPRI={zpri}; " if zpri is not None else ""
        sptech = it.get("sptech") or "?"
        st = it.get("status") or "?"
        st_txt = crm_status_label(st) if st != "?" else "?"
        return [
            f"  поз. **{key}** (S/4 {key.zfill(6)}) — **152**: Material={it.get('material')}; "
            f"{zpri_txt}ZZ1_SPTECH={sptech}; статус {st_txt}"
        ]
    return []


def format_143_152_brief_lines(
    lat: dict | None,
    snap: dict | None,
    folder: Path,
    *,
    dt0992: str = "",
) -> list[str]:
    """ZPRI 143 + позиция 152 и L2 для краткого отчёта (DEMO_ORDER_001)."""
    pos_143_b = extract_143_positions(lat)
    pos_152 = ""
    note_143 = ""
    if lat:
        for it in lat.get("items") or []:
            tid = str(it.get("typeID") or "")
            if tid.startswith("152"):
                pos_152 = _parse_152_position(it.get("note") or "") or pos_152
            elif tid.startswith("143") and not note_143:
                note_143 = it.get("note") or ""
    root_pos = _pos_from_143_note(note_143)
    znd_missing = not bool(list(folder.glob("*0993*")))
    lines = format_143_zpri_brief_lines(
        snap,
        pos_143_b,
        root_crm_pos=root_pos,
        dt0992=dt0992,
    )
    lines.extend(format_152_zpri_brief_line(snap, pos_152, dt0992=dt0992))
    lines.append("")
    pos_txt = ", ".join(_crm_pos_key(p) for p in pos_143_b) or "(из note)"
    lines.extend(
        [
            "L2 CRM:",
            f"  **143+152 в одном INT-2010:** поз. **{pos_txt}** (ZPRI после ЗНД) и "
            f"**{_crm_pos_key(pos_152) or '910'}** (152 — технология при ЗНД).",
            "  **143:** отменить ЗНД в CRM Web UI, затем ZPRI (DEMO_PAGE_ID).",
            f"  **152:** поз. **{_crm_pos_key(pos_152) or '910'}** — не менять технологию при активной ЗНД; "
            "при **E0020** в 0992 — статус «Рекламация» в CRM Web UI.",
        ]
    )
    if znd_missing:
        lines.append(
            "  **0973/0993** в выгрузке нет — номер ЗНД только из CRM Web UI или S/4 (VA03)."
        )
    return lines


def format_143_l2_brief_lines(
    positions_s4: list[str],
    *,
    root_crm_pos: str = "",
    znd_missing: bool = True,
) -> list[str]:
    pos_keys = [_crm_pos_key(p) for p in positions_s4]
    pos_txt = ", ".join(pos_keys) if pos_keys else "(из note)"
    root_txt = root_crm_pos or (pos_keys[0] if pos_keys else "")
    lines = [
        "L2 CRM:",
        f"  **Причина 143:** на поз. {pos_txt} S/4 отклоняет изменение ZPRI — создана ЗНД.",
    ]
    if root_txt:
        lines.append(
            f"  **Сначала поз. {root_txt}** (из note INT-2010) — см. ZPRI выше."
        )
    lines.extend(
        [
            "  1. CRM Web UI: есть ли ЗНД (E0010)? "
            "Если **да** — отменить ЗНД, затем изменить ZPRI (DEMO_PAGE_ID).",
            "  2. Если ЗНД в CRM **нет** — переадресовать L2 S/4 (VA03).",
        ]
    )
    if znd_missing:
        lines.append(
            "  3. «Было→стало» по цене без INT-0993 не указывать; текущий ZPRI — только из INT-0992."
        )
    return lines


def branch_0993(
    csv_cls: str,
    sppr_cls: str,
    folder: Path,
    lat: dict | None,
    znd_ids: list[str],
) -> str:
    n93 = len(list(folder.glob("*0993*")))
    kinds = _classify_0993_files(folder) if n93 else "нет файлов"
    rc = lat.get("resultCode") if lat else None
    root = ""
    if lat and lat.get("items"):
        root = lat["items"][0].get("typeID", "")
    lines = [
        f"  Класс: {sppr_cls}; файлов 0993: {n93}; {kinds}.",
        "  OMS после успешного 2010 (resultCode=4) ждёт INT-0993 до ~1 ч (Confluence DEMO_PAGE_ID).",
    ]
    if n93 == 0:
        lines.append(
            "  L2: эскалация OMS/S/4 — 0993 не дошёл в окно; в CRM проверить синхронизацию заказа/ЗНД."
        )
    else:
        lines.append(
            "  L2: 0993 в архиве есть — вариант V2/V3/V6 (СППР_ПРАВИЛА_LLM §5): сверить dateTime 2010→0993;"
            " SALES_ORD без DELIV_REQ при 152/0973 → V6 (не «0993 не приходил вообще»)."
        )
        if znd_ids:
            lines.append(f"  Номера ЗНД (DELIV_REQ): {', '.join(znd_ids)}.")
        elif "SALES_ORD" in kinds and "DELIV_REQ" not in kinds:
            lines.append(
                "  Подсказка V6: в 0993 только заказ (SALES_ORD); при 152 проверить репликацию ЗНД (4000…) в 0993."
            )
    if root.startswith("005"):
        lines.append(
            "  В 2010 есть 005 — часто следствие просроченного 0993; Confluence DEMO_PAGE_ID: репликация 0993 без SalesOrder-ZZ1_SOURCE_OMS."
        )
    elif root.startswith("152"):
        lines.append(
            "  В 2010 есть 152 (ЗНД) — при ЗНД смена данных позиции в S/4 недопустима (§7.8); "
            "проверить ЗНД в CRM UI и в INT 0973/0993."
        )
    return "\n".join(lines)


def extract_latest_0992_snapshot(folder: Path) -> dict | None:
    """Снимок полей 0992 как в JSON: ZZ1_LOT_NUMBER, Material, MaterialByCustomer (без склейки)."""
    files = [p for p in folder.glob("*0992*OmsReceivedFromCRM*.json") if "SYNTH" not in p.name]
    if not files:
        return None

    def _dt(p: Path) -> str:
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("dateTime") or p.name
        except (json.JSONDecodeError, OSError):
            return p.name

    files.sort(key=_dt)
    fp = files[-1]
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    data = (d.get("object") or {}).get("data") or {}
    order = data.get("Order") or data.get("order") or {}
    if isinstance(order, list):
        order = order[0] if order else {}
    lot = order.get("ZZ1_LOT_NUMBER") or ""
    hdr_status = order.get("ZZStatusUser") or ""
    ext_chg = order.get("ExternalDocLastChangeDateTime") or ""
    items_out = []
    for it in order.get("Item") or order.get("item") or []:
        if not isinstance(it, dict):
            continue
        rq = it.get("RequestedQuantity")
        qty = rq.get("value") if isinstance(rq, dict) else None
        unit = rq.get("SAPunitCode") if isinstance(rq, dict) else ""
        zpri = None
        for pe in it.get("PricingElement") or []:
            if isinstance(pe, dict) and pe.get("ConditionType") == "ZPRI":
                zpri = pe.get("ZZConditionRateValue")
                if zpri is None:
                    crv = pe.get("ConditionRateValue")
                    zpri = crv.get("value") if isinstance(crv, dict) else crv
        items_out.append(
            {
                "pos": it.get("SalesOrderItemID"),
                "material": it.get("Material"),
                "plant": it.get("Plant"),
                "sptech": it.get("ZZ1_SPTECH"),
                "category": it.get("SalesOrderItemCategory"),
                "mbc": it.get("MaterialByCustomer"),
                "status": it.get("ZZStatusUserItem"),
                "action": it.get("actionCode"),
                "qty": qty,
                "qty_unit": unit,
                "zpri": zpri,
            }
        )
    partners_out = []
    for pr in order.get("Partner") or []:
        if not isinstance(pr, dict):
            continue
        partners_out.append(
            {
                "role": pr.get("PartnerFunction"),
                "bp": pr.get("BusinessPartnerID"),
                "action": pr.get("actionCode"),
            }
        )
    obj = d.get("object") or {}
    return {
        "file": fp.name,
        "dateTime": d.get("dateTime") or "",
        "messageId": obj.get("messageId") or obj.get("sourceMessageId"),
        "partners": partners_out,
        "lot": lot,
        "order_status": hdr_status,
        "external_doc_last_change": ext_chg,
        "items": items_out,
    }


def _pick_025_items(snap: dict | None, limit: int = 5) -> list[dict]:
    if not snap:
        return []
    items = snap.get("items") or []
    changed = [x for x in items if str(x.get("action")) in ("04", "02", "01")]
    return (changed or items)[:limit]


def _025_notes_from_lat(lat: dict | None) -> list[str]:
    notes: list[str] = []
    if not lat:
        return notes
    for it in lat.get("items") or []:
        if not str(it.get("typeID") or "").startswith("025"):
            continue
        n = (it.get("note") or "").strip()
        if n and n not in notes:
            notes.append(n)
    return notes


def _znd_cancel_after_0992(znd_trace: list[dict], snap_dt: str) -> list[str]:
    """ЗНД с признаками отмены в INT позже последнего 0992 (статусы в CRM могли откатиться)."""
    if not snap_dt or not znd_trace:
        return []
    out: list[str] = []
    for z in znd_trace:
        zdt = z.get("dateTime") or ""
        if zdt <= snap_dt:
            continue
        if z.get("cancel_reason") or z.get("rejection_reason"):
            zid = z.get("znd_id") or "?"
            out.append(f"{zid} (INT {z.get('spec', '?')} {zdt})")
    return out


def branch_025(
    lat: dict | None,
    snap: dict | None,
    znd_trace: list[dict] | None = None,
) -> str:
    notes_025 = _025_notes_from_lat(lat)
    has_order_qty = any("ORDER_QTY" in n for n in notes_025)
    has_category = any("CATEGORY_CODE" in n for n in notes_025)
    has_art = any(
        "CUSTOMER_MATERIAL" in n or "артикул" in n.lower() for n in notes_025
    )
    branch_title = "025"
    if has_order_qty and not has_category and not has_art:
        branch_title = "025 / ORDER_QTY (количество)"
    elif has_category and not has_order_qty:
        branch_title = "025 / CATEGORY_CODE"

    lines = [
        f"  Класс A: ошибка в S/4 (INT-2010, typeID {branch_title}).",
        "  Чёткий откат из JSON не выводим — факты INT + проверки L2 CRM (CRM Web UI); L2 S/4 — при эскалации.",
        "",
        "  【Факт из 2010】",
    ]
    has_099 = False
    if lat:
        dt2010 = lat.get("dateTime") or ""
        for it in lat.get("items") or []:
            if str(it.get("typeID") or "").startswith("025"):
                lines.append(f"    [{dt2010}] typeID: {it.get('typeID')}")
                lines.append(f"    note: {it.get('note', '')[:200]}")
            if str(it.get("typeID") or "").startswith("099"):
                has_099 = True
                lines.append(f"    (контекст) {it.get('typeID')}: {it.get('note', '')[:120]}")

    lot = (snap or {}).get("lot") or "(не указан в 0992)"
    snap_dt = (snap or {}).get("dateTime") or ""
    hdr_st = (snap or {}).get("order_status") or ""
    lines.append("")
    lines.append("  【Контекст: JSON 0992 (снимок на dateTime файла; в CRM могло измениться позже)】")
    if snap:
        lines.append(
            f"    {int_moment_label('INT-0992', snap_dt, snap['file'], snap.get('messageId'))}"
        )
        if hdr_st:
            lines.append(
                f"    ZZStatusUser (заголовок): {hdr_st} — в CRM: {crm_status_label(hdr_st, header=True)}"
            )
        lines.append(f"    ZZ1_LOT_NUMBER (заголовок): {lot}")
        lines.append("    По позициям:")
        for it in _pick_025_items(snap):
            mat = it.get("material") or "?"
            mbc = it.get("mbc") or "?"
            st = it.get("status") or "?"
            qty = it.get("qty")
            qty_s = f"{qty} {it.get('qty_unit') or ''}".strip() if qty is not None else "?"
            block = " — статус часто блокирует изменение qty" if str(st) in STATUS_LIKELY_BLOCKS_QTY else ""
            lines.append(
                f"      поз. {it.get('pos')}: Material={mat}; RequestedQuantity={qty_s}; "
                f"ZZStatusUserItem={st} ({crm_status_label(st)}){block}; "
                f"MaterialByCustomer={mbc}; actionCode={it.get('action')}"
            )
        if not has_order_qty:
            sample = (_pick_025_items(snap) or [{}])[0]
            mat = sample.get("material") or "?"
            mbc = sample.get("mbc") or "?"
            lines.append(
                f"    (артикул) Material={mat}, ZZ1_LOT_NUMBER={lot}, MaterialByCustomer={mbc} — "
                f"три поля отдельно, без склейки."
            )
    else:
        lines.append("    (нет 0992 в папке)")

    znd_after = _znd_cancel_after_0992(znd_trace or [], snap_dt)
    if znd_after:
        lines.append(
            f"    После снимка 0992 в INT: отмена/отказ по ЗНД {', '.join(znd_after[:3])} — "
            "статусы в CRM могли откатиться, ориентир CRM Web UI."
        )

    sample = (_pick_025_items(snap) or [{}])[0]
    mat = sample.get("material") or "…"
    mbc = sample.get("mbc") or "…"
    pos = sample.get("pos") or "…"
    st = str(sample.get("status") or "")

    lines.append("")
    lines.append("  【Гипотезы】")
    if has_order_qty:
        qty = sample.get("qty")
        qty_s = f"{qty} {sample.get('qty_unit') or ''}".strip() if qty is not None else "…"
        if st in STATUS_LIKELY_BLOCKS_QTY and snap_dt:
            lines.append(
                f"  H_status (факт снимка JSON на {snap_dt}): поз. {pos} — {crm_status_label(st)}; "
                f"в 0992 ушло RequestedQuantity={qty_s} — на такой стадии S/4 мог отклонить "
                "изменение количества (ORDER_QTY). Актуальность — CRM Web UI."
            )
        else:
            lines.append(
                f"  H_status (гипотеза): вероятно, статус заказа/поз. {pos} в CRM на момент "
                "действия оператора не допускал изменение количества — проверить актуальный "
                "статус в CRM Web UI и последнее действие (рекламация, отказ, вычерк, ЗНД)."
            )
            if snap_dt and st:
                lines.append(
                    f"    (в снимке 0992 от {snap_dt}: ZZStatusUserItem={st} — "
                    f"{crm_status_label(st)}; не единственный критерий без UI.)"
                )
        lines.append(
            f"  H_qty: в 0992 RequestedQuantity={qty_s} по поз. {pos} (Material={mat}) — "
            "сверить с тем, что оператор видит/меняет в CRM."
        )
        if has_099:
            lines.append(
                "  H_docs: в 2010 есть 099 «последующие документы» — при эскалации L2 S/4."
            )
        lines.append(
            "  【L2 CRM】 CRM Web UI: актуальный статус заказа и позиции; количество; ЗНД; "
            "сверка с 0992 (таблица выше). Не VA03."
        )
        lines.append(
            "  【L2 S/4】 при повторе после проверки CRM: VA03 — причина отклонения на позиции "
            "(Confluence DEMO_PAGE_ID, ORDER_QTY); кто проставил PI_USER/пользователь."
        )
    else:
        lines.extend(
            [
                f"  H1: возможно, S/4 не принимает MaterialByCustomer={mbc} (JSON) по поз. {pos}, "
                f"Material={mat} — последующие документы"
                + (" (в 2010 есть 099)." if has_099 else "."),
                f"  H2: возможно, MaterialByCustomer={mbc} vs KDMAT в S/4 (лот {lot} — заголовок).",
                f"  H3: возможно, меняли ZZ1_LOT_NUMBER={lot}; MaterialByCustomer={mbc}.",
                "  【L2 CRM】 артикул, лот, статус позиции в UI; поля 0992 выше.",
                "  【L2 S/4】 при эскалации: VA03, KDMAT; DEMO_PAGE_ID, DEMO_PAGE_ID.",
            ]
        )
    return "\n".join(lines)


def _count_005_in_2010(folder: Path) -> int:
    n = 0
    for p in folder.glob("*2010*OmsReceivedFromS4*.json"):
        if "SYNTH" in p.name:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        data = (d.get("object") or {}).get("data") or {}
        for st in data.get("orderStatus") or []:
            log = (st.get("log") or [{}])[0]
            for i in log.get("item") or []:
                if str(i.get("typeID") or "").startswith("005"):
                    n += 1
                    break
    return n


def branch_005(folder: Path, lat: dict | None, snap: dict | None) -> str:
    n93 = len(list(folder.glob("*0993*")))
    n005_hist = _count_005_in_2010(folder)
    lines = [
        "  Класс A: ошибка в S/4 (INT-2010, typeID 005 / рассинхрон версии).",
        "  Нет одного действия «снять INT» — цепочка проверок + 0993 (S/4/OMS) + при необходимости повтор в CRM.",
        "",
        "  【Факт из 2010】",
    ]
    if lat:
        dt = lat.get("dateTime") or ""
        for it in lat.get("items") or []:
            if str(it.get("typeID") or "").startswith("005"):
                lines.append(f"    [{dt}] typeID: {it.get('typeID')}")
                lines.append(f"    note: {(it.get('note') or '')[:200]}")
    lines.extend(
        [
            "",
            "  【Контекст: JSON 0992 (снимок на dateTime; CRM мог измениться позже)】",
        ]
    )
    if snap:
        snap_dt = snap.get("dateTime") or ""
        lines.append(
            f"    {int_moment_label('INT-0992', snap_dt, snap['file'], snap.get('messageId'))}"
        )
        ext = snap.get("external_doc_last_change") or "(не указан в 0992)"
        lines.append(f"    ExternalDocLastChangeDateTime: {ext}")
        if snap.get("order_status"):
            lines.append(
                f"    ZZStatusUser: {snap['order_status']} — {crm_status_label(snap['order_status'], header=True)}"
            )
        lines.append("    Позиции с actionCode=04 (изменение):")
        changed = [x for x in (snap.get("items") or []) if str(x.get("action")) == "04"]
        for it in (changed or snap.get("items") or [])[:5]:
            st = it.get("status") or "?"
            lines.append(
                f"      поз. {it.get('pos')}: Material={it.get('material')}; "
                f"ZZStatusUserItem={st} ({crm_status_label(st)}); actionCode={it.get('action')}"
            )
        if not changed:
            lines.append("      (в снимке нет позиций с actionCode=04 — уточнить в CRM UI, что меняли)")
    else:
        lines.append("    (нет 0992 в папке)")
    lines.append(f"    Файлов *0993* в выгрузке: {n93} ({'нет' if n93 == 0 else 'есть'})")
    if n005_hist > 1:
        lines.append(f"    Эпизодов 005 в истории 2010 в папке: {n005_hist} (затяжной рассинхрон)")

    lines.extend(
        [
            "",
            "  【Причины — Confluence】",
            "  H1 (DEMO_PAGE_ID): не вовремя дошёл 0993 в CRM (ориентир 1 ч); новый 0992 — расхождение версий.",
            "  H2 (1010195422): в 0992 устаревшее ExternalDocLastChangeDateTime при новых правках в CRM.",
            "",
            "  【Устранение — не одно действие】",
            "  1) 【L2 CRM】 проверки: UI (синхронизация), что оператор менял; сверка с 0992; мониторинг INT по dateTime.",
            "  2) 【L2 S/4/OMS】 восстановление: цепочка 0992→2010→0993; репликация INT-0993 без SalesOrder-ZZ1_SOURCE_OMS.",
            "  3) После 0993 в CRM: если в UI уже нужное состояние — повтор не нужен; иначе оператор повторяет",
            "     то же бизнес-действие в CRM Web UI (не «пересохранить без изменений», не снять INT).",
            "",
            "  Confluence: pageId=DEMO_PAGE_ID, 1010195422; процесс 0992+2010+0993 — DEMO_PAGE_ID.",
        ]
    )
    return "\n".join(lines)


def branch_150(lat: dict | None, snap: dict | None) -> str:
    positions: list[str] = []
    if lat:
        for it in lat.get("items") or []:
            if str(it.get("typeID") or "").startswith("150"):
                note = it.get("note") or ""
                m = re.search(r"позиции\s+(\d+)", note, re.I)
                if m:
                    positions.append(m.group(1).lstrip("0") or m.group(1))
    pos_filter = positions[:3]
    lines = [
        "  Класс A: ошибка в S/4 (INT-2010, typeID 150 — смена статуса позиции).",
        "",
        "  【Факт из 2010】",
    ]
    if lat:
        dt2010 = lat.get("dateTime") or ""
        for it in lat.get("items") or []:
            if str(it.get("typeID") or "").startswith("150"):
                lines.append(f"    [{dt2010}] typeID: {it.get('typeID')}")
                lines.append(f"    note: {it.get('note', '')[:200]}")
    lines.append("")
    lines.append("  【Контекст: поля JSON 0992 (как в файле)】")
    if snap:
        lines.append(
            f"    {int_moment_label('INT-0992', snap.get('dateTime'), snap['file'], snap.get('messageId'))}"
        )
        for it in snap.get("items") or []:
            pos = str(it.get("pos") or "").lstrip("0")
            if pos_filter and pos not in pos_filter:
                continue
            st = it.get("status")
            lines.append(
                f"    поз. {it.get('pos')}: Material={it.get('material')}; "
                f"ZZStatusUserItem={st} — в CRM: {crm_status_label(st)}; "
                f"actionCode={it.get('action')}"
            )
    else:
        lines.append("    (нет 0992 в папке)")
    lines.extend(
        [
            "",
            "  Переход в note 2010: E0022 — в CRM «Передана в ТЭК»; целевой E0020 — в CRM «Рекламация».",
            "  (коды в JSON; расшифровка CRM — §8.2 СППР_ПРАВИЛА_LLM, источник DEMO_PAGE_ID)",
        ]
    )
    return "\n".join(lines)


def _lat_has_type(lat: dict | None, prefix: str) -> bool:
    return bool(
        lat
        and any(str(i.get("typeID") or "").startswith(prefix) for i in lat.get("items") or [])
    )


def _is_034_391_combo(lat: dict | None) -> bool:
    return _lat_has_type(lat, "034") and _lat_has_type(lat, "391")


def _parse_391_note(note: str) -> tuple[str, str]:
    m = re.search(
        r"Материал\s+(\d+)\s+на\s+заводе\s+(\S+)\s+в\s+стране",
        note or "",
        re.I,
    )
    if m:
        return m.group(1), m.group(2)
    return "", ""


def branch_391(lat: dict | None, snap: dict | None) -> str:
    mat_note, plant_note = "", ""
    if lat:
        for it in lat.get("items") or []:
            if str(it.get("typeID") or "").startswith("391"):
                mat_note, plant_note = _parse_391_note(it.get("note") or "")
                if mat_note:
                    break
    lines = [
        "  Класс A: ошибка в S/4 (INT-2010, typeID 391 — материал на заводе в ERP).",
        "  017(SOA_SD) в CSV — вторично (остановка после 391).",
        "",
        "  【Факт из 2010】",
    ]
    if lat:
        dt2010 = lat.get("dateTime") or ""
        for it in lat.get("items") or []:
            if str(it.get("typeID") or "").startswith("391"):
                lines.append(f"    [{dt2010}] typeID: {it.get('typeID')}")
                lines.append(f"    note: {it.get('note', '')[:200]}")
                if not mat_note:
                    mat_note, plant_note = _parse_391_note(it.get("note") or "")
    lines.append("")
    lines.append("  【Контекст: поля JSON 0992 (как в файле)】")
    if snap:
        lines.append(
            f"    {int_moment_label('INT-0992', snap.get('dateTime'), snap['file'], snap.get('messageId'))}"
        )
        matched = []
        other_ssk5 = []
        for it in snap.get("items") or []:
            if mat_note and str(it.get("material") or "") == mat_note:
                matched.append(it)
            elif plant_note and str(it.get("plant") or "") == plant_note:
                other_ssk5.append(it)
        if matched:
            for it in matched:
                pl = it.get("plant") or "(пусто в JSON)"
                lines.append(
                    f"    поз. {it.get('pos')}: Material={it.get('material')}; "
                    f"Plant={pl}; SalesOrderItemCategory={it.get('category')}; "
                    f"actionCode={it.get('action')}"
                )
            if plant_note and not any(it.get("plant") for it in matched):
                lines.append(
                    f"    Завод {plant_note} из note 2010 на этой позиции в 0992 не передан "
                    f"(типовой паттерн ZUSL — §7.6)."
                )
        elif mat_note:
            lines.append(f"    Material={mat_note} из note в последнем 0992 не найден — сверить вручную.")
        if other_ssk5 and matched and not any(m.get("plant") for m in matched):
            lines.append(
                f"    В том же 0992 Plant={plant_note} на других поз.: "
                + ", ".join(
                    f"поз.{x.get('pos')} mat.{x.get('material')}"
                    for x in other_ssk5[:4]
                )
            )
    else:
        lines.append("    (нет 0992 в папке)")
    lines.extend(
        [
            "",
            "  【L2 CRM】 CRM Web UI: материал из note на позиции; сверка с 0992; не VA03.",
            f"  【L2 S/4】 MM/VA03: материал {mat_note or '…'} на заводе {plant_note or '…'} (RU) — мастер-данные.",
            "  См. СППР_ПРАВИЛА_LLM §7.6, Confluence DEMO_PAGE_ID.",
        ]
    )
    return "\n".join(lines)


def _parse_152_position(note: str) -> str:
    m = re.search(r"[Пп]озици[ия]+\s+(\d+)", note or "")
    return m.group(1) if m else ""


def _parse_201_bp(note: str) -> str:
    m = re.search(r"[Дд]еловой\s+партнер\s+(\d+)", note or "")
    return m.group(1) if m else ""


def branch_003(lat: dict | None, snap: dict | None, origin: str = "") -> str:
    note_003 = ""
    if lat:
        for it in lat.get("items") or []:
            if str(it.get("typeID") or "").startswith("003"):
                note_003 = it.get("note") or note_003
                break
    dt2010 = (lat or {}).get("dateTime") or "?"
    e0020_lines: list[str] = []
    if snap:
        for it in snap.get("items") or []:
            if str(it.get("status") or "") == "E0020":
                st = it.get("status")
                e0020_lines.append(
                    f"    поз. {it.get('pos')}: {it.get('category') or '?'} / "
                    f"ZZ1_SPTECH={it.get('sptech') or '(пусто)'}; "
                    f"ZZStatusUserItem={st} — в CRM: {crm_status_label(st)}"
                )
    lines = [
        "  Класс A: ошибка в S/4 (INT-2010, typeID 003 — снять резерв невозможно).",
        "",
        "  【Факт из 2010】",
    ]
    if lat:
        for it in lat.get("items") or []:
            if str(it.get("typeID") or "").startswith("003"):
                lines.append(f"    [{dt2010}] typeID: {it.get('typeID')}")
                lines.append(f"    note: {note_003[:220]}")
    lines.extend(
        [
            "",
            "  【Смысл для тикета】 S/4 отклонила снятие резерва: для вида заказа, "
            "технологии отгрузки и типа позиции это не предусмотрено настройками (003).",
            "  Не путать с 004 (неотмененная ЗНД) — в note 003 текста про ЗНД нет.",
        ]
    )
    if origin:
        lines.append(f"  Источник заказа: {origin}.")
    lines.extend(["", "  【Контекст 0992 — позиции E0020 (рекламация)】"])
    if snap:
        lines.append(
            f"    {int_moment_label('INT-0992', snap.get('dateTime'), snap['file'], snap.get('messageId'))}"
        )
        if e0020_lines:
            lines.extend(e0020_lines)
        else:
            lines.append("    (позиций с E0020 в снимке нет — сверить UI и полный 0992)")
        hs = snap.get("order_status")
        if hs:
            lines.append(
                f"    ZZStatusUser (заголовок): {hs} — в CRM: {crm_status_label(hs)}"
            )
    else:
        lines.append("    (нет 0992 в папке)")
    lines.extend(
        [
            "",
            "  【L2 CRM】 UI: последнее действие (рекламация/резерв); сверка позиций с 0992.",
            "  【L2 S/4】 VA03: вид заказа, тех. отгрузки, тип позиции; первичный анализ (DEMO_PAGE_ID Ошибка 6).",
            "  【L3 S/4】 исправление настроек — по Confluence DEMO_PAGE_ID.",
            "  См. §7.12 СППР_ПРАВИЛА_LLM.",
        ]
    )
    return "\n".join(lines)


def branch_201(lat: dict | None, snap: dict | None, origin: str = "") -> str:
    note_201 = ""
    if lat:
        for it in lat.get("items") or []:
            if str(it.get("typeID") or "").startswith("201"):
                note_201 = it.get("note") or note_201
                break
    bp = _parse_201_bp(note_201)
    dt2010 = (lat or {}).get("dateTime") or "?"
    lines = [
        "  Класс A: ошибка в S/4 (INT-2010, typeID 201(R1) — деловой партнёр отсутствует в ERP).",
        "",
        "  【Факт из 2010】",
    ]
    if lat:
        for it in lat.get("items") or []:
            if str(it.get("typeID") or "").startswith("201"):
                lines.append(f"    [{dt2010}] typeID: {it.get('typeID')}")
                lines.append(f"    note: {it.get('note', '')[:220]}")
    lines.extend(
        [
            "",
            f"  【Смысл для ответа L2】 В S/4 нет бизнес-партнёра {bp or '(из note)'}; "
            "CRM передала его в 0992 — заказ не принят.",
        ]
    )
    if origin:
        lines.append(f"  Источник заказа: {origin} (для ИМК 0993/0973 в JSON часто нет).")
    lines.extend(["", "  【Контекст 0992 — Partner[]】"])
    if snap:
        lines.append(
            f"    {int_moment_label('INT-0992', snap.get('dateTime'), snap['file'], snap.get('messageId'))}"
        )
        for pr in snap.get("partners") or []:
            lines.append(
                f"    роль {pr.get('role')}: BusinessPartnerID={pr.get('bp')}; "
                f"actionCode={pr.get('action')}"
            )
        if bp:
            bp_pad = bp.zfill(10)
            roles = [
                pr.get("role")
                for pr in (snap.get("partners") or [])
                if bp in str(pr.get("bp") or "") or bp_pad in str(pr.get("bp") or "")
            ]
            if roles:
                lines.append(f"    ДП {bp} в 0992 на ролях: {', '.join(roles)}")
    else:
        lines.append("    (нет 0992 в папке)")
    pl = ""
    if snap and snap.get("partners"):
        pl = "; ".join(
            f"{p.get('role')}={p.get('bp')}" for p in snap["partners"][:8]
        )
    lines.append("")
    lines.append("  【L2 CRM】")
    try:
        from sppr_analyze import lines_201_l2_crm_text

        for ln in lines_201_l2_crm_text(bp, pl, numbered=False):
            lines.append(ln.rstrip())
    except ImportError:
        lines.append(
            f"  Найти делового партнёра {bp or '(из note)'} в CRM Web UI (карточка BP, КЛ); "
            "сверить роли с Partner[] в 0992."
        )
        lines.append(
            f"  Если ДП есть в CRM — проверить наличие {bp or '(из note)'} в S/4; "
            "при отсутствии в S/4 — загрузка BP в ERP, затем повтор 0992→2010."
        )
    return "\n".join(lines)


def _parse_151_note(note: str) -> tuple[str, str, str]:
    """Позиция, FROM, TO из note 151 (ZTRN не подлежит изменению на ZIPT)."""
    pos = _parse_152_position(note)
    m = re.search(
        r"(ZTRN|ZIPT|ZTAN|ZIP|ZMIP|ZPP|ZAB)\s+не\s+подлежит\s+изменению\s+на\s+"
        r"(ZTRN|ZIPT|ZTAN|ZIP|ZMIP|ZPP|ZAB)",
        note or "",
        re.I,
    )
    if m:
        return pos, m.group(1).upper(), m.group(2).upper()
    return pos, "", ""


def branch_151(lat: dict | None, snap: dict | None) -> str:
    note_151 = ""
    note_158 = ""
    if lat:
        for it in lat.get("items") or []:
            tid = str(it.get("typeID") or "")
            if tid.startswith("151"):
                note_151 = it.get("note") or note_151
            if tid.startswith("158") and "ZSD_AIF" in tid:
                note_158 = it.get("note") or note_158
    pos, tech_from, tech_to = _parse_151_note(note_151)
    dt2010 = (lat or {}).get("dateTime") or "?"
    cf = (
        "https://example.local/confluence?pageId=DEMO_PAGE_ID"
    )
    lines = [
        "  Класс A: ошибка в S/4 (INT-2010, typeID 151 — смена технологии отгрузки).",
        "",
        "  【Факт из 2010】",
    ]
    if lat:
        for it in lat.get("items") or []:
            tid = str(it.get("typeID") or "")
            if tid.startswith("151") or (
                tid.startswith("158") and "ZSD_AIF" in tid
            ):
                lines.append(f"    [{dt2010}] typeID: {it.get('typeID')}")
                lines.append(f"    note: {(it.get('note') or '')[:220]}")
    lines.extend(
        [
            "",
            f"  【Смысл для тикета】 S/4 отклонила смену технологии на поз. "
            f"{pos or '(из note)'}: {tech_from or '?'} → {tech_to or '?'}.",
        ]
    )
    if note_158:
        lines.append(f"  Дополнительно 158 (ZSD): {note_158[:180]}")
    lines.extend(
        [
            "",
            "  【Контекст 0992 — ZZ1_SPTECH】",
        ]
    )
    if snap and pos:
        lines.append(
            f"    {int_moment_label('INT-0992', snap.get('dateTime'), snap['file'], snap.get('messageId'))}"
        )
        for it in snap.get("items") or []:
            p = str(it.get("pos") or "").lstrip("0")
            if p == pos.lstrip("0"):
                st = it.get("status")
                lines.append(
                    f"    поз. {it.get('pos')}: ZZ1_SPTECH={it.get('sptech') or '?'}; "
                    f"ZZStatusUserItem={st} — в CRM: {crm_status_label(st)}; "
                    f"actionCode={it.get('action')}"
                )
    elif snap:
        lines.append(
            f"    {int_moment_label('INT-0992', snap.get('dateTime'), snap['file'], snap.get('messageId'))}"
        )
    else:
        lines.append("    (нет 0992 в папке)")
    lines.extend(
        [
            "",
            "  【L2 CRM】 UI: технология и статус позиции; сверка с 0992; "
            "при недопустимой смене — вернуть исходную технологию или отмена+новая позиция "
            f"(DEMO_PAGE_ID п. 10.59/10.60: {cf}).",
            "  【L2 S/4】 VA03: фактическая технология и статус «Открыт» на позиции.",
            "  См. §7.10 СППР_ПРАВИЛА_LLM.",
            "  Confluence: DEMO_PAGE_ID (INT-2010), DEMO_PAGE_ID (справочник технологий).",
        ]
    )
    return "\n".join(lines)


def branch_152(
    lat: dict | None, snap: dict | None, znd_trace: list[dict]
) -> str:
    pos_note = ""
    if lat:
        for it in lat.get("items") or []:
            if str(it.get("typeID") or "").startswith("152"):
                pos_note = _parse_152_position(it.get("note") or "")
                break
    dt2010 = (lat or {}).get("dateTime") or "?"
    lines = [
        "  Класс A: ошибка в S/4 (INT-2010, typeID 152 — технология отгрузки при ЗНД).",
        "",
        "  【Факт из 2010】",
    ]
    if lat:
        for it in lat.get("items") or []:
            if str(it.get("typeID") or "").startswith("152"):
                lines.append(f"    [{dt2010}] typeID: {it.get('typeID')}")
                lines.append(f"    note: {it.get('note', '')[:200]}")
    lines.extend(
        [
            "",
            f"  【Смысл для тикета】 S/4 отклонил изменение по поз. {pos_note or '(из note)'} "
            f"(в т.ч. технологии отгрузки): при существующей ЗНД менять данные позиции нельзя (152).",
            "",
        ]
    )
    lines.extend(format_znd_trace_lines(znd_trace, indent="    "))
    lines.extend(
        [
            "",
            "  【Контекст 0992 (что ушло в S/4)】",
        ]
    )
    if snap and pos_note:
        lines.append(
            f"    {int_moment_label('INT-0992', snap.get('dateTime'), snap['file'], snap.get('messageId'))}"
        )
        for it in snap.get("items") or []:
            p = str(it.get("pos") or "").lstrip("0")
            if p == pos_note.lstrip("0"):
                st = it.get("status")
                lines.append(
                    f"    поз. {it.get('pos')}: ZZ1_SPTECH={it.get('sptech') or '?'}; "
                    f"ZZStatusUserItem={st} — в CRM: {crm_status_label(st)}; "
                    f"actionCode={it.get('action')}"
                )
    elif snap:
        lines.append(f"    {int_moment_label('INT-0992', snap.get('dateTime'), snap['file'], snap.get('messageId'))}")
    else:
        lines.append("    (нет 0992 в папке)")
    lines.extend(
        [
            "",
            "  【L2 CRM】 ошибка 152 + проверка ЗНД в CRM Web UI (см. блок ЗНД выше); "
            "если ЗНД в JSON нет — запросить у L2 S/4.",
            "  【L2 S/4】 VA03: подтвердить ЗНД и блокировку 152; ответ с номером ЗНД.",
            "  См. §7.8 СППР_ПРАВИЛА_LLM.",
        ]
    )
    return "\n".join(lines)


def branch_034_391(
    lat: dict | None,
    snap: dict | None,
    order_id: str = "",
    order_guid: str = "",
) -> str:
    mat_note, plant_note = "", ""
    note_034 = ""
    if lat:
        for it in lat.get("items") or []:
            tid = str(it.get("typeID") or "")
            if tid.startswith("034"):
                note_034 = it.get("note") or note_034
            if tid.startswith("391"):
                mat_note, plant_note = _parse_391_note(it.get("note") or "")
                if mat_note:
                    break
    dt2010 = (lat or {}).get("dateTime") or ""
    dt0992 = (snap or {}).get("dateTime") or ""
    matched: list[dict] = []
    if snap and mat_note:
        matched = [
            it
            for it in snap.get("items") or []
            if str(it.get("material") or "") == mat_note
        ]
    pos_list = ", ".join(str(x.get("pos")) for x in matched[:6]) or "…"
    dup_warn = ""
    if len(matched) > 1:
        dup_warn = (
            f"  ⚠ Дубль материала {mat_note} в 0992: {len(matched)} поз. "
            f"({pos_list}) — проверить состав для 034 (§7.7)."
        )

    lines = [
        "  Класс A: составной кейс S/4 — INT-2010: 034 (changeset) + 391 (MD материал/завод).",
        "  017(SOA_SD) — вторично. В тикете указывать ОБА кода 034 и 391.",
        "",
        "  【Факты из 2010】",
    ]
    if lat:
        for it in lat.get("items") or []:
            tid = str(it.get("typeID") or "")
            if tid.startswith("034") or tid.startswith("391") or tid.startswith("158"):
                lines.append(f"    [{dt2010}] typeID: {it.get('typeID')}")
                lines.append(f"    note: {(it.get('note') or '')[:200]}")
    lines.append("")
    lines.append("  【Контекст 0992 — Material по note 391】")
    if snap:
        lines.append(
            f"    {int_moment_label('INT-0992', dt0992, snap['file'], snap.get('messageId'))}"
        )
        for it in matched:
            st = it.get("status")
            lines.append(
                f"    поз. {it.get('pos')}: Material={it.get('material')}; "
                f"Plant={it.get('plant') or '(пусто)'}; "
                f"SalesOrderItemCategory={it.get('category')}; "
                f"ZZStatusUserItem={st} — в CRM: {crm_status_label(st)}"
            )
        if not matched and mat_note:
            lines.append(f"    Material={mat_note} в 0992 не найден — сверить вручную.")
    else:
        lines.append("    (нет 0992 в папке)")
    if dup_warn:
        lines.append(dup_warn)

    lines.extend(
        [
            "",
            "  【L2 CRM — шаги】 (CRM Web UI; не VA03)",
            "    1) Заказ по guid в Web UI.",
            f"    2) INT-0992 от {dt0992[:19] or '…'} — зафиксировать messageId.",
            f"    3) INT-2010 от {dt2010[:19] or '…'} — в логе есть 034 и 391.",
            f"    4) Позиции с Material={mat_note or '…'} — сверка с UI.",
            "    5) Состав для 034: дубли услуг, лишние строки (E0320).",
            "    6) При корректном CRM — эскалация L2 S/4 с dateTime и позициями.",
            "",
            "  【L2 S/4 — шаги】 (VA03, MM)",
            f"    1) VA03: позиции с материалом {mat_note or '…'}.",
            f"    2) MM: материал {mat_note or '…'} на заводе {plant_note or '…'} (RU) — 391.",
            "    3) Bulk/changeset (034): один документ на changeset; сверка с 0992.",
            "    4) После MD и состава — повтор 0992→2010; ответ в тикет CRM.",
            "",
            "  【Эталон текста тикета】",
        ]
    )
    if order_id and order_guid:
        lines.append(
            f"    Заказ {order_id} (guid заказа {order_guid}): INT-2010 от {dt2010} — "
            f"034 — {note_034[:60] or '…'}; 391 — материал {mat_note or '…'} "
            f"на заводе {plant_note or '…'}. INT-0992 от {dt0992} — материал на поз. {pos_list}. "
            "Требуется MD S/4 и проверка состава / changeset."
        )
    else:
        lines.append(
            "    (см. шаблон §7.7 СППР_ПРАВИЛА_LLM — подставить номер, guid, dateTime)"
        )
    lines.append("  См. §7.7 СППР_ПРАВИЛА_LLM.")
    return "\n".join(lines)


def l2_crm_s4_blocks(
    root_tid: str,
    lat: dict | None,
    snap: dict | None,
    origin: str,
    znd_ids: list[str],
    pos_143: list[str],
    sppr_cls: str,
    root_ent: dict | None,
) -> tuple[list[str], list[str]]:
    crm: list[str] = []
    s4: list[str] = []
    is_025 = root_tid.startswith("025") or (
        lat
        and any("CUSTOMER_MATERIAL" in (i.get("note") or "") for i in lat.get("items") or [])
    )
    is_150 = root_tid.startswith("150") or (
        lat and any(str(i.get("typeID") or "").startswith("150") for i in lat.get("items") or [])
    )
    is_391 = _lat_has_type(lat, "391")
    is_034_391 = _is_034_391_combo(lat)
    is_003 = _lat_has_type(lat, "003")
    is_201 = _lat_has_type(lat, "201")
    is_151 = _lat_has_type(lat, "151")
    is_152 = _lat_has_type(lat, "152")
    is_005 = root_tid.startswith("005") or _lat_has_type(lat, "005")

    if is_025:
        notes_025 = _025_notes_from_lat(lat)
        if any("ORDER_QTY" in n for n in notes_025):
            crm.append(
                "  CRM Web UI (первично): актуальный статус заказа и позиции; количество; ЗНД; "
                "последнее действие оператора; сверка с 0992 "
                "(ZZStatusUser, ZZStatusUserItem, RequestedQuantity). Не VA03."
            )
            s4.append(
                "  При эскалации после CRM: VA03 — причина отклонения на позиции (ORDER_QTY, DEMO_PAGE_ID)."
            )
        else:
            crm.append(
                "  CRM Web UI: артикул клиента, лот, статус позиции; "
                "сверка с 0992 (Material, ZZ1_LOT_NUMBER, MaterialByCustomer)."
            )
            s4.append(
                "  При эскалации: VA03 — KDMAT vs MaterialByCustomer; последующие документы."
            )
    elif root_tid.startswith("143"):
        pos_txt = ", ".join(pos_143[:6]) or "(из note)"
        crm.extend(
            [
                f"  **Причина 143:** на поз. {pos_txt} S/4 отклоняет изменение ZPRI — "
                "на позиции уже создана ЗНД.",
                "  1. CRM Web UI: есть ли ЗНД на позиции (список ЗНД / статус E0010).",
                "     • ЗНД в CRM есть → изменить ZPRI только после отмены ЗНД, затем DEMO_PAGE_ID.",
                "     • ЗНД в CRM нет → переадресовать L2 S/4: проверить ЗНД в S/4 (VA03).",
                "  2. «Было → стало» по цене без INT-0993 или S/4 не указывать; "
                "текущий ZPRI — из INT-0992 (см. контекст выше).",
            ]
        )
        ztxt = ", ".join(znd_ids[:3]) if znd_ids else "номер ЗНД в JSON не найден"
        s4.append(f"  VA03: цена и ЗНД ({ztxt}) — эталон для CRM.")
    elif is_150:
        crm.append(
            "  CRM Web UI: позиция из note 150; статус позиции; последнее действие "
            "(рекламация/отказ/резерв); сверка ZZStatusUserItem с 0992. Не открывать VA03."
        )
        s4.append(
            "  VA03: фактический статус позиции (E0022/E0020); ЗНД, отгрузки; "
            "допустимость перехода E0022→E0020; последующие документы (099 в 2010)."
        )
    elif is_005:
        crm.extend(
            [
                "  §7.9 typeID 005:",
                "  1) INT-2010 (005, dateTime) + последний 0992 (ExternalDocLastChangeDateTime, позиции actionCode=04).",
                "  2) CRM Web UI: синхронизация; что оператор менял; не VA03.",
                "  3) После 0993 от S/4/OMS: если в UI уже верно — повтор не нужен; иначе то же действие в CRM.",
            ]
        )
        s4.extend(
            [
                "  §7.9: цепочка 0992→2010→0993; репликация 0993 без ZZ1_SOURCE_OMS (DEMO_PAGE_ID, 1010195422).",
                "  VA03 — только подтверждение наличия заказа; версия — из INT.",
            ]
        )
    elif is_003:
        crm.extend(
            [
                "  §7.12 typeID 003:",
                "  1) INT-2010 (003, dateTime) + полная цитата note.",
                "  2) INT-0992: позиции E0020, ZZ1_SPTECH, SalesOrderItemCategory.",
                "  3) CRM Web UI: действие оператора (рекламация/снятие резерва). Не путать с 004 (ЗНД).",
                "  4) Эскалация L2 S/4 (первичный анализ); L3 S/4 — настройки вида заказа.",
            ]
        )
        s4.append(
            "  §7.12: VA03 — допустимость снятия резерва; DEMO_PAGE_ID Ошибка 6; DEMO_PAGE_ID. "
            "Confluence: https://example.local/confluence?pageId=DEMO_PAGE_ID"
        )
    elif is_201:
        bp201 = _parse_201_bp(
            next(
                (
                    it.get("note") or ""
                    for it in (lat.get("items") or [])
                    if str(it.get("typeID") or "").startswith("201")
                ),
                "",
            )
        )
        try:
            from sppr_analyze import lines_201_l2_crm_text

            for ln in lines_201_l2_crm_text(bp201 or "(из note 2010)", "", numbered=False):
                if "CRM Web UI" in ln or "найти" in ln.lower():
                    crm.append(ln.strip())
                else:
                    s4.append(ln.strip())
        except ImportError:
            crm.append(
                f"  Найти делового партнёра {bp201 or '(из note 2010)'} в CRM Web UI; "
                "сверить с Partner[] в 0992."
            )
            s4.append(
                "  Если ДП есть в CRM — проверить S/4; при отсутствии — загрузка в ERP, 0992→2010."
            )
    elif is_151:
        crm.extend(
            [
                "  §7.10 typeID 151:",
                "  1) INT-2010 (151, dateTime) + note FROM→TO и позиция; при sev.3 — 158 ZSD.",
                "  2) INT-0992: ZZ1_SPTECH на позиции из note.",
                "  3) CRM Web UI: технология, статус позиции; регламент DEMO_PAGE_ID п. 10.59/10.60.",
                "  4) Не VA03; не путать с 152 без проверки ЗНД.",
            ]
        )
        s4.append(
            "  §7.10: VA03 — технология FROM, статус «Открыт», последующие документы; "
            "ответ в тикет CRM. Confluence: "
            "https://example.local/confluence?pageId=DEMO_PAGE_ID"
        )
    elif is_152:
        crm.extend(
            [
                "  §7.8 typeID 152:",
                "  1) В тезисе: ошибка 152 из INT-2010 (note, dateTime) и позиция из note.",
                "  2) Проверить ЗНД в CRM Web UI; сверить с блоком «ЗНД в INT» в отчёте.",
                "  3) Если ЗНД в JSON нет — запросить у L2 S/4 номер и статус ЗНД.",
                "  4) Сверить 0992: ZZ1_SPTECH, ZZStatusUserItem на позиции из note.",
            ]
        )
        s4.append(
            "  §7.8: VA03 — ЗНД, позиция из note, технология; подтвердить 152; ответ в тикет CRM."
        )
    elif is_034_391:
        crm.extend(
            [
                "  §7.7 составной 034+391 — пошагово:",
                "  1) Заказ по guid в CRM Web UI.",
                "  2) INT-0992 и INT-2010 по dateTime из отчёта (оба messageId).",
                "  3) Позиции с Material из note 391 — сверка с UI.",
                "  4) Состав заказа для 034: дубли услуг (несколько строк с одним Material), E0320.",
                "  5) Эскалация L2 S/4 при корректном CRM. Не VA03/MM.",
            ]
        )
        s4.extend(
            [
                "  §7.7 составной 034+391 — пошагово:",
                "  1) VA03 — позиции с материалом из note 391.",
                "  2) MM — материал на заводе из note (391).",
                "  3) Bulk/changeset (034): один документ на changeset; сверка с 0992.",
                "  4) После MD и состава — повтор 0992→2010; ответ в тикет с новым dateTime.",
            ]
        )
    elif is_391:
        crm.append(
            "  CRM Web UI: позиция с материалом из note 391; сверка Material с последним 0992; "
            "017 — вторично. Не открывать VA03/MM."
        )
        s4.append(
            "  VA03 + MM: материал на заводе из note 2010 (RU); расширение MD или исправление заказа."
        )
    elif root_ent and root_ent.get("l2_action"):
        crm.append(f"  CRM Web UI: {root_ent.get('crm', 'сверить заказ и позиции из note')}")
        s4.append(f"  S/4: {root_ent.get('s4', 'по note 2010')} — просмотр в VA03 при необходимости.")
    elif sppr_cls.startswith("B"):
        crm.append("  Мониторинг: INT-2026 в CRM; метка MSP — слой 2.")
        s4.append("  При наличии 2010 с ошибкой — VA03 по позициям из note.")
    else:
        crm.append("  CRM Web UI: заказ, позиции/цены/резерв по тексту note.")
        s4.append("  VA03: эталон S/4 по позициям из note (статусы, цены, документы).")

    if origin == "IMK" and not znd_ids and root_tid.startswith("143"):
        crm.append("  ИМК: номер ЗНД в JSON часто нет — не утверждать без файла.")
    return crm, s4


def branch_143_152_composite(
    lat: dict | None,
    snap: dict | None,
    pos_143: list[str],
    origin: str = "",
) -> str:
    pos_152 = _parse_152_position("")
    if lat:
        for it in lat.get("items") or []:
            if str(it.get("typeID") or "").startswith("152"):
                pos_152 = _parse_152_position(it.get("note") or "") or pos_152
    dt2010 = (lat or {}).get("dateTime") or "?"
    dt0992 = (snap or {}).get("dateTime") or "?"
    lines = [
        "  Класс A: составной кейс — в одном INT-2010 несколько корневых ошибок S/4.",
        "  Перечислить в ответе ВСЕ typeID severity 3: 143 (ZPRI после ЗНД) и 152 (технология при ЗНД).",
        "",
        "  【Факты из 2010】",
    ]
    if lat:
        for it in lat.get("items") or []:
            tid = str(it.get("typeID") or "")
            if tid.startswith("143") or tid.startswith("152"):
                lines.append(f"    [{dt2010}] {tid}")
                lines.append(f"    note: {(it.get('note') or '')[:200]}")
    lines.extend(["", "  【Контекст 0992】"])
    if snap:
        lines.append(
            f"    {int_moment_label('INT-0992', dt0992, snap['file'], snap.get('messageId'))}"
        )
        want = {p.lstrip("0") for p in pos_143}
        if pos_152:
            want.add(pos_152.lstrip("0"))
        for it in snap.get("items") or []:
            p = str(it.get("pos") or "").lstrip("0")
            if p in want:
                st = it.get("status")
                lines.append(
                    f"    поз. {it.get('pos')}: ZZ1_SPTECH={it.get('sptech') or '?'}; "
                    f"ZZStatusUserItem={st} — {crm_status_label(st)}; "
                    f"Material={it.get('material') or '?'}"
                )
    else:
        lines.append("    (нет 0992)")
    lines.extend(
        [
            "",
            "  【L2 CRM】 143: ZPRI по поз. из note (DEMO_PAGE_ID). 152: ЗНД в UI, поз. "
            f"{pos_152 or '?'} — не менять технологию при ЗНД.",
            "  【L2 S/4】 VA03: цены (143) + ЗНД/блокировка (152).",
            "  См. §7.13 СППР_ПРАВИЛА_LLM; Confluence DEMO_PAGE_ID, DEMO_PAGE_ID, DEMO_PAGE_ID.",
        ]
    )
    return "\n".join(lines)


def branch_143(origin: str, znd_ids: list[str], positions: list[str]) -> str:
    if not positions:
        positions = ["(из note 2010)"]
    pos_txt = ", ".join(positions[:8])
    if znd_ids:
        ztxt = ", ".join(znd_ids[:3])
        return (
            f"Ветка 143+ЗНД ({origin}): поз. {pos_txt}; ЗНД в JSON: {ztxt}. "
            "L2 CRM: ZPRI в UI vs 0993 (DEMO_PAGE_ID); L2 S/4: VA03."
        )
    if origin == "IMK":
        return (
            f"Ветка 143 без 0993 ({origin}): поз. {pos_txt}. "
            "L2 CRM: ЗНД в UI → при отсутствии проверка S/4 → отмена ЗНД → ZPRI (DEMO_PAGE_ID)."
        )
    return (
        f"Ветка 143 без 0993 ({origin}): поз. {pos_txt}. "
        "L2 CRM: ЗНД в UI → при отсутствии S/4 → отмена ЗНД → изменение ZPRI (DEMO_PAGE_ID)."
    )


def load_kb() -> dict:
    return json.loads(KB.read_text(encoding="utf-8"))


def kb_lookup(kb: dict, type_id: str) -> dict | None:
    for ent in kb.get("entries") or []:
        if ent.get("typeID") == type_id:
            return ent
    base = type_id.split("(")[0] if type_id else ""
    for ent in kb.get("entries") or []:
        eid = ent.get("typeID") or ""
        if eid.startswith(base + "("):
            return ent
    return None


def resolve_class(csv_cls: str, csv_err: str | None, lat: dict | None, folder: Path) -> tuple[str, str]:
    """Возвращает (класс_СППР, пояснение)."""
    if csv_cls == "B_2010":
        n = len(list(folder.glob("*2010*OmsReceivedFromS4*.json")))
        if lat and lat.get("items"):
            root = lat["items"][0]["typeID"]
            return "B->A", (
                f"Метка MSP «Не пришел 2010» (слой 2), но в JSON есть {n} файлов 2010; "
                f"корневая ошибка S/4: {root}"
            )
        if n == 0:
            return "B", "В папке нет INT-2010 — сбой оркестрации OMS/S/4"
        return "B", f"Есть {n} файлов 2010, но без severity 3 — уточнить вручную"
    if csv_cls == "B_0993":
        n93 = len(list(folder.glob("*0993*")))
        if n93:
            kinds = _classify_0993_files(folder)
            return "B->A", (
                f"Метка MSP «Не пришел 0993» (слой 2), но в папке {n93} файлов *0993*; "
                f"типы: {kinds}. Сверить время ожидания OMS (до 1 ч после 2010) и тип документа."
            )
        return "B", "Ожидался INT-0993 после успешного 2010 (resultCode=4) — проверить OMS/S/4"
    if csv_cls == "A":
        return "A", "Корень из INT-2010 (typeID в CSV или JSON)"
    if csv_cls == "ok":
        return "ok", "Заказ без ошибки в CSV"
    return csv_cls, csv_err or ""


def confluence_urls(kb: dict, ent: dict | None) -> list[str]:
    urls = []
    for g in kb.get("global_confluence") or []:
        pid = g.get("page_id")
        if pid:
            urls.append(f"{CONFLUENCE_BASE}{pid}  ({g.get('title', '')})")
    if ent:
        for c in ent.get("confluence") or []:
            pid = c.get("page_id")
            if pid:
                urls.append(f"{CONFLUENCE_BASE}{pid}  ({c.get('hint', ent.get('typeID', ''))})")
    extra = (kb.get("confluence_extra") or {}).get("price_fix_steps")
    if extra and ent and ent.get("family") == "ZND_price":
        urls.append(f"{CONFLUENCE_BASE}{extra['page_id']}  ({extra.get('title', '')})")
    return urls


def build_report_brief(order_id: str, folder: Path, csv_err: str | None) -> str:
    """Краткий отчёт для консоли / тикета (без дублирования веток)."""
    lat = extract_latest_2010(folder)
    snap = extract_latest_0992_snapshot(folder)
    csv_cls, _ = classify_csv_label(csv_err or "")
    sppr_cls, cls_note = resolve_class(csv_cls, csv_err, lat, folder)
    origin, _ = detect_order_origin(folder)
    crm_guid = extract_crm_order_guid(folder, order_id)
    znd_summary = format_znd_trace_summary(collect_znd_trace(folder, order_id))

    if is_integration_scenario_clean(csv_cls, sppr_cls, lat):
        dt0992 = (snap or {}).get("dateTime") or "?"
        lines = [
            format_order_header(order_id, crm_guid),
            "",
            *format_integration_ok_banner(
                order_id,
                csv_err=csv_err,
                cls_note=cls_note,
                origin=origin,
                lat=lat,
                znd_summary=znd_summary,
            ),
            "",
            f"INT-0992 ({dt0992}):",
        ]
        if snap and snap.get("partners"):
            parts = [
                f"{p.get('role')}={p.get('bp')}"
                for p in snap["partners"][:6]
            ]
            lines.append("Partner[]: " + "; ".join(parts))
        else:
            lines.append("Partner[]: (нет 0992)")
        lines.append("")
        lines.append("(краткий отчёт; полный: --full)")
        return "\n".join(lines)

    root_tid = ""
    note = ""
    composite_143_152 = False
    if lat and lat.get("items"):
        if _lat_has_type(lat, "143") and _lat_has_type(lat, "152"):
            composite_143_152 = True
            for it in lat["items"]:
                tid = str(it.get("typeID") or "")
                if tid.startswith("143"):
                    note = it.get("note") or note
                    break
        else:
            root_tid = lat["items"][0].get("typeID") or ""
            note = lat["items"][0].get("note") or ""
    dt2010 = (lat or {}).get("dateTime") or "?"
    dt0992 = (snap or {}).get("dateTime") or "?"
    if composite_143_152:
        root_label = "143+152"
        note_label = "143 (ZPRI после ЗНД) + 152 (технология при ЗНД)"
    else:
        root_label = root_tid or (
            "(нет ошибки severity 3 в 2010)" if lat else "(нет файла 2010)"
        )
        note_label = note or (
            "(нет ошибки severity 3 — см. --full для info-сообщений)"
            if lat and lat.get("info_items")
            else "(нет note)"
        )
    lines = [
        format_order_header(order_id, crm_guid),
        "",
        *format_integration_error_banner(order_id),
        f"Класс: {sppr_cls} | {origin} | **корень: {root_label}**",
        "",
        f"**INT-2010** ({dt2010}): **{note_label}**",
        "",
        f"**INT-0992** ({dt0992}):",
    ]
    if snap and snap.get("partners"):
        parts = [
            f"{p.get('role')}={p.get('bp')}"
            for p in snap["partners"][:6]
        ]
        lines.append("Partner[]: " + "; ".join(parts))
    else:
        lines.append("Partner[]: (нет 0992)")
    ext = (snap or {}).get("external_doc_last_change") or ""
    if ext:
        lines.append(f"ExternalDocLastChangeDateTime: {ext}")
    mid2010 = (lat or {}).get("messageId") or ""
    mid0992 = (snap or {}).get("messageId") or ""
    if mid2010 or mid0992:
        parts_mid = []
        if mid2010:
            parts_mid.append(f"2010={mid2010}")
        if mid0992:
            parts_mid.append(f"0992={mid0992}")
        lines.append(
            "INT (поиск XML в CRM): messageId " + "; ".join(parts_mid)
        )
    lines.append("")
    if root_tid.startswith("201") or _lat_has_type(lat, "201"):
        bp = _parse_201_bp(note)
        lines.append("L2 CRM:")
        pl201 = ""
        if snap and snap.get("partners"):
            pl201 = "; ".join(
                f"{p.get('role')}={p.get('bp')}" for p in snap["partners"][:8]
            )
        try:
            from sppr_analyze import lines_201_l2_crm_text

            for ln in lines_201_l2_crm_text(bp, pl201, numbered=False):
                lines.append(ln.rstrip())
        except ImportError:
            lines.append(
                f"  Найти делового партнёра {bp or '(из note)'} в CRM Web UI (карточка BP, КЛ); "
                "сверить роли с Partner[] в 0992."
            )
            lines.append(
                "  Если ДП есть в CRM — проверить наличие BP в S/4; "
                "при отсутствии в S/4 — загрузка BP в ERP, затем повтор 0992→2010."
            )
    elif root_tid.startswith("005") or _lat_has_type(lat, "005"):
        lines.append("L2 CRM:")
        lines.append(f"  {note[:200]}")
        if ext:
            lines.append(f"  Сверить 0992: ExternalDocLastChangeDateTime = {ext}")
        lines.append("  При E0020 — обновление рекламации в S/4.")
    elif composite_143_152 or (
        _lat_has_type(lat, "143") and _lat_has_type(lat, "152")
    ):
        lines.extend(
            format_143_152_brief_lines(lat, snap, folder, dt0992=dt0992)
        )
    elif root_tid.startswith("143") or _lat_has_type(lat, "143"):
        pos_143_b = extract_143_positions(lat)
        root_pos = _pos_from_143_note(note)
        znd_missing = not bool(list(folder.glob("*0993*")))
        lines.extend(
            format_143_zpri_brief_lines(
                snap,
                pos_143_b,
                root_crm_pos=root_pos,
                dt0992=dt0992,
            )
        )
        lines.append("")
        lines.extend(
            format_143_l2_brief_lines(
                pos_143_b,
                root_crm_pos=root_pos,
                znd_missing=znd_missing,
            )
        )
    else:
        lines.append("L2 CRM:")
        lines.append(f"  Сверить CRM Web UI с INT-2010: {note[:200]}")
    lines.append("")
    lines.append("(краткий отчёт; полный: --full)")
    return "\n".join(lines)


def build_report_integration_ok(
    order_id: str,
    folder: Path,
    csv_err: str | None,
    *,
    lat: dict,
    snap_0992: dict | None,
) -> str:
    """Полный отчёт для заказов MSP «Ок» без ошибки в последнем INT-2010."""
    kb = load_kb()
    csv_cls, _ = classify_csv_label(csv_err or "")
    sppr_cls, cls_note = resolve_class(csv_cls, csv_err, lat, folder)
    origin, cpt_raw = detect_order_origin(folder)
    znd_trace = collect_znd_trace(folder, order_id)
    crm_guid = extract_crm_order_guid(folder, order_id)
    timeline = scan_timeline(folder)
    order_hdr = format_order_header(order_id, crm_guid)

    lines = [
        f"=== СППР: {order_hdr} ===",
        f"Папка: {folder}",
        f"Метка CSV (MSP): {csv_err or 'Ок'}",
        f"Класс СППР: {sppr_cls}",
        f"Пояснение: {cls_note}",
        f"Источник заказа: {origin} (CustomerPurchaseOrderType={cpt_raw})",
        "",
        *format_integration_ok_banner(
            order_id,
            csv_err=csv_err,
            cls_note=cls_note,
            origin=origin,
            lat=lat,
            znd_summary=format_znd_trace_summary(znd_trace),
        ),
        "",
        "【Таймлайн INT】",
    ]
    if timeline:
        for ev in timeline[-15:]:
            lines.append(f"  {ev['dateTime']}  {ev['int']}  {ev['direction']}  {ev['file']}")
    else:
        lines.append("  (нет распознанных INT в именах файлов)")

    lines.append("")
    lines.append("【Поиск XML / сообщений INT (dateTime для поддержки CRM)】")
    lines.extend(build_int_search_anchor(lat, snap_0992, folder))

    lines.append("")
    lines.append("【Последний INT-2010 (S/4 → OMS) — без ошибки L2】")
    lines.append(
        f"  {int_moment_label('INT-2010', lat['dateTime'], lat['file'], lat.get('messageId'))}"
    )
    lines.append(f"  businessDocumentProcessingResultCode: {lat.get('resultCode')}")
    for it in (lat.get("info_items") or [])[:8]:
        lines.append(f"  [{lat['dateTime']}] typeID: {it.get('typeID')} (severity {it.get('severityCode')})")
        if it.get("note"):
            lines.append(f"  note: {it['note'][:500]}")

    lines.append("")
    lines.append("【Цепочка】 CRM 0989 -> OMS 0992 -> S/4 -> INT-2010 -> OMS 2026 -> CRM")
    lines.append("")
    lines.append("【Confluence (pageId)】")
    for u in confluence_urls(kb, None):
        lines.append(f"  {u}")

    lines.append("")
    lines.append("— Сгенерировано sppr_diagnose.py (заказ без ошибки интеграции) —")
    return "\n".join(lines)


def build_report(
    order_id: str, folder: Path, csv_err: str | None, *, brief: bool = False
) -> str:
    if brief:
        return build_report_brief(order_id, folder, csv_err)
    kb = load_kb()
    csv_cls, csv_tids = classify_csv_label(csv_err or "")
    timeline = scan_timeline(folder)
    lat = extract_latest_2010(folder)
    sppr_cls, cls_note = resolve_class(csv_cls, csv_err, lat, folder)
    if is_integration_scenario_clean(csv_cls, sppr_cls, lat):
        snap_0992 = extract_latest_0992_snapshot(folder)
        return build_report_integration_ok(
            order_id, folder, csv_err, lat=lat, snap_0992=snap_0992
        )
    origin, cpt_raw = detect_order_origin(folder)
    znd_trace = collect_znd_trace(folder, order_id)
    znd_ids = sorted({e["znd_id"] for e in znd_trace if e.get("znd_id")})
    pos_143 = extract_143_positions(lat)
    crm_guid = extract_crm_order_guid(folder, order_id)
    order_hdr = format_order_header(order_id, crm_guid)
    root_tid = ""
    if lat and lat.get("items"):
        root_tid = lat["items"][0].get("typeID") or ""

    lines = [
        f"=== СППР: {order_hdr} ===",
        "",
        *format_integration_error_banner(order_id),
        f"Папка: {folder}",
        f"Метка CSV (MSP): {csv_err or '(нет строки)'}",
        f"Класс СППР: {sppr_cls}",
        f"Пояснение: {cls_note}",
        f"Источник заказа: {origin} (CustomerPurchaseOrderType={cpt_raw})",
        f"ЗНД в JSON: {format_znd_trace_summary(znd_trace)}",
        f"Охват MVP: INT-2010 + рекомендации; детально typeID 143; прочие коды — по KB",
        "",
        "【Таймлайн INT】",
    ]
    if timeline:
        for ev in timeline[-15:]:
            lines.append(f"  {ev['dateTime']}  {ev['int']}  {ev['direction']}  {ev['file']}")
    else:
        lines.append("  (нет распознанных INT в именах файлов)")

    snap_0992 = extract_latest_0992_snapshot(folder)

    lines.append("")
    lines.append("【Поиск XML / сообщений INT (dateTime для поддержки CRM)】")
    lines.extend(build_int_search_anchor(lat, snap_0992, folder))

    lines.append("")
    lines.append("【Корень из INT-2010 (S/4 -> OMS)】")
    if lat:
        lines.append(
            f"  {int_moment_label('INT-2010', lat['dateTime'], lat['file'], lat.get('messageId'))}"
        )
        lines.append(f"  businessDocumentProcessingResultCode: {lat['resultCode']}")
        for it in lat["items"][:5]:
            lines.append(
                f"  [{lat['dateTime']}] typeID: {it['typeID']}"
            )
            lines.append(f"  note: {it['note'][:500]}")
            ent = kb_lookup(kb, it["typeID"])
            if ent:
                lines.append(f"  - CRM: {ent.get('crm', '-')}")
                lines.append(f"  - S/4: {ent.get('s4', '-')}")
                lines.append(f"  - L2: {ent.get('l2_action', '-')}")
                if ent.get("analog"):
                    lines.append(f"  - Аналоги: {', '.join(ent['analog'])}")
    else:
        lines.append("  Нет файла *2010*OmsReceivedFromS4*.json (или только SYNTH)")

    if csv_tids:
        lines.append("")
        lines.append(f"【typeID из CSV】 {', '.join(csv_tids)}")

    lines.append("")
    lines.append("【Цепочка】 CRM 0989 -> OMS 0992 -> S/4 -> INT-2010 -> OMS 2026 -> CRM")
    lines.append("")
    lines.append("【Confluence (pageId)】")
    root_ent = None
    if lat and lat.get("items"):
        root_ent = kb_lookup(kb, lat["items"][0]["typeID"])
    for u in confluence_urls(kb, root_ent):
        lines.append(f"  {u}")

    rules = kb.get("sppr_rules") or {}
    if csv_cls == "B_2010" and rules.get("csv_ne_prishel_2010"):
        lines.append("")
        lines.append(f"【Правило KB】 {rules['csv_ne_prishel_2010']}")

    if root_tid.startswith("025") or (
        lat and any("CUSTOMER_MATERIAL" in (i.get("note") or "") for i in lat.get("items") or [])
    ):
        lines.append("")
        lines.append("【Ветка 025】")
        lines.append(branch_025(lat, snap_0992, znd_trace))
        r025 = (kb.get("sppr_rules") or {}).get("typeid_025_customer_material")
        if r025:
            lines.append(f"  Правило KB: {r025}")

    is_143_lat = root_tid.startswith("143") or _lat_has_type(lat, "143")
    if is_143_lat and snap_0992 and pos_143:
        lines.append("")
        lines.append("【Контекст 0992 — ZPRI по позициям 143】")
        lines.append(
            f"  {int_moment_label('INT-0992', snap_0992.get('dateTime'), snap_0992['file'], snap_0992.get('messageId'))}"
        )
        want = {p.lstrip("0") for p in pos_143}
        for it in snap_0992.get("items") or []:
            p = str(it.get("pos") or "")
            if p.lstrip("0") not in want:
                continue
            zpri = it.get("zpri")
            ztxt = zpri if zpri is not None else "?"
            lines.append(
                f"  поз. {p}: Material={it.get('material')}; ZPRI={ztxt}; "
                f"ZZStatusUserItem={it.get('status')}; ZZ1_SPTECH={it.get('sptech') or '?'}"
            )
    if is_143_lat:
        lines.append("")
        lines.append(f"【Ветка 143】 {branch_143(origin, znd_ids, pos_143)}")
        orules = (kb.get("order_origin_rules") or {}).get(origin) or {}
        if orules.get("hint"):
            lines.append(f"  Подсказка ({origin}): {orules['hint']}")

    is_150 = root_tid.startswith("150") or (
        lat and any(str(i.get("typeID") or "").startswith("150") for i in (lat.get("items") or []))
    )
    if is_150:
        lines.append("")
        lines.append("【Ветка 150 / смена статуса позиции】")
        lines.append(branch_150(lat, snap_0992))
        r150 = (kb.get("sppr_rules") or {}).get("typeid_150_status")
        if r150:
            lines.append(f"  Правило KB: {r150}")

    is_034_391 = _is_034_391_combo(lat)
    is_391 = _lat_has_type(lat, "391")
    is_003 = _lat_has_type(lat, "003")
    is_201 = _lat_has_type(lat, "201")
    is_151 = _lat_has_type(lat, "151")
    is_152 = _lat_has_type(lat, "152")
    if is_003:
        lines.append("")
        lines.append("【Ветка 003 / снять резерв невозможно (не 004/ЗНД)】")
        lines.append(branch_003(lat, snap_0992, origin))
        r003 = (kb.get("sppr_rules") or {}).get("typeid_003_reserve_release")
        if r003:
            lines.append(f"  Правило KB: {r003}")
    elif is_201:
        lines.append("")
        lines.append("【Ветка 201(R1) / деловой партнёр отсутствует в S/4】")
        lines.append(branch_201(lat, snap_0992, origin))
        r201 = (kb.get("sppr_rules") or {}).get("typeid_201_bp_missing")
        if r201:
            lines.append(f"  Правило KB: {r201}")
    elif is_151:
        lines.append("")
        lines.append("【Ветка 151 / смена технологии отгрузки】")
        lines.append(branch_151(lat, snap_0992))
        r151 = (kb.get("sppr_rules") or {}).get("typeid_151_ship_tech_change")
        if r151:
            lines.append(f"  Правило KB: {r151}")
    elif is_152:
        lines.append("")
        lines.append("【Ветка 152 / технология отгрузки при ЗНД】")
        lines.append(branch_152(lat, snap_0992, znd_trace))
        r152 = (kb.get("sppr_rules") or {}).get("typeid_152_znd_ship_tech")
        if r152:
            lines.append(f"  Правило KB: {r152}")
    if is_143_lat and is_152:
        lines.append("")
        lines.append("【Ветка 143 + 152 / составной кейс в одном INT-2010】")
        lines.append(branch_143_152_composite(lat, snap_0992, pos_143, origin))
        r143152 = (kb.get("sppr_rules") or {}).get("typeid_143_152_composite")
        if r143152:
            lines.append(f"  Правило KB: {r143152}")
    elif is_034_391:
        lines.append("")
        lines.append("【Ветка 034 + 391 / changeset и материал на заводе】")
        lines.append(branch_034_391(lat, snap_0992, order_id, crm_guid or ""))
        r47 = (kb.get("sppr_rules") or {}).get("typeid_034_391_composite")
        if r47:
            lines.append(f"  Правило KB: {r47}")
    elif is_391:
        lines.append("")
        lines.append("【Ветка 391 / материал на заводе в S/4】")
        lines.append(branch_391(lat, snap_0992))
        r391 = (kb.get("sppr_rules") or {}).get("typeid_391_material_plant")
        if r391:
            lines.append(f"  Правило KB: {r391}")

    is_005 = root_tid.startswith("005") or _lat_has_type(lat, "005")
    if is_005:
        lines.append("")
        lines.append("【Ветка 005 / рассинхрон версии (более поздняя версия)】")
        lines.append(branch_005(folder, lat, snap_0992))
        r005 = (kb.get("sppr_rules") or {}).get("typeid_005_version_sync")
        if r005:
            lines.append(f"  Правило KB: {r005}")

    if csv_cls == "B_0993":
        lines.append("")
        lines.append("【Ветка 0993 / «Не пришел 0993»】")
        lines.append(branch_0993(csv_cls, sppr_cls, folder, lat, znd_ids))
        r0993 = (kb.get("sppr_rules") or {}).get("csv_ne_prishel_0993")
        if r0993:
            lines.append(f"  Правило KB: {r0993}")

    lines.append("")
    crm_chk, s4_chk = l2_crm_s4_blocks(
        root_tid, lat, snap_0992, origin, znd_ids, pos_143, sppr_cls, root_ent
    )
    lines.append("【L2 CRM】 (CRM Web UI; не VA03)")
    lines.extend(crm_chk)
    lines.append("")
    lines.append("【L2 S/4 (ERP LO)】 (VA03 и транзакции S/4)")
    lines.extend(s4_chk)
    lines.append("  См. разделение зон: СППР_ПРАВИЛА_LLM §2.1")

    lines.append("")
    lines.append("— Сгенерировано sppr_diagnose.py (факты из JSON; Confluence — по kb_typeid_index) —")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="СППР MVP: диагностика по JSON заказа")
    ap.add_argument("--order", required=True, help="Номер заказа")
    ap.add_argument("--folder", help="Путь к папке JSON (иначе поиск в Примеры)")
    ap.add_argument("--out", help="Файл отчёта (иначе stdout)")
    ap.add_argument("--include-synth", action="store_true", help="Учитывать SYNTH_TRAIN в 2010")
    ap.add_argument(
        "--full",
        action="store_true",
        help="Полный отчёт (таймлайн, Confluence, ветки KB); по умолчанию — краткий",
    )
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="Только запись в --out, без вывода в stdout (вызов из sppr_analyze)",
    )
    args = ap.parse_args()

    folder = Path(args.folder) if args.folder else find_folder(args.order)
    if not folder or not folder.is_dir():
        raise SystemExit(f"Папка для заказа {args.order} не найдена")

    csv_err = read_csv_row(args.order)
    report = build_report(args.order, folder, csv_err, brief=not args.full)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        if not args.quiet:
            print(f"Отчёт: {args.out}")
    else:
        _print_stdout(report)


def _print_stdout(text: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    except (UnicodeEncodeError, AttributeError):
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
