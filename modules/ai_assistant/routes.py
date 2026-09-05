"""Authenticated UI and JSON endpoints for the internal AI assistant."""

import datetime as dt
import threading

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from . import repository, services
from .constants import (
    CONVERSATION_LIST_LIMIT,
    DEFAULT_MAX_CONCURRENT_REQUESTS,
    DEFAULT_USER_REQUESTS_PER_MINUTE,
    MESSAGE_MAX_LENGTH,
)
from .openai_client import OpenAIClientError


def create_blueprint(
    get_db,
    current_user,
    responses_client,
    model_provider,
    boats,
    max_concurrent_requests=DEFAULT_MAX_CONCURRENT_REQUESTS,
    requests_per_minute=DEFAULT_USER_REQUESTS_PER_MINUTE,
):
    blueprint = Blueprint("ai_assistant", __name__)
    capacity = threading.BoundedSemaphore(max(1, int(max_concurrent_requests)))

    def user_or_none():
        user = current_user()
        if not user:
            return None
        return {
            "owner_type": str(user["owner_type"]),
            "owner_id": int(user["owner_id"]),
            "name": str(user["name"]),
            "positions": list(user.get("positions") or []),
            "manager_view": bool(user.get("manager_view")),
        }

    def login_redirect():
        if session.get("team_id"):
            return redirect(url_for("team_login"))
        return redirect(url_for("admin_login"))

    def conversation_for_user(db, user, raw_id):
        try:
            conversation_id = int(raw_id)
        except (TypeError, ValueError):
            return None
        return repository.get_conversation(
            db, conversation_id, user["owner_type"], user["owner_id"]
        )

    @blueprint.route("/assistant")
    def index():
        user = user_or_none()
        if user is None:
            return login_redirect()
        db = get_db()
        conversations = repository.list_conversations(
            db,
            user["owner_type"],
            user["owner_id"],
            CONVERSATION_LIST_LIMIT,
        )
        selected = conversation_for_user(db, user, request.args.get("conversation"))
        if selected is None and conversations:
            selected = repository.get_conversation(
                db,
                conversations[0]["id"],
                user["owner_type"],
                user["owner_id"],
            )
        messages = repository.list_messages(db, selected["id"]) if selected else []
        return render_template(
            "ai_assistant/index.html",
            active_page="assistant",
            manager_view=user["manager_view"],
            current_user=user,
            conversations=conversations,
            selected_conversation=selected,
            messages=messages,
            assistant_configured=responses_client.configured(),
            assistant_model=model_provider(),
        )

    @blueprint.route("/assistant/api/conversations", methods=["POST"])
    def create_conversation():
        user = user_or_none()
        if user is None:
            return jsonify({"ok": False, "error": "Требуется вход в систему."}), 401
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conversation_id = repository.create_conversation(
            get_db(),
            user["owner_type"],
            user["owner_id"],
            user["name"],
            "Новый диалог",
            now,
        )
        return jsonify({"ok": True, "conversation_id": conversation_id}), 201

    @blueprint.route("/assistant/api/conversations/<int:conversation_id>", methods=["DELETE"])
    def delete_conversation(conversation_id):
        user = user_or_none()
        if user is None:
            return jsonify({"ok": False, "error": "Требуется вход в систему."}), 401
        deleted = repository.delete_conversation(
            get_db(), conversation_id, user["owner_type"], user["owner_id"]
        )
        if not deleted:
            return jsonify({"ok": False, "error": "Диалог не найден."}), 404
        return jsonify({"ok": True})

    @blueprint.route("/assistant/api/chat", methods=["POST"])
    def chat():
        user = user_or_none()
        if user is None:
            return jsonify({"ok": False, "error": "Требуется вход в систему."}), 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "Ожидались данные в формате JSON."}), 400
        message = str(payload.get("message") or "").strip()
        if not message:
            return jsonify({"ok": False, "error": "Напишите вопрос помощнику."}), 400
        if len(message) > MESSAGE_MAX_LENGTH:
            return jsonify({
                "ok": False,
                "error": f"Вопрос не должен превышать {MESSAGE_MAX_LENGTH} символов.",
            }), 400
        if not responses_client.configured():
            return jsonify({
                "ok": False,
                "error": "AI-помощник ещё не подключён: на сервере не задан OPENAI_API_KEY.",
            }), 503

        db = get_db()
        since = (dt.datetime.now() - dt.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        recent_count = repository.count_recent_user_messages(
            db, user["owner_type"], user["owner_id"], since
        )
        if recent_count >= max(1, int(requests_per_minute)):
            return jsonify({
                "ok": False,
                "error": "Слишком много запросов за минуту. Подождите немного.",
            }), 429

        conversation = conversation_for_user(db, user, payload.get("conversation_id"))
        if payload.get("conversation_id") not in (None, "") and conversation is None:
            return jsonify({"ok": False, "error": "Диалог не найден."}), 404
        if conversation is None:
            now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conversation_id = repository.create_conversation(
                db,
                user["owner_type"],
                user["owner_id"],
                user["name"],
                services.conversation_title(message),
                now,
            )
            conversation = repository.get_conversation(
                db, conversation_id, user["owner_type"], user["owner_id"]
            )
        elif conversation["title"] == "Новый диалог":
            repository.update_conversation(
                db,
                conversation["id"],
                services.conversation_title(message),
                dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            db.commit()

        if not capacity.acquire(blocking=False):
            return jsonify({
                "ok": False,
                "error": "Сейчас помощник обрабатывает максимальное число запросов. Попробуйте через несколько секунд.",
            }), 429
        try:
            assistant_message = services.run_chat(
                db,
                user,
                conversation["id"],
                message,
                model_provider(),
                responses_client,
                boats,
            )
        except OpenAIClientError as error:
            return jsonify({"ok": False, "error": error.public_message}), error.status_code
        except (services.AssistantResponseError, ValueError) as error:
            return jsonify({"ok": False, "error": str(error)}), 502
        finally:
            capacity.release()

        return jsonify({
            "ok": True,
            "conversation_id": conversation["id"],
            "message": assistant_message,
        })

    return blueprint

