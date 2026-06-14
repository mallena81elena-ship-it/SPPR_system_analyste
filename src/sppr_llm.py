# -*- coding: utf-8 -*-
"""
Единый вызов LLM для СППР: OpenAI-compatible (OpenAI-compatible provider) или GigaChat (Сбер).

Переменные окружения (или файл .env в этой папке):
  SPPR_LLM_PROVIDER     — openai | gigachat  (по умолчанию openai)
  SPPR_LLM_API_KEY      — ключ API / Authorization key GigaChat
  GIGACHAT_CREDENTIALS  — то же для GigaChat (если SPPR_LLM_API_KEY пуст)
  SPPR_LLM_BASE_URL     — для openai: https://api.openai_compatible.com/v1
  SPPR_LLM_MODEL        — openai: openai_compatible-chat | gigachat: GigaChat
  SPPR_GIGACHAT_SCOPE   — GIGACHAT_API_PERS | GIGACHAT_API_B2B | GIGACHAT_API_CORP
  SPPR_GIGACHAT_VERIFY_SSL — true/false (на Windows часто false)
"""
from __future__ import annotations

import base64
import binascii
import os
import re
from pathlib import Path

MATERIALS = Path(__file__).parent


def load_env() -> None:
    """Загрузить .env из папки материалов (не коммитить .env в git)."""
    env_path = MATERIALS / ".env"
    try:
        from dotenv import load_dotenv

        if env_path.is_file():
            load_dotenv(env_path)
        else:
            load_dotenv()
    except ImportError:
        pass


def _provider() -> str:
    return (os.environ.get("SPPR_LLM_PROVIDER") or "openai").strip().lower()


def _api_key() -> str:
    return (
        os.environ.get("SPPR_LLM_API_KEY", "").strip()
        or os.environ.get("GIGACHAT_CREDENTIALS", "").strip()
    )


def normalize_gigachat_credentials(raw: str) -> str:
    """
    Ключ авторизации GigaChat = base64(Client ID:Client Secret).
    В личном кабинете он уже выдан одной строкой (не добавляйте Basic/Bearer).
    """
    s = (raw or "").strip().strip('"').strip("'")
    for prefix in ("Basic ", "basic ", "Bearer ", "bearer "):
        if s.startswith(prefix):
            s = s[len(prefix) :].strip()
    s = re.sub(r"\s+", "", s)

    cid = os.environ.get("GIGACHAT_CLIENT_ID", "").strip()
    secret = os.environ.get("GIGACHAT_CLIENT_SECRET", "").strip()
    if cid and secret:
        s = base64.b64encode(f"{cid}:{secret}".encode("utf-8")).decode("ascii")

    if ":" in s and not _is_valid_b64_credentials(s):
        s = base64.b64encode(s.encode("utf-8")).decode("ascii")

    return s


def _is_valid_b64_credentials(s: str) -> bool:
    try:
        base64.b64decode(s, validate=True)
        return True
    except (ValueError, binascii.Error):
        return False


def validate_gigachat_credentials(raw: str) -> tuple[str, str | None]:
    """Возвращает (нормализованный ключ, текст ошибки или None)."""
    cred = normalize_gigachat_credentials(raw)
    if not cred:
        return "", "Ключ пустой"
    if not _is_valid_b64_credentials(cred):
        return cred, (
            "Строка не похожа на ключ авторизации (base64). "
            "В developers.sber.ru скопируйте именно «Ключ авторизации», "
            "не Client Secret и не Access token."
        )
    try:
        decoded = base64.b64decode(cred).decode("utf-8", errors="replace")
        if ":" not in decoded:
            return cred, (
                "После расшифровки base64 нет формата client_id:client_secret. "
                "Скорее всего скопирован не тот ключ."
            )
    except Exception:
        pass
    return cred, None


def _gigachat_verify_ssl() -> bool:
    v = os.environ.get("SPPR_GIGACHAT_VERIFY_SSL", "false").strip().lower()
    return v in ("1", "true", "yes", "on")


def clean_l2_output(text: str) -> str:
    """Убрать из ответа LLM служебные блоки (имена файлов проекта, «формат вывода»)."""
    if not text:
        return text
    t = text.strip()
    cut_patterns = [
        r"\n#{1,3}\s*Итоговый формат вывода\b.*",
        r"\n\*\*Итоговый формат вывода\*\*.*",
        r"\n#{1,3}\s*Рекомендуемые ссылки\b.*",
        r"\n\*\*Рекомендуемые ссылки:?\*\*.*",
        r"\n---\s*\n\*\*Примечание:?\*\*\s*\n(?:- .*\n)+",
    ]
    for pat in cut_patterns:
        t = re.split(pat, t, maxsplit=1, flags=re.I | re.S)[0]

    leak_heads = (
        r"\n##\s*Факты из движка\b",
        r"\n##\s*Шапка:\s*ошибки\b",
        r"\n##\s*ЗНД\s*\(обязательно\b",
        r"\n##\s*Подсказки из KB\b",
        r"\n##\s*Задача\b",
        r"\n##\s*Итоговый номер ЗНД\b",
        r"\n##\s*Эталоны формата\b",
    )
    for pat in leak_heads:
        t = re.split(pat, t, maxsplit=1, flags=re.I | re.S)[0]
    t = re.split(r"\n```[\s\S]*?```\s*$", t, maxsplit=1)[0]
    t = re.split(r"\n#{1,3}\s*Файлы проекта\b.*", t, maxsplit=1, flags=re.I | re.S)[0]
    t = re.split(r"\n\s*【\s*L2\s*S/?4.*", t, maxsplit=1, flags=re.I | re.S)[0]
    t = re.split(r"\n#{1,3}\s*【?\s*L2\s*S/?4.*", t, maxsplit=1, flags=re.I | re.S)[0]
    # Только служебный хвост **ИТОГО:** (не резать «## Итого» — иначе пропадает весь ответ)
    t = re.split(r"\n\*\*ИТОГО:\*\*.*", t, maxsplit=1, flags=re.S)[0]
    t = re.sub(
        r"#{1,3}\s*Итого\s*\(\s*для\s+тикета\s*\)",
        "## Итого",
        t,
        flags=re.I,
    )

    skip_phrases = (
        "report_",
        "СППР_ПРАВИЛА_LLM",
        "kb_typeid_index",
        "json_error_identifiability",
        "sppr_diagnose",
        "FEW-SHOT",
        "json_error_ident",
        "охват mvp",
        "Охват MVP",
        "=== СППР:",
        "Папка: c:\\",
        "Папка: C:\\",
        "Класс СППР:",
        "единственный источник typeID",
    )
    lines = []
    for line in t.splitlines():
        low = line.lower()
        if any(p.lower() in low for p in skip_phrases):
            continue
        if re.match(r"^\*\*ИТОГО:\*\*", line.strip()):
            continue
        if "охват mvp" in low or (
            "тип ошибки" in low and "охват" in low
        ):
            continue
        if re.match(r"^-\s*`[^`]+\.(md|json)`", line.strip()):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


TICKET_OUTPUT_RULES = """
ВЫХОД — ТОЛЬКО ТЕКСТ ОТВЕТА L2 CRM (без формулировок «создать/оформить тикет»).

ШАПКА (сразу после # Итого: заказ …):
- guid заказа
- **Ошибка из INT-2010 (JSON):** — строка **dateTime** + **INT-2010** (+ messageId при наличии)
- **Выгрузка JSON:** markdown-ссылка [имя_файла](file:///…) на ошибочный JSON
- далее список: typeID | dateTime | note **дословно**

ЗАПРЕЩЕНО:
- Любой блок 【L2 S/4】, 【L2 S/4 (ERP LO)】, упоминания VA03 отдельным блоком.
- Имена файлов (.md, .json, report_*.txt), «Файлы проекта».

【L2 CRM】 — единственный блок рекомендаций.

Для **typeID 143** (ZPRI после ЗНД): причина **известна** из note — **не** писать
«причина не ясна». Чеклист: ЗНД в CRM Web UI (E0010) → если есть, **отмена ЗНД** → ZPRI (DEMO_PAGE_ID);
если ЗНД в CRM **нет** — **переадресовать L2 S/4** (VA03). **Не** выдумывать «было→стало» без INT-0993.

Для **typeID 201(R1)** (ДП отсутствует в S/4): **только** партнёр из note + Partner[] в 0992.
**Не** писать 143, ZPRI, ЗНД, таблицу позиций. CRM: карта ДП; при верном CRM — загрузка BP в S/4.

Для прочих typeID — в конце 【L2 CRM】 при необходимости **одна** фраза эскалации на S/4
(без дубля).

СТРУКТУРА:
1) # Итого: заказ … (typeID в заголовке)
2) guid + Ошибка из INT-2010 (typeID + note)
3) ## Итого (без «для тикета»)
4) ## Confluence — URL (опционально)
5) ## 【L2 CRM】

**Номер ЗНД** — только один раз в **шапке** (после guid / INT-2010), не повторять в ## Итого.
Формулировка: «номер ЗНД отсутствует во всех JSON обменах» (не «ЗНД нет»).

Для **не-143**: в 【L2 CRM】 при необходимости одна фраза про проверку в S/4
(без «запросить у L2 S/4»). Для **143** — чеклист отмены ЗНД, без «причина не ясна».

Без финального блока **ИТОГО**, без «охват MVP», без «тип ошибки A» в конце — класс A/B уже в ## Итого при необходимости одной фразой.
"""


def call_llm(system: str, user: str, *, ticket_format: bool = True) -> str:
    load_env()
    key = _api_key()
    if not key:
        raise RuntimeError(
            "Не задан ключ: создайте файл .env (см. .env.example) "
            "с SPPR_LLM_API_KEY=ваш_ключ_авторизации_GigaChat"
        )

    sys_prompt = system.rstrip()
    if ticket_format:
        sys_prompt = f"{sys_prompt}\n\n{TICKET_OUTPUT_RULES}"

    provider = _provider()
    if provider == "gigachat" or "gigachat" in (
        os.environ.get("SPPR_LLM_BASE_URL") or ""
    ).lower():
        raw = _call_gigachat(sys_prompt, user, key)
    else:
        raw = _call_openai_compatible(sys_prompt, user, key)
    return clean_l2_output(raw) if ticket_format else raw.strip()


def call_llm_qa(system: str, user: str) -> str:
    """Q&A по JSON — без правил оформления тикета L2."""
    return call_llm(system, user, ticket_format=False)


def _call_gigachat(system: str, user: str, credentials: str) -> str:
    try:
        from gigachat import GigaChat
        from gigachat.models import Chat, Messages, MessagesRole
    except ImportError as e:
        raise RuntimeError("Установите: pip install gigachat") from e

    cred, err = validate_gigachat_credentials(credentials)
    if err:
        raise RuntimeError(err)

    model = os.environ.get("SPPR_LLM_MODEL", "GigaChat")
    scope = os.environ.get("SPPR_GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
    payload = Chat(
        messages=[
            Messages(role=MessagesRole.SYSTEM, content=system),
            Messages(role=MessagesRole.USER, content=user),
        ],
        temperature=0.2,
    )
    try:
        giga_ctx = GigaChat(
            credentials=cred,
            verify_ssl_certs=_gigachat_verify_ssl(),
            model=model,
            scope=scope,
        )
    except Exception as e:
        msg = str(e)
        if "decode" in msg.lower() and "authorization" in msg.lower():
            raise RuntimeError(
                "GigaChat отклонил ключ (Can't decode Authorization header). "
                "Скопируйте «Ключ авторизации» целиком с developers.sber.ru "
                "или задайте GIGACHAT_CLIENT_ID и GIGACHAT_CLIENT_SECRET в .env. "
                f"Подробнее: {msg}"
            ) from e
        raise

    with giga_ctx as giga:
        response = giga.chat(payload)
    return (response.choices[0].message.content or "").strip()


def _call_openai_compatible(system: str, user: str, api_key: str) -> str:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("Установите: pip install openai") from e

    base = os.environ.get("SPPR_LLM_BASE_URL", "https://api.openai.com/v1").rstrip(
        "/"
    )
    model = os.environ.get("SPPR_LLM_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=api_key, base_url=base)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip()


def describe_config() -> str:
    load_env()
    key = _api_key()
    masked = f"{key[:8]}…{key[-4:]}" if len(key) > 16 else ("(задан)" if key else "(нет)")
    return (
        f"provider={_provider()}\n"
        f"model={os.environ.get('SPPR_LLM_MODEL', '(по умолчанию)')}\n"
        f"key={masked}\n"
        f"gigachat_scope={os.environ.get('SPPR_GIGACHAT_SCOPE', 'GIGACHAT_API_PERS')}\n"
        f"verify_ssl={_gigachat_verify_ssl()}\n"
        f"base_url={os.environ.get('SPPR_LLM_BASE_URL', '(openai default)')}\n"
    )
