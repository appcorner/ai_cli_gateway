from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any


class GatewaySessionStore:
    """Thread-safe JSON transcript store for CLIs without native history/list APIs."""

    def __init__(self, index_path_fn, message_limit: int = 80):
        self._index_path_fn = index_path_fn
        self._message_limit = message_limit
        self._lock = threading.Lock()

    def read_index(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self._index_path_fn(), encoding="utf-8") as index_file:
                payload = json.load(index_file)
            sessions = payload.get("sessions", {}) if isinstance(payload, dict) else {}
            return sessions if isinstance(sessions, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            print("session index could not be read", flush=True)
            return {}

    def write_index(self, sessions: dict[str, dict[str, Any]]) -> None:
        path = self._index_path_fn()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        temporary_path = path + ".tmp"
        with open(temporary_path, "w", encoding="utf-8") as index_file:
            json.dump({"sessions": sessions}, index_file, ensure_ascii=False)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)

    def record_turn(
        self,
        provider: str,
        session_id: str,
        prompt: str,
        answer: str,
        model: str | None = None,
        mode: str = "chat",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            sessions = self.read_index()
            session = sessions.get(session_id) or {
                "id": session_id,
                "provider": provider,
                "created_at": now,
                "updated_at": now,
                "model": model or "",
                "mode": mode,
                "title": "",
                "messages": [],
            }
            messages = session.get("messages", [])
            if not session.get("title"):
                session["title"] = next(
                    (line.strip() for line in prompt.splitlines() if line.strip()), ""
                )[:160]
            messages.extend([
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": answer},
            ])
            session.update({
                "provider": provider,
                "updated_at": now,
                "model": model or session.get("model") or "",
                "mode": mode,
                "messages": messages[-self._message_limit:],
            })
            sessions[session_id] = session
            self.write_index(sessions)

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions = self.read_index()
        return sorted(
            sessions.values(),
            key=lambda item: item.get("updated_at") or "",
            reverse=True,
        )

    def get_history(self, provider: str, session_id: str) -> dict[str, Any]:
        session = self.read_index().get(session_id)
        return {
            "id": session_id,
            "provider": provider,
            "messages": session.get("messages", []) if session else [],
        }

    def has_session(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self.read_index()

    def remove_session(self, session_id: str) -> None:
        with self._lock:
            sessions = self.read_index()
            sessions.pop(session_id, None)
            self.write_index(sessions)
