"""Storage for the customer support chatbot: conversations, messages, follow-up inquiries, and FAQs.

Tables (created and migrated by ``initialize()`` at startup, like the other repositories):

* ``chatbot_conversations`` — one chat session. ``customer_id`` links it to the signed-in customer
  (``NULL`` for guests); a conversation started by one account is never continued by another.
* ``chatbot_messages`` — every customer question and assistant reply, in order. An assistant reply
  records whether it answered confidently (``answered``) and what produced it (``source``: ``ai`` for
  Gemini, ``database`` for the built-in answers used when the AI is unavailable). Unanswered replies are
  listed for the administrator until marked reviewed.
* ``chatbot_inquiries`` — messages a customer sent to the shop administrator from the chat window.
* ``chatbot_faqs`` — the administrator-managed frequently asked questions the assistant answers from.

Chatbot settings (enabled, welcome message, suggested questions) are stored in ``app_meta`` under the
``admin.`` prefix through ``admin_repository`` (installation settings, kept when a backup is restored).

Every query uses bound parameters; table and column names are never built from user input.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any

from backend.app.database.connection import connection_scope, exclusive_write
from backend.app.utils.clock import now_iso


FAQ_SEED_MARKER = "chatbot_faqs_v1"

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS chatbot_conversations (
        id TEXT PRIMARY KEY,
        customer_id TEXT REFERENCES customer_accounts(id) ON DELETE SET NULL,
        customer_name TEXT NOT NULL DEFAULT '',
        customer_email TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        message_count INTEGER NOT NULL DEFAULT 0,
        unanswered_count INTEGER NOT NULL DEFAULT 0,
        first_question TEXT NOT NULL DEFAULT '',
        page TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chatbot_messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES chatbot_conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        reply_to TEXT,
        answered INTEGER,
        needs_admin INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT '',
        links TEXT NOT NULL DEFAULT '[]',
        reviewed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chatbot_inquiries (
        id TEXT PRIMARY KEY,
        conversation_id TEXT REFERENCES chatbot_conversations(id) ON DELETE SET NULL,
        customer_id TEXT REFERENCES customer_accounts(id) ON DELETE SET NULL,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        phone TEXT NOT NULL DEFAULT '',
        message TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
        created_at TEXT NOT NULL,
        resolved_at TEXT,
        resolved_by TEXT NOT NULL DEFAULT '',
        admin_note TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chatbot_faqs (
        id TEXT PRIMARY KEY,
        question TEXT NOT NULL,
        answer TEXT NOT NULL,
        keywords TEXT NOT NULL DEFAULT '',
        sort_order INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chatbot_conversations_updated ON chatbot_conversations(updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_chatbot_conversations_customer ON chatbot_conversations(customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_chatbot_messages_conversation ON chatbot_messages(conversation_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_chatbot_messages_unanswered ON chatbot_messages(role, answered, reviewed_at)",
    "CREATE INDEX IF NOT EXISTS idx_chatbot_inquiries_status ON chatbot_inquiries(status, created_at)",
)

# Starting FAQs, taken from the shop's existing order instructions and customer pages. The administrator
# edits, hides, or replaces them on the AI Chatbot page; prices are never written here (the assistant reads
# them from the catalog and package tables).
DEFAULT_FAQS = (
    (
        "What services do you offer?",
        "We print invitations, souvenirs, stickers, sintra boards, party needs, tarpaulins, and large format prints. "
        "The Services page lists every product that is currently available.",
        "services offer products ano serbisyo meron",
    ),
    (
        "Do you accept customized orders?",
        "Yes. Products that allow it can be ordered as a customized order instead of a ready-made package. "
        "Customized orders are priced by the shop administrator's quotation, which you accept or decline in the "
        "customer portal before paying anything.",
        "customized custom personalized quotation quote design",
    ),
    (
        "How do I place an order?",
        "Open the Order page, choose a service and product, pick a package or a customized order, fill in the "
        "order details and upload your files, then review and submit. You need to be signed in to a customer "
        "account to submit an order.",
        "how order place mag-order paano umorder",
    ),
    (
        "Do I need to create an account?",
        "Yes. Orders are submitted from a customer account so you can follow your order, accept quotations, "
        "upload your payment, and approve your draft in the customer portal. Creating an account is free.",
        "account register sign up create login",
    ),
    (
        "How much is the down payment?",
        "Every order requires a 50% down payment of the confirmed amount. The remaining 50% is paid before your "
        "order is released for pickup or delivery.",
        "down payment downpayment dp 50 percent bayad deposit",
    ),
    (
        "How do I pay?",
        "Pay through GCash and upload the screenshot or receipt in the customer portal. The shop verifies the "
        "payment; it counts as paid only after verification. Payment instructions come from the shop administrator.",
        "pay payment gcash bayad paano magbayad receipt",
    ),
    (
        "How long does the printing process take?",
        "Order processing takes approximately 2–5 days after your design is approved. This is processing time, "
        "not a guaranteed delivery date.",
        "how long processing time days gaano katagal ilang araw turnaround rush",
    ),
    (
        "How can I submit my design or images?",
        "Upload your design, photos, or reference images in the upload step of the order form (JPG, PNG, WebP, or "
        "PDF, up to 5 MB each). Send original, high-quality pictures for the best print quality.",
        "design upload file image picture photo larawan submit",
    ),
    (
        "Can I modify my order?",
        "Before printing, the shop sends a draft layout for you to check. You can approve it or request changes in "
        "the customer portal. NO APPROVAL, NO PRINT. For other changes, contact the shop administrator.",
        "modify change edit baguhin palitan cancel revise",
    ),
    (
        "How do I check my order status?",
        "Sign in and open My Orders in the customer portal, or ask me here while you are signed in and I will show "
        "your orders and what happens next.",
        "status track order check nasaan reference",
    ),
    (
        "What happens after I pay the down payment?",
        "The shop verifies your down payment and your order becomes Confirmed. The shop then prepares your draft "
        "layout for approval, prints your order after you approve it, and releases it for pickup or delivery once "
        "the remaining balance is paid.",
        "after down payment next step confirmed verified",
    ),
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def new_conversation_id() -> str:
    # Also the browser's handle for continuing the chat, so it must not be guessable.
    return secrets.token_urlsafe(24)


def initialize() -> None:
    with exclusive_write() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for statement in SCHEMA:
            connection.execute(statement)
        seeded = connection.execute("SELECT value FROM app_meta WHERE key = ?", (FAQ_SEED_MARKER,)).fetchone()
        if seeded is None:
            now = now_iso()
            for position, (question, answer, keywords) in enumerate(DEFAULT_FAQS):
                connection.execute(
                    "INSERT INTO chatbot_faqs (id, question, answer, keywords, sort_order, active, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (new_id("faq"), question, answer, keywords, position, now, now),
                )
            connection.execute("INSERT INTO app_meta (key, value) VALUES (?, ?)", (FAQ_SEED_MARKER, now))


# ================================================================== conversations


def _conversation(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "customer_id": row["customer_id"],
        "customer_name": row["customer_name"],
        "customer_email": row["customer_email"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "message_count": int(row["message_count"]),
        "unanswered_count": int(row["unanswered_count"]),
        "first_question": row["first_question"],
        "page": row["page"],
    }


def get_conversation(conversation_id: str) -> dict[str, Any] | None:
    with connection_scope() as connection:
        row = connection.execute("SELECT * FROM chatbot_conversations WHERE id = ?", (conversation_id,)).fetchone()
    return _conversation(row)


def create_conversation(customer: dict[str, Any] | None, page: str) -> dict[str, Any]:
    now = now_iso()
    conversation_id = new_conversation_id()
    with connection_scope() as connection:
        connection.execute(
            "INSERT INTO chatbot_conversations (id, customer_id, customer_name, customer_email, started_at, updated_at, page)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                customer["id"] if customer else None,
                customer.get("name", "") if customer else "",
                customer.get("email", "") if customer else "",
                now,
                now,
                page,
            ),
        )
    conversation = get_conversation(conversation_id)
    assert conversation is not None
    return conversation


def attach_customer(conversation_id: str, customer: dict[str, Any]) -> None:
    """Link a guest conversation to the customer who signed in during it."""
    with connection_scope() as connection:
        connection.execute(
            "UPDATE chatbot_conversations SET customer_id = ?, customer_name = ?, customer_email = ?"
            " WHERE id = ? AND customer_id IS NULL",
            (customer["id"], customer.get("name", ""), customer.get("email", ""), conversation_id),
        )


def _message(row: sqlite3.Row) -> dict[str, Any]:
    try:
        links = json.loads(row["links"] or "[]")
    except (TypeError, ValueError):
        links = []
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "role": row["role"],
        "content": row["content"],
        "created_at": row["created_at"],
        "reply_to": row["reply_to"],
        "answered": None if row["answered"] is None else bool(row["answered"]),
        "needs_admin": bool(row["needs_admin"]),
        "source": row["source"],
        "links": links if isinstance(links, list) else [],
        "reviewed_at": row["reviewed_at"],
    }


def list_messages(conversation_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Messages of one conversation, oldest first (the newest ``limit`` when given)."""
    with connection_scope() as connection:
        if limit is None:
            rows = connection.execute(
                "SELECT * FROM chatbot_messages WHERE conversation_id = ? ORDER BY created_at, rowid",
                (conversation_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM (SELECT *, rowid AS position FROM chatbot_messages WHERE conversation_id = ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?) ORDER BY created_at, position",
                (conversation_id, limit),
            ).fetchall()
    return [_message(row) for row in rows]


def add_exchange(
    conversation_id: str,
    question: str,
    reply: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Save a customer question and the assistant's reply together, and update the conversation counters."""
    question_at = now_iso()
    question_id = new_id("msg")
    reply_id = new_id("msg")
    answered = bool(reply.get("answered"))
    with connection_scope() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO chatbot_messages (id, conversation_id, role, content, created_at) VALUES (?, ?, 'user', ?, ?)",
            (question_id, conversation_id, question, question_at),
        )
        reply_at = now_iso()
        connection.execute(
            "INSERT INTO chatbot_messages (id, conversation_id, role, content, created_at, reply_to, answered, needs_admin, source, links)"
            " VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?)",
            (
                reply_id,
                conversation_id,
                reply["content"],
                reply_at,
                question_id,
                1 if answered else 0,
                1 if reply.get("needs_admin") else 0,
                str(reply.get("source", "")),
                json.dumps(reply.get("links") or []),
            ),
        )
        connection.execute(
            """
            UPDATE chatbot_conversations SET
                updated_at = ?,
                message_count = message_count + 2,
                unanswered_count = unanswered_count + ?,
                first_question = CASE WHEN first_question = '' THEN ? ELSE first_question END
            WHERE id = ?
            """,
            (reply_at, 0 if answered else 1, question[:200], conversation_id),
        )
        rows = connection.execute(
            "SELECT * FROM chatbot_messages WHERE id IN (?, ?)", (question_id, reply_id)
        ).fetchall()
    by_id = {row["id"]: _message(row) for row in rows}
    return by_id[question_id], by_id[reply_id]


def _like(text: str) -> str:
    """A LIKE pattern matching ``text`` literally (``%`` and ``_`` escaped; use with ESCAPE '\\')."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped.lower()}%"


def list_conversations(search: str = "", kind: str = "", limit: int = 25, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Conversations, most recent first. ``kind``: '' (all), 'customer', 'guest', or 'unanswered'."""
    conditions: list[str] = ["c.message_count > 0"]
    parameters: list[Any] = []
    if kind == "customer":
        conditions.append("c.customer_id IS NOT NULL")
    elif kind == "guest":
        conditions.append("c.customer_id IS NULL")
    elif kind == "unanswered":
        conditions.append(
            "EXISTS (SELECT 1 FROM chatbot_messages m WHERE m.conversation_id = c.id AND m.role = 'assistant'"
            " AND m.answered = 0 AND m.reviewed_at IS NULL)"
        )
    if search:
        pattern = _like(search)
        conditions.append(
            "(lower(c.customer_name) LIKE ? ESCAPE '\\' OR lower(c.customer_email) LIKE ? ESCAPE '\\'"
            " OR EXISTS (SELECT 1 FROM chatbot_messages m WHERE m.conversation_id = c.id AND lower(m.content) LIKE ? ESCAPE '\\'))"
        )
        parameters.extend([pattern, pattern, pattern])
    where = " WHERE " + " AND ".join(conditions)
    with connection_scope() as connection:
        total = connection.execute(f"SELECT COUNT(*) FROM chatbot_conversations c{where}", parameters).fetchone()[0]
        rows = connection.execute(
            f"SELECT c.* FROM chatbot_conversations c{where} ORDER BY c.updated_at DESC LIMIT ? OFFSET ?",
            [*parameters, limit, offset],
        ).fetchall()
    return [conversation for row in rows if (conversation := _conversation(row))], int(total)


def delete_conversation(conversation_id: str) -> bool:
    with connection_scope() as connection:
        cursor = connection.execute("DELETE FROM chatbot_conversations WHERE id = ?", (conversation_id,))
    return cursor.rowcount == 1


def list_unanswered(limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Replies that could not answer confidently and are not reviewed yet, with the question asked."""
    base = (
        " FROM chatbot_messages a JOIN chatbot_messages q ON q.id = a.reply_to"
        " JOIN chatbot_conversations c ON c.id = a.conversation_id"
        " WHERE a.role = 'assistant' AND a.answered = 0 AND a.reviewed_at IS NULL"
    )
    with connection_scope() as connection:
        total = connection.execute("SELECT COUNT(*)" + base).fetchone()[0]
        rows = connection.execute(
            "SELECT a.id AS id, a.conversation_id AS conversation_id, a.content AS reply, a.created_at AS created_at,"
            " a.needs_admin AS needs_admin, q.content AS question, c.customer_name AS customer_name,"
            " c.customer_email AS customer_email, c.customer_id AS customer_id"
            + base + " ORDER BY a.created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "question": row["question"],
            "reply": row["reply"],
            "created_at": row["created_at"],
            "needs_admin": bool(row["needs_admin"]),
            "customer_name": row["customer_name"],
            "customer_email": row["customer_email"],
            "is_customer": row["customer_id"] is not None,
        }
        for row in rows
    ], int(total)


def mark_reviewed(message_id: str) -> bool:
    with connection_scope() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT conversation_id FROM chatbot_messages WHERE id = ? AND role = 'assistant' AND answered = 0 AND reviewed_at IS NULL",
            (message_id,),
        ).fetchone()
        if row is None:
            return False
        connection.execute("UPDATE chatbot_messages SET reviewed_at = ? WHERE id = ?", (now_iso(), message_id))
        connection.execute(
            "UPDATE chatbot_conversations SET unanswered_count = MAX(unanswered_count - 1, 0) WHERE id = ?",
            (row["conversation_id"],),
        )
    return True


# ================================================================== inquiries


def _inquiry(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def create_inquiry(data: dict[str, Any]) -> dict[str, Any]:
    inquiry_id = new_id("inq")
    with connection_scope() as connection:
        connection.execute(
            "INSERT INTO chatbot_inquiries (id, conversation_id, customer_id, name, email, phone, message, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                inquiry_id,
                data.get("conversation_id"),
                data.get("customer_id"),
                data["name"],
                data["email"],
                data.get("phone", ""),
                data["message"],
                now_iso(),
            ),
        )
        row = connection.execute("SELECT * FROM chatbot_inquiries WHERE id = ?", (inquiry_id,)).fetchone()
    return _inquiry(row)


def get_inquiry(inquiry_id: str) -> dict[str, Any] | None:
    with connection_scope() as connection:
        row = connection.execute("SELECT * FROM chatbot_inquiries WHERE id = ?", (inquiry_id,)).fetchone()
    return _inquiry(row) if row else None


def list_inquiries(status: str = "", limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    where, parameters = ("", []) if status not in {"open", "resolved"} else (" WHERE status = ?", [status])
    with connection_scope() as connection:
        total = connection.execute(f"SELECT COUNT(*) FROM chatbot_inquiries{where}", parameters).fetchone()[0]
        rows = connection.execute(
            f"SELECT * FROM chatbot_inquiries{where} ORDER BY status = 'resolved', created_at DESC LIMIT ? OFFSET ?",
            [*parameters, limit, offset],
        ).fetchall()
    return [_inquiry(row) for row in rows], int(total)


def list_inquiries_for_conversation(conversation_id: str) -> list[dict[str, Any]]:
    with connection_scope() as connection:
        rows = connection.execute(
            "SELECT * FROM chatbot_inquiries WHERE conversation_id = ? ORDER BY created_at", (conversation_id,)
        ).fetchall()
    return [_inquiry(row) for row in rows]


def set_inquiry_status(inquiry_id: str, status: str, actor: str, note: str) -> dict[str, Any] | None:
    resolved = status == "resolved"
    with connection_scope() as connection:
        cursor = connection.execute(
            "UPDATE chatbot_inquiries SET status = ?, resolved_at = ?, resolved_by = ?, admin_note = ? WHERE id = ?",
            (status, now_iso() if resolved else None, actor if resolved else "", note, inquiry_id),
        )
        if cursor.rowcount != 1:
            return None
    return get_inquiry(inquiry_id)


def count_open_inquiries() -> int:
    with connection_scope() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM chatbot_inquiries WHERE status = 'open'").fetchone()[0])


# ================================================================== FAQs


def _faq(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "question": row["question"],
        "answer": row["answer"],
        "keywords": row["keywords"],
        "sort_order": int(row["sort_order"]),
        "active": bool(row["active"]),
        "updated_at": row["updated_at"],
    }


def list_faqs(active_only: bool) -> list[dict[str, Any]]:
    active = " WHERE active = 1" if active_only else ""
    with connection_scope() as connection:
        rows = connection.execute(f"SELECT * FROM chatbot_faqs{active} ORDER BY sort_order, created_at").fetchall()
    return [_faq(row) for row in rows]


def get_faq(faq_id: str) -> dict[str, Any] | None:
    with connection_scope() as connection:
        row = connection.execute("SELECT * FROM chatbot_faqs WHERE id = ?", (faq_id,)).fetchone()
    return _faq(row) if row else None


def create_faq(data: dict[str, Any]) -> dict[str, Any]:
    faq_id = new_id("faq")
    now = now_iso()
    with connection_scope() as connection:
        if data.get("sort_order") is None:
            data["sort_order"] = int(connection.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM chatbot_faqs").fetchone()[0])
        connection.execute(
            "INSERT INTO chatbot_faqs (id, question, answer, keywords, sort_order, active, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (faq_id, data["question"], data["answer"], data.get("keywords", ""), int(data["sort_order"]),
             1 if data.get("active", True) else 0, now, now),
        )
    faq = get_faq(faq_id)
    assert faq is not None
    return faq


def update_faq(faq_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    assignments: list[str] = []
    parameters: list[Any] = []
    for column in ("question", "answer", "keywords", "sort_order", "active"):
        if column in data and data[column] is not None:
            value = data[column]
            assignments.append(f"{column} = ?")
            parameters.append((1 if value else 0) if isinstance(value, bool) else value)
    if assignments:
        assignments.append("updated_at = ?")
        parameters.extend([now_iso(), faq_id])
        with connection_scope() as connection:
            connection.execute(f"UPDATE chatbot_faqs SET {', '.join(assignments)} WHERE id = ?", parameters)
    return get_faq(faq_id)


def delete_faq(faq_id: str) -> bool:
    with connection_scope() as connection:
        cursor = connection.execute("DELETE FROM chatbot_faqs WHERE id = ?", (faq_id,))
    return cursor.rowcount == 1


# ================================================================== activity


def activity(since: str) -> dict[str, Any]:
    """Counts since ``since`` (ISO time) and per-day question counts, for the admin overview."""
    with connection_scope() as connection:
        conversations = connection.execute(
            "SELECT COUNT(*) FROM chatbot_conversations WHERE started_at >= ? AND message_count > 0", (since,)
        ).fetchone()[0]
        questions = connection.execute(
            "SELECT COUNT(*) FROM chatbot_messages WHERE role = 'user' AND created_at >= ?", (since,)
        ).fetchone()[0]
        by_source = connection.execute(
            "SELECT source, COUNT(*) AS total, SUM(CASE WHEN answered = 1 THEN 1 ELSE 0 END) AS answered"
            " FROM chatbot_messages WHERE role = 'assistant' AND created_at >= ? GROUP BY source",
            (since,),
        ).fetchall()
        signed_in = connection.execute(
            "SELECT COUNT(*) FROM chatbot_conversations WHERE started_at >= ? AND message_count > 0 AND customer_id IS NOT NULL",
            (since,),
        ).fetchone()[0]
        timestamps = connection.execute(
            "SELECT created_at FROM chatbot_messages WHERE role = 'user' AND created_at >= ?", (since,)
        ).fetchall()
        unanswered_open = connection.execute(
            "SELECT COUNT(*) FROM chatbot_messages WHERE role = 'assistant' AND answered = 0 AND reviewed_at IS NULL"
        ).fetchone()[0]
        total_conversations = connection.execute(
            "SELECT COUNT(*) FROM chatbot_conversations WHERE message_count > 0"
        ).fetchone()[0]
    sources = {row["source"] or "unknown": {"replies": int(row["total"]), "answered": int(row["answered"] or 0)} for row in by_source}
    return {
        "conversations": int(conversations),
        "signed_in_conversations": int(signed_in),
        "questions": int(questions),
        "sources": sources,
        "question_times": [row["created_at"] for row in timestamps],
        "unanswered_open": int(unanswered_open),
        "total_conversations": int(total_conversations),
    }
