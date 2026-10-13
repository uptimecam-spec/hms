"""Admin pages and APIs for users, roles, and camera access."""

from __future__ import annotations

from typing import Any, Callable

from flask import Flask, jsonify, render_template, request

from src.access_control import (
    AccessError,
    build_resource_tree,
    create_role,
    create_user,
    delete_role,
    has_permission,
    list_roles,
    list_users,
    permission_catalog,
    preview_access,
    update_role,
    update_user,
)

ActorFn = Callable[[], dict[str, Any] | None]
TokenFn = Callable[[], str]
CheckFn = Callable[[], bool]


def register_admin_routes(app: Flask, get_actor: ActorFn, csrf_token: TokenFn, csrf_ok: CheckFn) -> None:
    def _guard(permission: str):
        actor = get_actor()
        if actor is None:
            return None, (jsonify({"ok": False, "error": "authentication required"}), 401)
        if not has_permission(actor, permission):
            return None, (jsonify({"ok": False, "error": "forbidden"}), 403)
        if request.method != "GET" and not csrf_ok():
            return None, (jsonify({"ok": False, "error": "invalid csrf token"}), 403)
        return actor, None

    @app.get("/admin")
    def admin_page():
        actor = get_actor()
        return render_template(
            "admin_access.html",
            actor=actor,
            can_users=bool(actor and has_permission(actor, "users.manage")),
            can_roles=bool(actor and has_permission(actor, "roles.manage")),
        )

    @app.get("/api/admin/bootstrap")
    def admin_bootstrap():
        actor = get_actor()
        if actor is None:
            return jsonify({"ok": False, "error": "authentication required"}), 401
        if not (has_permission(actor, "users.manage") or has_permission(actor, "roles.manage")):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        return jsonify(
            {
                "ok": True,
                "csrf": csrf_token(),
                "actor": {
                    "userId": actor["userId"],
                    "username": actor["username"],
                    "displayName": actor.get("displayName") or actor["username"],
                    "unrestricted": bool((actor.get("role") or {}).get("unrestricted")),
                    "permissions": list((actor.get("role") or {}).get("permissions") or []),
                },
                "permissionCatalog": permission_catalog(),
                "roles": list_roles() if has_permission(actor, "roles.manage") or has_permission(actor, "users.manage") else [],
                "users": list_users() if has_permission(actor, "users.manage") else [],
                "tree": build_resource_tree(actor) if has_permission(actor, "users.manage") else [],
            }
        )

    @app.post("/api/admin/preview")
    def admin_preview():
        actor, err = _guard("users.manage")
        if err:
            return err
        try:
            summary = preview_access(actor, request.get_json(silent=True) or {})
        except AccessError as exc:
            return jsonify({"ok": False, "error": exc.message}), exc.status
        return jsonify({"ok": True, "summary": summary})

    @app.post("/api/admin/users")
    def admin_create_user():
        actor, err = _guard("users.manage")
        if err:
            return err
        try:
            user = create_user(actor, request.get_json(silent=True) or {})
        except AccessError as exc:
            return jsonify({"ok": False, "error": exc.message}), exc.status
        return jsonify({"ok": True, "user": user}), 201

    @app.put("/api/admin/users/<user_id>")
    def admin_update_user(user_id: str):
        actor, err = _guard("users.manage")
        if err:
            return err
        try:
            user = update_user(actor, user_id, request.get_json(silent=True) or {})
        except AccessError as exc:
            return jsonify({"ok": False, "error": exc.message}), exc.status
        return jsonify({"ok": True, "user": user})

    @app.post("/api/admin/roles")
    def admin_create_role():
        actor, err = _guard("roles.manage")
        if err:
            return err
        try:
            role = create_role(actor, request.get_json(silent=True) or {})
        except AccessError as exc:
            return jsonify({"ok": False, "error": exc.message}), exc.status
        return jsonify({"ok": True, "role": role}), 201

    @app.put("/api/admin/roles/<role_id>")
    def admin_update_role(role_id: str):
        actor, err = _guard("roles.manage")
        if err:
            return err
        try:
            role = update_role(actor, role_id, request.get_json(silent=True) or {})
        except AccessError as exc:
            return jsonify({"ok": False, "error": exc.message}), exc.status
        return jsonify({"ok": True, "role": role})

    @app.delete("/api/admin/roles/<role_id>")
    def admin_delete_role(role_id: str):
        actor, err = _guard("roles.manage")
        if err:
            return err
        try:
            delete_role(actor, role_id)
        except AccessError as exc:
            return jsonify({"ok": False, "error": exc.message}), exc.status
        return jsonify({"ok": True})
