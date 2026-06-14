# -*- coding: utf-8 -*-
"""
Веб-оболочка СППР (Streamlit): диагностика, Q&A по JSON, база Confluence.

  streamlit run sppr_streamlit_app.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import streamlit as st

from sppr_llm import load_env
from sppr_ui_render import (
    inject_sppr_styles,
    render_button_with_help,
    render_help_icon,
    render_sppr_text,
    strip_brief_report_footer,
)

load_env()

MATERIALS = Path(__file__).parent
L2_TERMS = MATERIALS / "sppr_l2_terms.json"
FIELD_CATALOG = MATERIALS / "json_field_catalog_crm.json"
ANALYZE = MATERIALS / "sppr_analyze.py"
CHARTS = MATERIALS / "diploma_results" / "charts"

RULE_FILES = {
    "Правила LLM (основные)": MATERIALS / "СППР_ПРАВИЛА_LLM.md",
    "Системный промпт": MATERIALS / "СППР_СИСТЕМНЫЙ_ПРОМПТ.md",
    "Индекс typeID (JSON)": MATERIALS / "kb_typeid_index.json",
}

WORK_MODES: list[tuple[str, str]] = [
    ("diag", "Диагностика заказа"),
    ("qa", "Вопрос–ответ по заказу"),
    ("kb", "База Confluence"),
]
ADMIN_MODES: list[tuple[str, str]] = [
    ("terms", "Термины"),
    ("rules", "Правила СППР"),
    ("metrics", "Метрики"),
]
# Лимит пар вопрос–ответ (иначе десятки iframe тормозят страницу и кажется, что вопрос «не добавился»)
CHAT_MAX_MESSAGES = 24
CHAT_RICH_RENDER_LAST = 6  # цветной iframe только для последних N сообщений ассистента

WORK_MODE_HELP: dict[str, str] = {
    "diag": (
        "Разбор INT-JSON заказа: класс ошибки, typeID, шаг L2. "
        "Краткий отчёт — кнопка «Анализировать»; полный — блок ниже после анализа."
    ),
    "qa": (
        "Вопросы по полям заказа из JSON; при необходимости — поиск в Confluence (RAG). "
        "Номер заказа обязателен."
    ),
    "kb": (
        "Вопросы по выгрузкам Confluence (INT, OMS, регламенты) без номера заказа. "
        "Индекс собирается в разделе «Термины»."
    ),
}
ADMIN_MODE_HELP: dict[str, str] = {
    "terms": (
        "Синонимы L2 для Q&A, каталог полей JSON, журнал 😊/😞, "
        "пересборка каталога и индекса RAG Confluence."
    ),
    "rules": (
        "Просмотр и правка на диске: СППР_ПРАВИЛА_LLM.md, системный промпт, kb_typeid_index.json."
    ),
    "metrics": (
        "Онлайн: helpfulness, p95, успешность, доля LLM, события/заказ (журнал sppr_l2_events.jsonl). "
        "Офлайн: графики typeID, ablation, RAG — после пересчёта офлайн-метрик."
    ),
}
TERMS_HELP_REBUILD_CATALOG = (
    "Сканирует JSON-выборку и пересобирает каталог полей "
    "(эталон имён для терминов и Q&A)."
)
TERMS_HELP_REBUILD_RAG = (
    "Пересобирает TF-IDF индекс Confluence (нужен для RAG в Q&A и «База Confluence»)."
)
QA_HELP_RAG = (
    "**Confluence (RAG)** — если из JSON заказа нет готового ответа, "
    "подмешивается поиск по выгрузкам Confluence (регламенты, typeID, INT)."
)
QA_HELP_JSON = (
    "**Контекст JSON** — показывает сырой текст из файлов заказа: "
    "то, на чём строится ответ (для проверки и сложных кейсов)."
)
DIAG_HELP_LLM = (
    "Краткий отчёт движка + оформление ответа L2 через LLM. "
    "Не сочетается с **полным отчётом** (блок ниже)."
)
DIAG_HELP_RUN = (
    "**Анализировать** — краткий отчёт движка: класс, корневая ошибка **INT-2010** (выделена), "
    "INT-0992, шаг L2. Компактнее, чем ответ с LLM. "
    "Таймлайн и ветки KB — в **полном отчёте** ниже."
)
DIAG_HELP_FULL = (
    "**Полный отчёт** (`--full`): таймлайн INT, ветки KB, Confluence. "
    "Не сочетается с **Оформить через LLM**."
)
DIAG_HELP_FULL_BTN = (
    "Построить полный отчёт и показать его в блоке результата выше "
    "(включите галочку «Режим полного отчёта» или она применится при нажатии)."
)
METRIC_HELP_HELPFULNESS = (
    "**Полезность ответов (helpfulness).** Доля оценок 😊 среди всех оценок 😊 и 😞 "
    "под ответами в чатах (файл `sppr_l2_feedback.jsonl`). "
    "Показывает субъективное качество для L2, а не техническую успешность запроса."
)
METRIC_HELP_P95 = (
    "**95-й перцентиль времени ответа (p95).** По журналу `sppr_l2_events.jsonl`: "
    "95 % запросов укладываются в это время (диагностика, Q&A, Confluence). "
    "Подпись ниже — отдельно p95 для диагностики без LLM и с галкой «Оформить через LLM»."
)
METRIC_HELP_SUCCESS = (
    "**Успешность запросов.** Доля действий в UI, после которых СППР вернул результат: "
    "диагностика — есть текст отчёта или код завершения 0; Q&A и Confluence — ответ не пустой. "
    "Считаются события: `analyze_brief`, `analyze_llm`, `analyze_full`, `qa_question`, `kb_question`."
)
METRIC_HELP_LLM = (
    "**Доля диагностики с LLM.** Числитель — запуски «Анализировать» с галкой «Оформить через LLM» "
    "(`analyze_llm`). Знаменатель — все запуски диагностики: краткая, с LLM и полный отчёт. "
    "Полный отчёт (`analyze_full`) в знаменатель входит, но не считается «с LLM»."
)
METRIC_HELP_EVENTS = (
    "**Вовлечённость в пилот.** Среднее число записей в журнале на один номер заказа "
    "(диагностики, вопросы Q&A с указанным заказом). "
    "Вопросы Confluence без номера заказа в эту среднюю не входят."
)
METRIC_HELP_OFFLINE = (
    "**Офлайн-метрики** считаются пакетно по JSON-выборке (не из UI): классы ошибок, "
    "точность typeID движка, ablation (движок / гибрид / только LLM), время движка, Recall@k RAG. "
    "Пересчёт: `python diploma_metrics.py`."
)
KB_HELP_DIALOG = (
    "Короткие новые темы («Номер акции») не склеиваются с прошлым ответом про перевозчик."
)
KB_HELP_MODE = (
    "Поиск по **выгрузкам Confluence** (INT-0989, 0992, 2010, OMS, таблица L2). "
    "Номер заказа не нужен."
)
QA_HELP_HINTS = (
    "Выберите пример вопроса или введите свой в чат. Ответ строится из JSON заказа; "
    "при необходимости — с Confluence (RAG)."
)
MODE_BY_ID = {mid: label for mid, label in WORK_MODES + ADMIN_MODES}

# Подсказки на экране «Вопрос–ответ по заказу»
QA_HINT_QUESTIONS: tuple[str, ...] = (
    "Какое контактное лицо у заказчика?",
    "Какой вид оплаты в заказе?",
    "Сколько позиций в заказе?",
    "Какой способ доставки?",
    "Какая цена ZPRI у позиции 10?",
    "Адрес доставки грузополучателя",
    "Какие подарки в заказе?",
    "Какая ошибка в INT-2010?",
)

def _run_analysis(order_id: str, use_llm: bool, *, diagnose_full: bool = False) -> tuple[str, str, int]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp:
        out_path = tmp.name
    cmd = [sys.executable, str(ANALYZE), "--order", order_id, "--out", out_path]
    if diagnose_full:
        cmd.append("--full")
    if use_llm:
        cmd.append("--llm")
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(MATERIALS),
        env=env,
    )
    body = ""
    if Path(out_path).is_file():
        body = Path(out_path).read_text(encoding="utf-8").strip()
    if not body and proc.stdout:
        body = proc.stdout.strip()
    lines = [
        ln
        for ln in body.splitlines()
        if not re.match(r"^\s*Отчёт:\s*", ln, re.I)
    ]
    return "\n".join(lines).strip(), (proc.stderr or "").strip(), proc.returncode


def _new_msg_id() -> str:
    return uuid.uuid4().hex[:10]


def _qa_answer_used_llm(answer: str) -> bool:
    """Эвристика: прямой ответ из JSON без call_llm_qa."""
    if not answer:
        return False
    if "Извлечено кодом из JSON (не LLM)" in answer:
        return False
    if "*(LLM недоступен" in answer:
        return False
    if answer.strip().startswith("Заказ **") and "не найдена" in answer[:200]:
        return False
    return True


def _kb_answer_used_llm(answer: str) -> bool:
    if not answer or len(answer) < 80:
        return False
    if "релевантных фрагментов не найдено" in answer and len(answer) < 200:
        return False
    if "Индекс Confluence не собран" in answer:
        return False
    return True


def _feedback_use_llm_for_mode(mode: str) -> bool | None:
    if mode in ("diag", "diagnose"):
        return bool(st.session_state.get("diag_last_llm"))
    return None


def _rag_status() -> tuple[Any | None, bool]:
    try:
        import sppr_confluence_rag as rag

        return rag, rag.index_ready()
    except Exception:
        return None, False


def _render_l2_feedback(
    *,
    msg_id: str,
    mode: str,
    order_id: str,
    question: str,
    answer: str,
) -> None:
    from sppr_terms_io import save_answer_feedback

    state_key = f"fb_done_{msg_id}"
    if st.session_state.get(state_key):
        st.caption(st.session_state[state_key])
        return

    form_mode = st.session_state.get(f"fb_form_{msg_id}")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("😊", key=f"emo_good_{msg_id}", use_container_width=True):
            save_answer_feedback(
                mode=mode,
                order_id=order_id,
                question=question,
                answer=answer,
                rating="good",
                msg_id=msg_id,
                use_llm=_feedback_use_llm_for_mode(mode),
            )
            st.session_state[state_key] = "Сохранено 😊"
            st.rerun()
    with c2:
        if st.button("😞", key=f"emo_bad_{msg_id}", use_container_width=True):
            st.session_state[f"fb_form_{msg_id}"] = "bad"
            st.rerun()

    if form_mode == "bad":
        etalon = st.text_area(
            "Эталонный ответ (что не так)",
            key=f"etalon_{msg_id}",
            height=100,
        )
        if st.button("Отправить", key=f"send_{msg_id}", type="primary"):
            if not etalon.strip():
                st.warning("Введите текст.")
            else:
                save_answer_feedback(
                    mode=mode,
                    order_id=order_id,
                    question=question,
                    answer=answer,
                    rating=form_mode,
                    l2_etalon_answer=etalon,
                    msg_id=msg_id,
                    use_llm=_feedback_use_llm_for_mode(mode),
                )
                st.session_state[state_key] = "Эталон записан"
                st.session_state.pop(f"fb_form_{msg_id}", None)
                st.rerun()


def _trim_chat_messages(messages: list[dict]) -> list[dict]:
    if len(messages) <= CHAT_MAX_MESSAGES:
        return messages
    return messages[-CHAT_MAX_MESSAGES:]


def _append_chat_turn(messages: list[dict], user_text: str, assistant_text: str, **extra: Any) -> list[dict]:
    """Новая пара в конец списка (хронология для RAG/логики); порядок на экране — в _render_chat_history."""
    out = [
        *messages,
        {"role": "user", "content": user_text.strip()},
        {"role": "assistant", "content": assistant_text, **extra},
    ]
    return _trim_chat_messages(out)


def _chat_display_newest_first(messages: list[dict]) -> list[dict]:
    """Пары user→assistant; сверху — последний вопрос и ответ."""
    pairs: list[list[dict]] = []
    i = 0
    while i < len(messages):
        if messages[i].get("role") == "user":
            pair = [messages[i]]
            if (
                i + 1 < len(messages)
                and messages[i + 1].get("role") == "assistant"
            ):
                pair.append(messages[i + 1])
                i += 2
            else:
                i += 1
            pairs.append(pair)
        else:
            pairs.append([messages[i]])
            i += 1
    out: list[dict] = []
    for pair in reversed(pairs):
        out.extend(pair)
    return out


def _render_chat_history(
    messages: list[dict],
    *,
    mode: str,
    default_order: str,
    newest_first: bool = False,
) -> None:
    trimmed = _trim_chat_messages(messages)
    if len(trimmed) < len(messages):
        st.caption(
            f"Показаны последние {len(trimmed) // 2} пар вопрос–ответ "
            f"(старые скрыты, чтобы не тормозить интерфейс). Нажмите «Очистить», если нужен сброс."
        )
    display = _chat_display_newest_first(trimmed) if newest_first else trimmed
    n = len(display)
    rich_to = min(CHAT_RICH_RENDER_LAST, n)
    for i, msg in enumerate(display):
        with st.chat_message(msg["role"]):
            if msg["role"] == "assistant":
                if i < rich_to:
                    render_sppr_text(msg["content"])
                else:
                    st.markdown(msg["content"])
            else:
                st.markdown(msg["content"])
            if msg.get("role") == "assistant" and msg.get("msg_id"):
                q_for_fb = ""
                if i > 0 and display[i - 1].get("role") == "user":
                    q_for_fb = display[i - 1].get("content", "")
                _render_l2_feedback(
                    msg_id=msg["msg_id"],
                    mode=mode,
                    order_id=msg.get("order_id") or default_order,
                    question=q_for_fb,
                    answer=msg.get("content", ""),
                )


def _diag_on_llm_toggle() -> None:
    if st.session_state.get("diag_llm"):
        st.session_state.diag_full_report = False


def _diag_on_full_toggle() -> None:
    if st.session_state.get("diag_full_report"):
        st.session_state.diag_llm = False


def _diag_request_full_build() -> None:
    """Запрос полного отчёта (on_click): нельзя менять diag_full_report после чекбокса."""
    st.session_state.diag_llm = False
    st.session_state["_diag_pending_full"] = True


def _diag_store_result(
    oid: str, display: str, err: str, code: int, *, diagnose_full: bool, use_llm: bool
) -> None:
    old_mid = str(st.session_state.get("diag_feedback_msg_id") or "")
    if old_mid:
        st.session_state.pop(f"fb_done_{old_mid}", None)
        st.session_state.pop(f"fb_form_{old_mid}", None)
    st.session_state.diag_feedback_msg_id = _new_msg_id()

    st.session_state.diag_last_oid = oid
    if not diagnose_full:
        display = strip_brief_report_footer(display)
    st.session_state.diag_last_display = display
    st.session_state.diag_last_err = err
    st.session_state.diag_last_code = code
    st.session_state.diag_last_full = diagnose_full
    st.session_state.diag_last_llm = use_llm


def _diag_run_order(oid: str, *, use_llm: bool, diagnose_full: bool) -> tuple[str, str, int]:
    from sppr_terms_io import append_event

    label = "LLM + движок…" if use_llm else (
        "Полный отчёт движка…" if diagnose_full else "Диагностика…"
    )
    ev = "analyze_full" if diagnose_full else ("analyze_llm" if use_llm else "analyze_brief")
    t0 = time.perf_counter()
    with st.spinner(label):
        display, err, code = _run_analysis(oid, use_llm, diagnose_full=diagnose_full)
    ms = (time.perf_counter() - t0) * 1000
    ok = bool(display) or (code == 0)
    append_event(
        ev,
        mode="diag",
        order_id=oid,
        ok=ok,
        total_ms=ms,
        use_llm=use_llm or diagnose_full,
        detail=(err or "")[:200],
    )
    _diag_store_result(oid, display, err, code, diagnose_full=diagnose_full, use_llm=use_llm)
    return display, err, code


def _diag_render_result_block(oid: str, display: str, err: str, code: int) -> None:
    if code != 0 and not display:
        st.error(err or "Ошибка (см. НАСТРОЙКА_GIGACHAT.md)")
        return
    if err and code != 0:
        st.warning(err)
    if "✅ Ошибок в интеграционных сценариях" in display:
        st.success(
            "Ошибок в интеграционных сценариях по этому заказу не найдено "
            "(последний INT-2010 без severity 3)."
        )
    elif "Найдена ошибка в интеграционных сценариях по заказу" in display:
        st.warning(
            f"Найдена ошибка в интеграционных сценариях по заказу {oid}."
        )
    render_sppr_text(strip_brief_report_footer(display))
    fb_mid = str(st.session_state.get("diag_feedback_msg_id") or "")
    if not fb_mid:
        fb_mid = _new_msg_id()
        st.session_state.diag_feedback_msg_id = fb_mid
    _render_l2_feedback(
        msg_id=fb_mid,
        mode="diag",
        order_id=oid,
        question="диагностика заказа",
        answer=display,
    )


def _page_diagnose() -> None:
    if "diag_order" not in st.session_state:
        st.session_state.diag_order = "DEMO_ORDER_001"
    if "diag_full_report" not in st.session_state:
        st.session_state.diag_full_report = False
    if "diag_llm" not in st.session_state:
        st.session_state.diag_llm = False

    if st.session_state.pop("_diag_pending_full", False):
        oid_pending = (st.session_state.get("diag_order") or "").strip() or str(
            st.session_state.get("diag_last_oid") or ""
        )
        if oid_pending:
            st.session_state.diag_llm = False
            st.session_state.diag_full_report = True
            _diag_run_order(oid_pending, use_llm=False, diagnose_full=True)

    full_mode = bool(st.session_state.diag_full_report)
    llm_mode = bool(st.session_state.diag_llm)
    st.markdown(
        '<div class="sppr-order-field-narrow" aria-hidden="true"></div>',
        unsafe_allow_html=True,
    )
    order_id = st.text_input("Номер заказа", key="diag_order")

    st.checkbox(
        "Оформить через LLM",
        key="diag_llm",
        disabled=full_mode,
        on_change=_diag_on_llm_toggle,
        help=DIAG_HELP_LLM,
    )

    run = st.button("Анализировать", key="diag_run", type="primary")

    if run and order_id.strip():
        oid = order_id.strip()
        st.session_state.diag_full_report = False
        _diag_run_order(oid, use_llm=bool(st.session_state.diag_llm), diagnose_full=False)

    if st.session_state.get("diag_last_display"):
        oid = str(st.session_state.get("diag_last_oid") or "")
        _diag_render_result_block(
            oid,
            st.session_state.diag_last_display,
            st.session_state.get("diag_last_err") or "",
            int(st.session_state.get("diag_last_code") or 0),
        )

        st.divider()
        st.markdown("**Полный отчёт движка**")

        c_full, c_full_h = st.columns([0.94, 0.06], gap="small")
        with c_full:
            st.checkbox(
                "Режим полного отчёта (--full)",
                key="diag_full_report",
                disabled=llm_mode,
                on_change=_diag_on_full_toggle,
            )
        with c_full_h:
            render_help_icon(DIAG_HELP_FULL, key="diag_help_full")

        oid_full = order_id.strip() or oid
        render_button_with_help(
            "Построить полный отчёт",
            help_text=DIAG_HELP_FULL_BTN,
            key="diag_run_full",
            type="secondary",
            disabled=llm_mode or not oid_full,
            on_click=_diag_request_full_build,
            help_width=0.06,
        )


def _qa_sync_hint_from_pills() -> None:
    v = st.session_state.get("qa_hint_pills")
    if v:
        st.session_state.qa_pending = v


def _render_qa_hint_pills() -> None:
    st.pills(
        "Примеры вопросов",
        options=list(QA_HINT_QUESTIONS),
        key="qa_hint_pills",
        on_change=_qa_sync_hint_from_pills,
        selection_mode="single",
        label_visibility="collapsed",
        width="content",
        help=QA_HELP_HINTS,
    )


def _render_qa_order_toolbar() -> str:
    """Номер заказа и переключатели RAG / контекст JSON в одной строке."""
    if "qa_order" not in st.session_state:
        st.session_state.qa_order = "DEMO_ORDER_001"  # эталон 143 / B→A
    if "opt_use_rag" not in st.session_state:
        st.session_state.opt_use_rag = True
    if "qa_show_json_ctx" not in st.session_state:
        st.session_state.qa_show_json_ctx = False

    rag_on = bool(st.session_state.opt_use_rag)
    ctx_on = bool(st.session_state.qa_show_json_ctx)

    st.markdown(
        '<span class="sppr-qa-toolbar-anchor" aria-hidden="true" style="display:none"></span>',
        unsafe_allow_html=True,
    )
    c_ord, c_rag, c_rag_h, c_json, c_json_h = st.columns([1.15, 1.48, 0.08, 1.48, 0.08])
    with c_ord:
        st.markdown(
            '<div class="sppr-order-field-narrow" aria-hidden="true"></div>',
            unsafe_allow_html=True,
        )
        oid = st.text_input("Номер заказа", key="qa_order")
    with c_rag:
        st.markdown(
            f'<span class="qa-marker-rag" data-active="{"true" if rag_on else "false"}" '
            'style="display:none" aria-hidden="true"></span>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="sppr-btn-help-row" aria-hidden="true"></div>',
            unsafe_allow_html=True,
        )
        if st.button(
            ("✓ " if rag_on else "") + "Confluence (RAG)",
            key="qa_btn_rag",
            type="secondary",
            use_container_width=True,
        ):
            st.session_state.opt_use_rag = not rag_on
            st.rerun()
    with c_rag_h:
        render_help_icon(QA_HELP_RAG, key="qa_help_rag")
    with c_json:
        st.markdown(
            f'<span class="qa-marker-json" data-active="{"true" if ctx_on else "false"}" '
            'style="display:none" aria-hidden="true"></span>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="sppr-btn-help-row" aria-hidden="true"></div>',
            unsafe_allow_html=True,
        )
        if st.button(
            ("✓ " if ctx_on else "") + "Контекст JSON",
            key="qa_btn_json_ctx",
            type="secondary",
            use_container_width=True,
        ):
            st.session_state.qa_show_json_ctx = not ctx_on
            st.rerun()
    with c_json_h:
        render_help_icon(QA_HELP_JSON, key="qa_help_json")

    if st.session_state.qa_show_json_ctx and oid.strip():
        try:
            from sppr_json_qa import build_qa_context

            with st.expander("Контекст JSON для ответа", expanded=True):
                st.text_area(
                    "JSON",
                    build_qa_context(oid.strip()),
                    height=220,
                    disabled=True,
                    label_visibility="collapsed",
                )
        except Exception as ex:
            st.warning(str(ex))

    return oid


def _page_qa() -> None:
    """Вопросы по конкретному заказу: сначала факты из JSON, при необходимости Confluence+LLM."""
    oid = _render_qa_order_toolbar()
    _render_qa_hint_pills()

    if "qa_messages" not in st.session_state:
        st.session_state.qa_messages = []
    st.session_state.qa_messages = _trim_chat_messages(st.session_state.qa_messages)

    _render_chat_history(
        st.session_state.qa_messages,
        mode="qa",
        default_order=oid.strip(),
        newest_first=True,
    )

    q = st.chat_input("Вопрос по заказу…")
    pending = st.session_state.pop("qa_pending", None)
    if pending:
        q = pending

    use_rag = st.session_state.get("opt_use_rag", True)
    _, rag_ok = _rag_status()

    if q and oid.strip():
        with st.spinner("Ответ…"):
            try:
                from sppr_json_qa import answer_question
                from sppr_terms_io import append_event

                t0 = time.perf_counter()
                ans = answer_question(oid.strip(), q, use_rag=use_rag and rag_ok)
                ms = (time.perf_counter() - t0) * 1000
                llm_used = _qa_answer_used_llm(ans)
                append_event(
                    "qa_question",
                    mode="qa",
                    order_id=oid.strip(),
                    ok=bool(ans),
                    total_ms=ms,
                    use_llm=llm_used,
                )
                mid = _new_msg_id()
                st.session_state.qa_messages = _append_chat_turn(
                    st.session_state.qa_messages,
                    q,
                    ans,
                    msg_id=mid,
                    order_id=oid.strip(),
                )
                st.rerun()
            except Exception as ex:
                st.error(str(ex))

    if st.session_state.qa_messages and st.button("Очистить диалог", key="qa_clear"):
        st.session_state.qa_messages = []
        st.session_state.pop("qa_hint_pills", None)
        st.rerun()


def _page_confluence_kb() -> None:
    """Отдельный режим: инструкции и поля INT без привязки к номеру заказа."""
    if "kb_use_dialog_context" not in st.session_state:
        st.session_state.kb_use_dialog_context = True
    st.caption(
        "Поиск по выгрузкам Confluence (INT, OMS, L2). Номер заказа не нужен."
    )
    st.session_state.kb_use_dialog_context = st.checkbox(
        "Учитывать контекст диалога (только явные уточнения)",
        value=st.session_state.kb_use_dialog_context,
        key="kb_use_dialog_context_cb",
        help=f"{KB_HELP_DIALOG}\n\n{KB_HELP_MODE}",
    )

    rag, rag_ok = _rag_status()
    if not rag_ok:
        st.warning(
            "Индекс не собран: раздел **Термины** → «Пересобрать RAG Confluence» "
            "или `python build_confluence_rag_index.py`."
        )

    if "kb_messages" not in st.session_state:
        st.session_state.kb_messages = []
    st.session_state.kb_messages = _trim_chat_messages(st.session_state.kb_messages)

    _render_chat_history(
        st.session_state.kb_messages,
        mode="kb",
        default_order="",
    )

    q = st.chat_input("Вопрос по Confluence…")
    pending = st.session_state.pop("kb_pending", None)
    if pending:
        q = pending

    if q:
        with st.spinner("Confluence…"):
            try:
                from sppr_terms_io import append_event

                if rag is None:
                    raise RuntimeError("Модуль RAG недоступен")
                history = list(st.session_state.kb_messages)
                t0 = time.perf_counter()
                ans = rag.answer_confluence_only(
                    q,
                    chat_history=history,
                    use_dialog_context=st.session_state.kb_use_dialog_context,
                )
                ms = (time.perf_counter() - t0) * 1000
                append_event(
                    "kb_question",
                    mode="kb",
                    order_id="",
                    ok=bool(ans),
                    total_ms=ms,
                    use_llm=_kb_answer_used_llm(ans),
                )
                mid = _new_msg_id()
                st.session_state.kb_messages = _append_chat_turn(
                    st.session_state.kb_messages,
                    q,
                    ans,
                    msg_id=mid,
                    order_id="",
                )
                st.rerun()
            except Exception as ex:
                st.error(str(ex))

    if st.session_state.kb_messages and st.button("Очистить", key="kb_clear"):
        st.session_state.kb_messages = []
        st.rerun()


def _page_terms() -> None:
    from sppr_terms_io import (
        add_l2_term,
        append_feedback,
        delete_l2_term,
        load_code_user,
        load_feedback,
        load_l2_terms,
        run_rebuild_catalog,
        save_code_user,
    )

    tab_add, tab_list, tab_catalog, tab_codes, tab_fb = st.tabs(
        [
            "Термины L2",
            "Список",
            "Словарь полей JSON",
            "Коды",
            "Обратная связь",
        ]
    )

    with tab_add:
        c1, c2 = st.columns(2)
        with c1:
            user_terms_raw = st.text_input(
                "Как спрашивает L2 (через запятую)",
                placeholder="кл гп, контакт грузополучателя",
                key="term_user",
            )
            meaning = st.text_input("Расшифровка", key="term_meaning")
            category = st.selectbox(
                "Категория",
                ["partners", "delivery", "gifts", "services", "pricing", "logistics", "l2_custom"],
                key="term_cat",
            )
        with c2:
            path_0992 = st.text_input("json_path 0992", key="term_p992")
            path_0989 = st.text_input("json_path 0989", key="term_p989")
            role_s4 = st.text_input("Роль (AG, WE, ZF…)", key="term_role")
            note = st.text_area("Примечание", height=68, key="term_note")
        if st.button("Добавить термин", type="primary", key="term_add"):
            terms = [t.strip() for t in user_terms_raw.split(",") if t.strip()]
            if not terms or not meaning.strip():
                st.error("Укажите синонимы и расшифровку.")
            else:
                add_l2_term(
                    {
                        "user_terms": terms,
                        "meaning_ru": meaning.strip(),
                        "category": category,
                        "json_path_0992": path_0992.strip() or None,
                        "json_path_0989": path_0989.strip() or None,
                        "role_s4": role_s4.strip() or None,
                        "note": note.strip(),
                    }
                )
                st.success(f"Добавлено: {', '.join(terms)}")
                st.rerun()

    with tab_list:
        data = load_l2_terms()
        terms = data.get("terms") or []
        st.caption(f"`{L2_TERMS.name}` — {len(terms)} записей")
        for t in terms:
            with st.expander(f"{t.get('id')} — {', '.join(t.get('user_terms') or [])}"):
                st.json(t)
                if st.button("Удалить", key=f"del_{t.get('id')}"):
                    delete_l2_term(str(t.get("id")))
                    st.rerun()

    with tab_catalog:
        st.caption(
            "Эталон имён полей INT (0989, 0992, 0990). "
            "Собирается кнопкой «Пересобрать каталог» внизу страницы."
        )
        if FIELD_CATALOG.is_file():
            data = json.loads(FIELD_CATALOG.read_text(encoding="utf-8"))
            st.json(data)
        else:
            st.info("Файл каталога отсутствует — выполните пересборку каталога.")

    with tab_codes:
        cu = load_code_user()
        edited = st.text_area(
            "json_code_lookups_user.json",
            value=json.dumps(cu, ensure_ascii=False, indent=2),
            height=360,
            key="code_user_edit",
        )
        if st.button("Сохранить", key="code_save"):
            try:
                save_code_user(json.loads(edited))
                st.success("Сохранено")
            except json.JSONDecodeError as e:
                st.error(str(e))

    with tab_fb:
        from sppr_terms_io import (
            clear_all_feedback,
            delete_feedback_indices,
            feedback_row_label,
            feedback_summary,
            feedback_to_csv,
        )

        st.caption(
            "Оценки под ответами в чатах (😊 / 😞). Файл: `sppr_l2_feedback.jsonl`"
        )
        rows = load_feedback()
        if rows:
            st.dataframe(rows, use_container_width=True, height=min(420, 80 + len(rows) * 35))

            st.download_button(
                "Скачать CSV",
                data=feedback_to_csv().encode("utf-8-sig"),
                file_name="sppr_l2_feedback_export.csv",
                mime="text/csv",
                key="fb_csv_dl",
            )

            summ = feedback_summary()
            m1, m2, m3 = st.columns(3)
            m1, m2 = st.columns(2)
            m1.metric("😊 хорошо", summ.get("good", 0))
            m2.metric("😞 уточнить", summ.get("bad", 0))
            if summ.get("other", 0):
                st.caption(
                    f"Прочие строки в файле (не answer_rating): {summ['other']} — "
                    "удаляются через «Очистить записи» или «Очистить всё»."
                )
            st.caption(f"Всего строк в файле: {summ.get('total', 0)}")

            st.markdown("**Очистка журнала**")
            if "fb_delete_mode" not in st.session_state:
                st.session_state.fb_delete_mode = False
            if "fb_clear_all_confirm" not in st.session_state:
                st.session_state.fb_clear_all_confirm = False

            c_mode, c_all = st.columns(2)
            with c_mode:
                if not st.session_state.fb_delete_mode:
                    if st.button("Очистить записи", key="fb_start_select"):
                        st.session_state.fb_delete_mode = True
                        st.session_state.fb_clear_all_confirm = False
                        st.rerun()
                else:
                    st.info("Выберите строки в списке ниже и нажмите «Удалить выбранные».")
            with c_all:
                if st.button("Очистить всё", key="fb_start_clear_all"):
                    st.session_state.fb_clear_all_confirm = True
                    st.session_state.fb_delete_mode = False
                    st.rerun()

            if st.session_state.fb_delete_mode:
                options = list(range(len(rows)))
                picked = st.multiselect(
                    "Записи для удаления",
                    options=options,
                    format_func=lambda i: feedback_row_label(i, rows[i]),
                    key="fb_pick_rows",
                )
                c_del, c_cancel = st.columns(2)
                with c_del:
                    if st.button(
                        f"Удалить выбранные ({len(picked)})",
                        type="primary",
                        disabled=not picked,
                        key="fb_delete_picked",
                    ):
                        n = delete_feedback_indices(set(picked))
                        st.session_state.fb_delete_mode = False
                        st.success(f"Удалено записей: {n}")
                        st.rerun()
                with c_cancel:
                    if st.button("Отмена", key="fb_cancel_select"):
                        st.session_state.fb_delete_mode = False
                        st.rerun()

            if st.session_state.fb_clear_all_confirm:
                st.warning(
                    f"Будут удалены **все {len(rows)}** записей из `sppr_l2_feedback.jsonl`. "
                    "Действие необратимо (сначала скачайте CSV при необходимости)."
                )
                c_yes, c_no = st.columns(2)
                with c_yes:
                    if st.button("Да, удалить всё", type="primary", key="fb_clear_all_yes"):
                        n = clear_all_feedback()
                        st.session_state.fb_clear_all_confirm = False
                        st.session_state.fb_delete_mode = False
                        st.success(f"Журнал очищен ({n} записей).")
                        st.rerun()
                with c_no:
                    if st.button("Отмена", key="fb_clear_all_no"):
                        st.session_state.fb_clear_all_confirm = False
                        st.rerun()
        else:
            st.info("Записей обратной связи пока нет.")

        with st.expander("Ручная запись (редко)"):
            fb_order = st.text_input("Заказ", key="fb_order")
            fb_ctx = st.text_input("Контекст", key="fb_ctx")
            fb_sppr = st.text_area("СППР", height=60, key="fb_sppr")
            fb_l2 = st.text_area("L2", height=60, key="fb_l2")
            if st.button("Записать", key="fb_add"):
                append_feedback(
                    {
                        "order_id": fb_order.strip(),
                        "context": fb_ctx.strip(),
                        "sppr_hint": fb_sppr.strip(),
                        "l2_resolution": fb_l2.strip(),
                        "helpful": True,
                    }
                )
                st.success("Записано")
                st.rerun()

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        if render_button_with_help(
            "Пересобрать каталог полей",
            help_text=TERMS_HELP_REBUILD_CATALOG,
            key="term_rebuild_catalog",
            type="primary",
            help_width=0.08,
        ):
            with st.spinner("…"):
                code, out, err = run_rebuild_catalog()
            if code == 0:
                st.success("Каталог обновлён")
            else:
                st.error(err or "Ошибка")
    with c2:
        if render_button_with_help(
            "Пересобрать RAG Confluence",
            help_text=TERMS_HELP_REBUILD_RAG,
            key="term_rebuild_rag",
            help_width=0.08,
        ):
            try:
                from sppr_confluence_rag import build_index
                import sppr_confluence_rag as rag_mod

                meta = build_index(verbose=False)
                rag_mod._load_index.cache_clear()
                st.success(f"RAG: {meta['n_chunks']} фрагментов")
            except Exception as ex:
                st.error(str(ex))


def _page_rules() -> None:
    st.caption("Файлы правил на диске (UTF-8). Перед правками сделайте копию.")
    c_sel, c_h = st.columns([11, 0.6])
    with c_sel:
        choice = st.selectbox("Файл", list(RULE_FILES.keys()), key="rules_file")
    with c_h:
        render_help_icon(
            "Правила LLM — что подмешивается при «Оформить через LLM»; "
            "индекс typeID — подсказки движка по коду ошибки.",
            key="rules_file_help",
        )
    path = RULE_FILES[choice]
    if not path.is_file():
        st.error(f"Нет файла: {path}")
        return
    edited = st.text_area(
        path.name,
        value=path.read_text(encoding="utf-8"),
        height=480,
        key=f"rules_edit_{path.name}",
    )
    if st.button("Сохранить", type="primary", key="rules_save"):
        if path.suffix == ".json":
            try:
                json.loads(edited)
            except json.JSONDecodeError as e:
                st.error(f"JSON: {e}")
                return
        path.write_text(edited, encoding="utf-8")
        st.success("Сохранено")


def _page_metrics() -> None:
    from sppr_terms_io import events_to_csv, online_metrics_summary

    st.subheader("Онлайн-метрики пилота (Streamlit)")
    st.caption(
        "Журнал: `sppr_l2_events.jsonl` (запросы и время). Оценки: `sppr_l2_feedback.jsonl`. "
        "Пока мало данных — метрики обновляются после работы в UI."
    )
    om = online_metrics_summary()
    c1, c2, c3, c4, c5 = st.columns(5)
    hp = om.get("helpfulness_pct")
    c1.metric(
        "😊 полезных",
        f"{hp}%" if hp is not None else "—",
        help=METRIC_HELP_HELPFULNESS,
    )
    c1.caption(f"оценок: {om.get('feedback_rated', 0)}")
    c2.metric(
        "p95 ответа",
        f"{om.get('p95_total_ms') or '—'} мс",
        help=METRIC_HELP_P95,
    )
    c2.caption(
        f"диагн. без LLM: {om.get('p95_analyze_brief_ms') or '—'} · "
        f"с LLM: {om.get('p95_analyze_llm_ms') or '—'}"
    )
    sr = om.get("success_rate_pct")
    c3.metric(
        "Успешных запросов",
        f"{sr}%" if sr is not None else "—",
        help=METRIC_HELP_SUCCESS,
    )
    c3.caption(f"{om.get('requests_ok', 0)} / {om.get('requests', 0)}")
    la = om.get("llm_adoption_pct")
    c4.metric(
        "Диагностика с LLM",
        f"{la}%" if la is not None else "—",
        help=METRIC_HELP_LLM,
    )
    c4.caption(f"{om.get('analyze_llm', 0)} / {om.get('analyze_total', 0)}")
    c5.metric(
        "Событий / заказ",
        om.get("avg_events_per_order") or "—",
        help=METRIC_HELP_EVENTS,
    )
    c5.caption(f"заказов: {om.get('orders_with_events', 0)}")
    if om.get("by_mode_feedback"):
        with st.expander("😊/😞 по режимам"):
            st.json(om["by_mode_feedback"])
    st.download_button(
        "Скачать журнал событий (CSV)",
        data=events_to_csv().encode("utf-8-sig"),
        file_name="sppr_l2_events_export.csv",
        mime="text/csv",
        key="events_csv_dl",
    )

    st.divider()
    off_t, off_h = st.columns([12, 0.5])
    with off_t:
        st.subheader("Офлайн-метрики корпуса")
    with off_h:
        render_help_icon(METRIC_HELP_OFFLINE, key="metric_offline_help")
    st.caption(
        "Графики по JSON-выборке. Пересчёт: `python diploma_metrics.py` в папке проекта."
    )
    if not CHARTS.is_dir():
        st.info(
            f"Папка не найдена: `{CHARTS}` — выполните пересчёт офлайн-метрик "
            "(diploma_metrics.py)."
        )
        return
    pngs = sorted(CHARTS.glob("*.png"))
    if not pngs:
        st.info("Нет PNG — выполните пересчёт офлайн-метрик (diploma_metrics.py).")
        return
    labels = {
        "01_sppr_classes.png": "Классы ошибок A / B / B→A",
        "02_typeid_accuracy.png": "Точность typeID (движок)",
        "03_typeid_frequency.png": "Частота кодов typeID",
        "04_crm_imk.png": "Источник заказа: CRM vs ИМК",
        "05_diagnose_latency.png": "Время ответа движка",
        "06_ablation_typeid.png": "Ablation: движок vs LLM",
    }
    pick = st.multiselect(
        "Рисунки",
        [p.name for p in pngs],
        default=[p.name for p in pngs[:3]],
    )
    cols = st.columns(2)
    for i, name in enumerate(pick):
        path = CHARTS / name
        cap = labels.get(name, name)
        with cols[i % 2]:
            st.image(str(path), caption=cap, use_container_width=True)

    rag_report = MATERIALS / "diploma_results" / "rag" / "rag_eval_report.md"
    if rag_report.is_file():
        with st.expander("Отчёт RAG (Recall@k)"):
            st.markdown(rag_report.read_text(encoding="utf-8"))

    golden_report = MATERIALS / "diploma_results" / "rag" / "golden_regression_report.md"
    if golden_report.is_file():
        with st.expander("Эталонные 50 вопросов Confluence (регрессия)"):
            st.markdown(golden_report.read_text(encoding="utf-8"))
    if st.button("Пересобрать RAG + прогон 50 вопросов", key="btn_rebuild_rag_golden"):
        import subprocess
        py = sys.executable
        with st.spinner("Сборка индекса и прогон…"):
            subprocess.run([py, str(MATERIALS / "build_confluence_rag_index.py")], cwd=str(MATERIALS), check=False)
            subprocess.run([py, str(MATERIALS / "run_confluence_golden_regression.py"), "--with-rag"], cwd=str(MATERIALS), check=False)
        st.cache_data.clear()
        st.success("Готово. Обновите страницу или откройте отчёт в expander выше.")


@st.cache_data(ttl=300)
def _kb_sidebar_examples() -> tuple[str, ...]:
    """Эталонные вопросы из sppr_confluence_golden_qa.json + базовые."""
    default = (
        "Что значит typeID 143?",
        "Цепочка 0989→0992→2010",
        "Роль партнёра ZF",
        "В каком поле передаётся способ доставки по INT?",
        "Как называется поле по позиции подарка в INT?",
        "Что означает partnerFunction=00000001?",
    )
    path = MATERIALS / "sppr_confluence_golden_qa.json"
    if not path.is_file():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        qs = [str(x.get("question") or "").strip() for x in (data.get("questions") or [])]
        qs = [q for q in qs if q]
        if not qs:
            return default
        # приоритет: разные категории в начале sidebar
        seen_cat: set[str] = set()
        ordered: list[str] = []
        for item in data.get("questions") or []:
            q = str(item.get("question") or "").strip()
            cat = str(item.get("category") or "")
            if q and cat not in seen_cat:
                ordered.append(q)
                seen_cat.add(cat)
        for q in qs:
            if q not in ordered:
                ordered.append(q)
        return tuple(ordered[:50])
    except (json.JSONDecodeError, OSError):
        return default


def _sidebar_demo_orders(mode_id: str) -> None:
    """Кнопки эталонных заказов (список — sppr_demo_showcase.json)."""
    from sppr_demo_showcase import demo_orders

    orders = demo_orders()
    if not orders:
        return
    st.sidebar.subheader("Примеры заказов")
    prefix = "sb_diag" if mode_id == "diag" else "sb_qa"
    state_key = "diag_order" if mode_id == "diag" else "qa_order"
    for o in orders:
        oid = str(o.get("order_id") or "").strip()
        if not oid:
            continue
        lbl = str(o.get("label") or oid)
        c_oid, c_h = st.sidebar.columns([0.92, 0.08], gap="small")
        with c_oid:
            if st.button(oid, key=f"{prefix}_{oid}", use_container_width=True):
                st.session_state[state_key] = oid
                st.rerun()
        with c_h:
            render_help_icon(lbl, key=f"{prefix}_help_{oid}")


def _sidebar_options(mode_id: str) -> None:
    """Доп. настройки режима в sidebar."""
    st.sidebar.markdown("---")
    if mode_id in ("diag", "qa"):
        _sidebar_demo_orders(mode_id)
    elif mode_id == "kb":
        st.sidebar.subheader("Confluence")
        st.sidebar.caption("Без номера заказа")
        kb_examples = _kb_sidebar_examples()
        for i, ex in enumerate(kb_examples[:8]):
            if st.sidebar.button(ex, key=f"sb_kb_ex_{i}"):
                st.session_state.kb_pending = ex
        if len(kb_examples) > 8:
            with st.sidebar.expander(f"Ещё эталонные вопросы ({len(kb_examples) - 8})"):
                for j, ex in enumerate(kb_examples[8:20]):
                    if st.button(ex, key=f"sb_kb_ex_more_{j}"):
                        st.session_state.kb_pending = ex
                        st.rerun()


def _sidebar_nav() -> str:
    nav = st.session_state.get("nav_mode", "diag")

    for mid, label in WORK_MODES:
        btn_type = "primary" if nav == mid else "secondary"
        help_txt = WORK_MODE_HELP.get(mid, "")
        c_btn, c_h = st.columns([0.92, 0.08], gap="small")
        with c_btn:
            if st.button(
                label,
                key=f"nav_{mid}",
                use_container_width=True,
                type=btn_type,
            ):
                st.session_state.nav_mode = mid
                st.rerun()
        with c_h:
            if help_txt:
                render_help_icon(help_txt, key=f"nav_help_{mid}")

    st.markdown("---")
    st.markdown("**Для администратора**")
    for mid, label in ADMIN_MODES:
        btn_type = "primary" if nav == mid else "secondary"
        help_txt = ADMIN_MODE_HELP.get(mid, "")
        c_btn, c_h = st.columns([0.92, 0.08], gap="small")
        with c_btn:
            if st.button(
                label,
                key=f"nav_adm_{mid}",
                use_container_width=True,
                type=btn_type,
            ):
                st.session_state.nav_mode = mid
                st.rerun()
        with c_h:
            if help_txt:
                render_help_icon(help_txt, key=f"nav_adm_help_{mid}")

    return st.session_state.get("nav_mode", "diag")


def main() -> None:
    st.set_page_config(page_title="СППР", layout="wide", initial_sidebar_state="expanded")
    inject_sppr_styles()

    if "nav_mode" not in st.session_state:
        st.session_state.nav_mode = "diag"

    with st.sidebar:
        st.title("СППР")
        mode_id = _sidebar_nav()
        _sidebar_options(mode_id)

    mode_id = st.session_state.nav_mode
    title = MODE_BY_ID.get(mode_id, "")
    st.header(title)

    if mode_id == "diag":
        _page_diagnose()
    elif mode_id == "qa":
        _page_qa()
    elif mode_id == "kb":
        _page_confluence_kb()
    elif mode_id == "terms":
        _page_terms()
    elif mode_id == "rules":
        _page_rules()
    elif mode_id == "metrics":
        _page_metrics()


if __name__ == "__main__":
    main()
