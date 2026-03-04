from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .config import Settings
from .telemetry import record_stage_metric
from .utils import append_jsonl


PAY_PERIOD_RE = re.compile(r"pay\s*period\s*[:\-]?\s*([^\n]+)", re.IGNORECASE)
PAY_DATE_RE = re.compile(r"pay\s*date\s*[:\-]?\s*([^\n]+)", re.IGNORECASE)
GROSS_RE = re.compile(r"gross\s*pay\s*[:\-]?\s*([$€£]?\s*[0-9,]+(?:\.[0-9]{2})?)", re.IGNORECASE)
NET_RE = re.compile(r"net\s*pay\s*[:\-]?\s*([$€£]?\s*[0-9,]+(?:\.[0-9]{2})?)", re.IGNORECASE)
TOTAL_RE = re.compile(r"\b(total|amount\s*due|subtotal|balance)\b\s*[:\-]?\s*([$€£]?\s*[0-9,]+(?:\.[0-9]{2})?)", re.IGNORECASE)
ITEM_LINE_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9\s\-/,&().]{1,80}?)\s+([$€£]?\s*[0-9,]+(?:\.[0-9]{2})?)\s*$",
    re.IGNORECASE,
)
MONEY_RE = re.compile(r"([$€£]?\s*[0-9,]+(?:\.[0-9]{2})?)")


RECEIPT_HINTS = {
    "receipt",
    "invoice",
    "payment",
    "paid",
    "subtotal",
    "total",
    "amount due",
    "line item",
    "qty",
}


def _currency_symbol_to_code(symbol: str) -> str:
    return {"$": "USD", "€": "EUR", "£": "GBP"}.get(symbol, "USD")


def _parse_currency(value: str) -> tuple[float | None, str | None]:
    raw = value.strip()
    if not raw:
        return None, None
    symbol = raw[0] if raw[0] in {"$", "€", "£"} else ""
    cleaned = raw.replace("$", "").replace("€", "").replace("£", "").replace(",", "").strip()
    try:
        amount = float(cleaned)
    except Exception:
        return None, None
    return amount, _currency_symbol_to_code(symbol) if symbol else "USD"


def _detect_document_type(text: str) -> str:
    lower = text.lower()
    if "pay period" in lower or "gross pay" in lower or "net pay" in lower:
        return "pay_stub"
    if "receipt" in lower:
        return "receipt"
    if "invoice" in lower:
        return "invoice"
    if "payment" in lower or "amount due" in lower:
        return "payment_sheet"
    return "payment_record"


def _extract_items(text: str) -> list[dict]:
    items: list[dict] = []
    for line in text.splitlines():
        m = ITEM_LINE_RE.match(line)
        if not m:
            continue
        description = m.group(1).strip()
        amount_raw = m.group(2).strip()
        amount, currency = _parse_currency(amount_raw)
        if amount is None:
            continue
        items.append(
            {
                "description": description,
                "amount": amount,
                "currency": currency,
                "raw": line.strip(),
            }
        )
    return items


def _looks_like_payment_record(text: str) -> bool:
    lower = text.lower()
    hint = any(k in lower for k in RECEIPT_HINTS)
    money_hits = len(MONEY_RE.findall(text))
    return hint or money_hits >= 2


def _extract_one(text: str) -> dict:
    period = PAY_PERIOD_RE.search(text)
    pay_date = PAY_DATE_RE.search(text)
    gross_match = GROSS_RE.search(text)
    net_match = NET_RE.search(text)
    total_match = TOTAL_RE.search(text)

    gross, currency_g = _parse_currency(gross_match.group(1) if gross_match else "")
    net, currency_n = _parse_currency(net_match.group(1) if net_match else "")
    total, currency_t = _parse_currency(total_match.group(2) if total_match else "")
    items = _extract_items(text)

    currency = currency_g or currency_n or currency_t
    if not currency and items:
        currency = items[0].get("currency")

    notes = []
    amount_signals = [x for x in [gross, net, total] if x is not None]
    if not amount_signals and not items:
        notes.append("no_detectable_payment_amounts")
    if gross is not None and net is not None and net > gross:
        notes.append("net_pay_exceeds_gross_pay")

    status = "valid" if not notes else "needs_review"

    return {
        "document_type": _detect_document_type(text),
        "pay_period": period.group(1).strip() if period else None,
        "pay_date": pay_date.group(1).strip() if pay_date else None,
        "gross_pay": gross,
        "net_pay": net,
        "currency": currency,
        "items": items,
        "validation_status": status,
        "validation_notes": ",".join(notes) if notes else None,
    }


def extract_paystubs(settings: Settings, conn: sqlite3.Connection) -> tuple[int, int]:
    rows = conn.execute(
        """
        SELECT p.document_id, p.page_number, p.text_path, d.path
        FROM pages p
        JOIN documents d ON p.document_id = d.document_id
        ORDER BY p.document_id, p.page_number
        """
    ).fetchall()

    processed = 0
    needs_review = 0
    skipped = 0
    for row in rows:
        text = Path(row["text_path"]).read_text(encoding="utf-8", errors="ignore")
        if not _looks_like_payment_record(text):
            skipped += 1
            continue

        payload = _extract_one(text)
        paystub_id = hashlib.sha1(
            f"{row['document_id']}|{row['page_number']}|{payload.get('pay_date')}|{payload.get('document_type')}".encode(
                "utf-8"
            )
        ).hexdigest()[:16]

        conn.execute(
            """
            INSERT INTO paystubs (
                paystub_id, document_id, page_number, document_type, pay_period, pay_date,
                gross_pay, net_pay, currency, items_json, validation_status, validation_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paystub_id) DO UPDATE SET
                document_type = excluded.document_type,
                pay_period = excluded.pay_period,
                pay_date = excluded.pay_date,
                gross_pay = excluded.gross_pay,
                net_pay = excluded.net_pay,
                currency = excluded.currency,
                items_json = excluded.items_json,
                validation_status = excluded.validation_status,
                validation_notes = excluded.validation_notes
            """,
            (
                paystub_id,
                row["document_id"],
                row["page_number"],
                payload["document_type"],
                payload["pay_period"],
                payload["pay_date"],
                payload["gross_pay"],
                payload["net_pay"],
                payload["currency"],
                json.dumps(payload["items"], sort_keys=True),
                payload["validation_status"],
                payload["validation_notes"],
            ),
        )

        append_jsonl(
            settings.paystubs_jsonl,
            {
                "paystub_id": paystub_id,
                "document_id": row["document_id"],
                "page_number": row["page_number"],
                **payload,
            },
        )
        processed += 1
        if payload["validation_status"] != "valid":
            needs_review += 1

    record_stage_metric(
        settings,
        conn,
        "payment_records_extract",
        processed=processed,
        skipped=skipped,
        failed=0,
        metadata={"needs_review": needs_review},
    )
    conn.commit()
    return processed, needs_review
