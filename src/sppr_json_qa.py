# -*- coding: utf-8 -*-
"""Контекст из JSON заказа для режима «вопрос–ответ» (сначала факты из кода, потом LLM)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sppr_diagnose as sd
import sppr_json_glossary as gloss
import sppr_code_decode as dec

MAT = Path(__file__).parent
FIELD_CATALOG = MAT / "json_field_catalog_crm.json"

QA_SYSTEM = """Ты помощник по данным интеграции заказа CRM.
Отвечай КРАТКО, структурировано (markdown: заголовки, списки, таблицы).
Используй ТОЛЬКО блок «Данные из JSON».
Если в JSON нет фактов для ответа на вопрос — одна строка: «Ответ на данный вопрос не найден» (без домыслов, без общих описаний INT из Confluence).
ЗАПРЕЩЕНО: придумывать значения; копировать все note из 2010 подряд; формат «Итого: заказ» для тикета Jira.
Для цены — одна цифра ZPRI + file + dateTime."""


ROLE_LABELS = {
    "ZF": "КЛ ГП (контактное лицо грузополучателя)",
    "ZY": "КЛ (контактное лицо заказчика)",
    "ZP": "ZP",
    "ZQ": "ZQ",
    "AG": "заказчик",
    "WE": "грузополучатель",
    "RG": "плательщик",
    "ZA": "ZA",
}


@dataclass
class JsonFact:
    """Один факт из JSON с путём поля для контроля (эталон — имя в CRM/INT)."""
    value: str
    json_path: str
    file: str
    date_time: str
    int_spec: str


@dataclass
class OrderJsonSnapshot:
    order_id: str
    folder: Path
    items_0992: list[dict[str, Any]]
    file_0992: str
    dt_0992: str
    znd_by_pos: dict[str, dict]
    errors_2010: list[dict]
    partners_header: list[dict[str, Any]]
    contacts_0989: list[dict[str, Any]]
    file_0989: str
    dt_0989: str
    gift_items_0989: list[dict[str, Any]]
    delivery_rows_0989: list[dict[str, Any]]
    services_0990: list[dict[str, Any]]
    file_0990: str
    dt_0990: str
    payment_terms_0989: str
    payment_type_0989: str
    payment_method_0992: str
    credit_days_type_0989: str
    credit_days_amount_0989: int | str | None


def _load_json(fp: Path) -> dict | None:
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _file_dt(fp: Path) -> str:
    d = _load_json(fp)
    if d:
        return str(d.get("dateTime") or "")
    return fp.name


def _latest_matching(folder: Path, pattern: str) -> tuple[str, str, dict] | None:
    best: tuple[str, str, dict] | None = None
    for fp in folder.glob(pattern):
        d = _load_json(fp)
        if not d:
            continue
        dt = str(d.get("dateTime") or "")
        if best is None or dt > best[0]:
            best = (dt, fp.name, d)
    return best


def _norm_pos(key: str) -> str:
    digits = re.sub(r"\D", "", key or "")
    if not digits:
        return ""
    return str(int(digits))


def _items_from_0992(data: dict) -> list[dict[str, Any]]:
    order = (data.get("object") or {}).get("data", {}).get("Order") or {}
    out = []
    for it in order.get("Item") or []:
        sid = it.get("SalesOrderItemID")
        if sid is None:
            continue
        zpri = None
        currency = None
        for pe in it.get("PricingElement") or []:
            if pe.get("ConditionType") == "ZPRI":
                zpri = pe.get("ZZConditionRateValue")
                currency = pe.get("ZZCurrency")
        crm_id = int(sid)
        mat = it.get("Material")
        out.append(
            {
                "crm_pos": crm_id,
                "s4_pos": str(crm_id).zfill(6),
                "material": mat,
                "productCode": mat,
                "zpri": zpri,
                "currency": currency,
                "status": it.get("ZZStatusUserItem"),
                "plant": it.get("Plant"),
                "transit_werk": it.get("ZZ1_TRANSIT_WERK"),
                "log_region": it.get("ZZ1_LOG_REGION"),
            }
        )
    return out


def _znd_last_by_pos(folder: Path, order_id: str) -> dict[str, dict]:
    rows: list[dict] = []
    for fp in sorted(folder.glob("*0993*OmsReceivedFromS4*.json"), key=_file_dt):
        d = _load_json(fp)
        if not d:
            continue
        dt = d.get("dateTime") or ""
        for so in (d.get("object") or {}).get("data", {}).get("salesOrder") or []:
            if so.get("ZZObjectType") != "DELIV_REQ":
                continue
            ref = str(so.get("ZZSalesOrdID") or so.get("ReferenceSDDocument") or "")
            if order_id not in ref and order_id not in fp.name:
                continue
            znd_id = str(so.get("SalesOrderID") or "")
            for it in so.get("Item") or []:
                sid = int(it["SalesOrderItemID"])
                s4 = str(sid).zfill(6)
                qty = (it.get("RequestedQuantity") or {}).get("value") or 1
                unit = None
                for pe in it.get("PricingElement") or []:
                    if pe.get("ConditionType") == "ZPRI":
                        amt = (pe.get("ConditionAmount") or {}).get("value")
                        if amt and qty:
                            unit = round(float(amt) / float(qty), 4)
                rows.append(
                    {
                        "file": fp.name,
                        "dateTime": dt,
                        "znd_id": znd_id,
                        "s4_pos": s4,
                        "crm_pos": sid,
                        "zpri_unit": unit,
                        "material": it.get("Material"),
                    }
                )
    last: dict[str, dict] = {}
    for r in rows:
        key = r["s4_pos"]
        if key not in last or (r["dateTime"] or "") >= (last[key].get("dateTime") or ""):
            last[key] = r
    return last


def _phone_from_address(addr: dict | None) -> str | None:
    if not addr:
        return None
    comm = addr.get("Communication") or {}
    mob = comm.get("MobilePhone") or {}
    return mob.get("MobilePhoneNumber") or comm.get("PhoneNumber")


def _format_partner_name(addr: dict | None) -> str:
    """ФИО/название: AddressName + AddressAdditionalName (имя, отчество)."""
    if not addr:
        return ""
    main = str(addr.get("AddressName") or "").strip()
    add = str(addr.get("AddressAdditionalName") or "").strip()
    if main and add:
        if add.casefold() in main.casefold() or main.casefold() in add.casefold():
            return main if len(main) >= len(add) else add
        return f"{main} {add}"
    return main or add


def _partners_from_0992_order(order: dict) -> list[dict[str, Any]]:
    out = []
    for p in order.get("Partner") or []:
        role = str(p.get("PartnerFunction") or "")
        if not role:
            continue
        addr = p.get("Address") or {}
        phys = addr.get("PhysicalAddress") or {}
        out.append(
            {
                "role": role,
                "role_ru": ROLE_LABELS.get(role, role),
                "bp": p.get("BusinessPartnerID"),
                "name": _format_partner_name(addr) or addr.get("AddressName"),
                "phone": _phone_from_address(addr),
                "addr_additional": addr.get("AddressAdditionalName"),
                "tsd_address": phys.get("ZZTSDAdress"),
                "city": phys.get("CityName"),
                "street": phys.get("StreetName"),
                "postal": phys.get("PostalCode"),
                "json_path_fio": f"Order.Partner[PartnerFunction={role}].Address.AddressName",
                "json_path_phone": (
                    f"Order.Partner[PartnerFunction={role}].Address.Communication"
                    ".MobilePhone.MobilePhoneNumber"
                ),
                "json_path_address": (
                    f"Order.Partner[PartnerFunction={role}].Address.PhysicalAddress"
                ),
            }
        )
    return out


def load_field_catalog() -> dict:
    if FIELD_CATALOG.is_file():
        return json.loads(FIELD_CATALOG.read_text(encoding="utf-8"))
    return {}


def _format_fact_block(title: str, fact: JsonFact, *, extra: str = "") -> list[str]:
    lines = [
        f"## {title}",
        "",
        f"**Значение:** {fact.value}",
        "",
        "**Источник в JSON (эталон CRM):**",
        f"- Поле: `{fact.json_path}`",
        f"- INT: **{fact.int_spec}**",
        f"- Файл: `{fact.file}`",
        f"- dateTime: `{fact.date_time}`",
    ]
    if extra:
        lines.append(f"- {extra}")
    lines.append("")
    lines.append("*Извлечено кодом из JSON (не LLM).*")
    return lines


def _pick_contact_fio(
    snap: OrderJsonSnapshot, role_hint: str | None = None
) -> JsonFact | None:
    """Приоритет: role_hint → ZY (КЛ) → ZF (КЛ ГП) → 0989 receiverContactFullName."""

    def _fact_from_partner(p: dict[str, Any]) -> JsonFact:
        role = str(p.get("role") or "")
        return JsonFact(
            value=str(p.get("name") or "").strip(),
            json_path=str(
                p.get("json_path_fio")
                or f"Order.Partner[PartnerFunction={role}].Address"
            ),
            file=snap.file_0992,
            date_time=snap.dt_0992,
            int_spec="INT-0992",
        )

    if role_hint:
        p = next((x for x in snap.partners_header if x.get("role") == role_hint), None)
        if p and (p.get("name") or "").strip():
            return _fact_from_partner(p)
    for role in ("ZY", "ZF"):
        p = next((x for x in snap.partners_header if x.get("role") == role), None)
        if p and (p.get("name") or "").strip():
            return _fact_from_partner(p)
    best_989 = ""
    for c in snap.contacts_0989:
        nm = str(c.get("name") or "").strip()
        if len(nm) > len(best_989):
            best_989 = nm
    if best_989:
        return JsonFact(
            value=best_989,
            json_path="order[].items[].receiverContactFullName (INT-0989)",
            file=snap.file_0989,
            date_time=snap.dt_0989,
            int_spec="INT-0989",
        )
    return None


def _item_for_question(snap: OrderJsonSnapshot, question: str) -> dict | None:
    pos = parse_position_hint(question)
    if pos:
        return _item_by_pos(snap, pos)
    if snap.items_0992:
        return snap.items_0992[0]
    return None


def _contacts_from_0989_data(data: dict) -> list[dict[str, str]]:
    """receiverContact* в структуре 0989 (CRM)."""
    found: list[dict[str, str]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if obj.get("receiverContactFullName") or obj.get("receiverContactPhone"):
                found.append(
                    {
                        "name": str(obj.get("receiverContactFullName") or ""),
                        "phone": str(obj.get("receiverContactPhone") or ""),
                    }
                )
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for x in obj:
                walk(x)

    walk((data.get("object") or {}).get("data") or data)
    uniq: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for c in found:
        key = (c.get("name") or "", c.get("phone") or "")
        if key in seen or not any(key):
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


def _items_from_0989_order(order: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Позиции 0989: подарки (itemGiftFlag/ZTNN), параметры доставки, giftCards."""
    gifts: list[dict] = []
    delivery: list[dict] = []
    for it in order.get("items") or []:
        pos = it.get("crmPosNum") or it.get("itemOmsGuid")
        row = {
            "crm_pos": pos,
            "product": it.get("productCode"),
            "description": (it.get("description") or "")[:80],
            "deliveryType": it.get("deliveryType"),
            "deliveryInterval": it.get("deliveryInterval"),
            "requestedDate": it.get("requestedDate"),
            "carrier": it.get("carrier"),
        }
        delivery.append(row)
        if it.get("itemGiftFlag") == "X" or str(it.get("itemType") or "").upper() == "ZTNN":
            gifts.append(
                {
                    **row,
                    "itemGiftFlag": it.get("itemGiftFlag"),
                    "itemType": it.get("itemType"),
                    "price": it.get("userUnitGrossPrice"),
                    "campaignId": it.get("campaignId"),
                }
            )
    gift_cards = []
    for gc in order.get("giftCards") or []:
        gift_cards.append(
            {
                "giftCardCode": gc.get("giftCardCode"),
                "giftCardAmount": gc.get("giftCardAmount"),
                "giftCardCoID": gc.get("giftCardCoID"),
            }
        )
    if gift_cards:
        gifts.append({"_giftCards": gift_cards})
    return gifts, delivery, gift_cards


def _services_from_0990_data(data: dict) -> list[dict[str, Any]]:
    out: list[dict] = []
    for svc in ((data.get("object") or {}).get("data", {}).get("services") or {}).get(
        "service"
    ) or []:
        entry: dict[str, Any] = {"type": svc.get("type")}
        items = (svc.get("items") or {}).get("item") or []
        if items:
            entry["service_name"] = items[0].get("fullName") or items[0].get("shortName")
            entry["totalGrossCost"] = items[0].get("totalGrossCost")
        dlv = svc.get("delivery") or {}
        if dlv:
            addr = dlv.get("address") or {}
            recv = dlv.get("receiver") or {}
            parts = [
                addr.get("postalCode"),
                addr.get("city"),
                addr.get("streetName") or addr.get("street"),
                addr.get("building"),
            ]
            entry["delivery_address"] = ", ".join(str(x) for x in parts if x)
            entry["receiver_name"] = recv.get("name")
            entry["receiver_phone"] = recv.get("phone")
            entry["logisticRegionCode"] = dlv.get("logisticRegionCode")
            cons = (dlv.get("consignments") or {}).get("consignment") or []
            if cons:
                slot = (cons[0] or {}).get("slot") or {}
                entry["slot_date"] = slot.get("date")
                entry["slot_from"] = slot.get("intervalFrom")
                entry["slot_to"] = slot.get("intervalTo")
        fa = svc.get("furnitureAssembly")
        if fa:
            entry["furnitureAssembly_date"] = fa.get("date")
            entry["furnitureAssembly_cost"] = fa.get("totalGrossCost")
        out.append(entry)
    return out


def _errors_2010(folder: Path) -> list[dict]:
    out = []
    lat = sd.extract_latest_2010(folder)
    if not lat:
        return out
    for it in lat.get("items") or []:
        out.append(
            {
                "file": lat.get("file"),
                "dateTime": lat.get("dateTime"),
                "typeID": it.get("typeID"),
                "note": it.get("note"),
            }
        )
    return out


def load_snapshot(order_id: str, folder: Path | None = None) -> OrderJsonSnapshot | None:
    folder = folder or sd.find_folder(order_id)
    if not folder:
        return None
    lat992 = _latest_matching(folder, "*0992*OmsReceivedFromCRM*.json")
    lat989 = _latest_matching(folder, "*0989*OmsReceivedFromCRM*.json")
    items: list[dict] = []
    partners: list[dict] = []
    f992, dt992 = "", ""
    pay_method = ""
    if lat992:
        dt992, f992, data = lat992
        order = (data.get("object") or {}).get("data", {}).get("Order") or {}
        items = _items_from_0992(data)
        partners = _partners_from_0992_order(order)
        pay_method = str(order.get("PaymentMethod") or "")
    contacts989: list[dict] = []
    f989, dt989 = "", ""
    gifts989: list[dict] = []
    delivery989: list[dict] = []
    pay_terms, pay_type, cd_type, cd_amt = "", "", "", None
    if lat989:
        dt989, f989, d989 = lat989
        contacts989 = _contacts_from_0989_data(d989)
        order0 = ((d989.get("object") or {}).get("data", {}).get("order") or [{}])[0]
        gifts989, delivery989, _ = _items_from_0989_order(order0)
        pay_terms = str(order0.get("paymentTerms") or "")
        pay_type = str(order0.get("paymentType") or "")
        cd_type = str(order0.get("creditDaysType") or "")
        cd_amt = order0.get("creditDaysAmount")
    services990: list[dict] = []
    f990, dt990 = "", ""
    lat990 = _latest_matching(folder, "*0990*")
    if lat990:
        dt990, f990, d990 = lat990
        services990 = _services_from_0990_data(d990)
    return OrderJsonSnapshot(
        order_id=order_id,
        folder=folder,
        items_0992=items,
        file_0992=f992,
        dt_0992=dt992,
        znd_by_pos=_znd_last_by_pos(folder, order_id),
        errors_2010=_errors_2010(folder),
        partners_header=partners,
        contacts_0989=contacts989,
        file_0989=f989,
        dt_0989=dt989,
        gift_items_0989=gifts989,
        delivery_rows_0989=delivery989,
        services_0990=services990,
        file_0990=f990,
        dt_0990=dt990,
        payment_terms_0989=pay_terms,
        payment_type_0989=pay_type,
        payment_method_0992=pay_method,
        credit_days_type_0989=cd_type,
        credit_days_amount_0989=cd_amt,
    )


def parse_position_hint(question: str) -> str | None:
    q = question or ""
    if re.search(r"\bцена\b|\bzpri\b", q, re.I) and re.search(
        r"\b(\d{1,3})\b", q
    ):
        m = re.search(
            r"(?:поз(?:иции|иция|\.?)\s*№?\s*|поз\.?\s*|^|\s)(\d{1,3})(?:\s|$|\.|,)",
            q,
            re.I,
        )
        if m:
            return _norm_pos(m.group(1))
    for pat in (
        r"поз(?:иции|иция|\.?)\s*№?\s*(\d+)",
        r"поз\.?\s*(\d+)",
        r"position\s*(\d+)",
    ):
        m = re.search(pat, q, re.I)
        if m:
            return _norm_pos(m.group(1))
    return None


def _item_by_pos(snap: OrderJsonSnapshot, pos: str) -> dict | None:
    p = _norm_pos(pos)
    if not p:
        return None
    s4 = p.zfill(6)
    for it in snap.items_0992:
        if str(it["crm_pos"]) == p or it["s4_pos"] == s4:
            return it
    return None


def _is_price_question(q: str) -> bool:
    return bool(
        re.search(r"цена|zpri|стоим|сколько\s+стоит", q, re.I)
    )


def _is_price_change_int_question(q: str) -> bool:
    """Вопрос про изменение/историю цены в интеграции (INT-2010, 0992, 0993)."""
    return bool(
        re.search(
            r"(менял|изменил|изменен|изменял|пересчит|обновил|истори).{0,50}(цен|zpri)|"
            r"(цен|zpri).{0,50}(менял|изменил|изменен|пересчит)|"
            r"цена\s+менял",
            q,
            re.I,
        )
    )


def _note_matches_position(note: str, pos: str) -> bool:
    p = _norm_pos(pos)
    if not p:
        return True
    s4 = p.zfill(6)
    n = note or ""
    if s4 in n:
        return True
    if re.search(rf"поз\.?\s*0*{p}\b", n, re.I):
        return True
    if re.search(rf"\b{p}\b", n) and "поз" in n.lower():
        return True
    return False


def _zpri_timeline_0992(folder: Path) -> dict[str, list[dict[str, Any]]]:
    """CRM-позиция → хронология ZPRI по всем файлам 0992."""
    by_pos: dict[str, list[dict[str, Any]]] = {}
    for fp in sorted(folder.glob("*0992*OmsReceivedFromCRM*.json"), key=_file_dt):
        d = _load_json(fp)
        if not d:
            continue
        dt = d.get("dateTime") or _file_dt(fp)
        for it in _items_from_0992(d):
            key = str(it["crm_pos"])
            zpri = it.get("zpri")
            hist = by_pos.setdefault(key, [])
            if hist and hist[-1].get("zpri") == zpri and hist[-1].get("file") == fp.name:
                continue
            hist.append(
                {
                    "zpri": zpri,
                    "file": fp.name,
                    "dateTime": dt,
                    "s4_pos": it["s4_pos"],
                }
            )
    return by_pos


def format_qa_not_found(order_id: str, question: str) -> str:
    q = (question or "").strip()
    return (
        f"## Ответ на данный вопрос не найден\n\n"
        f"По заказу **{order_id}** в данных JSON интеграции **нет достоверного ответа** "
        f"на вопрос: «{q}».\n\n"
        "Система **не дополняет** ответ домыслами из справочника Confluence, "
        "если фактов по заказу в JSON нет.\n\n"
        "Уточните вопрос (номер позиции, INT 0992/0993/2010) или проверьте папку JSON заказа.\n\n"
        "*Правило СППР: нет ответа в JSON — без LLM-домыслов.*"
    )


def _qa_llm_body_is_unanswered(body: str) -> bool:
    """LLM не дал содержательного ответа по JSON."""
    b = (body or "").strip()
    if not b:
        return True
    low = b.lower()
    if re.search(
        r"ответ\s+на\s+данн\w+\s+вопрос\s+не\s+найден",
        low,
    ):
        return True
    not_found = bool(
        re.search(
            r"в\s+json\s+не\s+найден|"
            r"в\s+данных\s+json\s+не\s+найден|"
            r"ответ.*не\s+найден|"
            r"данных\s+недостаточ|"
            r"нет\s+данных\s+в\s+json|"
            r"не\s+удалось\s+найти",
            low,
        )
    )
    if not not_found:
        return False
    if re.search(r"zpri[`:\s*]+\d", b, re.I):
        return False
    if re.search(r"\*\*ответ:\*\*", low) and "не найден" not in low:
        return False
    if re.search(r"typeid\s*[`:\s*]+\d{3,4}", b, re.I):
        return False
    return True


def _is_error_question(q: str) -> bool:
    return bool(re.search(r"ошибк|typeid|2010|корень|причин", q, re.I))


def _is_contact_question(q: str) -> bool:
    return bool(
        re.search(
            r"контакт|контактн|кл\b|к\.л\.|фио|телефон\s+контакт|кому\s+звон",
            q,
            re.I,
        )
    )


def _wants_phone_in_answer(q: str) -> bool:
    return bool(re.search(r"телефон|тел\.|phone|номер\s+тел", q, re.I))


def _is_contact_only_question(q: str) -> bool:
    """Только КЛ: без запроса телефона и без списка всех партнёров."""
    if not _is_contact_question(q):
        return False
    if _wants_phone_in_answer(q):
        return False
    if re.search(r"все\s+контакт|партн|список", q, re.I):
        return False
    return True


def _is_partner_question(q: str) -> bool:
    return bool(re.search(r"партн|заказчик|грузополуч|рол[ьи]\s+(ag|we|zf)", q, re.I))


def _is_address_question(q: str) -> bool:
    if _is_gift_question(q) or _is_services_question(q):
        return False
    return bool(
        re.search(
            r"адрес|улиц|город|индекс|tsd|zztsd|физическ",
            q,
            re.I,
        )
    ) and not _is_price_question(q)


def _is_gift_question(q: str) -> bool:
    return bool(
        re.search(
            r"подар|gift|itemgift|ztnn|подарочн\s*карт|zlo1|zz_gift|промокод",
            q,
            re.I,
        )
    )


def _is_payment_question(q: str) -> bool:
    return _is_payment_type_question(q) or _is_payment_terms_question(q)


def _is_payment_type_question(q: str) -> bool:
    """Вид оплаты (ВО): paymentType / PaymentMethod — Безнал, Н/c и т.д."""
    if re.search(r"услови.*оплат|paymentterms|отсрочк|кредит.*дн|creditdays", q, re.I):
        return False
    return bool(
        re.search(
            r"вид\s+оплат|paymenttype|paymentmethod|безнал|"
            r"способ\s+оплат(?!\s+достав)|\bво\b",
            q,
            re.I,
        )
    )


def _is_payment_terms_question(q: str) -> bool:
    """Условия оплаты (УО): paymentTerms, отсрочка, кредитные дни."""
    return bool(
        re.search(
            r"услови.*оплат|paymentterms|отсрочк|кредит.*дн|creditdays|"
            r"кредитн.*лин|payment\s+terms",
            q,
            re.I,
        )
    )


def _is_items_count_question(q: str) -> bool:
    return bool(
        re.search(
            r"сколько\s+позиц|число\s+позиц|количеств.*позиц|сколько\s+стр",
            q,
            re.I,
        )
    )


def _is_delivery_params_question(q: str) -> bool:
    return bool(
        re.search(
            r"способ\s+доставк|тип\s+доставк|deliverytype|zz1_dlv|интервал\s+доставк|"
            r"дата\s+доставк|deliveryinterval|слот\s+доставк|перевозчик",
            q,
            re.I,
        )
    ) and not _is_address_question(q)


def _is_delivery_type_only_question(q: str) -> bool:
    return bool(
        re.search(r"способ\s+доставк|тип\s+доставк|deliverytype|zz1_dlv", q, re.I)
    ) and not re.search(
        r"перевозчик|интервал|дата\s+доставк|deliveryinterval|услови.*оплат|оплат",
        q,
        re.I,
    )


def _is_services_question(q: str) -> bool:
    return bool(
        re.search(
            r"услуг|сборк|assembly|furniture|стоимость\s+доставк|"
            r"услуга\s+по\s+доставк|servicechannel|канал\s+обслуж",
            q,
            re.I,
        )
    ) and not _is_gift_question(q)


def _is_logistics_question(q: str) -> bool:
    return bool(
        re.search(
            r"склад|сох|лог\s*регион|регион|перевоз|plant|transit|warehouse|carrier",
            q,
            re.I,
        )
    )


def answer_logistics_direct(snap: OrderJsonSnapshot, question: str) -> str | None:
    """Склад / регион СОХ / перевозчик — из Item 0992 или items 0989."""
    q = question or ""
    it = _item_for_question(snap, q)
    blocks: list[str] = []

    if re.search(r"склад|plant|transit|локальн", q, re.I) and it:
        plant = it.get("plant")
        transit = it.get("transit_werk")
        pos_lbl = it.get("crm_pos", "?")
        if plant is not None:
            f = JsonFact(
                value=str(plant),
                json_path=f"Order.Item[SalesOrderItemID={pos_lbl}].Plant",
                file=snap.file_0992,
                date_time=snap.dt_0992,
                int_spec="INT-0992",
            )
            blocks.extend(
                _format_fact_block(
                    f"Склад отгрузки (Plant), поз. {pos_lbl}",
                    f,
                    extra="Confluence: код локального склада (INT-0992)",
                )
            )
        if transit is not None and re.search(r"транзит|transit|склад", q, re.I):
            f = JsonFact(
                value=str(transit),
                json_path=f"Order.Item[SalesOrderItemID={pos_lbl}].ZZ1_TRANSIT_WERK",
                file=snap.file_0992,
                date_time=snap.dt_0992,
                int_spec="INT-0992",
            )
            blocks.append("\n".join(_format_fact_block(
                f"Код склада (ZZ1_TRANSIT_WERK), поз. {pos_lbl}", f
            )))

    if re.search(r"сох|регион|log.?region", q, re.I):
        if it and it.get("log_region") is not None:
            pos_lbl = it.get("crm_pos", "?")
            f = JsonFact(
                value=str(it["log_region"]),
                json_path=f"Order.Item[SalesOrderItemID={pos_lbl}].ZZ1_LOG_REGION",
                file=snap.file_0992,
                date_time=snap.dt_0992,
                int_spec="INT-0992",
            )
            blocks.extend(
                _format_fact_block(
                    f"Регион СОХ (ZZ1_LOG_REGION), поз. {pos_lbl}",
                    f,
                    extra="Confluence: регион СОХ на позиции",
                )
            )
        lat989 = _latest_matching(snap.folder, "*0989*OmsReceivedFromCRM.json")
        if lat989 and not blocks:
            _dt, fname, d = lat989
            items = (d.get("object") or {}).get("data", {}).get("items") or []
            if items and items[0].get("regionSOH"):
                f = JsonFact(
                    value=str(items[0]["regionSOH"]),
                    json_path="items[].regionSOH",
                    file=fname,
                    date_time=_dt,
                    int_spec="INT-0989",
                )
                blocks.extend(_format_fact_block("Регион СОХ (0989)", f))

    if re.search(r"перевоз|carrier", q, re.I):
        lat989 = _latest_matching(snap.folder, "*0989*OmsReceivedFromCRM.json")
        if lat989:
            _dt, fname, d = lat989
            carriers: set[str] = set()
            for row in (d.get("object") or {}).get("data", {}).get("items") or []:
                if row.get("carrier") is not None:
                    carriers.add(str(row["carrier"]))
            if carriers:
                parts = [
                    dec.format_code("carrier", c) for c in sorted(carriers)
                ]
                val = "; ".join(parts)
                f = JsonFact(
                    value=val,
                    json_path="items[].carrier",
                    file=fname,
                    date_time=_dt,
                    int_spec="INT-0989",
                )
                blocks.extend(
                    _format_fact_block(
                        "Перевозчик (код + расшифровка)",
                        f,
                        extra="Справочник SHIPPER_CODE; доп. — json_code_lookups_user.json",
                    )
                )

    if not blocks:
        return None
    return "\n".join(blocks)


def answer_price_direct(snap: OrderJsonSnapshot, pos: str) -> str | None:
    it = _item_by_pos(snap, pos)
    if not it and not snap.items_0992:
        return None
    s4 = pos.zfill(6)
    znd = snap.znd_by_pos.get(s4)
    lines = [
        f"## Цена ZPRI — позиция **{pos}** (CRM) / **{s4}** (S/4)",
        "",
    ]
    if it:
        cur = it.get("currency") or "RUB4"
        zpri = it.get("zpri")
        lines.extend(
            [
                "### Последний INT-0992 (CRM → OMS)",
                f"- **ZPRI:** `{zpri}` ({cur})",
                f"- **Материал:** {it.get('material') or '—'}",
                f"- **Статус позиции:** {it.get('status') or '—'}",
                f"- **Файл:** `{snap.file_0992}`",
                f"- **dateTime:** `{snap.dt_0992}`",
                "",
            ]
        )
    else:
        lines.append(
            f"В последнем 0992 (`{snap.file_0992}`) позиция **{pos}** не найдена.\n"
        )

    if znd:
        lines.extend(
            [
                "### Последний INT-0993 (цена по ЗНД)",
                f"- **ZPRI (unit):** `{znd.get('zpri_unit')}`",
                f"- **ЗНД:** {znd.get('znd_id')}",
                f"- **Файл:** `{znd.get('file')}`",
                f"- **dateTime:** `{znd.get('dateTime')}`",
                "",
            ]
        )
    elif it:
        lines.append(
            "*В 0993 цена по этой позиции не найдена (для ИМК 0993 часто нет).*\n"
        )

    if snap.errors_2010:
        rel = [
            e
            for e in snap.errors_2010
            if s4 in (e.get("note") or "") or f"поз. {pos}" in (e.get("note") or "")
        ]
        if rel:
            lines.append("### Связанная ошибка INT-2010")
            for e in rel[:2]:
                lines.append(f"- **{e.get('typeID')}:** {e.get('note')}")
            lines.append("")

    if it and it.get("zpri") is not None:
        lines.append(
            f"**Ответ:** в последнем **0992** цена ZPRI позиции **{pos}** = **{it['zpri']}** {it.get('currency') or ''}."
        )
    else:
        lines.append("**Ответ:** точная ZPRI в последнем 0992 для этой позиции **не найдена**.")
    lines.append("\n*Источник: расчёт по JSON (без LLM).*")
    return "\n".join(lines)


def answer_price_change_by_int_direct(
    snap: OrderJsonSnapshot, pos: str | None = None
) -> str | None:
    """Изменение ZPRI по INT: 2010 (143 и др.), при необходимости — история 0992."""
    price_notes = [
        e
        for e in snap.errors_2010
        if re.search(r"zpri|цен|стоим|изменен|пересчит|знд", e.get("note") or "", re.I)
        and (not pos or _note_matches_position(e.get("note") or "", pos))
    ]
    timeline = _zpri_timeline_0992(snap.folder)
    changed_rows: list[str] = []
    pos_filter = _norm_pos(pos) if pos else None
    for crm_pos, hist in sorted(timeline.items(), key=lambda x: int(x[0])):
        if pos_filter and str(crm_pos) != pos_filter:
            continue
        zvals = [h.get("zpri") for h in hist if h.get("zpri") is not None]
        if len(zvals) >= 2 and len(set(zvals)) > 1:
            parts = [
                f"`{h.get('zpri')}` ({h.get('dateTime') or '—'}, `{h.get('file')}`)"
                for h in hist[-4:]
            ]
            changed_rows.append(
                f"- Поз. **{crm_pos}** (S/4 `{hist[-1].get('s4_pos')}`): "
                f"ZPRI в 0992 менялась → {' → '.join(parts)}"
            )

    if not price_notes and not changed_rows:
        return None

    lines = [
        f"## Изменение цены по INT — заказ **{snap.order_id}**",
        "",
    ]
    if pos_filter:
        lines.append(f"Фильтр позиции CRM: **{pos_filter}**\n")

    if price_notes:
        lines.append("### INT-2010 (ответ S/4)")
        for e in price_notes[:12]:
            lines.append(
                f"- **{e.get('typeID')}** — {e.get('note')}  \n"
                f"  `{e.get('dateTime')}` | `{e.get('file')}`"
            )
        lines.append("")

    if changed_rows:
        lines.append("### INT-0992 (хронология ZPRI в JSON)")
        lines.extend(changed_rows[:15])
        lines.append("")

    if pos_filter:
        it = _item_by_pos(snap, pos_filter)
        znd = snap.znd_by_pos.get(pos_filter.zfill(6))
        if it or znd:
            lines.append("### Срез на последний 0992 / 0993")
            if it:
                lines.append(
                    f"- ZPRI в последнем 0992: `{it.get('zpri')}` "
                    f"({it.get('currency') or 'RUB4'}) | `{snap.file_0992}`"
                )
            if znd:
                lines.append(
                    f"- ZPRI по ЗНД (0993): `{znd.get('zpri_unit')}` | "
                    f"ЗНД `{znd.get('znd_id')}` | `{znd.get('file')}`"
                )
            lines.append("")

    if price_notes:
        summary = price_notes[0].get("note") or "есть сообщения INT-2010 о смене ZPRI"
        lines.append(f"**Ответ:** да — по **INT-2010** зафиксировано изменение цены: {summary}")
    else:
        lines.append(
            "**Ответ:** по **INT-0992** в JSON видна смена ZPRI между выгрузками "
            "(см. хронологию выше); отдельного note в последнем 2010 нет."
        )
    lines.append("\n*Источник: JSON (без LLM).*")
    return "\n".join(lines)


def answer_error_direct(snap: OrderJsonSnapshot) -> str | None:
    if not snap.errors_2010:
        return None
    lines = [
        f"## Ошибка интеграции — заказ **{snap.order_id}**",
        "",
        "### INT-2010 (последний ответ S/4)",
    ]
    for e in snap.errors_2010[:6]:
        lines.append(
            f"- **{e.get('typeID')}** — {e.get('note')}  \n"
            f"  `{e.get('dateTime')}` | `{e.get('file')}`"
        )
    root = snap.errors_2010[0]
    lines.append(
        f"\n**Корень:** **{root.get('typeID')}** — {root.get('note')}\n"
        "\n*Источник: JSON (без LLM).*"
    )
    return "\n".join(lines)


def _md_table_cell(val: Any) -> str:
    """Ячейка markdown-таблицы: без переносов и символа |."""
    s = str(val if val is not None else "—").strip().replace("\n", " ").replace("\r", " ")
    return s.replace("|", "／") or "—"


def answer_gifts_direct(snap: OrderJsonSnapshot) -> str | None:
    lines = [f"## Подарки / бесплатные позиции — заказ **{snap.order_id}**", ""]
    real_gifts = [g for g in snap.gift_items_0989 if "_giftCards" not in g]
    cards = next((g.get("_giftCards") for g in snap.gift_items_0989 if "_giftCards" in g), None)
    if not real_gifts and not cards:
        return (
            f"## Подарки — заказ **{snap.order_id}**\n\n"
            "В последнем **0989** нет позиций с `itemGiftFlag=X` / `itemType=ZTNN` "
            "и нет блока `giftCards`.\n"
        )
    lines.append(f"INT-0989: `{snap.file_0989}` | {snap.dt_0989}\n")
    if real_gifts:
        lines.append("### Позиции-подарки (0989)")
        lines.append("| CRM поз. | productCode | itemType | itemGiftFlag | описание |")
        lines.append("|----------|-------------|----------|--------------|----------|")
        for g in real_gifts[:20]:
            lines.append(
                f"| {_md_table_cell(g.get('crm_pos'))} | {_md_table_cell(g.get('product'))} | "
                f"{_md_table_cell(g.get('itemType'))} | {_md_table_cell(g.get('itemGiftFlag'))} | "
                f"{_md_table_cell(g.get('description'))} |"
            )
        lines.append("")
        lines.append("")
        lines.append(
            "**Поля JSON:** `order[].items[].itemGiftFlag`, `itemType` (ZTNN), "
            "`campaignId` — см. INT-0989 / Confluence DEMO_PAGE_ID."
        )
    if cards:
        lines.append("")
        lines.append("### Подарочные карты (0989 `giftCards`)")
        for c in cards:
            lines.append(
                f"- код: `{c.get('giftCardCode')}` | сумма: `{c.get('giftCardAmount')}` | "
                f"CO: `{c.get('giftCardCoID') or '—'}`"
            )
        lines.append("")
        lines.append(
            "**В 0992:** `Order.PricingElement[ConditionType=ZLO1].ZZ_GIFT` "
            "(если карта уходит в S/4)."
        )
    lines.append("")
    lines.append("*Источник: JSON (код).*")
    return "\n".join(lines)


def answer_payment_type_direct(snap: OrderJsonSnapshot) -> str | None:
    """Вид оплаты (ВО): paymentType / PaymentMethod — не путать с УО (paymentTerms)."""
    if not (snap.payment_type_0989 or snap.payment_method_0992):
        return None
    lines = [
        f"## Вид оплаты (ВО) — заказ **{snap.order_id}**",
        "",
        f"INT-0989: `{snap.file_0989}` | {snap.dt_0989}",
        "",
    ]
    if snap.payment_type_0989:
        lines.append(
            f"**Ответ:** {dec.format_code('payment_type', snap.payment_type_0989)}"
        )
        lines.append(f"- Поле: `order[].paymentType` (INT-0989)")
    if snap.payment_method_0992:
        lines.append(
            f"- **PaymentMethod (0992):** "
            f"{dec.format_code('payment_type', snap.payment_method_0992)}"
        )
        lines.append(f"- Файл: `{snap.file_0992}` | {snap.dt_0992}")
    if snap.payment_terms_0989:
        lines.append("")
        lines.append(
            f"*Условия оплаты (УО, `paymentTerms` = "
            f"{dec.format_code('payment_terms', snap.payment_terms_0989)}) — "
            "это **не** вид оплаты. Спросите «условия оплаты» или «отсрочка», если нужны.*"
        )
    lines.append("\n*Источник: JSON INT-0989/0992 (код).*")
    return "\n".join(lines)


def answer_payment_terms_direct(snap: OrderJsonSnapshot) -> str | None:
    """Условия оплаты (УО): paymentTerms, creditDays*."""
    if not (
        snap.payment_terms_0989
        or snap.credit_days_type_0989
        or snap.credit_days_amount_0989 is not None
    ):
        return None
    lines = [
        f"## Условия оплаты (УО) — заказ **{snap.order_id}**",
        "",
        f"INT-0989: `{snap.file_0989}` | {snap.dt_0989}",
        "",
    ]
    if snap.payment_terms_0989:
        lines.append(
            f"- **paymentTerms (УО):** "
            f"{dec.format_code('payment_terms', snap.payment_terms_0989)}"
        )
    if snap.credit_days_type_0989:
        lines.append(
            f"- **creditDaysType:** "
            f"{dec.format_code('credit_days_type', snap.credit_days_type_0989)}"
        )
    if snap.credit_days_amount_0989 is not None:
        lines.append(f"- **creditDaysAmount:** `{snap.credit_days_amount_0989}`")
    if snap.payment_type_0989:
        lines.append("")
        lines.append(
            f"*Вид оплаты (ВО, `paymentType`): "
            f"{dec.format_code('payment_type', snap.payment_type_0989)} — отдельный вопрос.*"
        )
    lines.append("\n*Источник: JSON INT-0989 (код).*")
    return "\n".join(lines)


def answer_payment_direct(snap: OrderJsonSnapshot, question: str = "") -> str | None:
    """Маршрутизация: вид оплаты (ВО) vs условия (УО)."""
    q = question or ""
    parts: list[str] = []
    if _is_payment_type_question(q):
        block = answer_payment_type_direct(snap)
        if block:
            parts.append(block)
    if _is_payment_terms_question(q):
        block = answer_payment_terms_direct(snap)
        if block:
            parts.append(block)
    if not parts:
        if snap.payment_type_0989 and not snap.payment_terms_0989:
            block = answer_payment_type_direct(snap)
        else:
            block = answer_payment_terms_direct(snap)
        if block:
            parts.append(block)
    return "\n\n".join(parts) if parts else None


def _append_items_table(
    lines: list[str],
    items: list[dict[str, Any]],
    *,
    max_rows: int = 80,
) -> None:
    """Таблица позиций 0992; показываем все строки (до max_rows), без «… ещё N»."""
    lines.append("")
    lines.append("| CRM поз. | S/4 | Артикул (Material) |")
    lines.append("|----------|-----|-------------------|")
    shown = items[:max_rows]
    for it in shown:
        art = it.get("productCode") or it.get("material") or "—"
        lines.append(
            f"| {_md_table_cell(it.get('crm_pos'))} | {_md_table_cell(it.get('s4_pos'))} | "
            f"{_md_table_cell(art)} |"
        )
    if len(items) > max_rows:
        lines.append("")
        lines.append(
            f"*Показаны первые **{max_rows}** из **{len(items)}** позиций "
            "(остальные — в файле INT-0992).*"
        )
    elif items:
        lines.append("")
        lines.append(f"*Показаны все **{len(items)}** позиций из последнего INT-0992.*")


def answer_items_count_direct(snap: OrderJsonSnapshot) -> str | None:
    """Число позиций в последнем 0992."""
    if not snap.items_0992:
        return (
            f"## Позиции заказа **{snap.order_id}**\n\n"
            f"В последнем **0992** (`{snap.file_0992}`) позиции **не найдены**.\n"
        )
    gift_n = sum(
        1
        for it in snap.items_0992
        if str(it.get("itemType") or "").upper() == "ZTNN"
        or it.get("itemGiftFlag") == "X"
    )
    lines = [
        f"## Позиции заказа **{snap.order_id}**",
        "",
        f"INT-0992: `{snap.file_0992}` | {snap.dt_0992}",
        "",
        f"**Ответ:** в последнем 0992 — **{len(snap.items_0992)}** позиций (строк Item).",
    ]
    if gift_n:
        lines.append(f"- из них подарки / ZTNN: **{gift_n}**")
    _append_items_table(lines, snap.items_0992)
    lines.append("\n*Источник: JSON INT-0992 (код).*")
    return "\n".join(lines)


def answer_delivery_params_direct(snap: OrderJsonSnapshot, question: str = "") -> str | None:
    it = _item_for_question(snap, question)
    delivery_only = _is_delivery_type_only_question(question)
    lines = [
        f"## Параметры доставки — заказ **{snap.order_id}**",
        "",
        f"INT-0989: `{snap.file_0989}` | {snap.dt_0989}",
        "",
    ]

    dlv992 = None
    if it and snap.items_0992:
        lat = _latest_matching(snap.folder, "*0992*OmsReceivedFromCRM*.json")
        if lat:
            _, _, d = lat
            order = (d.get("object") or {}).get("data", {}).get("Order") or {}
            for item in order.get("Item") or []:
                if str(item.get("SalesOrderItemID")) == str(it.get("crm_pos")):
                    dlv992 = item.get("ZZ1_DLVType")
                    break

    row = None
    if it:
        row = next(
            (r for r in snap.delivery_rows_0989 if r.get("crm_pos") == it.get("crm_pos")),
            None,
        )
    if not row and snap.delivery_rows_0989:
        row = snap.delivery_rows_0989[0]

    if row:
        dt = row.get("deliveryType")
        if delivery_only:
            lines.append(
                f"**Ответ:** {dec.format_code('delivery_type', dt)}"
            )
            lines.append(f"- Поле: `order[].items[].deliveryType` (INT-0989)")
            if dlv992 is not None:
                lines.append(
                    f"- **0992** `Item.ZZ1_DLVType`: "
                    f"{dec.format_code('delivery_type', dlv992)}"
                )
            lines.append("")
        else:
            lines.append("| Источник | Поле | Код | Расшифровка |")
            lines.append("|----------|------|-----|-------------|")
            lines.append(
                f"| 0989 | items[].deliveryType | `{dt}` | "
                f"{dec.decode('delivery_type', dt) or dec.format_code('delivery_type', dt)} |"
            )
            if not delivery_only:
                lines.append(
                    f"| 0989 | items[].deliveryInterval | `{row.get('deliveryInterval')}` | "
                    "интервал OMS |"
                )
                lines.append(
                    f"| 0989 | items[].requestedDate | `{row.get('requestedDate')}` | "
                    "требуемая дата |"
                )
            car = row.get("carrier")
            if car:
                car_ru = dec.decode("carrier", car) or dec.format_code("carrier", car)
                lines.append(
                    f"| 0989 | items[].carrier | `{car}` | {car_ru} |"
                )
            if dlv992 is not None:
                lines.append(
                    f"| 0992 | Item.ZZ1_DLVType | `{dlv992}` | "
                    f"{dec.decode('delivery_type', dlv992) or dec.format_code('delivery_type', dlv992)} |"
                )
            lines.append("")
            lines.append(f"**Способ доставки:** {dec.format_code('delivery_type', dt)}")
            car = row.get("carrier")
            if car:
                lines.append(f"**Перевозчик:** {dec.format_code('carrier', car)}")
            lines.append("")
    else:
        return None

    lines.append("")
    lines.append(
        "**Поля JSON:** `items[].deliveryType`, `ZZ1_DLVType` (0992), "
        "`carrier` — Confluence OMS-01 / SD-08."
    )
    lines.append("\n*Источник: JSON INT-0989/0992 (код).*")
    return "\n".join(lines)


def answer_services_direct(snap: OrderJsonSnapshot) -> str | None:
  lines = [f"## Услуги заказа — **{snap.order_id}**", ""]
  if snap.services_0990:
    lines.append(f"### INT-0990 (`{snap.file_0990}` | {snap.dt_0990})")
    for i, s in enumerate(snap.services_0990[:5], 1):
      lines.append(f"**Услуга {i}:** {s.get('service_name') or '—'}")
      if s.get("totalGrossCost") is not None:
        lines.append(f"- Стоимость: `{s['totalGrossCost']}`")
      if s.get("delivery_address"):
        lines.append(f"- Адрес доставки: {s['delivery_address']}")
        lines.append(
          "  - Поля: `services.service[].delivery.address`, "
          "`delivery.receiver`, `consignments.consignment[].slot`"
        )
      if s.get("receiver_name"):
        lines.append(
          f"- Получатель: {s.get('receiver_name')} | тел: {s.get('receiver_phone') or '—'}"
        )
      if s.get("slot_date"):
        lines.append(
          f"- Слот: {s['slot_date']} {s.get('slot_from')}–{s.get('slot_to')}"
        )
      if s.get("furnitureAssembly_date"):
        lines.append(
          f"- Сборка мебели: дата `{s['furnitureAssembly_date']}`, "
          f"стоимость `{s.get('furnitureAssembly_cost')}`"
        )
      lines.append("")
  lat989 = _latest_matching(snap.folder, "*0989*OmsReceivedFromCRM*.json")
  if lat989:
    _, fname, d = lat989
    order0 = ((d.get("object") or {}).get("data", {}).get("order") or [{}])[0]
    ch = order0.get("serviceChannel")
    if ch:
      lines.append(f"### INT-0989 — канал обслуживания")
      lines.append(f"- **serviceChannel:** `{ch}` (`{fname}`)\n")
    asm = [
      it
      for it in (order0.get("items") or [])
      if it.get("assemblyStatus") or it.get("positionAssemblyCostPerUnit")
    ]
    if asm:
      lines.append("### Сборка на позициях (0989)")
      for it in asm[:5]:
        lines.append(
          f"- поз. {it.get('crmPosNum')}: assemblyStatus=`{it.get('assemblyStatus')}`, "
          f"cost/unit=`{it.get('positionAssemblyCostPerUnit')}`"
        )
      lines.append("")
  if len(lines) <= 2:
    return (
      f"## Услуги — заказ **{snap.order_id}**\n\n"
      "В JSON нет **0990** с `services` и нет полей сборки/канала в **0989**.\n"
    )
  lines.append("*Источник: JSON 0990/0989 (код).*")
  return "\n".join(lines)


def answer_address_direct(snap: OrderJsonSnapshot, question: str = "") -> str | None:
    """Адрес AG (заказчик) или WE (ГП / доставка) из INT-0992."""
    role = gloss.detect_partner_role(question) or "WE"
    if role not in ("AG", "WE"):
        role = "WE"
    p = next((x for x in snap.partners_header if x.get("role") == role), None)
    if not p:
        return (
            f"## Адрес — заказ **{snap.order_id}**\n\n"
            f"В последнем **0992** партнёр с ролью **{role}** не найден.\n"
        )
    title = "Адрес доставки (ГП, WE)" if role == "WE" else "Адрес / заказчик (AG)"
    lines = [
        f"## {title} — заказ **{snap.order_id}**",
        "",
        f"- **Роль:** `{role}` ({p.get('role_ru')})",
        f"- **BusinessPartnerID:** {p.get('bp') or '—'}",
        f"- **Файл:** `{snap.file_0992}` | {snap.dt_0992}",
        "",
    ]
    if p.get("tsd_address"):
        fact = JsonFact(
            value=str(p["tsd_address"]),
            json_path=f"{p.get('json_path_address')}.ZZTSDAdress",
            file=snap.file_0992,
            date_time=snap.dt_0992,
            int_spec="INT-0992",
        )
        lines.extend(_format_fact_block("Полный адрес (ZZTSDAdress)", fact))
    elif p.get("name"):
        fact = JsonFact(
            value=str(p["name"]),
            json_path=str(p.get("json_path_fio")),
            file=snap.file_0992,
            date_time=snap.dt_0992,
            int_spec="INT-0992",
        )
        lines.extend(_format_fact_block("AddressName", fact))
    else:
        lines.append(
            "В JSON нет **PhysicalAddress** / **AddressName** для этой роли — "
            "только BusinessPartnerID; проверьте карту ДП в CRM Web UI.\n"
        )
    parts = []
    if p.get("city"):
        parts.append(f"город: {p['city']}")
    if p.get("street"):
        parts.append(f"улица: {p['street']}")
    if p.get("postal"):
        parts.append(f"индекс: {p['postal']}")
    if parts:
        lines.append("**Структура PhysicalAddress:** " + "; ".join(parts) + "\n")
    if p.get("addr_additional"):
        lines.append(f"**AddressAdditionalName:** {p['addr_additional']}\n")
    lines.append("*Источник: JSON INT-0992 (код).*")
    return "\n".join(lines)


def answer_contact_direct(snap: OrderJsonSnapshot, question: str = "") -> str | None:
    """Контактное лицо: режим «только КЛ» — ФИО + путь JSON, без телефона."""
    role_hint = gloss.detect_partner_role(question)
    if role_hint in ("WE", "AG"):
        role_hint = None
    fact = _pick_contact_fio(snap, role_hint)
    if not fact:
        return (
            f"## Контактное лицо — заказ **{snap.order_id}**\n\n"
            "В последних **0992/0989** ФИО контактного лица (ZF/ZY / receiverContactFullName) "
            "**не заполнено** — только BusinessPartnerID на ролях.\n\n"
            "*Проверьте карту ДП в CRM Web UI (в JSON нет AddressName).*"
        )

    if _is_contact_only_question(question):
        return "\n".join(
            _format_fact_block(
                f"Контактное лицо — заказ {snap.order_id}",
                fact,
                extra=(
                    "**КЛ** — роль **ZY** (`PartnerFunction=ZY`); "
                    "**КЛ ГП** — отдельный вопрос, роль **ZF**."
                ),
            )
        )

    lines = [
        f"## Контактные лица — заказ **{snap.order_id}**",
        "",
        f"**ФИО (основное):** {fact.value}",
        f"- Поле: `{fact.json_path}` | {fact.int_spec} | `{fact.file}`",
        "",
    ]
    if _wants_phone_in_answer(question):
        zf = next((p for p in snap.partners_header if p.get("role") == "ZF"), None)
        phone = (zf or {}).get("phone")
        if phone:
            lines.append(f"**Телефон:** {phone}")
            lines.append(
                f"- Поле: `{zf.get('json_path_phone')}` | INT-0992 | `{snap.file_0992}`"
            )
            lines.append("")
    else:
        lines.append("*Телефон не выводится (вопрос только про контактное лицо / ФИО).*\n")
    lines.append("| Роль | PartnerFunction | ФИО | BP |")
    lines.append("|------|-----------------|-----|-----|")
    for p in snap.partners_header:
        if p.get("role") in ("ZF", "ZY", "WE", "AG"):
            lines.append(
                f"| {p.get('role_ru')} | {p['role']} | {p.get('name') or '—'} | {p.get('bp') or '—'} |"
            )
    lines.append("\n*Источник: JSON (код).*")
    return "\n".join(lines)


def answer_partners_direct(snap: OrderJsonSnapshot) -> str | None:
    if not snap.partners_header:
        return None
    lines = [
        f"## Партнёры заказа **{snap.order_id}**",
        f"INT-0992: `{snap.file_0992}` | {snap.dt_0992}",
        "",
        "| Роль | Описание | BusinessPartnerID | ФИО/название |",
        "|------|----------|-------------------|--------------|",
    ]
    for p in snap.partners_header:
        lines.append(
            f"| {p['role']} | {p.get('role_ru')} | {p.get('bp') or '—'} | {p.get('name') or '—'} |"
        )
    lines.append("\n*Источник: JSON (без LLM).*")
    return "\n".join(lines)


def build_qa_context_slim(snap: OrderJsonSnapshot) -> str:
    lines = [
        f"Заказ {snap.order_id}",
        f"Последний 0992: {snap.file_0992} | {snap.dt_0992}",
        "",
    ]
    if snap.partners_header:
        lines.append("Партнёры (0992 заголовок): роль | BP | ФИО | телефон")
        for p in snap.partners_header:
            lines.append(
                f"{p['role']} | {p.get('bp')} | {p.get('name')} | {p.get('phone')}"
            )
        lines.append("")
    if snap.contacts_0989:
        lines.append(f"0989 контакты ({snap.file_0989}):")
        for c in snap.contacts_0989:
            lines.append(f"  {c.get('name')} | {c.get('phone')}")
        lines.append("")
    lines.append("Позиции (0992): CRM | S4 | Material | ZPRI | Status")
    for it in sorted(snap.items_0992, key=lambda x: x["crm_pos"])[:30]:
        lines.append(
            f"{it['crm_pos']} | {it['s4_pos']} | {it.get('material')} | "
            f"{it.get('zpri')} | {it.get('status')}"
        )
    if snap.znd_by_pos:
        lines.append("\nЗНД (последний 0993 на позицию): S4 | ZPRI unit | ZND | file")
        for s4 in sorted(snap.znd_by_pos.keys())[:20]:
            z = snap.znd_by_pos[s4]
            lines.append(
                f"{s4} | {z.get('zpri_unit')} | {z.get('znd_id')} | {z.get('file')}"
            )
    if snap.errors_2010:
        lines.append("\n2010 (корень):")
        e = snap.errors_2010[0]
        lines.append(f"{e.get('typeID')} | {e.get('note')}")
    if snap.gift_items_0989:
        n = len([g for g in snap.gift_items_0989 if "_giftCards" not in g])
        lines.append(f"\nПодарки 0989: {n} поз.")
    if snap.services_0990:
        lines.append(f"Услуги 0990: {len(snap.services_0990)}")
    return "\n".join(lines)


def build_qa_context(order_id: str, folder: Path | None = None) -> str:
    snap = load_snapshot(order_id, folder)
    if not snap:
        return f"Заказ {order_id}: папка JSON не найдена."
    return build_qa_context_slim(snap)


def answer_question(
    order_id: str,
    question: str,
    folder: Path | None = None,
    *,
    use_rag: bool = True,
) -> str:
    snap = load_snapshot(order_id, folder)
    if not snap:
        return f"Заказ **{order_id}**: папка JSON не найдена."

    q = (question or "").strip()
    pos = parse_position_hint(q)

    if _is_logistics_question(q) and not _is_price_question(q):
        direct = answer_logistics_direct(snap, q)
        if direct:
            return direct

    if _is_items_count_question(q):
        direct = answer_items_count_direct(snap)
        if direct:
            return direct

    if _is_payment_question(q) and not _is_logistics_question(q):
        direct = answer_payment_direct(snap, q)
        if direct:
            return direct

    if _is_gift_question(q):
        direct = answer_gifts_direct(snap)
        if direct:
            return direct

    if _is_services_question(q):
        direct = answer_services_direct(snap)
        if direct:
            return direct

    if _is_delivery_params_question(q):
        direct = answer_delivery_params_direct(snap, q)
        if direct:
            return direct

    if _is_address_question(q) and not _is_contact_question(q):
        direct = answer_address_direct(snap, q)
        if direct:
            return direct

    if _is_contact_question(q):
        direct = answer_contact_direct(snap, q)
        if direct:
            return direct

    if _is_partner_question(q) and not _is_contact_question(q):
        direct = answer_partners_direct(snap)
        if direct:
            return direct

    if _is_price_change_int_question(q):
        direct = answer_price_change_by_int_direct(snap, pos)
        if direct:
            return direct

    if pos and _is_price_question(q):
        direct = answer_price_direct(snap, pos)
        if direct:
            return direct

    if _is_error_question(q) and not _is_price_question(q):
        direct = answer_error_direct(snap)
        if direct:
            return direct

    if pos and not _is_price_question(q):
        direct = answer_price_direct(snap, pos)
        if direct:
            return direct

    from sppr_llm import call_llm_qa

    ctx = build_qa_context_slim(snap)
    matched = gloss.match_question_to_catalog(q)
    glossary_block = gloss.format_glossary_for_llm(matched)
    rag_hits: list = []
    decode_hint = (
        "Справочник расшифровки кодов (payment_terms, delivery_type, carrier): "
        "используй блок «Расшифровка» если есть в данных; иначе код как есть.\n"
    )
    extra = decode_hint
    if snap.payment_terms_0989:
        extra += (
            f"paymentTerms: {dec.format_code('payment_terms', snap.payment_terms_0989)}\n"
        )

    user: str | None = None
    if use_rag:
        try:
            import sppr_confluence_rag as rag_mod

            if rag_mod.index_ready():
                user, rag_hits = rag_mod.merge_rag_into_qa_prompt(
                    q, ctx, glossary_block
                )
        except Exception:
            pass

    if user is None:
        user = f"Данные из JSON:\n\n{ctx}\n\n"
        if glossary_block:
            user += f"---\n{glossary_block}\n"
        user += (
            f"---\n{extra}---\nВопрос: {q}\n\n"
            "Формат ответа: 3–8 строк markdown, таблица или список. "
            "Используй глоссарий для расшифровки терминов (КЛ, КЛ ГП, адрес ГП = роль WE и т.д.). "
            "Не повторяй все ошибки 2010 — только ответ на вопрос."
        )
    elif extra not in user:
        user = user.replace(f"---\nВопрос: {q}", f"---\n{extra}---\nВопрос: {q}", 1)

    try:
        body = call_llm_qa(QA_SYSTEM, user)
    except Exception as exc:
        return format_qa_not_found(snap.order_id, q) + f"\n\n*(LLM недоступен: {exc})*"

    if _qa_llm_body_is_unanswered(body):
        return format_qa_not_found(snap.order_id, q)

    if rag_hits:
        import sppr_confluence_rag as rag

        links = rag.format_source_links(rag_hits)
        if links and links not in body:
            return f"{links}\n\n{body}"
    return body
