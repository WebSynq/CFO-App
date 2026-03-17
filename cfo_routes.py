"""
CFO Routes Blueprint — Board of Directors AI
All routes scoped under /cfo prefix.
"""

import sqlite3
import json
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template, g
from cfo_advisor import call_cfo, get_all_modes, get_mode_config

logger = logging.getLogger(__name__)

cfo_bp = Blueprint("cfo", __name__, url_prefix="/cfo")

# ---------------------------------------------------------------------------
# DB Helper — reuse existing app db connection pattern
# ---------------------------------------------------------------------------

def get_db():
    """Get database connection. Expects app to have DATABASE config."""
    from flask import current_app
    db = sqlite3.connect(
        current_app.config["DATABASE"],
        detect_types=sqlite3.PARSE_DECLTYPES
    )
    db.row_factory = sqlite3.Row
    return db


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@cfo_bp.route("/", methods=["GET"])
def cfo_interface():
    """Full CFO advisor interface (standalone page)."""
    modes = get_all_modes()
    return render_template("cfo.html", modes=modes)


@cfo_bp.route("/embed", methods=["GET"])
def cfo_embed():
    """GHL-embeddable lightweight interface."""
    modes = get_all_modes()
    return render_template("cfo_embed.html", modes=modes)


@cfo_bp.route("/modes", methods=["GET"])
def get_modes():
    """Return mode configurations for frontend."""
    return jsonify({"success": True, "modes": get_all_modes()})


@cfo_bp.route("/chat", methods=["POST"])
def cfo_chat():
    """
    Main CFO chat endpoint.
    
    Expected JSON body:
    {
        "message": "string",
        "mode": "advisory|budget|cashflow|strategic",
        "session_id": "string (optional)",
        "history": [{"role": "user|assistant", "content": "..."}] (optional)
    }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Invalid JSON body"}), 400

    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "Message is required"}), 400

    mode = data.get("mode", "advisory")
    session_id = data.get("session_id") or f"session_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
    history = data.get("history") or []

    # Sanitize history — only allow valid roles
    clean_history = [
        {"role": m["role"], "content": m["content"]}
        for m in history
        if isinstance(m, dict)
        and m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
    ][-10:]  # Cap at last 10 exchanges to control token usage

    # Call CFO advisor
    result = call_cfo(
        user_message=message,
        mode=mode,
        conversation_history=clean_history
    )

    # Persist to DB (non-blocking on failure)
    try:
        db = get_db()
        db.execute(
            """
            INSERT INTO cfo_conversations 
                (session_id, mode, user_message, cfo_response, model_used, success, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                mode,
                message,
                result.get("response") or result.get("error"),
                result.get("model"),
                1 if result["success"] else 0,
                datetime.utcnow().isoformat()
            )
        )
        db.commit()
        db.close()
    except Exception as e:
        logger.error(f"CFO DB write failed: {e}")

    if result["success"]:
        return jsonify({
            "success": True,
            "response": result["response"],
            "mode": mode,
            "mode_config": get_mode_config(mode),
            "session_id": session_id
        })
    else:
        return jsonify({
            "success": False,
            "error": result["error"],
            "session_id": session_id
        }), 500


@cfo_bp.route("/history", methods=["GET"])
def cfo_history():
    """
    Get conversation history.
    Query params: session_id (optional), mode (optional), limit (default 20)
    """
    session_id = request.args.get("session_id")
    mode = request.args.get("mode")
    limit = min(int(request.args.get("limit", 20)), 100)

    try:
        db = get_db()
        query = "SELECT * FROM cfo_conversations WHERE 1=1"
        params = []

        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if mode:
            query += " AND mode = ?"
            params.append(mode)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = db.execute(query, params).fetchall()
        db.close()

        return jsonify({
            "success": True,
            "conversations": [dict(row) for row in rows]
        })
    except Exception as e:
        logger.error(f"CFO history fetch failed: {e}")
        return jsonify({"success": False, "error": "Failed to retrieve history"}), 500


@cfo_bp.route("/history/<int:conversation_id>", methods=["DELETE"])
def delete_conversation(conversation_id):
    """Delete a specific conversation entry."""
    try:
        db = get_db()
        db.execute("DELETE FROM cfo_conversations WHERE id = ?", (conversation_id,))
        db.commit()
        db.close()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"CFO delete failed: {e}")
        return jsonify({"success": False, "error": "Delete failed"}), 500
