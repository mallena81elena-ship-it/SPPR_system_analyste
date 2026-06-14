# -*- coding: utf-8 -*-
"""
RAG по выгрузкам Confluence (ML: TF-IDF + косинусное сходство).

Сборка индекса:
  python build_confluence_rag_index.py

Использование в Q&A: фрагменты Confluence + JSON → grounded LLM.
"""
from __future__ import annotations

import json
import pickle
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

MAT = Path(__file__).parent
SOURCES_JSON = MAT / "confluence_rag_sources.json"
INDEX_DIR = MAT / "diploma_results" / "rag"
INDEX_PKL = INDEX_DIR / "confluence_tfidf_index.pkl"
CHUNKS_JSON = INDEX_DIR / "chunks.json"
META_JSON = INDEX_DIR / "index_meta.json"

CONFLUENCE_BASE = "https://example.local/confluence?pageId="

CONFLUENCE_QA_SYSTEM = """Ты помощник L2 по интеграции заказов CRM (OMS ↔ CRM ↔ S/4).
Отвечай КРАТКО (markdown: нумерованный список или таблица).
Используй ТОЛЬКО блок «Фрагменты Confluence»; при наличии — «Данные из JSON».
Если в фрагментах нет ответа — напиши «в выгрузке Confluence не найдено».
ЗАПРЕЩЕНО: придумывать общие шаги («получение данных», «преобразование в S/4») без кодов INT-XXXX из фрагментов.
Для вопросов про цепочку INT: таблица с колонками «Исходящий INT (направление)» и «Ожидаемый ответ (откуда)» —
у исходящего укажи CRM→OMS, у ответа систему-источник: OMS→CRM или S/4→OMS (не только код INT).
Если есть блок «Глоссарий L2» — используй роли партнёров (AG/WE/ZF/ZY) и пути полей INT-0989/0992.
Запрещено отвечать только строкой «Источники» без тела ответа.
В конце одной строкой: **Источники:** pageId через запятую."""

# Спеки с цепочками INT / ЗНД (приоритет при поиске)
PRIORITY_INT_PAGE_IDS = (
    "DEMO_PAGE_ID",  # CRM_SPEC цепочка интеграционных потоков
    "DEMO_PAGE_ID",  # INT-0989
    "DEMO_PAGE_ID",  # INT-0991
    "DEMO_PAGE_ID",  # INT-2026
    "DEMO_PAGE_ID",  # INT-2025
    "DEMO_PAGE_ID",  # ПР часть 1
    "DEMO_PAGE_ID",  # INTEGRATION_CONFIG / поезда интеграции
    "DEMO_PAGE_ID",  # отчёт INT-0989 → S/4
    "DEMO_PAGE_ID",  # INT-0992 / INT-2010 OMS↔S/4
)
PRIORITY_L2_PAGE_IDS = (
    "DEMO_PAGE_ID",  # таблица взаимодействия команд L2 CRM
)

# Спецификации INT → pageId Confluence (двухэтапный retrieval)
INT_SPEC_PAGE_MAP: dict[str, str] = {
    "0989": "DEMO_PAGE_ID",
    "0992": "DEMO_PAGE_ID",
    "0991": "DEMO_PAGE_ID",
    "0993": "DEMO_PAGE_ID",
    "0973": "DEMO_PAGE_ID",
    "2010": "DEMO_PAGE_ID",
    "2026": "DEMO_PAGE_ID",
    "0970": "DEMO_PAGE_ID",
    "0827": "DEMO_PAGE_ID",
    "0972": "DEMO_PAGE_ID",
    "0990": "DEMO_PAGE_ID",
}


@dataclass
class RagHit:
    score: float
    text: str
    page_id: str
    title: str
    url: str
    source_file: str
    chunk_id: str


def _load_sources_manifest() -> dict[str, Any]:
    if SOURCES_JSON.is_file():
        return json.loads(SOURCES_JSON.read_text(encoding="utf-8"))
    return {"sources": [], "base_url": CONFLUENCE_BASE}


def discover_markdown_files() -> list[Path]:
    """Все выгрузки Confluence в папке материалов."""
    seen: set[str] = set()
    out: list[Path] = []
    for pattern in ("confluence_*.md", "_confluence_*.md"):
        for p in sorted(MAT.glob(pattern)):
            key = p.name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out


def parse_frontmatter(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    if not text.startswith("---"):
        return meta
    end = text.find("\n---", 3)
    if end < 0:
        return meta
    block = text[3:end]
    for line in block.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip("'\"")
    return meta


def _clean_chunk(s: str) -> str:
    t = re.sub(r"\[Макрос:\s*[^\]]+\]", " ", s)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def chunk_markdown(
    text: str,
    *,
    max_chars: int = 900,
    min_chars: int = 50,
) -> list[str]:
    """Разбиение страницы на фрагменты для поиска."""
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            body = text[end + 4 :]

    parts = re.split(r"\n(?=\|)", body)
    raw_blocks: list[str] = []
    buf = ""
    for part in parts:
        piece = part.strip()
        if not piece:
            continue
        if piece.startswith("|") and buf:
            buf = buf + "\n" + piece
        elif len(buf) + len(piece) + 2 <= max_chars:
            buf = (buf + "\n\n" + piece).strip() if buf else piece
        else:
            if buf:
                raw_blocks.append(buf)
            if len(piece) > max_chars * 2:
                for para in re.split(r"\n{2,}", piece):
                    if para.strip():
                        raw_blocks.append(para.strip())
                buf = ""
            else:
                buf = piece
    if buf:
        raw_blocks.append(buf)

    if not raw_blocks:
        raw_blocks = [p.strip() for p in re.split(r"\n{2,}", body) if p.strip()]

    chunks: list[str] = []
    for block in raw_blocks:
        block = _clean_chunk(block)
        if len(block) < min_chars:
            continue
        if len(block) <= max_chars:
            chunks.append(block)
            continue
        for i in range(0, len(block), max_chars):
            sub = block[i : i + max_chars].strip()
            if len(sub) >= min_chars:
                chunks.append(sub)
    return chunks


def _page_meta_for_file(path: Path, manifest: dict[str, Any]) -> tuple[str, str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(text)
    page_id = fm.get("confluence_id") or ""
    title = fm.get("title") or ""
    url = fm.get("confluence_url") or ""
    for src in manifest.get("sources") or []:
        if src.get("file") == path.name:
            page_id = page_id or str(src.get("pageId") or "")
            if not title:
                title = str(src.get("title") or "")
            break
    if not title:
        title = path.stem
    if page_id and not url:
        url = f"{manifest.get('base_url', CONFLUENCE_BASE)}{page_id}"
    return page_id, title, url


def _title_for_page_id(page_id: str) -> str:
    if not page_id:
        return ""
    manifest = _load_sources_manifest()
    for src in manifest.get("sources") or []:
        if str(src.get("pageId") or "") == page_id:
            return str(src.get("title") or "")[:200]
    return f"pageId {page_id}"


def page_url(page_id: str) -> str:
    return f"{CONFLUENCE_BASE}{page_id}" if page_id else ""


def _page_markdown_link(page_id: str, *, short: bool = False) -> str:
    """Кликабельная ссылка на страницу Confluence (markdown для UI)."""
    pid = str(page_id or "").strip()
    if not pid:
        return ""
    title = _title_for_page_id(pid)
    label = f"pageId {pid}" if short or title.startswith("pageId") else title[:90]
    return f"[{label}]({page_url(pid)})"


def _sources_line(*page_ids: str) -> str:
    links = ", ".join(_page_markdown_link(p, short=True) for p in page_ids if p)
    return f"**Источники:** {links}" if links else ""


def _int_outgoing(int_code: str, *, src: str = "CRM", dst: str = "OMS") -> str:
    """Исходящий INT с направлением (как в INTEGRATION_CONFIG)."""
    return f"**{int_code}** ({src}→{dst})"


def _int_response(int_code: str, *, src: str = "OMS", dst: str = "CRM") -> str:
    """Ожидаемый ответный INT с системой-источником."""
    return f"**{int_code}** ({src}→{dst})"


def _int_chain_table_header(*, with_step: bool = True) -> str:
    if with_step:
        return (
            "| Шаг | seq | Исходящий INT | Ожидаемый ответ | Назначение |\n"
            "| --- | --- | --- | --- | --- |"
        )
    return (
        "| seq | Исходящий INT | Ожидаемый ответ | Назначение |\n"
        "| --- | --- | --- | --- |"
    )


def _int_chain_row(
    seq: int,
    outgoing: str,
    response: str,
    purpose: str,
    *,
    step: int | None = None,
    out_src: str = "CRM",
    out_dst: str = "OMS",
    resp_src: str = "OMS",
    resp_dst: str = "CRM",
) -> str:
    out_cell = _int_outgoing(outgoing, src=out_src, dst=out_dst)
    resp_cell = _int_response(response, src=resp_src, dst=resp_dst)
    if step is not None:
        return f"| {step} | {seq} | {out_cell} | {resp_cell} | {purpose} |"
    return f"| {seq} | {out_cell} | {resp_cell} | {purpose} |"


def build_chunks() -> list[dict[str, Any]]:
    from sppr_confluence_chunking import build_spec_field_chunks, int_spec_from_path

    manifest = _load_sources_manifest()
    listed = {s.get("file"): s for s in manifest.get("sources") or [] if s.get("file")}
    files: list[Path] = []
    for name in listed:
        p = MAT / name
        if p.is_file():
            files.append(p)
    for p in discover_markdown_files():
        if p not in files:
            files.append(p)

    chunks: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        page_id, title, url = _page_meta_for_file(path, manifest)
        int_spec = int_spec_from_path(path.name)
        if not int_spec and page_id:
            for spec, pid in INT_SPEC_PAGE_MAP.items():
                if pid == page_id:
                    int_spec = spec
                    break

        spec_rows: list[dict[str, Any]] = []
        if int_spec:
            spec_rows = build_spec_field_chunks(
                text,
                int_spec=int_spec,
                page_id=page_id,
                title=title,
                url=url,
                source_file=path.name,
            )
            for row in spec_rows:
                key = (row.get("field_name") or "") + row["text"][:80]
                if key in seen_text:
                    continue
                seen_text.add(key)
                chunks.append(row)

        prose_limit = 8 if spec_rows else 9999
        for i, ch in enumerate(chunk_markdown(text)):
            if i >= prose_limit:
                break
            key = ch[:200]
            if key in seen_text:
                continue
            seen_text.add(key)
            cid = f"{page_id or path.stem}_{i}"
            chunks.append(
                {
                    "chunk_id": cid,
                    "text": ch,
                    "page_id": page_id,
                    "title": title,
                    "url": url,
                    "source_file": path.name,
                    "chunk_kind": "prose",
                    "int_spec": int_spec or "",
                }
            )
    return chunks


def build_index(*, verbose: bool = True) -> dict[str, Any]:
    """Построить TF-IDF индекс и сохранить на диск."""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError as e:
        raise RuntimeError("Установите: pip install scikit-learn") from e

    chunks = build_chunks()
    if not chunks:
        raise RuntimeError("Нет фрагментов: положите выгрузки confluence_*.md в папку материалов.")

    texts = [c["text"] for c in chunks]
    vectorizer = TfidfVectorizer(
        max_features=25000,
        ngram_range=(1, 2),
        sublinear_tf=True,
        min_df=1,
    )
    matrix = vectorizer.fit_transform(texts)

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"vectorizer": vectorizer, "matrix": matrix, "chunks": chunks}
    with INDEX_PKL.open("wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)

    CHUNKS_JSON.write_text(
        json.dumps(chunks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    n_spec = sum(1 for c in chunks if c.get("chunk_kind") == "spec_field")
    meta = {
        "n_chunks": len(chunks),
        "n_files": len({c["source_file"] for c in chunks}),
        "n_spec_field_chunks": n_spec,
        "method": "TfidfVectorizer + cosine_similarity + spec_field rows",
    }
    META_JSON.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if verbose:
        print(f"RAG: {meta['n_chunks']} фрагментов из {meta['n_files']} файлов → {INDEX_PKL}")
    return meta


@lru_cache(maxsize=1)
def _load_index() -> dict[str, Any] | None:
    if not INDEX_PKL.is_file():
        return None
    with INDEX_PKL.open("rb") as f:
        return pickle.load(f)


def index_ready() -> bool:
    return INDEX_PKL.is_file()


def _int_specs_in_question(question: str) -> list[str]:
    q = question or ""
    low = q.lower()
    found: list[str] = []
    for m in re.finditer(r"int-?0?(\d{4})", low):
        found.append(m.group(1))
    for spec in INT_SPEC_PAGE_MAP:
        if spec in low.replace("-", "") or f"int-{spec}" in low:
            found.append(spec)
    return list(dict.fromkeys(found))


def _page_ids_for_question(question: str) -> list[str]:
    """pageId Confluence для двухэтапного поиска по спецификации."""
    specs = _int_specs_in_question(question)
    pids: list[str] = []
    for spec in specs:
        pid = INT_SPEC_PAGE_MAP.get(spec)
        if pid and pid not in pids:
            pids.append(pid)
    try:
        import sppr_json_glossary as gloss

        topic = gloss.resolve_field_topic(question)
        field_topics = (
            "delivery",
            "gift_item",
            "gift_tech",
            "kl",
            "kl_phone",
            "partner_code",
            "bank",
            "carrier",
            "campaign",
            "item_type",
            "item_type_code",
            "order_status",
            "tech_ip",
            "tech_zmip",
            "tech_transit",
            "transit_field",
            "payment_type",
            "order_date",
            "znd_date",
            "delivery_enum",
            "payment_enum",
        )
        if topic in field_topics:
            for spec in ("0989", "0992"):
                pid = INT_SPEC_PAGE_MAP.get(spec)
                if pid and pid not in pids:
                    pids.append(pid)
        if topic == "znd_date":
            for spec in ("0970", "0973", "0989"):
                pid = INT_SPEC_PAGE_MAP.get(spec)
                if pid and pid not in pids:
                    pids.append(pid)
        if topic in ("zns_doc", "zvu_doc"):
            for spec in ("0989",):
                pid = INT_SPEC_PAGE_MAP.get(spec)
                if pid and pid not in pids:
                    pids.append(pid)
            if "DEMO_PAGE_ID" not in pids:
                pids.append("DEMO_PAGE_ID")
        if topic == "partners_block":
            for spec in ("0989", "0992"):
                pid = INT_SPEC_PAGE_MAP.get(spec)
                if pid and pid not in pids:
                    pids.append(pid)
        if topic == "imk_auto":
            for spec in ("0991", "0989"):
                pid = INT_SPEC_PAGE_MAP.get(spec)
                if pid and pid not in pids:
                    pids.append(pid)
    except ImportError:
        pass
    return pids


def _search_tfidf_once(
    query: str,
    *,
    top_k: int,
    min_score: float,
    page_ids: frozenset[str] | None = None,
    chunk_kinds: frozenset[str] | None = None,
) -> list[RagHit]:
    q = (query or "").strip()
    if not q:
        return []

    data = _load_index()
    if data is None:
        return []

    try:
        from sklearn.metrics.pairwise import cosine_similarity
    except ImportError:
        return []

    vectorizer = data["vectorizer"]
    matrix = data["matrix"]
    chunks: list[dict[str, Any]] = data["chunks"]

    q_vec = vectorizer.transform([q])
    scores = cosine_similarity(q_vec, matrix).ravel()
    order = scores.argsort()[::-1]

    hits: list[RagHit] = []
    for idx in order[: max(top_k * 4, top_k)]:
        sc = float(scores[idx])
        if sc < min_score:
            break
        c = chunks[int(idx)]
        pid = str(c.get("page_id") or "")
        if page_ids is not None and pid not in page_ids:
            continue
        if chunk_kinds is not None and c.get("chunk_kind") not in chunk_kinds:
            continue
        hits.append(
            RagHit(
                score=sc,
                text=c["text"],
                page_id=pid,
                title=_title_for_page_id(pid) if pid else str(c.get("title") or ""),
                url=str(c.get("url") or ""),
                source_file=str(c.get("source_file") or ""),
                chunk_id=str(c.get("chunk_id") or ""),
            )
        )
        if len(hits) >= top_k:
            break
    return hits


def _glossary_entries(question: str) -> list[dict[str, Any]]:
    try:
        import sppr_json_glossary as gloss

        return gloss.match_all_glossary(question, max_entries=8)
    except Exception:
        return []


def _expand_queries(question: str) -> list[str]:
    q = (question or "").strip()
    if not q:
        return []
    out = [q]
    low = q.lower()
    for phrase in _glossary_search_phrases_from_question(q):
        out.append(phrase)
    if any(m in low for m in ("цепочк", "int", "знд", "znd", "последователь", "start_exchange")):
        out.extend(
            [
                "INT-0989 INT-0970 INT-0973 цепочка создания ЗНД START_EXCHANGE iv_type",
                "CRM_SPEC последовательный запуск интеграционных потоков",
                "INT-0827 INT-2011 ЗНС ЗВУ INT-0989",
                "INT-0989 передача заказа OMS INT-2026",
            ]
        )
    if "s4" in low or "s/4" in low or "s4" in low.replace(" ", ""):
        out.extend(
            [
                "INT-0989 неотправленные заказа S/4 xml",
                "INT-0992 INT-2010 OMS S/4 цепочка выгрузка заказа",
                "CRM OMS S4 INT-2026 результат обработки",
            ]
        )
    if re.search(r"int-?0989", low) or (
        "0989" in low and any(m in low for m in ("для чего", "назначен", "роль", "интерфейс"))
    ):
        out.append("INT-0989 передача заказа CRM OMS INT-2026")
    if re.search(r"int-?2026", low) or (
        "2026" in low and any(m in low for m in ("для чего", "назначен", "роль"))
    ):
        out.append("INT-2026 результаты обработки OMS CRM INT-0989")
    if "zcrm_r189" in low.replace(" ", "") or (
        any(m in low for m in ("пары int", "пара int", "ожидаемый ответ"))
        and any(m in low for m in ("настраив", "zcrm", "r189", "таблиц"))
    ):
        out.extend(
            [
                "INTEGRATION_CONFIG ZCRM_V_R189_INT настройка поездов",
                "INT_RESP INT_CLOSE закрывающий интерфейс",
            ]
        )
    if any(m in low for m in ("матриц", "кто чинит", "команда l2", "l2 crm")) and any(
        m in low for m in ("интеграц", "заказ", "ошибк")
    ):
        out.extend(
            [
                "таблица взаимодействия команд CRM L2 интеграция заказов",
                "DEMO_PAGE_ID первичный анализ команда исправление",
            ]
        )
    if "iv_type" in low.replace(" ", ""):
        out.append("IV_TYPE START_EXCHANGE ZCRM_DT_R189_INT_ACTION 0989 0827 0970")
    if re.search(r"\bpi\b|sap\s*pi|pi/po", low):
        out.append("PI PO MSP CRM OMS INT-0989 INT-2026 маршрутизация")
    if any(m in low for m in ("грузополуч", "заказчик", "партнёр", "партнер", "partner")):
        out.append("PartnerFunction AG WE ZF ZY INT-0989 INT-0992")
    if re.search(r"\bкл\b", low) or "контакт" in low:
        out.append("контактное лицо ZF ZY PartnerFunction AddressName INT-0989")
    if any(m in low for m in ("цен", "zpri", "typeid", "143")):
        out.append("typeID 143 ZPRI INT-2010 цена позиции 0992 0993")
    try:
        import sppr_json_glossary as gloss

        topic = gloss.resolve_field_topic(q)
        if topic in (
            "delivery",
            "delivery_enum",
            "gift_item",
            "gift_tech",
            "bank",
            "payment_type",
            "payment_enum",
            "order_date",
            "znd_date",
            "transit_field",
            "tech_transit",
            "tech_ip",
            "carrier",
            "campaign",
            "item_type",
        ):
            for phrase in gloss.glossary_search_phrases(gloss.match_all_glossary(q, 4)):
                out.append(phrase)
        if topic == "gift_tech":
            out.extend(["techType ZZDELIV_TECH ZZ1_SPTECH технология отгрузки позиция"])
        if topic == "gift_item":
            out.extend(["itemGiftFlag ZTNN подарочная позиция"])
        if topic in ("delivery", "delivery_enum"):
            out.extend(["deliveryType ZZ1_DLVType способ доставки SHIP_METHOD"])
        if topic == "bank":
            out.extend(["partnerBankId partnerBankBikId partnerBankAccount банковские реквизиты"])
        if topic in ("payment_type", "payment_enum"):
            out.extend(["paymentType вид оплаты ВО paymentTerms условие оплаты УО"])
        if topic == "order_date":
            out.extend(["orderRegistrationDate дата оформления заказа"])
        if topic in ("znd_date",):
            out.extend(["INT-0970 INT-0973 deliveryOrders ExternalDocLastChangeDateTime ЗНД"])
        if topic in ("transit_field", "tech_transit"):
            out.extend(["techType ZTRN whseLocalCode ZZ1_TRANSIT_WERK транзит"])
        if topic == "tech_ip":
            out.extend(["techType ZIPT индивидуальная поставка ИП"])
        if topic in ("zns_doc", "zvu_doc"):
            out.extend(["INT-0827 INT-2011 ЗНС ЗВУ START_EXCHANGE iv_type"])
        if topic == "partners_block":
            out.extend(["order[].partners partnerFunction partnerId"])
    except ImportError:
        pass
    return list(dict.fromkeys(out))


def _glossary_search_phrases_from_question(question: str) -> list[str]:
    try:
        import sppr_json_glossary as gloss

        entries = gloss.match_all_glossary(question, max_entries=6)
        return gloss.glossary_search_phrases(entries)
    except Exception:
        return []


def _rerank_hits(hits: list[RagHit], question: str) -> list[RagHit]:
    if not hits:
        return hits
    low = (question or "").lower()

    def bonus(h: RagHit) -> float:
        b = 0.0
        if h.page_id in PRIORITY_INT_PAGE_IDS:
            b += 0.35
        if h.page_id in PRIORITY_L2_PAGE_IDS:
            b += 0.4
        if any(m in low for m in ("матриц", "кто чинит")) and h.page_id == "DEMO_PAGE_ID":
            b += 0.45
        text = (h.text or "").lower()
        if re.search(r"int-\d{4}", h.text or "", re.I):
            b += 0.2
        if any(m in low for m in ("знд", "znd")) and any(
            m in text for m in ("знд", "znd_create", "int-0970", "int-0973")
        ):
            b += 0.25
        if re.search(r"int-?0989", low) and h.page_id in ("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID"):
            b += 0.3
        if re.search(r"int-?2026", low) and h.page_id == "DEMO_PAGE_ID":
            b += 0.35
        if "zcrm_r189" in low.replace(" ", "") and h.page_id in ("DEMO_PAGE_ID", "DEMO_PAGE_ID"):
            b += 0.4
        if any(m in low for m in ("попадает", "выгруз", "s/4", "s4")) and h.page_id in (
            "DEMO_PAGE_ID",
            "DEMO_PAGE_ID",
            "DEMO_PAGE_ID",
        ):
            b += 0.28
        if "oms-01" in (h.title or "").lower() and "параметры заказа" in (h.title or "").lower():
            b -= 0.25
        chunk_kind = ""
        field_name = ""
        data = _load_index()
        if data:
            for c in data.get("chunks") or []:
                if str(c.get("chunk_id")) == h.chunk_id:
                    chunk_kind = str(c.get("chunk_kind") or "")
                    field_name = str(c.get("field_name") or "").lower()
                    break
        if chunk_kind == "spec_field":
            b += 0.42
            if field_name and field_name in low:
                b += 0.35
        try:
            import sppr_json_glossary as gloss

            if gloss.is_int_field_lookup_question(question) and chunk_kind == "spec_field":
                b += 0.3
            intent = gloss.classify_confluence_intent(question)
            if intent in ("field_lookup", "enum_delivery", "enum_payment"):
                if h.page_id == "DEMO_PAGE_ID":
                    b += 0.25
                if h.page_id in ("DEMO_PAGE_ID",) and chunk_kind != "spec_field":
                    b -= 0.4
                if any(m in low for m in ("технолог", "techtype", "sptech", "zzdeliv")):
                    if h.page_id == "DEMO_PAGE_ID" or "int-2026" in (h.title or "").lower():
                        b -= 0.55
                if field_name and field_name in low:
                    b += 0.4
        except ImportError:
            pass
        return b

    return sorted(hits, key=lambda h: h.score + bonus(h), reverse=True)


def _hits_for_page_ids(page_ids: tuple[str, ...], *, max_chunks: int = 4) -> list[RagHit]:
    data = _load_index()
    if not data:
        return []
    chunks: list[dict[str, Any]] = data["chunks"]
    out: list[RagHit] = []
    for pid in page_ids:
        n = 0
        for c in chunks:
            if str(c.get("page_id") or "") != pid:
                continue
            text = str(c.get("text") or "")
            if "INT-" not in text and pid not in ("DEMO_PAGE_ID", *PRIORITY_L2_PAGE_IDS):
                continue
            out.append(
                RagHit(
                    score=0.99 - n * 0.01,
                    text=text,
                    page_id=pid,
                    title=_title_for_page_id(pid),
                    url=str(c.get("url") or f"{CONFLUENCE_BASE}{pid}"),
                    source_file=str(c.get("source_file") or ""),
                    chunk_id=str(c.get("chunk_id") or ""),
                )
            )
            n += 1
            if n >= max_chunks:
                break
    return out


def search_confluence(
    query: str,
    *,
    top_k: int = 4,
    min_score: float = 0.06,
) -> list[RagHit]:
    """Поиск релевантных фрагментов Confluence (TF-IDF + расширение запроса)."""
    queries = _expand_queries(query)
    if not queries:
        return []

    merged: dict[str, RagHit] = {}
    per_q = max(3, top_k)
    target_pids = _page_ids_for_question(query)
    pid_set = frozenset(target_pids) if target_pids else None

    if pid_set:
        for q in queries:
            for h in _search_tfidf_once(
                q,
                top_k=per_q + 2,
                min_score=min_score * 0.7,
                page_ids=pid_set,
            ):
                h.score = min(1.0, h.score + 0.12)
                prev = merged.get(h.chunk_id)
                if prev is None or h.score > prev.score:
                    merged[h.chunk_id] = h
        for q in queries[:3]:
            for h in _search_tfidf_once(
                q,
                top_k=2,
                min_score=min_score * 0.9,
                page_ids=pid_set,
                chunk_kinds=frozenset({"spec_field"}),
            ):
                h.score = min(1.0, h.score + 0.2)
                prev = merged.get(h.chunk_id)
                if prev is None or h.score > prev.score:
                    merged[h.chunk_id] = h

    for q in queries:
        for h in _search_tfidf_once(q, top_k=per_q, min_score=min_score * 0.85):
            prev = merged.get(h.chunk_id)
            if prev is None or h.score > prev.score:
                merged[h.chunk_id] = h

    if is_int_chain_question(query):
        for h in _hits_for_page_ids(PRIORITY_INT_PAGE_IDS[:6], max_chunks=2):
            merged[h.chunk_id] = h
    low_q = (query or "").lower()
    if any(m in low_q for m in ("s/4", "s4", "попадает", "выгруз")):
        for h in _hits_for_page_ids(("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID"), max_chunks=2):
            merged[h.chunk_id] = h
    if re.search(r"int-?0989", low_q) or re.search(r"int-?2026", low_q):
        for h in _hits_for_page_ids(("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID"), max_chunks=2):
            merged[h.chunk_id] = h
    if "zcrm_r189" in low_q.replace(" ", "") or (
        "r189" in low_q and "int" in low_q and any(m in low_q for m in ("настраив", "пары", "таблиц"))
    ):
        for h in _hits_for_page_ids(("DEMO_PAGE_ID", "DEMO_PAGE_ID"), max_chunks=3):
            merged[h.chunk_id] = h
    if is_l2_matrix_question(query):
        for h in _hits_for_page_ids(PRIORITY_L2_PAGE_IDS, max_chunks=3):
            merged[h.chunk_id] = h

    hits = _rerank_hits(list(merged.values()), query)
    return hits[:top_k]


def is_int_chain_question(question: str) -> bool:
    q = (question or "").lower()
    return any(
        m in q
        for m in (
            "цепочк",
            "последователь",
            "int-",
            "интеграционн",
            "start_exchange",
        )
    )


def is_l2_matrix_question(question: str) -> bool:
    q = (question or "").lower()
    return any(m in q for m in ("матриц", "кто чинит", "команда")) and any(
        m in q for m in ("l2", "интеграц", "заказ", "ошибк", "чинит")
    )


def is_znd_creation_question(question: str) -> bool:
    """Создание / цепочка ЗНД — не путать с отменой/изменением."""
    q = (question or "").lower()
    if not any(m in q for m in ("знд", "znd")):
        return False
    if any(m in q for m in ("отмен", "изменен", "изменени", "отмена")):
        return False
    return any(
        m in q
        for m in (
            "создан",
            "цепочк",
            "последователь",
            "int",
            "выгруз",
            "iv_type",
            "start_exchange",
        )
    )


def _iv_type_in_question(question: str) -> str | None:
    q = (question or "").lower().replace(" ", "")
    m = re.search(r"iv_type[=:]?([1-8])", q)
    return m.group(1) if m else None


def template_int_chain_znd_answer() -> str:
    """Ответ по спеке DEMO_PAGE_ID (без LLM) — iv_type=1, создание ЗНД."""
    hdr = _int_chain_table_header(with_step=True)
    r1 = _int_chain_row(
        1, "INT-0989", "INT-2026", "Передача/изменение заказа **CRM → OMS**", step=1
    )
    r2 = _int_chain_row(
        2, "INT-0970", "INT-0973", "Запрос на **создание ЗНД** в OMS", step=2
    )
    return f"""**Цепочка INT при создании ЗНД из CRM** (метод `START_EXCHANGE`, `iv_type=1` — только ЗНД; зона **L2 CRM** + OMS):

{hdr}
{r1}
{r2}

Закрывающий для **INT-0970** — **INT-0989** (таблица `INTEGRATION_CONFIG` / представление `ZCRM_V_R189_INT`).

**Связь с S/4:** выгрузка заказа в S/4 — отдельная ветка через **INT-0989** (xml в OMS → **INT-0992** → S/4 → **INT-2010** → **INT-2026**); **0970/0973** — только ЗНД в OMS.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_int_chain_iv_type3_answer() -> str:
    hdr = _int_chain_table_header(with_step=False)
    rows = "\n".join(
        [
            _int_chain_row(1, "INT-0989", "INT-2026", "Заказ **CRM → OMS** (и далее в S/4)"),
            _int_chain_row(2, "INT-0970", "INT-0973", "Создание **ЗНД**"),
            _int_chain_row(3, "INT-0827", "INT-2011", "Создание **ЗНС/ЗВУ**"),
        ]
    )
    return f"""**Цепочка INT при создании ЗНД и ЗНС/ЗВУ** (`START_EXCHANGE`, **`iv_type=3`**):

{hdr}
{rows}

Закрывающий для **INT-0970** и **INT-0827** — **INT-0989**.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_iv_type2_answer() -> str:
    hdr = _int_chain_table_header(with_step=False)
    rows = "\n".join(
        [
            _int_chain_row(1, "INT-0989", "INT-2026", "Заказ CRM → OMS"),
            _int_chain_row(2, "INT-0827", "INT-2011", "Создание **ЗНС/ЗВУ** (без ЗНД)"),
        ]
    )
    return f"""**`iv_type=2` в `START_EXCHANGE`** — только **ЗНС/ЗВУ** (без создания ЗНД):

{hdr}
{rows}

{_sources_line("DEMO_PAGE_ID")}"""


def template_znd_modify_cancel_answer() -> str:
    return f"""**Изменение / отмена ЗНД** (не создание):

| Исходящий INT | Ожидаемый ответ | Назначение |
| --- | --- | --- |
| {_int_outgoing("INT-0972")} | {_int_response("INT-0997")} | Запрос на изменение/отмену ЗНД |

В `IV_TYPE` метода `START_EXCHANGE`: **5** — изменение ЗНД, **6** — отмена ЗНД.

{_sources_line("DEMO_PAGE_ID")}"""


def template_int_0989_answer() -> str:
    return f"""**Назначение INT-0989** (исходящий из CRM):

| | |
| --- | --- |
| **Исходящий** | {_int_outgoing("INT-0989")} — передача последней версии заказа при сохранении в **OMS** (далее OMS маршрутизирует в **S/4**) |
| **Ожидаемый ответ** | {_int_response("INT-2026")} — результат обработки заказа/изменений в OMS (в т.ч. после ответа **S/4** по **INT-2010**) |
| **Настройка пары** | Таблица **`INTEGRATION_CONFIG`**: `INT_TYPE=INT-0989`, `INT_RESP=INT-2026` |

Если заказ «не ушёл в S/4» — проверять xml по **INT-0989** и отчёт по неотправленным — {_page_markdown_link("DEMO_PAGE_ID")}.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_int_2026_answer() -> str:
    return f"""**Назначение INT-2026** (входящий в CRM):

| | |
| --- | --- |
| **Триггер** | Ответ **OMS → CRM** на обработку изменений, полученных по **INT-0989** |
| **Код интерфейса** | {_int_response("INT-2026")} |
| **Связь с S/4** | После цепочки **INT-0992** (OMS→S/4) и **INT-2010** (**S/4→OMS**) OMS передаёт итог в CRM через **INT-2026** |
| **Данные в CRM** | Таблица `Z_OMS_SO_TRANSFER` / `ZCRM_OMS_SO_TNSR` — признак передачи заказа в OMS и S/4 |

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_zcrm_r189_int_answer() -> str:
    return f"""**Где в CRM настраиваются пары INT и ожидаемые ответы:**

1. Таблица **`INTEGRATION_CONFIG`** (настройка поездов интеграции), просмотр **`ZCRM_V_R189_INT`** — поля **`INT_TYPE`** (исходящий INT), **`INT_RESP`** (ожидаемый ответ), **`INT_CLOSE`** (закрывающий INT), таймауты.
2. Метод запуска цепочки — **`START_EXCHANGE`** в классе **`ZCL_CRM_R189_INT_UTILS`** (`IV_TYPE` задаёт сценарий: ЗНД, ЗНС, сохранение заказа и т.д.).
3. Примеры пар (направление → ответ):

| Исходящий INT | Ожидаемый ответ | Закрывающий | Назначение |
| --- | --- | --- | --- |
| {_int_outgoing("INT-0989")} | {_int_response("INT-2026")} | — | Передача заказа CRM→OMS |
| {_int_outgoing("INT-0970")} | {_int_response("INT-0973")} | INT-0989 | Создание ЗНД |
| {_int_outgoing("INT-0827")} | {_int_response("INT-2011")} | INT-0989 | Создание ЗНС/ЗВУ |
| {_int_outgoing("INT-0972")} | {_int_response("INT-0997")} | — | Изменение/отмена ЗНД |

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_crm_to_s4_answer() -> str:
    return f"""**Как заказ из CRM попадает в S/4** (зона L2: CRM + OMS; S/4 — конечная система учёта):

| Шаг | Интерфейс | Направление | Ожидаемый ответ / роль |
| --- | --- | --- | --- |
| 1 | **INT-0989** | CRM→OMS | {_int_response("INT-2026")} — итог обработки в CRM после цепочки |
| 2 | **INT-0992** | OMS→S/4 | Создание/изменение заказа в **S/4** (асинхронно) |
| 3 | **INT-2010** | S/4→OMS | {_int_response("INT-2010", src="S/4", dst="OMS")} — признак создания/ошибки в S/4 |
| 4 | **INT-2026** | OMS→CRM | Результат для оператора CRM (в т.ч. после **INT-2010**) |

Прямого вызова CRM→S/4 нет: **CRM → OMS → S/4 → OMS → CRM**.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_s4_chain_0989_0992_2010_answer() -> str:
    return f"""**Цепочка выгрузки заказа в S/4** (после сохранения в CRM):

| Шаг | Интерфейс | Направление | Ожидаемый ответ / роль |
| --- | --- | --- | --- |
| 1 | **INT-0989** | CRM→OMS | Передача заказа в OMS (через **PI/PO**) |
| 2 | **INT-0992** | OMS→S/4 | Создание/изменение заказа в **S/4** |
| 3 | **INT-2010** | S/4→OMS | {_int_response("INT-2010", src="S/4", dst="OMS")} — результат/ошибка в S/4 |
| 4 | **INT-2026** | OMS→CRM | {_int_response("INT-2026")} — итог для оператора CRM |

**PI/PO** — транспорт между CRM, OMS и S/4 (не отдельный «ответный INT» в CRM, а middleware).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_sap_pi_answer() -> str:
    return f"""**Роль PI/PO в цепочке обмена заказа (L2 CRM):**

| Участок | Кто вызывает | Через PI | Куда |
| --- | --- | --- | --- |
| Сохранение заказа | **CRM** | **INT-0989** | **OMS-05** (далее S/4) |
| Выгрузка в S/4 | **OMS** | **INT-0992** | **S/4** |
| Ответ S/4 | **S/4** | **INT-2010** | **OMS** |
| Итог в CRM | **OMS** | **INT-2026** | **CRM** |

PI/PO не подменяет коды INT: в журнале CRM смотрите пары **INT_TYPE / INT_RESP** из `INTEGRATION_CONFIG`, а маршрутизацию сообщений — на стороне PI/OMS.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_partner_roles_answer(question: str) -> str | None:
    try:
        import sppr_json_glossary as gloss
    except ImportError:
        return None
    role = gloss.detect_partner_role(question)
    entries = gloss.match_all_glossary(question, max_entries=6)
    by_role: dict[str, dict] = {}
    for e in entries:
        r = e.get("role_s4")
        if r:
            by_role[str(r)] = e

    def _row(r: str, title: str) -> str:
        e = by_role.get(r) or {}
        p99 = e.get("json_path_0992") or "—"
        p98 = e.get("json_path_0989") or "—"
        return (
            f"| **{r}** ({title}) | `{p99}` | `{p98}` | "
            f"роль партнёра в **INT-0992** / блок partners в **INT-0989** |"
        )

    lines = [
        "**Партнёры в CRM и в INT (глоссарий L2):**",
        "",
        "| Роль S/4 | Кто это для L2 | INT-0992 (типовой путь) | INT-0989 |",
        "| --- | --- | --- | --- |",
        _row("AG", "заказчик"),
        _row("WE", "грузополучатель / адрес доставки"),
        _row("ZF", "контактное лицо ГП (КЛ ГП)"),
        _row("ZY", "контактное лицо заказчика (КЛ)"),
    ]
    if role:
        lines.insert(
            2,
            f"По формулировке вопроса акцент на роли **{role}**.",
        )
    lines.append("")
    lines.append(
        "**Отличие:** **WE** — адрес/получатель товара; **AG** — заказчик сделки; "
        "**ZF** — контакт **у ГП**, **ZY** — контакт **у заказчика** (не путать с адресом WE)."
    )
    lines.append("")
    lines.append(_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID"))
    return "\n".join(lines)


def template_kl_in_int_answer() -> str:
    return f"""**Где в INT передаются данные по контактному лицу (КЛ):**

| Кого | Роль | INT-0992 (OMS) | INT-0989 (CRM→OMS) |
| --- | --- | --- | --- |
| КЛ заказчика | **ZY** | `Order.Partner[PartnerFunction=ZY].Address.AddressName` (+ телефон/e-mail в Communication) | `order[].partners[partnerFunction=00000015]` |
| КЛ грузополучателя (КЛ ГП) | **ZF** | `Order.Partner[PartnerFunction=ZF].Address.AddressName` | `order[].partners[partnerFunction=Z002]` |

ФИО часто дробится на **AddressName**, **AddressAdditionalName**, **AddressName3** (по 40 символов).  
**Грузополучатель (WE)** — отдельная роль: адрес доставки, не путать с **ZF** (контакт ГП).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_kl_gp_phone_int_answer() -> str:
    return f"""**Телефон КЛ грузополучателя (КЛ ГП) в INT:**

| Уровень | INT-0989 (CRM→OMS) | INT-0992 (OMS→S/4) |
| --- | --- | --- |
| Партнёр **ZF** (контакт ГП) | в блоке `partners[partnerFunction=Z002]` — телефон партнёра (если заполнен в CRM) | `Order.Partner[PartnerFunction=ZF].Address.MobilePhoneNumber` |
| Позиция / наряд доставки | `order[].items[].receiverContactPhone` — **КЛ ГП: телефон** (обязательное при передаче контакта на позиции) | для чека иногда `Partner[ZY]` / позиционные поля — см. спецификацию |

Для L2 в JSON заказа CRM смотрите **receiverContactPhone** на позиции и партнёра с **partnerFunction=Z002** / роль **ZF** в **0992**.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_delivery_type_int_answer() -> str:
    return f"""**Способ доставки в INT из CRM (уровень позиции заказа):**

| INT | Поле JSON | Источник в CRM |
| --- | --- | --- |
| **INT-0989** (CRM→OMS) | `order[].items[].deliveryType` | способ доставки на позиции |
| **INT-0992** (OMS→S/4) | `Order.Item.ZZ1_DLVType` | преобразуется из **deliveryType** позиции |

Не путать с цепочкой **CRM→OMS→S/4** (это маршрут интеграции, а не имя поля).  
Связанные поля: `deliveryInterval`, `requestedDate` — интервал и дата доставки на той же позиции.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_carrier_int_answer() -> str:
    return f"""**Перевозчик из CRM в INT (уровень позиции):**

| INT | Поле JSON | Смысл |
| --- | --- | --- |
| **INT-0989** (CRM→OMS) | `order[].items[].carrier` | код перевозчика (SHIPPER_CODE в CRM) |
| **INT-0992** (OMS→S/4) | см. маппинг позиции в спецификации 0992 | передаётся в составе позиции заказа |

Не путать с цепочкой интеграции **CRM→OMS→S/4** — это маршрут, а не имя поля.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_campaign_int_answer() -> str:
    return f"""**Номер / id маркетинговой акции на позиции в INT-0989:**

| Поле JSON | Назначение | Когда заполняется |
| --- | --- | --- |
| `order[].items[].campaignId` | id ценовой акции (внешний id из CRM) | для строк с **itemType=ZTNN** (подарочная/акционная позиция) |
| `order[].items[].campaignCoId` | номер **СО-заказа акции** | также при **ZTNN**, из связки CGPL / ZTD_CPG_OAU |

В **INT-0992** (OMS→S/4) для акции используются поля уровня позиции S/4, напр. **ZZPromoID**, **ZZCOOrderPromo** (см. спецификацию 0992).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_item_type_int_answer() -> str:
    return f"""**Тип позиции из CRM в INT:**

| INT | Поле JSON | Источник CRM |
| --- | --- | --- |
| **INT-0989** | `order[].items[].itemType` | **ITEM_TYPE** / `ET_ORDERADM_I-ITM_TYPE` (напр. ZTA1, ZTNN) |
| **INT-0992** | категория позиции **Order.Item** | преобразование из **itemType** |

Примеры значений **itemType**: **ZTNN** — подарочная/акционная позиция; **ZTA1** — типовая товарная позиция (см. таблицу INT-0989).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_item_type_code_answer(code: str) -> str:
    code_u = (code or "").upper()
    note = ""
    if code_u == "ZTIN":
        note = (
            "\n\nВ спецификации INT-0989 для подарочных/акционных строк обычно указан код **ZTNN**, "
            "не ZTIN — проверьте **ITM_TYPE** в CRM по позиции."
        )
    if code_u == "ZTNN":
        note = (
            "\n\n**ZTNN** — тип позиции «подарок/акция»: заполняются `itemGiftFlag`, "
            "`campaignId`, `campaignCoId` (см. INT-0989)."
        )
    return f"""**Код типа позиции `{code_u}` в INT:**

| Уровень | Где в JSON INT-0989 | Смысл |
| --- | --- | --- |
| Позиция | `order[].items[].itemType` = `{code_u}` | тип позиции CRM (**ITEM_TYPE**) |
| Признак подарка | `order[].items[].itemGiftFlag` | X, если **itemType=ZTNN** |

Расшифровка бизнес-смысла кода — в справочнике типов позиций CRM (**ITM_TYPE**), не в имени поля JSON.{note}

{_sources_line("DEMO_PAGE_ID")}"""


def template_order_status_code_answer(code: str) -> str:
    code_u = (code or "").upper()
    meanings = {
        "E0001": "Открыто (статус **позиции** в CRM; на заголовке в других схемах может быть «Создана» — уточняйте уровень)",
        "E0004": "Передана в закупку / Закрыто (зависит от объекта — позиция vs входящая почта)",
        "E0025": "пример значения **orderStatus** в INT-0989 (статус заголовка заказа)",
    }
    meaning = meanings.get(code_u, "см. статусную схему CRM / спецификацию OMS по коду")
    return f"""**Статус `{code_u}` в контексте заказа CRM / INT:**

| Уровень | Поле JSON (INT-0989) | Комментарий |
| --- | --- | --- |
| Заголовок заказа | `order[].orderStatus` | статус заказа из CRM (`ls_status-status`, пример в спеке: E0025) |
| Позиция | пользовательский статус позиции в CRM | коды **E0001**, **E0004** и др. — в статусной схеме **позиции**, не в `orderStatus` |

**По коду {code_u}:** {meaning}

Для L2: уточните, спрашивают про **заголовок** (`orderStatus`) или **статус строки** в CRM.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_gift_position_int_answer() -> str:
    return f"""**Поля позиции «подарок» / бесплатная маркетинговая позиция в INT:**

| Назначение | INT-0989 (CRM→OMS) | INT-0992 (OMS→S/4) | CRM |
| --- | --- | --- | --- |
| **Признак подарочной позиции** | `order[].items[].itemGiftFlag` (= X при **itemType=ZTNN**) | `Order.Item` с категорией **ZTNN** | тип позиции ZTNN |
| **Технология отгрузки на позиции** (не «признак подарка») | `order[].items[].techType` | `Order.Item.ZZ1_SPTECH` | **ZZDELIV_TECH** |

Если вопрос про **тип технологии по позиции** (отгрузка со склада, транзит, МИП и т.д.) — это **`techType` / `ZZ1_SPTECH`**, а не `itemGiftFlag`.  
Подарочная **карта** — отдельный блок `giftCards[]` / условие **ZLO1**, не путать с подарочной **позицией** товара.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_partner_crm_code_answer(code: str) -> str | None:
    try:
        import sppr_json_glossary as gloss

        cat = gloss.load_catalog()
    except Exception:
        return None
    mapping = cat.get("partner_crm_codes_0989") or {}
    role = mapping.get(code.upper())
    if not role:
        return None
    roles_ru = (cat.get("partner_roles_s4") or {}).get(role, role)
    p98 = f"order[].partners[partnerFunction={code}]"
    for g in cat.get("glossary") or []:
        if g.get("role_s4") == role and g.get("json_path_0989"):
            p98 = g["json_path_0989"]
            break
    p99 = f"Order.Partner[PartnerFunction={role}]"
    return f"""**Код partnerFunction в INT-0989: `{code}`**

| CRM (0989) | S/4 роль | INT-0992 |
| --- | --- | --- |
| `{p98}` | **{role}** — {roles_ru} | `{p99}` |

В **INT-0989** передаётся CRM-код функции партнёра; в **INT-0992** — роль S/4 (**AG**, **WE**, **ZF**, **ZY** и др.) после преобразования по таблице спецификации.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_int_0827_zns_answer() -> str:
    return f"""**INT-0827 и INT-2011 (ЗНС / ЗВУ):**

| Исходящий INT | Ожидаемый ответ | Закрывающий | Назначение |
| --- | --- | --- | --- |
| {_int_outgoing("INT-0827")} | {_int_response("INT-2011")} | INT-0989 | Создание **ЗНС** или **ЗВУ** (сборка) |

Запуск в CRM — **START_EXCHANGE** с **IV_TYPE=2** (или 3 в комбинированном сценарии). Настройка пар — таблица **INTEGRATION_CONFIG**.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_partners_block_int_answer() -> str:
    return f"""**Партнёры заказа в INT (не `assemblyStatus`):**

| Уровень | Структура JSON | Смысл |
| --- | --- | --- |
| **INT-0989** | `order[].partners[]` | блок партнёров CRM |
| Ключ роли | `partners[].partnerFunction` | CRM-код: `00000001` (AG), `00000002` (WE), `Z002` (ZF), `00000015` (ZY), `ZCOMPANYCC` … |
| Идентификатор | `partners[].partnerId` | BP партнёра |
| Адрес / реквизиты | `partners[].addresses[]`, `partnerBankId`, … | адрес доставки, банк |

**INT-0992** — `Order.Partner[]` с ролями S/4 (**AG**, **WE**, **ZF**, **ZY**).

Не путать с **`order[].items[].assemblyStatus`** — это **сборка мебели** («партнёр желает сборку»), не роль партнёра сделки.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_zns_doc_answer() -> str:
    return f"""**ЗНС (заказ на сборку) — для L2 CRM:**

| Вопрос | Ответ |
| --- | --- |
| **Что это** | отдельный логистический документ **ЗНС** (сборка), не поле заголовка заказа |
| **Цепочка INT** | {_int_outgoing("INT-0827")} → {_int_response("INT-2011")} (закрывающий **INT-0989**) |
| **Запуск в CRM** | **START_EXCHANGE**, **IV_TYPE=2** (только ЗНС/ЗВУ) или **3** (ЗНД+ЗНС) |
| **Поле в JSON заказа 0989?** | **Нет** отдельного `order[].zns` — передаётся **запрос на создание документа** в OMS, ответ в **INT-2011** |

Услуги сборки в ЗНС в S/4 — тип позиции **ZSF** (см. спецификацию OMS/S/4). Для «в каком поле» уточните: **роль партнёра**, **позиция заказа** или **документ ЗНС** — это разные объекты.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_zvu_doc_answer() -> str:
    return f"""**ЗВУ (вывоз упаковки) — для L2 CRM:**

| Вопрос | Ответ |
| --- | --- |
| **Что это** | услуга **«вывоз упаковки»** (документ **ЗВУ** / заявка на услугу в составе сценария ЗНС) |
| **Цепочка INT** | тот же поток, что **ЗНС**: {_int_outgoing("INT-0827")} → {_int_response("INT-2011")} |
| **Поле в `order[]` INT-0989?** | **Нет** отдельного поля «ЗВУ» в JSON заказа — создание через **INT-0827**, не через таблицу полей **0989** |

Смотрите журнал **INTEGRATION_CONFIG**, сценарий **IV_TYPE=2/3**, карточку услуги/ЗНС в OMS.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_tech_type_ip_answer() -> str:
    return f"""**Тип технологии «ИП» (индивидуальная поставка) на позиции:**

| Уровень | Поле JSON | Примечание |
| --- | --- | --- |
| INT-0989 | `order[].items[].techType` | Код технологии отгрузки из CRM **ZZDELIV_TECH** |
| INT-0992 | `Order.Item.ZZ1_SPTECH` | То же в S/4 |

Для **ИП** в логистике Company обычно код семейства **ZIPT** (индивидуальная поставка) — сверьте значение в позиции заказа CRM и в справочнике технологий.  
Не путать с **itemGiftFlag** (подарочная позиция ZTNN).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_tech_type_zmip_answer() -> str:
    return f"""**Технология «дозакупка» на позиции заказа:**

| Уровень | Поле JSON | Примечание |
| --- | --- | --- |
| INT-0989 | `order[].items[].techType` | **ZZDELIV_TECH** → JSON `techType` |
| INT-0992 | `Order.Item.ZZ1_SPTECH` | Технология отгрузки в S/4 |

Типовой код технологии дозакупки — **ZMIP** (материал закупается по запросу после ЗНД). Проверка доступности — OMS/резерв.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_payment_type_field_answer() -> str:
    return f"""**ВО (вид оплаты) в INT из CRM:**

| INT | Поле JSON | CRM |
| --- | --- | --- |
| **INT-0989** (→ OMS) | `order[].paymentType` | `ls_pricing-payment_method` (PAYMENT_METHOD) |
| **INT-0991** (ИМК→CRM) | `order[].paymentType` | то же |
| **INT-0992** (→ S/4) | `Order.PaymentMethod` | преобразование для S/4 |

**ВО** — **вид оплаты**, не путать с **УО** (`order[].paymentTerms` — условие оплаты).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_payment_terms_field_answer() -> str:
    return f"""**УО (условие оплаты) в INT из CRM:**

| INT | Поле JSON | CRM |
| --- | --- | --- |
| **INT-0989** (→ OMS) | `order[].paymentTerms` | `ls_pricing-pmnttrms` (PMNTTRMS), CHAR4 |
| **INT-0991** (ИМК→CRM) | `order[].paymentTerms` | то же |
| **INT-0992** (→ S/4) | `Order.ZZ1_TERM_CODE` | «Условия платежа CRM» |

Пример кода **Я** — **Отгрузка по заявке** (справочник OMS-01).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_payment_type_values_answer() -> str:
    try:
        from sppr_crm_abbrev_glossary import format_payment_type_enum

        return format_payment_type_enum(_sources_line)
    except ImportError:
        pass
    return f"""**Коды ВО (вид оплаты / `paymentType`):**

| Код | Смысл (OMS-01 / CRM) |
| --- | --- |
| **1** | безнал |
| **3** | ККА |
| **4** | Н/c (в S/4 → 1) |

Поле: **`order[].paymentType`**; S/4: **`Order.PaymentMethod`**.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_payment_terms_values_answer() -> str:
    try:
        from sppr_crm_abbrev_glossary import format_payment_terms_enum

        return format_payment_terms_enum(_sources_line)
    except ImportError:
        pass
    return f"""**Коды УО (условие оплаты / `paymentTerms`):**

| Код | Смысл |
| --- | --- |
| **Я** | Отгрузка по заявке |
| **W** | Онлайн-платёж |
| **N** | Оплата по факту поставки |
| **O** | Предоплата |
| **A** | Самовывоз |

Поле: **`order[].paymentTerms`**.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_payment_vo_vs_uo_answer() -> str:
    try:
        from sppr_crm_abbrev_glossary import format_payment_vo_vs_uo

        return format_payment_vo_vs_uo(_sources_line)
    except ImportError:
        return template_payment_type_field_answer() + "\n\n" + template_payment_terms_field_answer()


def template_delivery_type_values_answer() -> str:
    return f"""**Способы / виды доставки при передаче по INT (уровень позиции):**

| Уровень | Где задаётся | Комментарий |
| --- | --- | --- |
| **INT-0989** | `order[].items[].deliveryType` | код **SHIP_METHOD** из CRM (CHAR2, напр. **99**) |
| **INT-0992** | `Order.Item.ZZ1_DLVType` | преобразование из **deliveryType** |

Перечень допустимых кодов и маппинг CRM→OMS→S/4 — в спецификации **INT-0992**, раздел **«Таблица преобразования значений поля Позиция. deliveryType»** (не путать с маркетинговыми названиями «курьер/самовывоз» без кода).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_bank_details_int_answer() -> str:
    return f"""**Банковские реквизиты партнёра в INT-0989 (CRM→OMS):**

| Поле JSON | Назначение |
| --- | --- |
| `order[].partners[].partnerBankId` | ID банковских реквизитов (заказчик / плательщик / ГП / ЮЛ) |
| `order[].partners[].partnerBankBikId` | БИК |
| `order[].partners[].partnerBankAccount` | номер расчётного счёта |
| `order[].partners[].partnerBankS4Indicator` | признак «реквизит S/4» |

Набор полей зависит от **partnerFunction** (00000001 заказчик, 00000004 плательщик, 00000002 ГП, **ZCOMPANYCC** и т.д.) — см. логику начитки в **INT-0989**.

{_sources_line("DEMO_PAGE_ID")}"""


def template_transit_field_int_answer() -> str:
    return f"""**Транзит в INT — какие поля смотреть L2:**

| Назначение | INT-0989 (CRM→OMS) | INT-0992 (OMS→S/4) |
| --- | --- | --- |
| **Технология «транзит»** | `order[].items[].techType` = **ZTRN** / **ZIPT** / **ZMPT** | `Order.Item.ZZ1_SPTECH` |
| **Локальный склад / транзитный узел** | `order[].items[].whseLocalCode` | `Order.Item.ZZ1_TRANSIT_WERK` (код склада транзита) |
| **Способ доставки** (при транзитном самовывозе) | `order[].items[].deliveryType` | `Order.Item.ZZ1_DLVType` |

Если вопрос «**в каком поле передаётся транзит**» — уточните: **тип технологии** (`techType`) или **склад транзита** (`whseLocalCode` / **ZZ1_TRANSIT_WERK**).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_transit_tech_type_answer() -> str:
    return f"""**Тип технологии для транзита на позиции:**

| Код CRM (**ZZDELIV_TECH** → JSON) | Кратко |
| --- | --- |
| **ZTRN** | транзитная отгрузка |
| **ZIPT** | ИП + транзит |
| **ZMPT** | МИП + транзит |

Поле в **INT-0989**: `order[].items[].techType`; в **INT-0992**: `Order.Item.ZZ1_SPTECH`.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_tech_type_enum_answer() -> str:
    return f"""**Типы технологий отгрузки/поставки в INT из CRM (позиция заказа):**

| Код **ZZDELIV_TECH** → JSON `techType` | Кратко |
| --- | --- |
| **ZTAN** / отгрузка со склада СОХ | стандартная отгрузка |
| **ZTRN** | транзит |
| **ZIPT** | ИП + транзит |
| **ZMIP** / **ZMPT** | МИП (+ транзит) |
| **ZIP** | индивидуальная поставка (ИП) |
| **ZPP** | прямая поставка |
| **ZAB** | дозакупка |

| Уровень | Поле |
| --- | --- |
| **INT-0989** (CRM→OMS) | `order[].items[].techType` |
| **INT-0992** (OMS→S/4) | `Order.Item.ZZ1_SPTECH` |

Это **не** `itemGiftFlag` и **не** `deliveryType` (способ доставки). Полный справочник кодов — Confluence **DEMO_PAGE_ID** (технологии OMS).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_warehouse_shipment_answer() -> str:
    return f"""**Склад отгрузки / локальный склад на позиции в INT:**

| Назначение | INT-0989 (CRM→OMS) | INT-0992 (OMS→S/4) |
| --- | --- | --- |
| **Локальный склад CRM** | `order[].items[].whseLocalCode` (иногда **whseCode** в выгрузке) | `Order.Item.Plant` |
| **Склад транзита** | `order[].items[].whseLocalCode` (при транзите) | `Order.Item.ZZ1_TRANSIT_WERK` |

Для L2 в JSON исходящего **0989** смотрите **whseLocalCode** на позиции; **Plant** — уже в потоке **0992** в S/4.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_item_position_int_answer() -> str:
    return f"""**Позиция заказа в INT-0989 — блок `order[].items[]` (не документ ЗНС):**

| Поле JSON | Смысл |
| --- | --- |
| `order[].items[]` | массив позиций заказа CRM |
| `items[].entryOmsGuid` / **item_guid** | GUID строки в OMS |
| `items[].productId` | артикул (**ORDERED_PROD**) |
| `items[].itemType` | тип позиции CRM (**ITM_TYPE**: ZTA1, ZTNN, …) |
| `items[].techType` | технология отгрузки (**ZZDELIV_TECH**) |
| `items[].deliveryType` | способ доставки на позиции |

**ЗНС/ЗВУ** — отдельные логистические документы OMS, не строка в `order[].items[]`.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_tek_order_answer() -> str:
    return f"""**Как понять, что заказ / позиция — доставка через ТЭК (L2 CRM):**

| Признак | Где в INT / CRM |
| --- | --- |
| **Способ доставки** | `order[].items[].deliveryType` = **90** («Доставка ТЭК») → **0992**: `Order.Item.ZZ1_DLVType` |
| **Перевозчик** | `order[].items[].carrier` — код перевозчика (SHIPPER) |
| **Тип перевозчика** | домен **ZDT_CARRIER_TYPE**: **EXTERNAL_TEK** (внешняя ТЭК) |
| **Статус позиции CRM** | **E0022** «Передана в ТЭК» / **E0017** на заголовке |
| **Партнёр ТЭК в 0989** | доработка передачи адреса партнёра ТЭК (вер. 1.42 INT-0989) |

В JSON заказа проверьте **deliveryType=90** и **carrier** на позиции; в UI CRM — статус и способ доставки.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_contact_fio_fields_answer() -> str:
    return f"""**ФИО контактного лица в INT (КЛ заказчика / КЛ ГП):**

| Роль | INT-0992 (OMS→S/4) | INT-0989 (CRM→OMS) |
| --- | --- | --- |
| **КЛ заказчика (ZY)** | `Order.Partner[PartnerFunction=ZY].Address.AddressName` (+ **AddressAdditionalName**, **AddressName3** по 40 символов) | `order[].partners[partnerFunction=00000015]` |
| **КЛ ГП (ZF)** | `Order.Partner[PartnerFunction=ZF].Address.AddressName` (+ доп. поля ФИО) | `order[].partners[partnerFunction=Z002]` |
| **Телефон на позиции** | — | `order[].items[].receiverContactPhone` (КЛ ГП на наряде) |

ФИО дробится на **AddressName** / **AddressAdditionalName** / **AddressName3** (до 120 символов суммарно).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


INT0991_SPEC_PAGE = "DEMO_PAGE_ID"


def template_int0991_zito_order_answer() -> str:
    return f"""**Сумма товара с НДС по заказу (метод расчёта сумм) — INT-0991 → CRM:**

| Уровень | Что в CRM после обработки 0991 | Комментарий L2 |
| --- | --- | --- |
| Ценовое условие на заказе | **Условие ZITO** | **Сумма с НДС** по заказу (при методе расчёта «от цены с НДС», `cust_pric_proc = 2`) |
| Входящий JSON (0991) | `order[].customerPriceProcedure`, цены позиций | Метод расчёта задаётся из `paymentType` / `paymentTerms` и типа клиента (см. §2.2.1) |
| Позиция (вход) | `order[].items[].userUnitGrossPrice` | Цена с НДС с ИМК → условие **ZPR3** на позиции |

**Не путать:** **ZITO** — вид условия на **заголовке** (итог с НДС); **subtotal_2** — цена с НДС **на позиции** (см. тест §3.3 спецификации).

{_sources_line(INT0991_SPEC_PAGE)}"""


def template_int0991_subtotal2_answer() -> str:
    return f"""**Цена позиции с НДС (вид условия на строке) — INT-0991 → CRM:**

| Уровень | Имя / поле | Смысл |
| --- | --- | --- |
| Входящий **INT-0991** | `order[].items[].userUnitGrossPrice` | Цена с НДС с ИМК (в спеке — «Цена c НДС») |
| После создания заказа в CRM | **subtotal_2** | Вид ценового условия на позиции: **«Цена с НДС (шт)»** (тест §3.3) |
| Связанные условия | **ZNDS** | Цена без НДС (шт); **ZSMN** — сумма НДС по строке |

**Ответ L2:** для вопроса «какой метод/условие у позиции с ценой с НДС» — **subtotal_2** (не ZITO на заголовке).

{_sources_line(INT0991_SPEC_PAGE)}"""


def template_int0991_delay_field_answer(code: str = "IM03") -> str:
    code_u = (code or "IM03").upper()
    return f"""**Код задержания ИМК в INT-0991:**

| Поле JSON | Блок | Пример |
| --- | --- | --- |
| `order[].orderDelays[].delayCode` | Причины задержания заказа | **{code_u}** |
| `delayStatus` | Активность (Y/N) | **Y** — задержание действует |
| `delaySetTime`, `delaySetUser` | Когда/кто установил | из OMS |

**{code_u}** (типовой смысл для ИМК): задержание по сценарию интернет-заказа / ручной обработки (см. маршрутизацию ГОЭЗ, событие **orderEvents=5** — обновление кодов ИМК ЗДР).

Полная таблица полей блока — §**2.2.1** спецификации **INT-0991**.

{_sources_line(INT0991_SPEC_PAGE)}"""


def template_int0991_delay_codes_enum_answer() -> str:
    return f"""**Какие коды могут приходить в `delayCode` (INT-0991, блок `orderDelays`):**

| Код | Кратко (ИМК / L2) |
| --- | --- |
| **IM01** | Оформление заказа в ИМК не завершено |
| **IM03** | Заказ в ручной обработке / очередь ГОЭЗ («взять в работу») |
| **IM06** | Консолидированный заказ ИМК / онлайн-закупка |
| **IM02** | Пример из спеки: задержание до распознавания платежа |

Структура в JSON: `order[].orderDelays[]` — поля **delayCode**, **delayStatus**, **delaySetTime**, **delayReleaseComment** и др.

Справочник кодов и логика снятия — **INT-0991** §2.2.1, §2.4.1 (событие **5** — обновление кодов ИМК ЗДР); настройки CRM — таблицы причин задержания (ZTC / маршрутизация ИМК).

{_sources_line(INT0991_SPEC_PAGE)}"""


def template_int0991_gp_answer() -> str:
    return f"""**Грузополучатель (ГП) в INT-0991 (OMS/ИМК → CRM):**

| Уровень | Поле / роль | Значение |
| --- | --- | --- |
| **INT-0991** (заголовок) | `order[].partners[].partnerFunction` | **00000002** — получатель материала / ГП |
| | `partners[].partnerId` | ID BP грузополучателя в CRM |
| **INT-0991** (позиция) | `order[].items[].partners[].partnerFunction` | **00000002** — ГП на строке (если на уровне позиции) |
| После загрузки в CRM | роль **WE** | S/4-роль грузополучателя (преобразование из CRM-кода) |
| Адрес доставки | блок `addresses[]` + привязка к **partnerAddressHash** | см. §2.2.1 |

**Не путать:** **00000001** — заказчик (AG); **Z002** / **ZF** — контакт ГП; **00000002** — сам **ГП**.

{_sources_line(INT0991_SPEC_PAGE, "DEMO_PAGE_ID")}"""


def template_int0991_operator_dept_answer() -> str:
    return f"""**Код отдела оператора (выписки заказа) в INT-0991:**

| Поле JSON | Блок | Пример | CRM |
| --- | --- | --- | --- |
| `order[].salesData.orderGroupEliteId` | Сбытовые данные | **0VT** | CUSTOMER_H-**ZZ_USER_DEF** / OPERATOR_DIVISION |

В спецификации: «Код отдела выписки заказа (Элиты)» — **IS_ORDER-ORDER_HEADER-OPERATOR_DIVISION**.

{_sources_line(INT0991_SPEC_PAGE)}"""


def template_int0991_order_type_answer() -> str:
    return f"""**Тип / вид заказа CRM в INT-0991:**

| Поле JSON | Смысл | Пример |
| --- | --- | --- |
| `order[].orderProcType` | **Вид заказа CRM** (тип операции) | **ZOR1** — заказ на продажу (ИМК) |
| `order[].orderHybrisType` | Тип заказа **ИМК** (авто/ручной) | **AUTO_ORDER**, **HANDLY_ORDER** |

Для L2 «какой тип заказа в INT» — чаще **`orderProcType`** (ZOR1, ZTA2, …); **orderHybrisType** — именно классификация ИМК.

{_sources_line(INT0991_SPEC_PAGE)}"""


def template_contact_ko_int_answer() -> str:
    return f"""**«КО» в вопросе L2 — обычно имеется в виду контактное лицо (КЛ), не отдельный код:**

| Кого | Роль S/4 | INT-0989 (`partners`) | INT-0992 |
| --- | --- | --- | --- |
| **КЛ заказчика** | **ZY** | `partnerFunction=00000015` | `Order.Partner[PartnerFunction=ZY]` |
| **КЛ грузополучателя** | **ZF** | `partnerFunction=Z002` | `Order.Partner[PartnerFunction=ZF]` |

ФИО — в **AddressName** (+ **AddressAdditionalName**, **AddressName3**). Не путать с **WE** (адрес ГП) и **AG** (заказчик).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_order_registration_date_answer() -> str:
    return f"""**Дата оформления / создания заказа в INT-0989 (заголовок):**

| Поле JSON | Смысл в CRM |
| --- | --- |
| `order[].orderRegistrationDate` | **Дата оформления заказа** (`lt_appointment`, тип даты начала заказа) |

Системные даты S/4 и технические timestamp отмены — отдельные поля (`orderCancelPlan`, `orderCancelFact` и др.).

{_sources_line("DEMO_PAGE_ID")}"""


def template_znd_creation_date_answer() -> str:
    return f"""**Дата создания ЗНД — где в интеграции:**

| Объект | Где фиксируется |
| --- | --- |
| **Заказ CRM** | дата оформления заказа — `order[].orderRegistrationDate` в **INT-0989** (это не дата документа ЗНД) |
| **Создание ЗНД в OMS** | исходящий **INT-0970** → ответ **INT-0973**; в OMS — структура **deliveryOrders** / GUID ЗНД |
| **Тех. дата во внешней системе** | в потоках OMS→S/4 — **`ExternalDocLastChangeDateTime`** (для заказа / ЗНД / ЗНС по правилам 0992) |

Отдельного поля «дата создания ЗНД» в типовом JSON **order[]** **INT-0989** нет — смотрите ответ **0973** и карточку ЗНД в OMS.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_personal_order_field_answer() -> str:
    return f"""**Личный заказ из CRM в INT:**

| INT | Поле JSON | CRM |
| --- | --- | --- |
| **INT-0989** | `order[].personalOrder` | Признак **X** — принадлежность к личному заказу (`ZZ_PRIVORDER`) |

Передаётся на **заголовке** заказа в **INT-0989** (CRM→OMS), не путать с выгрузкой в S/4 (**0992** — отдельный поток).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_order_delays_fields_answer() -> str:
    return f"""**Задержания заказа (в т.ч. из ИМК) в INT:**

| Направление | Структура / поле | Назначение |
| --- | --- | --- |
| **INT-0989** (CRM→OMS) | `order[].orderDelays[]` | Причины задержания на заказе |
| **INT-0991** (ИМК→CRM) | `orderDelays` / коды ЗДР | Обновление кодов задержания с ИМК (авто- vs ручной сценарий) |

Для L2: в JSON исходящего **0989** — блок **orderDelays**; входящий из ИМК — см. спецификацию **INT-0991** (раздел orderDelays / manualProcessingCauses).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_partner_ag_legal_answer() -> str:
    return f"""**Поля передачи юрлица заказчика (ЮЛ) в INT:**

| Роль | INT-0989 (`partners`) | INT-0992 |
| --- | --- | --- |
| Заказчик **AG** | `partnerFunction=00000001` | `Order.Partner[PartnerFunction=AG]` |
| ЮЛ Комус (продавец) | `partnerFunction=ZCOMPANYCC` | по таблице преобразований |

Реквизиты, ИНН, адрес — в блоке **partners** + **addresses** на **0989**; для S/4 — структура **Partner** на **0992**.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_price_error_followup_answer() -> str:
    return f"""**Что делать L2 при ошибке по цене (в т.ч. typeID 143 / ZPRI):**

1. Сверить цену позиции в CRM и в **INT-0992** (`PricingElement`, **ConditionType=ZPRI**).
2. После ЗНД — ответ **INT-2010** / цепочка **0993**; сравнить **ZPRI** до и после S/4.
3. Не менять заказ без сверки позиции и статуса (**E0010** позиция / **E0008** заказ-ЗНД).
4. При расхождении — лог INT, журнал **INTEGRATION_CONFIG**, повтор **START_EXCHANGE** только по регламенту.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_imk_autorder_answer() -> str:
    return f"""**Как в CRM понять, что заказ из ИМК — автозаказ:**

| Признак | Где смотреть L2 | Значения / логика |
| --- | --- | --- |
| Тип заказа ИМК | **INT-0991** (входящий в CRM): `orderHybrisType` | **AUTO_ORDER** — полный автозаказ; **PARTIALLY_AUTO_ORDER** — частичный; **HANDLY_ORDER** — ручная обработка |
| Причины ручной обработки | `manualProcessingCauses[]` в **0991** | **пусто** (или только коды с признаком «отправлен в S4») ≈ полный автозаказ без оператора |
| Исторический № ИМК | **INT-0989**: `orderHybrisOldId` | номер заказа в ИМК (история) |
| Поведение в CRM | обработчик статуса заказа | для полного автозаказа из ИМК статус «В обработке» **не** ставится автоматически (нет **manualProcessingCauses**) |

В UI CRM дополнительно: канал/источник заказа, автор «Интернет-бот» для автозаказа без КЦ (см. ПР R189).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_price_errors_answer() -> str:
    return f"""**Типовые ошибки по ценам для L2 CRM (интеграция):**

| Код / тема | Откуда | Смысл для L2 |
| --- | --- | --- |
| **typeID 143** | **INT-2010** (S/4→OMS→CRM) | Расхождение **ZPRI**: сравнить цену в CRM (**0992**) и после обработки ЗНД (**0993** / S/4) |
| Пересчёт после ЗНД | **START_EXCHANGE**, iv_type 1/3 | После **INT-0970/0973** проверить **ZZConditionRateValue** для `ConditionType=ZPRI` |
| Общий принцип | Confluence **DEMO_PAGE_ID**, **DEMO_PAGE_ID** | Не менять заказ «вслепую» — сверить позицию, статус **E0010** (позиция) vs **E0008** (заказ/ЗНД) |

В JSON заказа смотрите `PricingElement[ConditionType=ZPRI]`; в UI CRM — цену позиции и историю INT.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_znd_vs_s4_answer() -> str:
    return f"""**Чем создание ЗНД отличается от выгрузки заказа в S/4:**

| | **Создание ЗНД** | **Выгрузка заказа в S/4** |
| --- | --- | --- |
| **Цель** | Заказ-направление-доставки в **OMS** | Учётный заказ в **S/4** |
| **Исходящий INT** | {_int_outgoing("INT-0970")} | {_int_outgoing("INT-0989")} (далее OMS→S/4) |
| **Ожидаемый ответ** | {_int_response("INT-0973")} | {_int_response("INT-2026")} (после **INT-2010** от **S/4**) |
| **Закрывающий** | **INT-0989** (`INTEGRATION_CONFIG`) | — |
| **iv_type** | **1** (только ЗНД) / **3** (+ ЗНС) | **4** — сохранение заказа |

**0970/0973** не создают документ в S/4; **0989** запускает передачу полного заказа в OMS и далее в S/4 (**0992** / **2010**).

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def template_l2_matrix_answer() -> str:
    return f"""**Матрица «кто чинит» по ошибкам интеграции заказа (L2 CRM):**

- Страница Confluence: **«Таблица взаимодействия команд по проблемам интеграции сбытовых заказов для CRM»** — {_page_markdown_link("DEMO_PAGE_ID")}.
- Колонки: команда **первичного анализа**, **доп. анализа**, **исправления**, **проверки синхронизации после fix**, **связь с бизнесом**.
- Источники ошибок для мониторинга: выгрузка MSP `failed-auto-cancel`, данные AIF LO (см. текст страницы).
- Памятки по сценариям — ссылки в начале той же статьи (DTS-103, L1, ZPRI после ЗНД и др.).

{_sources_line("DEMO_PAGE_ID")}"""


def template_s4_not_sent_answer() -> str:
    return f"""**Заказ не ушёл в S/4 после сохранения — что смотреть в CRM (L2):**

1. **INT-0989** — сформировался и ушёл **xml** в S/4 после сохранения в БД (иначе заказ «завис» на CRM→OMS/S4).
2. Отчёт **«неотправленные в S/4 заказы»** (нет xml для **INT-0989**) — {_page_markdown_link("DEMO_PAGE_ID")}.
3. Транзакция/программа **`ZCRM_OMS_ORDERSEND_WAIT`** — заказы, отправленные в S/4 **с задержкой**.
4. Виды заказа S/4 в отборе: `standard_s4` (ZOR1), `surplus_s4`, `free_delivery_s4`.
5. Направление интеграции заказа: **CRM → OMS → S/4**, не прямой вызов из CRM в S/4.

{_sources_line("DEMO_PAGE_ID", "DEMO_PAGE_ID", "DEMO_PAGE_ID")}"""


def is_znd_vs_s4_question(question: str) -> bool:
    q = (question or "").lower()
    return any(m in q for m in ("отлича", "разниц", "чем ")) and any(
        m in q for m in ("знд", "znd")
    ) and any(m in q for m in ("s/4", "s4", "выгруз"))


def is_s4_chain_0989_question(question: str) -> bool:
    q = (question or "").lower().replace(" ", "")
    return bool(
        re.search(r"0989.{0,20}0992.{0,20}2010", q)
        or re.search(r"0992.{0,20}2010", q) and "0989" in q
    )


def is_sap_pi_question(question: str) -> bool:
    q = (question or "").lower()
    return bool(re.search(r"\bsap\s*pi\b|\bpi/po\b|\bучаствует\s+pi\b", q))


def is_partner_compare_question(question: str) -> bool:
    q = (question or "").lower()
    return any(m in q for m in ("отлич", "различ", "чем ")) and any(
        m in q for m in ("грузополуч", "заказчик", "партнёр", "партнер")
    )


def is_kl_int_field_question(question: str) -> bool:
    q = (question or "").lower()
    if re.search(r"телефон", q) and re.search(r"кл|контакт", q) and (
        "гп" in q or "грузополуч" in q or re.search(r"\bzf\b", q)
    ):
        return True
    return (
        re.search(r"\bкл\b", q) or "контакт" in q
    ) and any(m in q for m in ("поле", "int", "переда", "0992", "0989"))


def is_price_error_question(question: str) -> bool:
    q = (question or "").lower()
    return any(m in q for m in ("цен", "zpri", "typeid", "143", "ошибк")) and any(
        m in q for m in ("цен", "price", "zpri", "typeid", "143", "2010")
    )


def is_crm_to_s4_question(question: str) -> bool:
    q = (question or "").lower()
    if is_znd_vs_s4_question(q) or is_s4_chain_0989_question(q):
        return False
    try:
        import sppr_json_glossary as gloss

        if gloss.is_int_field_lookup_question(question):
            return False
        if gloss.resolve_field_topic(question) in (
            "delivery",
            "delivery_enum",
            "gift_item",
            "gift_tech",
            "kl",
            "kl_phone",
            "partner_code",
            "imk_auto",
            "bank",
            "carrier",
            "campaign",
            "item_type",
            "item_type_code",
            "order_status",
            "tech_ip",
            "tech_zmip",
            "tech_transit",
            "transit_field",
            "payment_type",
            "payment_enum",
            "order_date",
            "znd_date",
            "partners_block",
            "zns_doc",
            "zvu_doc",
            "tech_type_enum",
            "warehouse_ship",
            "tek_order",
            "item_position",
            "contact_fio",
            "contact_ko",
            "int0991_zito_order",
            "int0991_subtotal2",
            "int0991_delay_field",
            "int0991_delay_enum",
            "int0991_gp",
            "int0991_operator_dept",
            "int0991_order_type",
        ):
            return False
    except ImportError:
        pass
    if any(m in q for m in ("способ доставки", "deliverytype")) and any(
        m in q for m in ("поле", "переда", "int", "0989")
    ):
        return False
    return any(m in q for m in ("попадает", "попасть", "выгруз", "идёт в s", "идет в s")) and any(
        m in q for m in ("crm", "s/4", "s4")
    ) and not (
        any(m in q for m in ("поле", "как называ", "в каком поле"))
        and any(m in q for m in ("int", "0989", "0992", "доставк", "подар", "технолог"))
    )


def is_int_0989_question(question: str) -> bool:
    q = (question or "").lower()
    if not re.search(r"int-?0?989", q) and "0989" not in q:
        return False
    return any(m in q for m in ("для чего", "назначен", "роль", "интерфейс", "что такое", "зачем"))


def is_int_2026_question(question: str) -> bool:
    q = (question or "").lower()
    if not re.search(r"int-?2026", q) and "2026" not in q:
        return False
    return any(m in q for m in ("для чего", "назначен", "роль", "интерфейс", "что такое", "зачем"))


def is_zcrm_r189_int_question(question: str) -> bool:
    q = (question or "").lower()
    if "zcrm_r189" in q.replace(" ", "") or "r189_int" in q.replace(" ", ""):
        return True
    return any(m in q for m in ("настраив", "настройк", "пары int", "пара int", "ожидаемый ответ")) and any(
        m in q for m in ("crm", "int", "zcrm", "r189", "интеграц")
    )


def _answer_body_too_short(body: str) -> bool:
    if not body or not body.strip():
        return True
    if re.match(r"(?is)^\s*\*\*источники\s+confluence", body.strip()):
        return True
    stripped = re.sub(r"(?is)\*\*источники[^*]*\*\*[^\n]*", "", body)
    stripped = re.sub(r"(?is)источники confluence[^\n]*", "", stripped)
    stripped = re.sub(r"\[pageId \d+\]\([^)]+\)", "", stripped)
    stripped = re.sub(r"\[[^\]]+\]\([^)]+\)", "", stripped)
    return len(stripped.strip()) < 50


_FOLLOW_UP_MARKERS = (
    "ты мне",
    "а что ",
    "а что?",
    "при такой",
    "мне нужн",
    "тех поля",
    "а можешь",
    "уточни",
    "не int",
    "поля а не",
    "название пол",
    "а как же",
    "скажи ещё",
    "скажи еще",
    "выше",
    "предыдущ",
    "тот же",
    "это поле",
    "то же поле",
    "про это поле",
    "про него",
    "про неё",
    "про нее",
)

_TOPIC_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("carrier", ("перевозчик", "carrier", "shipper")),
    ("campaign", ("акци", "campaignid", "campaigncoid", "номер акции", "ценовой акции")),
    ("item_type", ("тип позици", "itemtype", "itm_type", "ztnn", "ztin", "zta1")),
    ("order_status", ("статус заказа", "orderstatus", "e0001", "e00")),
    ("delivery", ("доставк", "deliverytype")),
    ("gift", ("подар", "itemgift", "ztnn")),
    ("kl", ("контакт", "кл ", "кл.", "partnerfunction")),
    ("payment", ("оплат", "paymenttype")),
    ("zns", ("знс", "zns", "заказ на сборку")),
    ("zvu", ("зву", "zvu", "вывоз упаков")),
    ("partners", ("партнер", "partners", "partnerfunction")),
    ("tech", ("технолог", "techtype", "zzdeliv", "sptech")),
    ("warehouse", ("склад отгруз", "whse", "plant")),
    ("tek", ("тэк", "external_tek")),
    ("item_pos", ("позиция заказа", "позиц", "entryomsguid", "productid")),
    ("fio", ("фио", "addressname")),
    ("int0991", ("0991", "имк", "hybris", "orderdelays", "delaycode")),
    ("pricing_zito", ("zito", "subtotal", "метод расчет")),
)


def _question_topics(text: str) -> set[str]:
    low = (text or "").lower().replace("ё", "е")
    found: set[str] = set()
    for topic, keys in _TOPIC_KEYWORDS:
        if any(k in low for k in keys):
            found.add(topic)
    if re.search(r"\be\d{4}\b", low):
        found.add("order_status")
    return found


def is_dialog_follow_up(
    question: str,
    chat_history: list[dict[str, Any]] | None,
) -> bool:
    """Явное уточнение в диалоге — не любой короткий вопрос."""
    q = (question or "").strip()
    if not q or not chat_history:
        return False
    low = q.lower()
    if any(m in low for m in _FOLLOW_UP_MARKERS):
        return True
    if re.search(r"\b(этот|эта|это|тот|та|такой|такая|такое|оно|он|она)\b", low):
        if len(q) < 80:
            return True
    if re.search(r"^(а|и)\s+", low) and len(q) < 70:
        cur = _question_topics(q)
        prev_user = ""
        for msg in reversed(chat_history):
            if msg.get("role") == "user" and (msg.get("content") or "").strip():
                prev_user = (msg.get("content") or "").strip()
                break
        if prev_user and cur and cur & _question_topics(prev_user):
            return True
    return False


def _prev_user_question(chat_history: list[dict[str, Any]] | None) -> str:
    if not chat_history:
        return ""
    for msg in reversed(chat_history):
        if msg.get("role") == "user" and (msg.get("content") or "").strip():
            return (msg.get("content") or "").strip()
    return ""


def effective_question_for_routing(
    question: str,
    chat_history: list[dict[str, Any]] | None,
) -> str:
    """Уточнение в диалоге: ЗНС/ЗВУ, КЛ→ФИО/ТЭК/КО; не смешивать с новой темой."""
    q = (question or "").strip()
    if not chat_history or not q:
        return q
    low = q.lower()
    try:
        import sppr_json_glossary as gloss

        cur_topic = gloss.resolve_field_topic(q)
        prev = _prev_user_question(chat_history)
        prev_topic = gloss.resolve_field_topic(prev) if prev else None

        if cur_topic and prev_topic in ("zns_doc", "zvu_doc") and cur_topic not in (
            "zns_doc",
            "zvu_doc",
        ):
            return q

        if prev and (
            prev_topic == "kl"
            or re.search(r"\bкл\b", prev.lower())
            or "контактн" in prev.lower()
        ):
            if re.search(r"фио", low):
                return (
                    "В каких полях INT передается ФИО контактного лица "
                    "AddressName AddressAdditionalName"
                )
            if re.search(r"\bтэк\b", low) and any(
                m in low for m in ("заказ", "понять", "признак", "как")
            ):
                return "Как понять что заказ ТЭК deliveryType carrier EXTERNAL_TEK"
            if re.search(r"\bко\b", low) and "поле" in low:
                return "в каком поле передается КЛ заказчика partnerFunction ZY INT-0989"
            if prev_topic == "int0991_delay_field" or re.search(r"\bim0[0-9]\b", prev.lower()):
                if any(m in low for m in ("какие", "виды", "еще", "ещё", "другие")):
                    return "какие коды delayCode orderDelays INT-0991 IM01 IM03 IM06"

        if len(q) > 55:
            return q
        if not re.search(
            r"в\s+каком\s+поле|какое\s+поле|а\s+в\s+каком|ты\s+мне|не\s+тот\s+ответ|придумыва",
            low,
        ):
            return q
        if not prev:
            return q
        if prev_topic in ("zns_doc", "zvu_doc", "partners_block") or (
            prev_topic and not cur_topic
        ):
            return f"{prev}. {q}"
    except ImportError:
        pass
    return q


def is_meta_complaint_question(question: str) -> bool:
    low = (question or "").lower()
    return any(
        m in low
        for m in (
            "не тот ответ",
            "не то",
            "не нужен",
            "придумываешь",
            "придумал",
            "выдумал",
            "галлюцин",
            "ты мне выдал",
            "что ты мне",
            "сам придум",
        )
    )


def template_meta_complaint_answer(
    question: str,
    chat_history: list[dict[str, Any]] | None,
) -> str:
    prev = _prev_user_question(chat_history)
    try:
        import sppr_json_glossary as gloss

        eq = effective_question_for_routing(question, chat_history)
        topic = gloss.resolve_field_topic(eq) or gloss.resolve_field_topic(prev)
        retry = _try_int_field_lookup_answer(eq) or _try_int_field_lookup_answer(prev)
        if retry:
            return (
                "**Уточнение по вашему диалогу (без LLM, только каталог/шаблон L2):**\n\n"
                f"{retry}\n\n"
                "_Если ответ снова не тот — переформулируйте: «в каком поле JSON INT-0989 …» "
                "или укажите INT и роль (AG/WE/ZF)._"
            )
    except ImportError:
        topic = None
        retry = None
    return (
        "**По каталогу L2 ответ строится из шаблонов INT-0989/0992, без «додумывания».**\n\n"
        "Если предыдущий ответ был неверным:\n"
        "1. Уточните объект (**заказ**, **ЗНД**, **ЗНС**, **партнёр AG/WE**).\n"
        "2. Спросите про **имя поля JSON** (напр. `order[].partners[]`, `paymentType`).\n"
        "3. Для **ЗНС/ЗВУ** — это отдельные документы (**INT-0827→2011**), не поле в `order[]`.\n\n"
        f"Предыдущий вопрос в сессии: _{prev[:120] if prev else '—'}_"
    )


def expand_question_with_history(
    question: str,
    chat_history: list[dict[str, Any]] | None,
    *,
    use_history: bool = True,
) -> str:
    """Склеить уточнение с предыдущим вопросом пользователя (только для RAG/LLM)."""
    q = (question or "").strip()
    if not use_history or not chat_history or not q:
        return q
    if not is_dialog_follow_up(q, chat_history):
        return q

    prev_user = ""
    for msg in reversed(chat_history):
        if msg.get("role") == "user" and (msg.get("content") or "").strip():
            prev_user = (msg.get("content") or "").strip()[:220]
            break
    if not prev_user:
        return q

    cur_topics = _question_topics(q)
    prev_topics = _question_topics(prev_user)
    if cur_topics and prev_topics and not (cur_topics & prev_topics):
        return q

    return (
        f"Контекст диалога L2 (уточнение к предыдущему вопросу).\n"
        f"Предыдущий вопрос пользователя: {prev_user}\n"
        f"Текущее уточнение: {q}"
    )


def _glossary_fallback_answer(
    question: str,
    entries: list[dict[str, Any]],
    hits: list[RagHit],
) -> str | None:
    """Если LLM вернул только «Источники» — краткий ответ из глоссария + фрагмент."""
    import sppr_json_glossary as gloss

    structured = _try_int_field_lookup_answer(question)
    if structured:
        return structured

    if not entries and not hits:
        return None
    low = (question or "").lower()
    if "партнер" in low and "сборк" not in low:
        entries = [
            e
            for e in entries
            if "assembly" not in str(e.get("json_path") or e.get("json_path_0989") or "").lower()
        ]
    table = gloss.format_catalog_field_answer(entries)
    if table and "assemblystatus" in table.lower() and "партнер" in low:
        table = None
    if table:
        links = format_source_links(hits)
        return f"{table}\n\n{links}" if links else table
    parts: list[str] = ["**По глоссарию L2 и найденным фрагментам Confluence:**", ""]
    block = gloss.format_glossary_for_llm(entries)
    if block and "Глоссарий" in block and block.count("- **") >= 1:
        parts.append(block)
    if hits:
        snippet = (hits[0].text or "").strip()[:600]
        if snippet and "INT_TYPE" not in snippet[:80]:
            parts.append(f"> {snippet}")
    links = format_source_links(hits)
    if links:
        parts.append(links)
    body = "\n".join(parts)
    return body if len(body.strip()) > 80 else None


def _try_crm_process_glossary_answer(question: str) -> str | None:
    """Коды технологий, роли партнёров, статусы CRM — по ПР SD-08 Часть 1."""
    try:
        import sppr_crm_process_glossary as crm_gloss
    except ImportError:
        return None
    return crm_gloss.format_crm_glossary_answer(question, _sources_line)


def _try_int_field_lookup_answer(question: str) -> str | None:
    """Ответы «в каком поле INT» из глоссария (без LLM)."""
    try:
        from sppr_crm_abbrev_glossary import (
            match_abbreviation,
            resolve_abbrev_topic,
            try_abbrev_glossary_answer,
        )

        if match_abbreviation(question) and resolve_abbrev_topic(question):
            abbrev_ans = try_abbrev_glossary_answer(question, _sources_line)
            if abbrev_ans:
                return abbrev_ans
    except ImportError:
        pass
    try:
        import sppr_json_glossary as gloss
    except ImportError:
        gloss = None
    low = (question or "").lower()
    if "partnerfunction" in low.replace(" ", "") and any(
        x in low for x in ("0992", "int", "переда")
    ):
        return template_partner_function_field_answer()

    if gloss is None:
        return _try_crm_process_glossary_answer(question)

    topic = gloss.resolve_field_topic(question)
    if topic in (
        "payment_type",
        "payment_terms",
        "payment_enum",
        "payment_terms_enum",
        "payment_vo_vs_uo",
    ):
        try:
            from sppr_crm_abbrev_glossary import try_abbrev_glossary_answer

            abbrev_ans = try_abbrev_glossary_answer(question, _sources_line)
            if abbrev_ans:
                return abbrev_ans
        except ImportError:
            pass
    if not topic:
        try:
            from sppr_crm_abbrev_glossary import try_abbrev_glossary_answer

            abbrev_ans = try_abbrev_glossary_answer(question, _sources_line)
            if abbrev_ans:
                return abbrev_ans
        except ImportError:
            pass
        return _try_crm_process_glossary_answer(question)
    if topic == "partner_code":
        code = gloss.parse_partner_function_code(question)
        if code:
            return template_partner_crm_code_answer(code)
    if topic == "delivery":
        return template_delivery_type_int_answer()
    if topic == "carrier":
        return template_carrier_int_answer()
    if topic == "campaign":
        return template_campaign_int_answer()
    if topic == "item_type":
        return template_item_type_int_answer()
    if topic == "item_type_code":
        m = re.search(r"\b(ZT[A-Z0-9]{2,3})\b", question, re.I)
        return template_item_type_code_answer(m.group(1) if m else "ZTNN")
    if topic == "order_status":
        m = re.search(r"\b(E\d{4})\b", question, re.I)
        return template_order_status_code_answer(m.group(1) if m else "E0001")
    if topic == "gift_item" or topic == "gift_tech":
        return template_gift_position_int_answer()
    if topic == "kl_phone":
        return template_kl_gp_phone_int_answer()
    if topic == "kl":
        return template_kl_in_int_answer()
    if topic == "tech_ip":
        return template_tech_type_ip_answer()
    if topic == "tech_zmip":
        return template_tech_type_zmip_answer()
    if topic == "payment_type":
        return template_payment_type_field_answer()
    if topic == "payment_terms":
        return template_payment_terms_field_answer()
    if topic == "payment_vo_vs_uo":
        return template_payment_vo_vs_uo_answer()
    if topic == "payment_enum":
        return template_payment_type_values_answer()
    if topic == "payment_terms_enum":
        return template_payment_terms_values_answer()
    if topic == "delivery_enum":
        return template_delivery_type_values_answer()
    if topic == "bank":
        return template_bank_details_int_answer()
    if topic == "transit_field":
        return template_transit_field_int_answer()
    if topic == "tech_transit":
        return template_transit_tech_type_answer()
    if topic == "order_date":
        return template_order_registration_date_answer()
    if topic == "znd_date":
        return template_znd_creation_date_answer()
    if topic == "personal_order":
        return template_personal_order_field_answer()
    if topic == "order_delays":
        return template_order_delays_fields_answer()
    if topic == "partner_ag":
        return template_partner_ag_legal_answer()
    if topic == "partners_block":
        return template_partners_block_int_answer()
    if topic == "zns_doc":
        return template_zns_doc_answer()
    if topic == "zvu_doc":
        return template_zvu_doc_answer()
    if topic == "tech_type_enum":
        return template_tech_type_enum_answer()
    if topic == "warehouse_ship":
        return template_warehouse_shipment_answer()
    if topic == "tek_order":
        return template_tek_order_answer()
    if topic == "item_position":
        return template_item_position_int_answer()
    if topic == "contact_fio":
        return template_contact_fio_fields_answer()
    if topic == "contact_ko":
        return template_contact_ko_int_answer()
    if topic == "int0991_zito_order":
        return template_int0991_zito_order_answer()
    if topic == "int0991_subtotal2":
        return template_int0991_subtotal2_answer()
    if topic == "int0991_delay_field":
        m = re.search(r"\b(IM\d{2})\b", question, re.I)
        return template_int0991_delay_field_answer(m.group(1) if m else "IM03")
    if topic == "int0991_delay_enum":
        return template_int0991_delay_codes_enum_answer()
    if topic == "int0991_gp":
        return template_int0991_gp_answer()
    if topic == "int0991_operator_dept":
        return template_int0991_operator_dept_answer()
    if topic == "int0991_order_type":
        return template_int0991_order_type_answer()
    if topic == "int0989_gip_block":
        try:
            import sppr_int0989_glossary as g989

            return g989.format_int0989_gip_block_answer(_sources_line)
        except ImportError:
            pass
    if topic == "int0989_field":
        try:
            import sppr_int0989_glossary as g989

            hits = g989.match_int0989_field(question)
            if hits:
                return g989.format_int0989_field_answer(hits[0], _sources_line)
        except ImportError:
            pass
    if topic == "imk_auto":
        return template_imk_autorder_answer()
    if topic == "catalog":
        hint = gloss.answer_from_field_hints(question)
        if hint:
            return f"{hint}\n\n{_sources_line('DEMO_PAGE_ID', 'DEMO_PAGE_ID')}"
        entries = gloss.match_all_glossary(question, max_entries=6)
        ans = gloss.format_catalog_field_answer(entries)
        if ans:
            return f"{ans}\n\n{_sources_line('DEMO_PAGE_ID', 'DEMO_PAGE_ID', 'DEMO_PAGE_ID')}"
    try:
        import sppr_int0989_glossary as g989

        ans989 = g989.try_int0989_field_answer(question, _sources_line)
        if ans989:
            return ans989
    except ImportError:
        pass
    return _try_crm_process_glossary_answer(question)


def template_partner_function_field_answer() -> str:
    return f"""**PartnerFunction в INT-0992:**

| Уровень | Поле | Смысл |
| --- | --- | --- |
| Партнёр в JSON | `Order.Partner[].PartnerFunction` | Роль S/4 после преобразования: **AG**, **WE**, **ZF**, **ZY**, **RG** |
| Исходящий CRM (0989) | `order[].partners[].partnerFunction` | CRM-код (напр. `00000001`→**AG**, `Z002`→**ZF**) |

Преобразование CRM→S/4 — по таблице в спецификации **INT-0989** / **INT-0992**; не путать с полем заголовка **INT_TYPE** в **INTEGRATION_CONFIG**.

{_sources_line('DEMO_PAGE_ID', 'DEMO_PAGE_ID', 'DEMO_PAGE_ID')}"""


def _try_template_answer(
    question: str,
    chat_history: list[dict[str, Any]] | None = None,
) -> str | None:
    q = (question or "").strip()
    low = q.lower()
    if is_meta_complaint_question(q):
        return template_meta_complaint_answer(q, chat_history)
    eq = effective_question_for_routing(q, chat_history)
    field_ans = _try_int_field_lookup_answer(eq) or _try_int_field_lookup_answer(q)
    if field_ans:
        return field_ans
    if is_l2_matrix_question(q):
        return template_l2_matrix_answer()
    if any(m in low for m in ("не уш", "неотправ", "не сформир")) and any(
        m in low for m in ("s/4", "s4")
    ):
        return template_s4_not_sent_answer()
    if is_s4_chain_0989_question(q):
        return template_s4_chain_0989_0992_2010_answer()
    if is_sap_pi_question(q):
        return template_sap_pi_answer()
    if is_partner_compare_question(q) or (
        re.search(r"\bzf\b", low) and any(m in low for m in ("роль", "партнёр", "партнер"))
    ):
        t = template_partner_roles_answer(q)
        if t:
            return t
    if is_kl_int_field_question(q):
        if re.search(r"телефон", low) and ("гп" in low or "грузополуч" in low):
            return template_kl_gp_phone_int_answer()
        return template_kl_in_int_answer()
    if is_price_error_question(q):
        return template_price_errors_answer()
    if any(m in low for m in ("при такой ошиб", "что делать", "что надо делать", "при такой")):
        return template_price_error_followup_answer()
    if any(m in low for m in ("поля а не int", "название пол", "тех поля", "имя поля")):
        hint = _try_int_field_lookup_answer(question)
        if hint:
            return hint
    if is_znd_vs_s4_question(q):
        return template_znd_vs_s4_answer()
    if is_int_0989_question(q):
        return template_int_0989_answer()
    if is_int_2026_question(q):
        return template_int_2026_answer()
    if is_zcrm_r189_int_question(q):
        return template_zcrm_r189_int_answer()
    if "0827" in low and "2011" in low:
        return template_int_0827_zns_answer()
    if re.search(r"\b(знс|zns)\b", low) and any(
        m in low for m in ("что такое", "это что", "означает", "что за")
    ):
        return template_zns_doc_answer()
    if re.search(r"\b(зву|zvu)\b", low) and any(
        m in low for m in ("что такое", "это что", "означает", "что за")
    ):
        return template_zvu_doc_answer()
    if any(m in low for m in ("типы технолог", "тип технолог", "технолог")) and any(
        m in low for m in ("какие", "типы", "переда", "int", "crm", "бывают")
    ):
        return template_tech_type_enum_answer()
    if re.search(r"\bтэк\b", low) and any(m in low for m in ("заказ", "понять", "признак", "как")):
        return template_tek_order_answer()
    if "фио" in low and any(m in low for m in ("поле", "полях", "переда", "int", "вывод")):
        return template_contact_fio_fields_answer()
    if re.search(r"(?<![а-яa-z])ко(?![а-яa-z])", low) and any(m in low for m in ("поле", "переда", "int")):
        if "код" not in low:
            return template_contact_ko_int_answer()
    if any(m in low for m in ("метод расчет", "метод расчёта", "zito")) and any(
        m in low for m in ("сумм", "заказ", "итог")
    ) and "ндс" in low and "позиц" not in low:
        return template_int0991_zito_order_answer()
    if "subtotal" in low.replace(" ", "") or (
        "ндс" in low and any(m in low for m in ("позиц", "цен", "услов")) and "метод" in low
    ):
        return template_int0991_subtotal2_answer()
    if is_crm_to_s4_question(q):
        return template_crm_to_s4_answer()
    iv = _iv_type_in_question(q)
    if iv == "2":
        return template_iv_type2_answer()
    if iv == "3" and any(m in low for m in ("цепочк", "знд", "знс", "int")):
        return template_int_chain_iv_type3_answer()
    if any(m in low for m in ("отмен", "изменен")) and any(m in low for m in ("знд", "znd")):
        return template_znd_modify_cancel_answer()
    if is_znd_creation_question(q) and (
        is_int_chain_question(q) or "int" in low or "цепочк" in low
    ):
        return template_int_chain_znd_answer()
    if re.search(r"start_exchange", low) and any(m in low for m in ("знд", "znd", "метод", "запуск")):
        return template_int_chain_znd_answer()
    return None


def format_rag_block(hits: list[RagHit]) -> str:
    if not hits:
        return ""
    lines = ["Фрагменты Confluence (ML-поиск TF-IDF, использовать для ответа):", ""]
    for i, h in enumerate(hits, 1):
        pid = h.page_id or "—"
        lines.append(f"### [{i}] {h.title} (pageId={pid}, score={h.score:.3f})")
        if h.url:
            lines.append(f"URL: {h.url}")
        lines.append(h.text[:2000])
        lines.append("")
    return "\n".join(lines)


def format_source_links(hits: list[RagHit]) -> str:
    if not hits:
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    for h in hits:
        key = h.page_id or h.source_file
        if key in seen:
            continue
        seen.add(key)
        label = (_title_for_page_id(h.page_id) if h.page_id else "") or h.title or h.source_file
        if h.page_id:
            parts.append(_page_markdown_link(h.page_id) or f"[{label}]({page_url(h.page_id)})")
        elif h.url:
            parts.append(f"[{label}]({h.url})")
    if not parts:
        return ""
    return "**Источники Confluence (RAG):** " + " · ".join(parts)


def is_general_confluence_question(question: str) -> bool:
    """Вопрос про процесс/поля INT без привязки к заказу."""
    q = (question or "").lower()
    markers = (
        "typeid",
        "type id",
        "int-2010",
        "int-0992",
        "int-0989",
        "int-0993",
        "что значит",
        "как исправить",
        "инструкция",
        "confluence",
        "ошибка 143",
        "ошибка 004",
        "l2 crm",
        "цепочка int",
        "oms алгоритм",
        "partnerfunction",
        "роль zf",
        "роль we",
        "справочник",
        "код ошибки",
    )
    return any(m in q for m in markers)


def answer_confluence_only(
    question: str,
    *,
    top_k: int = 5,
    chat_history: list[dict[str, Any]] | None = None,
    use_dialog_context: bool = True,
) -> str:
    """Ответ только по Confluence (без JSON заказа). chat_history — предыдущие реплики UI."""
    if not index_ready():
        return (
            "Индекс Confluence не собран. В папке материалов выполните:\n\n"
            "`python build_confluence_rag_index.py`"
        )

    q = (question or "").strip()
    q_expanded = expand_question_with_history(
        q, chat_history, use_history=use_dialog_context
    )
    if is_meta_complaint_question(q):
        return template_meta_complaint_answer(q, chat_history)

    eq = effective_question_for_routing(q, chat_history)
    templ = _try_template_answer(eq, chat_history) or _try_template_answer(q, chat_history)
    if templ:
        return templ

    try:
        import sppr_json_glossary as gloss

        intent = gloss.classify_confluence_intent(eq) or gloss.classify_confluence_intent(q)
    except Exception:
        gloss = None
        intent = "rag_llm"

    glossary_entries = _glossary_entries(q)
    try:
        glossary_block = gloss.format_glossary_for_llm(glossary_entries) if gloss else ""
    except Exception:
        glossary_block = ""

    q_search = q_expanded if q_expanded != q else q
    hits = search_confluence(q_search, top_k=max(top_k, 8))
    if not hits and q_search != q:
        hits = search_confluence(q, top_k=max(top_k, 8))
    if not hits and not glossary_entries:
        return "По выгрузкам Confluence релевантных фрагментов не найдено (попробуйте другие слова)."

    if intent in ("field_lookup", "enum_delivery", "enum_payment"):
        fb = (
            _try_int_field_lookup_answer(eq)
            or _try_int_field_lookup_answer(q)
            or _glossary_fallback_answer(eq, glossary_entries, hits)
            or _glossary_fallback_answer(q, glossary_entries, hits)
        )
        if fb and not _answer_body_too_short(fb):
            links = format_source_links(hits)
            if links and links not in fb:
                return f"{fb}\n\n{links}"
            return fb
        short = (
            "В каталоге полей INT-0989/0992 (L2) однозначного ответа нет. "
            "Уточните INT (0989/0992) и переформулируйте вопрос («в каком поле JSON…»)."
        )
        links = format_source_links(hits)
        return f"{short}\n\n{links}" if links else short

    from sppr_llm import call_llm_qa

    user_parts = []
    if glossary_block:
        user_parts.append(glossary_block)
    if hits:
        user_parts.append(format_rag_block(hits))
    if q_expanded != q:
        user_parts.append(f"---\nКонтекст (уточнение в диалоге):\n{q_expanded}\n")
    user_parts.append(f"---\nВопрос L2: {q}\n\n")
    user_parts.append(
        "Ответ: 5–12 строк markdown (таблица или список). Только факты из глоссария и фрагментов. "
        "Для цепочек INT — колонки «Исходящий» и «Ожидаемый ответ (система)». "
        "Если спрашивают про поле — укажи имя JSON (например paymentType), не только INT-0989. "
        "Обязательно тело ответа, не только источники."
    )
    user = "\n---\n".join(user_parts)
    body = call_llm_qa(CONFLUENCE_QA_SYSTEM, user)
    body = _linkify_page_ids_in_text(body or "")
    if _answer_body_too_short(body):
        fb = _glossary_fallback_answer(q_expanded, glossary_entries, hits) or _glossary_fallback_answer(
            q, glossary_entries, hits
        )
        if fb:
            body = fb
        else:
            fb2 = _try_int_field_lookup_answer(q)
            if fb2:
                body = fb2
    links = format_source_links(hits)
    if links and links not in body:
        return f"{body}\n\n{links}" if body and body.strip() else links
    return body or "Не удалось сформировать ответ. Уточните вопрос или добавьте термин в «Термины L2»."


def _linkify_page_ids_in_text(text: str) -> str:
    """Числовые pageId в строке «Источники» → markdown-ссылки."""
    if not text:
        return text

    def _src_line(m: re.Match[str]) -> str:
        prefix = m.group(1)
        tail = m.group(2)
        ids = re.findall(r"\b(\d{8,10})\b", tail)
        if not ids:
            return m.group(0)
        links = ", ".join(_page_markdown_link(pid, short=True) for pid in ids)
        return f"{prefix}{links}"

    out = re.sub(
        r"(^\*\*Источники:?\*\*[:\s]*)(.+)$",
        _src_line,
        text,
        flags=re.I | re.M,
    )
    return out


def merge_rag_into_qa_prompt(
    question: str,
    json_context: str,
    glossary_block: str,
    *,
    top_k: int = 4,
) -> tuple[str, list[RagHit]]:
    """Дополнить промпт Q&A фрагментами Confluence."""
    hits = search_confluence(question, top_k=top_k) if index_ready() else []
    user = f"Данные из JSON:\n\n{json_context}\n\n"
    if glossary_block:
        user += f"---\n{glossary_block}\n"
    if hits:
        user += f"---\n{format_rag_block(hits)}\n"
    user += (
        f"---\nВопрос: {question.strip()}\n\n"
        "Сначала факты из JSON; для смысла полей/процесса — фрагменты Confluence. "
        "3–10 строк markdown. Не повторяй все ошибки 2010. "
        "Если в JSON нет ответа на вопрос — только фраза «Ответ на данный вопрос не найден», "
        "без выдуманных значений и без подмены ответа общим описанием INT из Confluence."
    )
    return user, hits
