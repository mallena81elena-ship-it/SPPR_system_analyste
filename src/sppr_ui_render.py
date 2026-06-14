# -*- coding: utf-8 -*-
"""Цветной вывод ответов СППР в Streamlit (HTML + встроенные стили)."""
from __future__ import annotations

import html
import re
from typing import Any

import streamlit as st

CONFLUENCE_PAGE_URL = "https://example.local/confluence?pageId="

# Стили в каждом ответе — иначе в st.chat_message / st.html iframe они не видны
_EMBEDDED_CSS = """
.sppr-answer {
  font-size: 0.95rem; line-height: 1.5; padding: 0.65rem 0.9rem;
  border-radius: 10px; background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
  border: 1px solid #cbd5e1; color: #1e293b;
}
.sppr-answer h3.sppr-h {
  font-size: 1.06rem; margin: 0.55rem 0 0.35rem; padding: 0.35rem 0.55rem;
  border-radius: 6px; border-left: 4px solid #3b82f6;
  background: #eff6ff; color: #1e3a8a;
}
.sppr-answer h4.sppr-h4 {
  font-size: 0.98rem; margin: 0.45rem 0 0.25rem; padding: 0.25rem 0.45rem;
  border-left: 3px solid #8b5cf6; background: #f5f3ff; color: #5b21b6;
}
.sppr-answer h3.sppr-h-title {
  border-left-color: #6366f1; background: #eef2ff; color: #3730a3; font-size: 1.1rem;
}
.sppr-answer h3.sppr-h-error {
  border-left-color: #dc2626; background: #fef2f2; color: #991b1b;
}
.sppr-answer h3.sppr-h-price {
  border-left-color: #059669; background: #ecfdf5; color: #065f46;
}
.sppr-answer h3.sppr-h-partner {
  border-left-color: #7c3aed; background: #f5f3ff; color: #5b21b6;
}
.sppr-answer h3.sppr-h-delivery {
  border-left-color: #d97706; background: #fffbeb; color: #92400e;
}
.sppr-answer p { margin: 0.22rem 0; }
.sppr-answer .sppr-line-bullet { color: #334155; padding-left: 0.45rem; }
.sppr-answer .sppr-line-bullet::before { content: "▸ "; color: #3b82f6; font-weight: bold; }
.sppr-answer .sppr-line-num { color: #1e40af; padding-left: 0.3rem; }
.sppr-answer .sppr-line-num::before { content: "● "; color: #f59e0b; }
.sppr-answer .sppr-line-sources {
  background: #faf5ff; color: #6b21a8; border-left: 3px solid #a855f7; padding: 0.35rem;
}
.sppr-answer .sppr-line-l2 {
  background: #eff6ff; color: #1e40af; border-left: 3px solid #2563eb;
  font-weight: 600; padding: 0.35rem;
}
.sppr-answer .sppr-line-file { color: #475569; font-size: 0.9em; }
.sppr-answer .sppr-meta { color: #64748b; font-style: italic; font-size: 0.88em; }
.sppr-answer .sppr-strong { color: #0d9488; font-weight: 700; }
.sppr-answer .sppr-val { color: #0369a1; font-weight: 600; }
.sppr-answer .sppr-code {
  background: #ede9fe; color: #6d28d9; padding: 0.06rem 0.35rem;
  border-radius: 4px; font-family: Consolas, monospace; font-size: 0.87em;
}
.sppr-answer .sppr-file {
  background: #fef3c7; color: #b45309; padding: 0.06rem 0.3rem;
  border-radius: 4px; font-family: Consolas, monospace;
}
.sppr-answer .sppr-int { color: #ea580c; font-weight: 700; }
.sppr-answer .sppr-typeid { color: #dc2626; font-weight: 700; }
.sppr-answer .sppr-order {
  color: #be185d; font-weight: 700; background: #fce7f3;
  padding: 0 0.2rem; border-radius: 3px;
}
.sppr-answer .sppr-role {
  color: #7c3aed; font-weight: 700; background: #ede9fe;
  padding: 0 0.15rem; border-radius: 3px;
}
.sppr-answer .sppr-field { color: #0e7490; font-weight: 600; }
.sppr-answer .sppr-date { color: #64748b; }
.sppr-answer .sppr-link { color: #0284c7; text-decoration: underline; }
.sppr-answer .sppr-line-ok {
  background: #ecfdf5; color: #065f46; border-left: 4px solid #10b981;
  font-weight: 600; padding: 0.45rem 0.55rem; border-radius: 6px;
}
.sppr-answer .sppr-line-root-error {
  background: #fef2f2; color: #7f1d1d; border-left: 5px solid #dc2626;
  font-weight: 600; padding: 0.55rem 0.7rem; border-radius: 8px;
  margin: 0.45rem 0 0.55rem; box-shadow: 0 1px 2px rgba(220, 38, 38, 0.12);
  line-height: 1.45;
}
.sppr-answer .sppr-root-note {
  color: #991b1b; font-weight: 700; background: #fee2e2;
  padding: 0.12rem 0.35rem; border-radius: 4px;
}
.sppr-answer .sppr-line-class-row {
  background: #fff7ed; color: #9a3412; border-left: 4px solid #f59e0b;
  padding: 0.35rem 0.55rem; border-radius: 6px; font-weight: 600;
}
.sppr-answer .sppr-line-int-block {
  color: #334155; padding-left: 0.35rem; border-left: 2px solid #94a3b8;
  margin: 0.15rem 0;
}
.sppr-answer .sppr-line-l2-brief {
  background: #eff6ff; color: #1e3a8a; border-left: 4px solid #2563eb;
  padding: 0.4rem 0.55rem; border-radius: 6px; margin-top: 0.35rem;
}
.sppr-answer h3.sppr-h-ok {
  border-left-color: #10b981; background: #ecfdf5; color: #065f46;
}
.sppr-answer .sppr-crm { color: #1d4ed8; font-weight: 600; }
.sppr-answer .sppr-s4 { color: #047857; font-weight: 600; }
.sppr-answer .sppr-guid { color: #4f46e5; }
.sppr-answer .sppr-note { color: #9f1239; font-weight: 500; }
/* Таблица позиций / 0992 — обычная сетка столбцов */
.sppr-answer table.sppr-table-grid {
  display: table;
  width: 100%;
  margin: 0.65rem 0 1.1rem 0;
  font-size: 0.88rem;
  border-collapse: collapse;
  table-layout: auto;
}
.sppr-answer table.sppr-table-grid tr {
  display: table-row;
  margin: 0;
  padding: 0;
  border: none;
  border-radius: 0;
  background: transparent;
}
.sppr-answer table.sppr-table-grid tr:first-child {
  background: linear-gradient(180deg, #dbeafe, #bfdbfe);
  font-weight: 600;
}
.sppr-answer table.sppr-table-grid th,
.sppr-answer table.sppr-table-grid td {
  display: table-cell;
  width: auto !important;
  border: 1px solid #cbd5e1;
  padding: 0.35rem 0.45rem;
  vertical-align: top;
  text-align: left;
}
.sppr-answer table.sppr-table-grid th {
  color: #1e3a8a;
  white-space: nowrap;
}
.sppr-answer table.sppr-table-grid td {
  color: #1e293b;
  word-wrap: break-word;
  overflow-wrap: anywhere;
}
.sppr-answer table.sppr-table-grid tr:nth-child(even) td {
  background: #f8fafc;
}
/* Confluence (2 колонки) — карточки на узком экране */
.sppr-answer table.sppr-table-cards {
  display: block;
  width: 100%;
  margin: 0.65rem 0 1.1rem 0;
  font-size: 0.9rem;
  border: none;
}
.sppr-answer table.sppr-table-cards tr {
  display: block;
  margin: 0.45rem 0;
  padding: 0.4rem 0.55rem;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  background: #f8fafc;
}
.sppr-answer table.sppr-table-cards tr:first-child {
  background: linear-gradient(180deg, #dbeafe, #bfdbfe);
  font-weight: 600;
}
.sppr-answer table.sppr-table-cards th,
.sppr-answer table.sppr-table-cards td {
  display: block;
  width: 100% !important;
  border: none;
  padding: 0.15rem 0;
}
.sppr-answer table.sppr-table-cards th {
  color: #1e3a8a; text-align: left;
  word-wrap: break-word; overflow-wrap: anywhere; white-space: normal;
}
.sppr-answer table.sppr-table-cards td {
  color: #1e293b;
  word-wrap: break-word; overflow-wrap: anywhere; white-space: normal;
}
.sppr-answer table.sppr-table-cards td .sppr-link { display: inline-block; margin-top: 0.2rem; }
.sppr-answer table + p,
.sppr-answer table + h3,
.sppr-answer table + h4,
.sppr-answer p.sppr-table-foot,
.sppr-answer .sppr-meta.sppr-table-foot {
  display: block;
  clear: both;
  width: 100%;
  margin-top: 0.75rem !important;
  padding-top: 0.45rem;
  border-top: 1px dashed #cbd5e1;
}
.sppr-answer .sppr-post-table {
  display: block;
  clear: both;
  width: 100%;
  margin: 0.65rem 0 1rem;
  padding: 0.55rem 0.7rem;
  background: #f8fafc;
  border-left: 4px solid #64748b;
  border-radius: 6px;
  box-sizing: border-box;
}
.sppr-answer .sppr-post-table p,
.sppr-answer .sppr-post-table h4 {
  margin: 0.35rem 0;
}
.sppr-answer .sppr-line-ref {
  color: #475569;
  font-size: 0.92rem;
}
"""

# Primary-кнопки Streamlit и активный пункт меню — зелёный акцент (вместо красного)
_SPPR_PRIMARY_GREEN_CSS = """
[data-testid="stButton"] button[kind="primary"],
[data-testid="stButton"] [data-testid="baseButton-primary"],
[data-testid="baseButton-primary"],
[data-testid="baseButton-primary"] > button,
button[data-testid="baseButton-primary"] {
  background: linear-gradient(165deg, #10b981 0%, #059669 100%) !important;
  background-color: #059669 !important;
  border-color: #047857 !important;
  color: #ffffff !important;
  box-shadow: 0 1px 3px rgba(5, 150, 105, 0.35) !important;
}
[data-testid="stButton"] button[kind="primary"]:hover,
[data-testid="stButton"] [data-testid="baseButton-primary"]:hover,
[data-testid="baseButton-primary"]:hover,
button[data-testid="baseButton-primary"]:hover {
  background: linear-gradient(165deg, #059669 0%, #047857 100%) !important;
  background-color: #047857 !important;
  border-color: #065f46 !important;
  color: #ffffff !important;
}
[data-testid="stButton"] button[kind="primary"]:focus,
[data-testid="stButton"] button[kind="primary"]:focus-visible,
[data-testid="baseButton-primary"]:focus,
[data-testid="baseButton-primary"]:focus-visible {
  box-shadow: 0 0 0 0.2rem rgba(16, 185, 129, 0.45) !important;
  outline: none !important;
}
section[data-testid="stSidebar"] [data-testid="baseButton-primary"],
section[data-testid="stSidebar"] button[kind="primary"] {
  background: linear-gradient(165deg, #10b981 0%, #059669 100%) !important;
  border-color: #047857 !important;
  color: #ffffff !important;
}
/* Вкладки: активная — зелёная линия */
button[data-baseweb="tab"][aria-selected="true"] {
  border-bottom-color: #059669 !important;
  color: #047857 !important;
}
"""

# Поле «Номер заказа» — узкое (~10 цифр, до конца слова «заказа» в заголовке)
_SPPR_ORDER_FIELD_CSS = """
div[data-testid="column"]:has(.sppr-order-field-narrow) {
  flex: 0 0 auto !important;
  width: auto !important;
  min-width: 0 !important;
  max-width: 11.5rem !important;
}
div[data-testid="column"]:has(.sppr-order-field-narrow) [data-testid="stTextInput"],
div[data-testid="column"]:has(.sppr-order-field-narrow) [data-testid="stTextInput"] > div,
div[data-testid="column"]:has(.sppr-order-field-narrow) [data-testid="stTextInput"] input,
div[data-testid="stVerticalBlock"]:has(.sppr-order-field-narrow) [data-testid="stTextInput"],
div[data-testid="stVerticalBlock"]:has(.sppr-order-field-narrow) [data-testid="stTextInput"] > div,
div[data-testid="stVerticalBlock"]:has(.sppr-order-field-narrow) [data-testid="stTextInput"] input {
  max-width: 11.5rem !important;
  width: 11.5rem !important;
}
div[data-testid="stHorizontalBlock"]:has(.sppr-qa-toolbar-anchor) {
  align-items: flex-end !important;
}
div[data-testid="column"]:has(.sppr-btn-help-row) {
  display: flex !important;
  align-items: flex-end !important;
  justify-content: center !important;
  padding-bottom: 0.45rem !important;
}
div[data-testid="column"]:has(.sppr-help-only) {
  display: flex !important;
  align-items: flex-end !important;
  justify-content: center !important;
  padding-bottom: 0.45rem !important;
}
"""

# Панель Q&A: зелёная подсветка — маркер в той же колонке, что и кнопка (без «>»: вложенность Streamlit)
_QA_TOOLBAR_ACTIVE_CSS = """
div[data-testid="column"]:has(.qa-marker-rag[data-active="true"]) [data-testid="stButton"] button,
div[data-testid="column"]:has(.qa-marker-json[data-active="true"]) [data-testid="stButton"] button,
div[data-testid="column"]:has(.qa-marker-rag[data-active="true"]) [data-testid="stButton"] [data-testid="baseButton-secondary"],
div[data-testid="column"]:has(.qa-marker-json[data-active="true"]) [data-testid="stButton"] [data-testid="baseButton-secondary"] {
  background: linear-gradient(165deg, #ecfdf5 0%, #d1fae5 45%, #a7f3d0 100%) !important;
  color: #065f46 !important;
  border: 1.5px solid #34d399 !important;
  box-shadow: 0 0 0 1px rgba(16, 185, 129, 0.25), 0 1px 4px rgba(5, 150, 105, 0.2) !important;
  font-weight: 600 !important;
}
div[data-testid="column"]:has(.qa-marker-rag[data-active="true"]) [data-testid="stButton"] button:hover,
div[data-testid="column"]:has(.qa-marker-json[data-active="true"]) [data-testid="stButton"] button:hover,
div[data-testid="column"]:has(.qa-marker-rag[data-active="true"]) [data-testid="stButton"] [data-testid="baseButton-secondary"]:hover,
div[data-testid="column"]:has(.qa-marker-json[data-active="true"]) [data-testid="stButton"] [data-testid="baseButton-secondary"]:hover {
  background: linear-gradient(165deg, #d1fae5 0%, #6ee7b7 100%) !important;
  border-color: #10b981 !important;
  color: #064e3b !important;
}
/* Выровнять колонку с 🛈 по вертикали с соседней кнопкой */
div[data-testid="stHorizontalBlock"] > div[data-testid="column"]:has([data-testid="stPopover"]) {
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  align-self: stretch !important;
}
div[data-testid="stHorizontalBlock"] > div[data-testid="column"]:has([data-testid="stPopover"]) > div {
  width: 100% !important;
  display: flex !important;
  justify-content: center !important;
  align-items: center !important;
}
section[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"] > div[data-testid="column"]:has([data-testid="stPopover"]) {
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
}
section[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"] > div[data-testid="column"]:has([data-testid="stPopover"]) > div {
  width: 100% !important;
  display: flex !important;
  justify-content: center !important;
  align-items: center !important;
}
section[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"] > div[data-testid="column"]:has(.sppr-help-popover-anchor) > div {
  width: auto !important;
  min-width: 0 !important;
}
section[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"] > div[data-testid="column"]:has(.sppr-help-popover-anchor) {
  align-self: center !important;
}
"""

_HELP_POPOVER_CSS = """
/* Popover help: только тонкая «ℹ», без рамки и chevron */
div[data-testid="column"]:has(.sppr-help-popover-anchor) {
  display: flex !important;
  align-items: center !important;
  justify-content: flex-start !important;
  min-width: 0.75rem !important;
  max-width: 1.1rem !important;
  flex: 0 0 auto !important;
  padding-left: 0 !important;
}
div[data-testid="stHorizontalBlock"]:has(.sppr-help-popover-anchor) {
  align-items: center !important;
  gap: 0.15rem !important;
}
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"],
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopoverBody"],
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] > div {
  margin: 0 !important;
  padding: 0 !important;
  background: transparent !important;
  background-color: transparent !important;
  border: none !important;
  border-width: 0 !important;
  outline: none !important;
  box-shadow: none !important;
  min-height: 0 !important;
  width: auto !important;
}
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] button,
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] [data-testid="baseButton-secondary"],
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopoverBody"] button {
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  gap: 0 !important;
  width: auto !important;
  height: auto !important;
  min-width: 0 !important;
  min-height: 0 !important;
  max-width: none !important;
  max-height: none !important;
  padding: 0 !important;
  margin: 0 !important;
  font-size: 0 !important;
  line-height: 1 !important;
  color: transparent !important;
  background: transparent !important;
  background-color: transparent !important;
  border: none !important;
  border-width: 0 !important;
  border-radius: 0 !important;
  outline: none !important;
  box-shadow: none !important;
}
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] button::before,
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] [data-testid="baseButton-secondary"]::before,
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopoverBody"] button::before {
  content: "ℹ" !important;
  display: inline-block !important;
  font-size: 0.72rem !important;
  font-weight: 400 !important;
  line-height: 1 !important;
  color: #94a3b8 !important;
}
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] button:hover::before,
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] [data-testid="baseButton-secondary"]:hover::before,
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopoverBody"] button:hover::before {
  color: #475569 !important;
}
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] button:hover,
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] [data-testid="baseButton-secondary"]:hover,
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopoverBody"] button:hover,
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] button:focus,
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] button:focus-visible {
  background: transparent !important;
  background-color: transparent !important;
  border: none !important;
  outline: none !important;
  box-shadow: none !important;
}
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] button *,
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] [data-testid="baseButton-secondary"] *,
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopoverBody"] button * {
  display: none !important;
  visibility: hidden !important;
  width: 0 !important;
  height: 0 !important;
  margin: 0 !important;
  padding: 0 !important;
  border: none !important;
  box-shadow: none !important;
}
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] svg,
div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopoverBody"] svg {
  display: none !important;
  width: 0 !important;
  height: 0 !important;
  visibility: hidden !important;
}
section[data-testid="stSidebar"] div[data-testid="column"]:has(.sppr-help-popover-anchor) {
  padding-top: 0.35rem !important;
  align-self: center !important;
}
section[data-testid="stSidebar"] div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopover"] button,
section[data-testid="stSidebar"] div[data-testid="column"]:has(.sppr-help-popover-anchor) [data-testid="stPopoverBody"] button {
  min-height: 0 !important;
  height: auto !important;
}
div[data-testid="stHorizontalBlock"]:has([data-testid="stCheckbox"]) div[data-testid="column"]:has(.sppr-help-popover-anchor) {
  padding-top: 0.42rem !important;
}
"""

# Якорь .sppr-help-only — только нативный ? (Streamlit 1.55: галка вне label > div:first-child)
_HELP_ICON_CHECKBOX_CSS = """
div[data-testid="column"]:has(.sppr-help-only),
div[data-testid="stVerticalBlock"]:has(.sppr-help-only) {
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  min-width: 1.35rem !important;
  max-width: 1.65rem !important;
}
section[data-testid="stSidebar"] div[data-testid="column"]:has(.sppr-help-only) {
  padding-top: 0.42rem !important;
}
div:has(> .sppr-help-only) [data-testid="stCheckbox"] {
  min-height: 0 !important;
  width: auto !important;
  padding: 0 !important;
  margin: 0 !important;
}
div:has(> .sppr-help-only) [data-testid="stCheckbox"] > div {
  margin: 0 !important;
  padding: 0 !important;
  min-height: 0 !important;
  gap: 0 !important;
}
div:has(> .sppr-help-only) [data-testid="stCheckbox"] label {
  gap: 0 !important;
  min-height: 0 !important;
  margin: 0 !important;
  padding: 0 !important;
  justify-content: center !important;
  align-items: center !important;
}
/* Галка checkbox (1.55: checkmark — первый ребёнок label, help — в stWidgetLabel) */
div:has(> .sppr-help-only) [data-testid="stCheckbox"] > div > label > *:first-child:not([data-testid="stWidgetLabel"]),
div:has(> .sppr-help-only) [data-testid="stCheckbox"] [role="checkbox"],
div:has(> .sppr-help-only) [data-testid="stCheckbox"] input[type="checkbox"],
div:has(> .sppr-help-only) [data-testid="stCheckbox"] label > div:first-child:not(:has([data-testid="stTooltipHoverTarget"])),
div:has(> .sppr-help-only) [data-testid="stCheckbox"] label > span:first-of-type {
  display: none !important;
  width: 0 !important;
  height: 0 !important;
  min-width: 0 !important;
  min-height: 0 !important;
  margin: 0 !important;
  padding: 0 !important;
  overflow: hidden !important;
  opacity: 0 !important;
  pointer-events: none !important;
  position: absolute !important;
  clip: rect(0, 0, 0, 0) !important;
}
/* Пустая подпись nbsp */
div:has(> .sppr-help-only) [data-testid="stCheckbox"] [data-testid="stWidgetLabel"] p,
div:has(> .sppr-help-only) [data-testid="stCheckbox"] [data-testid="stWidgetLabel"] [data-testid="stMarkdownContainer"],
div:has(> .sppr-help-only) [data-testid="stCheckbox"] label p {
  display: none !important;
  width: 0 !important;
  height: 0 !important;
  margin: 0 !important;
  padding: 0 !important;
  overflow: hidden !important;
}
div:has(> .sppr-help-only) [data-testid="stWidgetLabel"] {
  padding: 0 !important;
  margin: 0 !important;
  min-height: 0 !important;
  line-height: 1 !important;
}
div:has(> .sppr-help-only) [data-testid="stWidgetLabel"] > *:not([data-testid="stTooltipHoverTarget"]):not(:has([data-testid="stTooltipHoverTarget"])) {
  display: none !important;
  width: 0 !important;
  height: 0 !important;
  margin: 0 !important;
  padding: 0 !important;
  overflow: hidden !important;
}
div:has(> .sppr-help-only) [data-testid="stTooltipHoverTarget"] {
  display: inline-flex !important;
  visibility: visible !important;
  opacity: 1 !important;
  pointer-events: auto !important;
  margin: 0 !important;
  flex-shrink: 0 !important;
}
"""

# Нативный help на метриках (страница «Метрики») — без кружка
_HELP_TOOLTIP_GLOBAL_CSS = """
[data-testid="stMetric"] [data-testid="stTooltipHoverTarget"],
[data-testid="stMetric"] [data-testid="stTooltipIcon"] {
  margin-left: 0.2rem !important;
  border: none !important;
  background: transparent !important;
}
[data-testid="stMetric"] [data-testid="stTooltipHoverTarget"]::after,
[data-testid="stMetric"] [data-testid="stTooltipIcon"]::after {
  content: none !important;
  display: none !important;
}
"""

# Сайдбар: номера заказов в одну строку; колонка help не сжимает кнопку
_SIDEBAR_LAYOUT_CSS = """
section[data-testid="stSidebar"] [data-testid="stButton"] button,
section[data-testid="stSidebar"] [data-testid="stButton"] button p,
section[data-testid="stSidebar"] [data-testid="stButton"] [data-testid="stMarkdownContainer"] p {
  white-space: nowrap !important;
  word-break: keep-all !important;
  overflow-wrap: normal !important;
  text-overflow: ellipsis !important;
}
"""

# Подсказки-вопросы (st.pills): выбранный вариант — зелёный
_PILLS_HINT_CSS = """
[data-testid="stPills"] button[aria-pressed="true"],
[data-testid="stPills"] button[data-state="active"],
[data-testid="stPills"] [data-baseweb="button"][aria-checked="true"] {
  background: linear-gradient(165deg, #10b981 0%, #059669 100%) !important;
  color: #ffffff !important;
  border-color: #047857 !important;
  font-weight: 600 !important;
}
[data-testid="stPills"] button[aria-pressed="true"]:hover {
  background: linear-gradient(165deg, #059669 0%, #047857 100%) !important;
  color: #ffffff !important;
}
"""


def inject_sppr_styles() -> None:
    """Глобальные стили СППР — на каждом rerun (иначе при смене вкладки пропадает ? в кружке)."""
    st.markdown(
        f"<style>{_EMBEDDED_CSS}{_SPPR_PRIMARY_GREEN_CSS}{_SPPR_ORDER_FIELD_CSS}"
        f"{_HELP_POPOVER_CSS}{_HELP_ICON_CHECKBOX_CSS}{_HELP_TOOLTIP_GLOBAL_CSS}"
        f"{_SIDEBAR_LAYOUT_CSS}{_QA_TOOLBAR_ACTIVE_CSS}{_PILLS_HINT_CSS}</style>",
        unsafe_allow_html=True,
    )


_BRIEF_REPORT_FOOTER_RE = re.compile(
    r"\n*\(краткий отчёт; полный: --full\)\s*$",
    re.IGNORECASE,
)


def strip_brief_report_footer(text: str) -> str:
    """Убрать служебную метку краткого отчёта (для UI; в CLI/report_*.txt остаётся)."""
    return _BRIEF_REPORT_FOOTER_RE.sub("", (text or "").rstrip()).rstrip()


def render_help_icon(help_text: str, *, key: str) -> None:
    """Тонкая «ℹ» + popover — сайдбар и прочие подсказки без нативного help."""
    text = (help_text or "").strip()
    if not text:
        return
    st.markdown(
        '<div class="sppr-help-popover-anchor" aria-hidden="true"></div>',
        unsafe_allow_html=True,
    )
    with st.popover("\u200b", key=key):
        st.markdown(text)


def render_button_with_help(
    label: str,
    *,
    help_text: str = "",
    key: str,
    help_key: str | None = None,
    help_width: float = 0.1,
    **button_kwargs: Any,
) -> bool:
    """Кнопка без help на себе + отдельная «ℹ» справа (tooltip не перекрывает кнопку)."""
    text = (help_text or "").strip()
    if not text:
        return bool(st.button(label, key=key, **button_kwargs))
    ratio = max(0.05, min(0.2, float(help_width)))
    c_btn, c_help = st.columns([1.0 - ratio, ratio], gap="small")
    with c_btn:
        clicked = st.button(label, key=key, **button_kwargs)
    with c_help:
        st.markdown(
            '<div class="sppr-btn-help-row" aria-hidden="true"></div>',
            unsafe_allow_html=True,
        )
        render_help_icon(text, key=help_key or f"{key}_help")
    return bool(clicked)


def render_help_popover(help_text: str, *, key: str | None = None) -> None:
    """Совместимость: то же, что render_help_icon."""
    kid = key or f"sppr_help_{abs(hash(help_text or '')) % 10**9}"
    render_help_icon(help_text, key=kid)


def _header_class(title: str) -> str:
    t = title.lower()
    if "ошибок в интеграционных" in t or t.startswith("✅"):
        return "sppr-h-ok"
    if re.search(r"^итого|итого:", t):
        return "sppr-h-title"
    if any(w in t for w in ("ошибк", "typeid", "2010", "корень")):
        return "sppr-h-error"
    if any(w in t for w in ("цен", "zpri", "знд")):
        return "sppr-h-price"
    if any(w in t for w in ("контакт", "партн", "кл", "заказчик", "грузополуч")):
        return "sppr-h-partner"
    if any(w in t for w in ("доставк", "оплат", "логист", "перевозчик", "номер знд")):
        return "sppr-h-delivery"
    return "sppr-h"


def _line_class(stripped: str) -> str:
    if stripped.startswith("✅") or "ошибок в интеграционных сценариях" in stripped.lower():
        return "sppr-line-ok"
    if stripped.startswith("Справочно") or stripped.startswith("### Справочно"):
        return "sppr-line-ref"
    if stripped.startswith("Информационные сообщения"):
        return "sppr-line-ref"
    if re.match(r"^INT-2010\s*\([^)]+\)\s*:", stripped, re.I):
        return "sppr-line-root-error"
    if stripped.startswith("Класс:") and "корень:" in stripped:
        return "sppr-line-class-row"
    if stripped.startswith("INT-0992") or stripped.startswith("Partner[]"):
        return "sppr-line-int-block"
    if stripped.startswith("L2 CRM:"):
        return "sppr-line-l2-brief"
    if stripped.startswith("**Источники"):
        return "sppr-line-sources"
    if stripped.startswith("【"):
        return "sppr-line-l2"
    if re.search(r"guid\s+заказа|\.json", stripped, re.I):
        return "sppr-line-file"
    if re.search(r"^INT\s*\(поиск XML", stripped, re.I):
        return "sppr-line-int-block"
    if re.match(r"^\d+\.\s+", stripped):
        return "sppr-line-num"
    if stripped.startswith(("- ", "* ")):
        return "sppr-line-bullet"
    return "sppr-line"


def _linkify_page_ids(segment: str) -> str:
    """pageId DEMO_PAGE_ID и голые id в блоке источников → кликабельные ссылки."""

    def _page_ref(m: re.Match[str]) -> str:
        pid = m.group(2)
        return (
            f'<a class="sppr-link" href="{CONFLUENCE_PAGE_URL}{pid}" '
            f'target="_blank" rel="noopener">pageId {pid}</a>'
        )

    # Не трогать pageId= внутри уже полного URL (иначе ломается href при LLM-ответе).
    def _page_ref_safe(m: re.Match[str]) -> str:
        if m.start() > 0 and segment[m.start() - 1] in "?&":
            return m.group(0)
        before = segment[max(0, m.start() - 48) : m.start()].lower()
        if "viewpage.action" in before or "example.local" in before:
            return m.group(0)
        return _page_ref(m)

    segment = re.sub(
        r"\b(pageId)\s*[=:]?\s*(\d{6,12})\b",
        _page_ref_safe,
        segment,
        flags=re.I,
    )

    if re.search(r"источник|confluence\s*\(rag\)", segment, re.I):
        _order_id_re = re.compile(r"^(?:9000\d{6}|500[56]\d{6})$")

        def _bare_id(m: re.Match[str]) -> str:
            pid = m.group(1)
            # Номера заказов CRM (9000… / 5005… / 5006…) — не pageId Confluence.
            if _order_id_re.fullmatch(pid):
                return pid
            return (
                f'<a class="sppr-link" href="{CONFLUENCE_PAGE_URL}{pid}" '
                f'target="_blank" rel="noopener">{pid}</a>'
            )

        segment = re.sub(
            r'(?<!pageId\s)(?<!pageId=)(?<!">)\b(\d{8,10})\b',
            _bare_id,
            segment,
        )
    return segment


def _linkify_bare_urls(segment: str) -> str:
    """Кликабельные URL вне уже вставленных <a> (диагностика LLM часто без markdown)."""
    trailing_punct = ".,;:)"

    def _wrap(m: re.Match[str]) -> str:
        url = m.group(1)
        tail = ""
        while url and url[-1] in trailing_punct:
            tail = url[-1] + tail
            url = url[:-1]
        if not url:
            return m.group(0)
        return (
            f'<a class="sppr-link" href="{url}" target="_blank" rel="noopener">'
            f"{url}</a>{tail}"
        )

    segment = re.sub(
        r"(https://example\.local/pages/viewpage\.action\?pageId=\d+)",
        _wrap,
        segment,
    )
    # Уже испорченные двойные URL (старые ответы в сессии) — одна нормальная ссылка.
    segment = re.sub(
        r"https://example\.local/pages/viewpage\.action\?"
        r"https://example\.local/pages/viewpage\.action\?pageId=(\d+)"
        r'[^<\s]*"[^>]*>pageId\s+\1',
        lambda m: (
            f'<a class="sppr-link" href="{CONFLUENCE_PAGE_URL}{m.group(1)}" '
            f'target="_blank" rel="noopener">pageId {m.group(1)}</a>'
        ),
        segment,
        flags=re.I,
    )
    segment = re.sub(
        r'(?<!href=")(?<!href=\')(?<!">)(https?://[^\s<>"\']+)',
        _wrap,
        segment,
    )
    return segment


def _linkify_outside_anchors(s: str) -> str:
    parts = re.split(r"(<a\s[^>]*>.*?</a>)", s, flags=re.I | re.S)
    for i, part in enumerate(parts):
        if part.lower().startswith("<a "):
            continue
        part = _linkify_bare_urls(part)
        parts[i] = _linkify_page_ids(part)
    return "".join(parts)


def _colorize_inline(text: str) -> str:
    s = html.escape(text)

    s = re.sub(
        r"\[([^\]]+)\]\((file:///[^)]+)\)",
        r'<a class="sppr-link" href="\2" target="_blank" rel="noopener">\1</a>',
        s,
    )
    s = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a class="sppr-link" href="\2" target="_blank" rel="noopener">\1</a>',
        s,
    )
    s = _linkify_outside_anchors(s)
    s = re.sub(
        r"\*\*Ответ:?\*\*",
        r'<span class="sppr-strong">Ответ:</span>',
        s,
        flags=re.I,
    )
    s = re.sub(r"\*\*(.+?)\*\*", r'<span class="sppr-strong">\1</span>', s)

    s = re.sub(
        r"\b(9000\d{6}|500[56]\d{6})\b",
        r'<span class="sppr-order">\1</span>',
        s,
    )
    s = re.sub(
        r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
        r'<span class="sppr-guid">\1</span>',
        s,
        flags=re.I,
    )
    s = re.sub(r"\b(INT-\d{4})\b", r'<span class="sppr-int">\1</span>', s)
    s = re.sub(
        r"\b(typeID\s*[\w().]+|\d{3}\(ZSD_[^)]+\))",
        r'<span class="sppr-typeid">\1</span>',
        s,
        flags=re.I,
    )
    s = re.sub(
        r"\b(004|025|143|152|003|201)\b(?=\s*\(|,|\||\s*—)",
        r'<span class="sppr-typeid">\1</span>',
        s,
    )
    s = re.sub(
        r"\b(AG|WE|ZF|ZY|RG|ZQ|ZP)\b",
        r'<span class="sppr-role">\1</span>',
        s,
    )

    def _sys_tag(m: re.Match[str]) -> str:
        w = m.group(1)
        if w == "CRM":
            return '<span class="sppr-crm">CRM</span>'
        return f'<span class="sppr-s4">{html.escape(w)}</span>'

    s = re.sub(r"\b(CRM|S/4|ERP)\b", _sys_tag, s)

    s = re.sub(
        r"\b(paymentTerms|creditDaysType|creditDaysAmount|deliveryType|"
        r"ZZ1_DLVType|carrier|itemGiftFlag|SalesOrderItemID|"
        r"PartnerFunction|AddressName|messageId|resultCode|dateTime|"
        r"ExternalDocLastChangeDateTime|ZZStatusUserItem)\b",
        r'<span class="sppr-field">\1</span>',
        s,
    )

    s = re.sub(
        r"\b(\d{4}-\d{2}-\d{2}T[\d:.]+)\b",
        r'<span class="sppr-date">\1</span>',
        s,
    )

    s = re.sub(
        r"([\w.\- ]+\.json)",
        r'<span class="sppr-file">\1</span>',
        s,
    )

    s = re.sub(r"`([^`]+)`", r'<span class="sppr-code">\1</span>', s)
    s = re.sub(r" — (.+)$", r' — <span class="sppr-val">\1</span>', s)
    s = re.sub(
        r"(Снять резерв[^<.]+|неотмененн\w* ЗНД[^<.]*)",
        r'<span class="sppr-note">\1</span>',
        s,
        flags=re.I,
    )
    s = re.sub(
        r"(Деловой партн[её]р)\s+(\d{6,})",
        r'\1 <span class="sppr-typeid">\2</span>',
        s,
        flags=re.I,
    )
    s = re.sub(
        r"(INT-2010\s*\([^)]+\)\s*:\s*)(.+)$",
        lambda m: f"{m.group(1)}<span class=\"sppr-root-note\">{m.group(2)}</span>",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"(корень:\s*)([^|]+?)(\s*\||\s*$)",
        lambda m: f'{m.group(1)}<span class="sppr-typeid">{m.group(2).strip()}</span>{m.group(3)}',
        s,
        flags=re.I,
    )

    return s


def _is_table_sep(line: str) -> bool:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return bool(cells) and all(set(c) <= {"-", ":"} for c in cells)


def _header_level(stripped: str) -> int:
    m = re.match(r"^(#+)\s+", stripped)
    return len(m.group(1)) if m else 0


def _header_text(stripped: str) -> str:
    return re.sub(r"^#+\s*", "", stripped).strip()


def _is_table_footnote_line(stripped: str) -> bool:
    if not stripped:
        return False
    if stripped.startswith("**Поля JSON"):
        return True
    if stripped.startswith("**В 0992"):
        return True
    if stripped.startswith("*Источник:") or stripped.startswith("*Источник "):
        return True
    if stripped.startswith("Справочно") or stripped.startswith("### Справочно"):
        return True
    if stripped.startswith("Информационные сообщения"):
        return True
    if stripped.startswith("**Действие L2 CRM:**"):
        return True
    if stripped.startswith("**Номер ЗНД:**") or stripped.startswith("Номер ЗНД:"):
        return True
    if re.match(r"^Класс\s+\*\*", stripped) or stripped.startswith("Класс "):
        return True
    if stripped.startswith("Источник заказа:"):
        return True
    if stripped.strip() == "---":
        return True
    return False


def _is_post_table_footnote(stripped: str) -> bool:
    """Строки под таблицей проверки (заказ «Ок»), не продолжение таблицы."""
    if not stripped:
        return True
    if _is_table_footnote_line(stripped):
        return True
    if stripped.startswith("- ") or stripped.startswith("* "):
        return True
    if re.match(r"^\d+\.\s+", stripped):
        return True
    return False


def _breaks_post_table_footnote(stripped: str) -> bool:
    if not stripped:
        return False
    if stripped.startswith("|"):
        return True
    if stripped.startswith("INT-") or stripped.startswith("Partner[]"):
        return True
    if stripped.startswith("Класс:") or stripped.startswith("Заказ "):
        return True
    if stripped.startswith("✅") or stripped.startswith("⚠️"):
        return True
    if stripped.startswith("(краткий отчёт"):
        return True
    return False


def _escape_table_cell(cell: str) -> str:
    s = (cell or "").strip().replace("\n", " ").replace("\r", " ")
    return s.replace("|", "&#124;")


def _line_to_html(line: str, *, after_table: bool = False) -> str:
    stripped = line.strip()
    if not stripped:
        return "<br/>"

    foot = after_table or _is_table_footnote_line(stripped)
    foot_cls = " sppr-table-foot" if foot else ""

    if stripped == "---":
        return '<hr class="sppr-hr sppr-table-foot"/>'

    level = _header_level(stripped)
    if level:
        title = _header_text(stripped)
        plain = re.sub(r"\*\*", "", title)
        if level >= 3:
            return f'<h4 class="sppr-h4{foot_cls}">{_colorize_inline(title)}</h4>'
        cls = _header_class(plain)
        return f'<h3 class="sppr-h {cls}{foot_cls}">{_colorize_inline(title)}</h3>'

    if _is_table_sep(stripped):
        return ""

    if stripped.startswith("|") and stripped.endswith("|"):
        cells = [_escape_table_cell(c) for c in stripped.strip("|").split("|")]
        row = "".join(f"<td>{_colorize_inline(c)}</td>" for c in cells)
        return f"<tr>{row}</tr>"

    if (
        stripped.startswith("*")
        and stripped.endswith("*")
        and not stripped.startswith("**")
    ):
        return f'<p class="sppr-meta{foot_cls}">{_colorize_inline(stripped.strip("*"))}</p>'

    cls = _line_class(stripped)
    body = stripped
    if stripped.startswith(("- ", "* ")):
        body = stripped[2:].strip()

    return f'<p class="{cls}{foot_cls}">{_colorize_inline(body)}</p>'


def _normalize_markdown_spacing(text: str) -> str:
    """Пустая строка перед подвалом таблицы (Поля JSON, Источник)."""
    if not text:
        return text
    out = text
    for pat in (
        r"(?m)(?<!\n\n)(\*\*Поля JSON)",
        r"(?m)(?<!\n\n)(\*\*В 0992)",
        r"(?m)(?<!\n\n)(\*Источник:)",
        r"(?m)(\|[^\n]+\|\n\| --- \|)(?!\n\n)",
        r"(?m)(?<!\n\n)(### Справочно)",
        r"(?m)(?<!\n\n)(Справочно \(не ошибка)",
        r"(?m)(?<!\n\n)(\*\*Действие L2 CRM:\*\*)",
        r"(?m)(\|[^\n]+\|)\n(\*\*Номер ЗНД)",
        r"(?m)(\| --- \|[^\n]*\n)(\*\*Номер ЗНД)",
        r"(?m)(?<!\n\n)(---\s*$)",
    ):
        out = re.sub(pat, r"\n\n\1", out)
    return re.sub(r"\n{3,}", "\n\n", out)


def _sppr_table_class(col_count: int) -> str:
    """≥3 колонок — сетка (позиции 0992); 2 — карточки (Confluence)."""
    return "sppr-table-grid" if col_count >= 3 else "sppr-table-cards"


def sppr_text_to_html(text: str, *, wrap_document: bool = True) -> str:
    parts: list[str] = ['<div class="sppr-answer">']
    in_table = False
    table_class = "sppr-table-cards"
    in_post_table = False
    header_done = False
    header_pending = False
    just_closed_table = False
    for line in _normalize_markdown_spacing(text or "").splitlines():
        stripped = line.strip()
        if _is_table_sep(stripped):
            header_done = True
            header_pending = False
            continue
        if stripped.startswith("|"):
            if in_post_table:
                parts.append("</div>")
                in_post_table = False
            cells = [_escape_table_cell(c) for c in stripped.strip("|").split("|")]
            if not in_table:
                table_class = _sppr_table_class(len(cells))
                parts.append(f'<table class="{table_class}">')
                in_table = True
                header_done = False
                header_pending = False
            if header_done:
                row = "".join(f"<td>{_colorize_inline(c)}</td>" for c in cells)
                parts.append(f"<tr>{row}</tr>")
            elif header_pending:
                row = "".join(f"<td>{_colorize_inline(c)}</td>" for c in cells)
                parts.append(f"<tr>{row}</tr>")
                header_done = True
            else:
                head = "".join(f"<th>{_colorize_inline(c)}</th>" for c in cells)
                parts.append(f"<tr>{head}</tr>")
                header_pending = True
            just_closed_table = False
            continue
        if in_table:
            parts.append("</table>")
            in_table = False
            header_done = False
            header_pending = False
            just_closed_table = True
        if not in_table and not in_post_table and _is_post_table_footnote(stripped):
            parts.append('<div class="sppr-post-table">')
            in_post_table = True
        if in_post_table:
            if _breaks_post_table_footnote(stripped):
                parts.append("</div>")
                in_post_table = False
                parts.append(_line_to_html(line, after_table=just_closed_table))
                just_closed_table = False
            else:
                parts.append(_line_to_html(line, after_table=True))
            continue
        parts.append(_line_to_html(line, after_table=just_closed_table))
        just_closed_table = False
    if in_table:
        parts.append("</table>")
    if in_post_table:
        parts.append("</div>")
    parts.append("</div>")

    body = "\n".join(parts)
    if not wrap_document:
        return body
    return f"<style>{_EMBEDDED_CSS}</style>\n{body}"


def _estimate_height(text: str) -> int:
    """Высота iframe под весь текст (без внутреннего скролла)."""
    raw = text or ""
    line_count = max(1, raw.count("\n") + 1)
    table_rows = 0
    for ln in raw.splitlines():
        s = ln.strip()
        if s.startswith("|") and not _is_table_sep(s):
            table_rows += 1
    bullets = raw.count("\n- ") + raw.count("\n* ")
    # запас по строкам и таблице; верхний предел — очень длинные отчёты
    post_table = 0
    if "Справочно" in raw or "### Справочно" in raw:
        post_table = 120
    h = 80 + line_count * 32 + table_rows * 10 + bullets * 8 + post_table
    return max(160, min(3600, h))


def render_sppr_text(text: str) -> None:
    """Цветной вывод — работает в чате, диагностике и Confluence."""
    if not (text or "").strip():
        st.caption("Пустой ответ")
        return
    try:
        from sppr_code_decode import sanitize_display_text

        text = sanitize_display_text(text)
    except ImportError:
        pass
    try:
        from sppr_markdown_fix import (
            finalize_display_markdown,
            polish_llm_answer,
            should_skip_polish_wording,
        )

        if should_skip_polish_wording(text):
            text = finalize_display_markdown(text)
        else:
            text = polish_llm_answer(text)
        try:
            from sppr_markdown_fix import strip_html_link_artifacts

            text = strip_html_link_artifacts(text)
        except ImportError:
            pass
    except ImportError:
        try:
            from sppr_markdown_fix import polish_llm_answer

            text = polish_llm_answer(text)
        except ImportError:
            pass
    text = _normalize_markdown_spacing(text)
    inner = sppr_text_to_html(text, wrap_document=False)
    block = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"</head><body>"
        f"<style>{_EMBEDDED_CSS}</style>\n{inner}</body></html>"
    )
    height = _estimate_height(text)
    try:
        import streamlit.components.v1 as components

        components.html(block, height=height, scrolling=True)
    except Exception:
        inject_sppr_styles()
        st.markdown(f"<style>{_EMBEDDED_CSS}</style>{inner}", unsafe_allow_html=True)

    try:
        from sppr_markdown_fix import should_skip_polish_wording

        if should_skip_polish_wording(text):
            with st.expander("Скопировать ответ (markdown)", expanded=False):
                st.text_area(
                    "markdown_copy",
                    value=text,
                    height=min(480, max(140, (text.count("\n") + 1) * 22)),
                    label_visibility="collapsed",
                    key=f"sppr_md_{abs(hash(text[:400])) % 10**9}",
                )
    except ImportError:
        pass
