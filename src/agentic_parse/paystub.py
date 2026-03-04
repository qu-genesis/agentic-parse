from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .chunk_embed import retrieve_top_k_chunks
from .config import Settings
from .db import Connection
from .llm import get_llm_client
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


def _extract_payment_records_llm(settings: Settings, document_id: str, context: str) -> list[dict] | None:
    llm = get_llm_client()
    payload = llm.json(
        task="payment_records_from_embeddings",
        cache_dir=settings.llm_cache_dir,
        system_prompt=(
            "Extract payment records from the provided chunk context. "
            "Return strict JSON only. Do not infer facts not present."
        ),
        user_prompt=(
            "Return JSON with schema:\n"
            "{\"records\":[{\"document_type\":\"receipt|invoice|payment_sheet|pay_stub|payment_record\","
            "\"pay_period\":null,\"pay_date\":null,\"gross_pay\":null,\"net_pay\":null,"
            "\"currency\":null,\"items\":[{\"description\":\"...\",\"amount\":0.0,\"currency\":\"USD\"}],"
            "\"validation_notes\":null,\"evidence_chunk_ids\":[\"...\"]}]}\n\n"
            "Rules:\n"
            "- Include only records supported by context.\n"
            "- Keep optional fields null if missing.\n"
            "- Use numbers for amounts.\n\n"
            f"document_id: {document_id}\n"
            f"context:\n{context}"
        ),
        max_output_tokens=1500,
    )
    if not payload:
        return None
    records = payload.get("records")
    if not isinstance(records, list):
        return None
    return [r for r in records if isinstance(r, dict)]


def _normalize_record(record: dict) -> dict:
    document_type = str(record.get("document_type", "payment_record")).strip().lower() or "payment_record"
    pay_period = record.get("pay_period")
    pay_date = record.get("pay_date")
    gross_pay = record.get("gross_pay")
    net_pay = record.get("net_pay")
    currency = record.get("currency")

    items_raw = record.get("items")
    items: list[dict] = []
    if isinstance(items_raw, list):
        for item in items_raw:
            if not isinstance(item, dict):
                continue
            desc = str(item.get("description", "")).strip()
            amount = item.get("amount")
            item_currency = item.get("currency")
            try:
                amount = float(amount) if amount is not None else None
            except Exception:
                amount = None
            if not desc and amount is None:
                continue
            items.append(
                {
                    "description": desc or "item",
                    "amount": amount,
                    "currency": str(item_currency).strip().upper() if item_currency else None,
                }
            )

    notes: list[str] = []
    if (gross_pay is None and net_pay is None) and not items:
        notes.append("no_detectable_payment_amounts")
    try:
        if gross_pay is not None and net_pay is not None and float(net_pay) > float(gross_pay):
            notes.append("net_pay_exceeds_gross_pay")
    except Exception:
        notes.append("invalid_numeric_pay_fields")
    provided_notes = record.get("validation_notes")
    if isinstance(provided_notes, str) and provided_notes.strip():
        notes.append(provided_notes.strip())

    status = "valid" if not notes else "needs_review"
    return {
        "document_type": document_type,
        "pay_period": pay_period if isinstance(pay_period, str) and pay_period.strip() else None,
        "pay_date": pay_date if isinstance(pay_date, str) and pay_date.strip() else None,
        "gross_pay": float(gross_pay) if isinstance(gross_pay, (int, float)) else None,
        "net_pay": float(net_pay) if isinstance(net_pay, (int, float)) else None,
        "currency": str(currency).strip().upper() if isinstance(currency, str) and currency.strip() else None,
        "items": items,
        "validation_status": status,
        "validation_notes": ",".join(notes) if notes else None,
        "evidence_chunk_ids": [x for x in record.get("evidence_chunk_ids", []) if isinstance(x, str)],
    }


def extract_paystubs(settings: Settings, conn: Connection) -> tuple[int, int]:
    llm = get_llm_client()
    before_in, before_out = llm.usage_snapshot()
    docs = conn.execute(
        "SELECT document_id FROM documents WHERE status_embed = 'done' ORDER BY created_at ASC"
    ).fetchall()

    processed = 0
    needs_review = 0
    skipped = 0
    for doc in docs:
        document_id = doc["document_id"]
        chunks = retrieve_top_k_chunks(
            conn,
            "Payment records, receipts, invoices, line items, totals, amounts, due dates, paid amounts, payroll references.",
            top_k=16,
            max_chunks=32,
            max_tokens=7000,
            document_id=document_id,
        )
        if not chunks:
            skipped += 1
            continue

        context_parts: list[str] = []
        chunk_to_page: dict[str, int | None] = {}
        context_chars = 0
        for row in chunks:
            text = Path(row["text_path"]).read_text(encoding="utf-8", errors="ignore").strip()
            if not text:
                continue
            chunk_to_page[row["chunk_id"]] = row["page_number"]
            part = f"[chunk_id:{row['chunk_id']} page:{row['page_number']}]\n{text}\n"
            if context_chars + len(part) > 18000:
                break
            context_parts.append(part)
            context_chars += len(part)
        context = "\n".join(context_parts).strip()
        if not context:
            skipped += 1
            continue

        llm_records = _extract_payment_records_llm(settings, document_id, context)
        if not llm_records:
            skipped += 1
            continue

        for idx, raw_record in enumerate(llm_records, start=1):
            payload = _normalize_record(raw_record)
            evidence_ids = payload.get("evidence_chunk_ids", [])
            page_number = None
            for cid in evidence_ids:
                page_number = chunk_to_page.get(cid)
                if page_number is not None:
                    break

            paystub_id = hashlib.sha1(
                f"{document_id}|{idx}|{payload.get('pay_date')}|{payload.get('document_type')}".encode("utf-8")
            ).hexdigest()[:16]

            conn.execute(
                """
                INSERT INTO paystubs (
                    paystub_id, document_id, page_number, document_type, pay_period, pay_date,
                    gross_pay, net_pay, currency, items_json, validation_status, validation_notes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(paystub_id) DO UPDATE SET
                    page_number = EXCLUDED.page_number,
                    document_type = EXCLUDED.document_type,
                    pay_period = EXCLUDED.pay_period,
                    pay_date = EXCLUDED.pay_date,
                    gross_pay = EXCLUDED.gross_pay,
                    net_pay = EXCLUDED.net_pay,
                    currency = EXCLUDED.currency,
                    items_json = EXCLUDED.items_json,
                    validation_status = EXCLUDED.validation_status,
                    validation_notes = EXCLUDED.validation_notes
                """,
                (
                    paystub_id,
                    document_id,
                    page_number,
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
                    "document_id": document_id,
                    "page_number": page_number,
                    **payload,
                },
            )
            processed += 1
            if payload["validation_status"] != "valid":
                needs_review += 1

    after_in, after_out = llm.usage_snapshot()
    record_stage_metric(
        settings,
        conn,
        "payment_records_extract",
        processed=processed,
        skipped=skipped,
        failed=0,
        token_input=max(0, after_in - before_in),
        token_output=max(0, after_out - before_out),
        metadata={"needs_review": needs_review},
    )
    conn.commit()
    return processed, needs_review
