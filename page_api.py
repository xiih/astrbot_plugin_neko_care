from __future__ import annotations

import time
from typing import Any, Dict, Iterable

from astrbot.api import logger
from quart import request

from .catgirl_schema import (
    BODY_TYPES,
    PERSONALITIES,
    bond_score,
    companion_days,
    normalize_catgirl,
    stage_name,
    status_tag,
)

PLUGIN_NAME = "astrbot_plugin_neko_care"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"


class NekoCarePageApi:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def register_routes(self) -> None:
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            return
        routes = [
            ("/config", self.get_config, ["GET"], "Neko Care runtime config"),
            ("/config/save", self.save_config, ["POST"], "Neko Care save runtime config"),
            ("/config/reset", self.reset_config, ["POST"], "Neko Care reset runtime config"),
            ("/users", self.list_users, ["GET"], "Neko Care user list"),
            ("/users/detail", self.get_user, ["GET"], "Neko Care user detail"),
            ("/users/save", self.save_user, ["POST"], "Neko Care save user"),
            ("/users/delete", self.delete_user, ["POST"], "Neko Care delete user"),
        ]
        for path, handler, methods, desc in routes:
            register(f"{PAGE_API_PREFIX}{path}", handler, methods, desc)

    async def get_config(self) -> Dict[str, Any]:
        try:
            return self._ok({"config": self.plugin.runtime_config.snapshot(), "summary": self._summary()})
        except Exception as exc:
            logger.warning(f"[猫娘养成] 拓展页配置读取失败: {exc}")
            return self._error(str(exc))

    async def save_config(self) -> Dict[str, Any]:
        try:
            payload = await request.get_json(silent=True) or {}
            config = payload.get("config") if isinstance(payload.get("config"), dict) else payload
            saved = self.plugin.runtime_config.replace(config)
            self.plugin._apply_runtime_config()
            return self._ok({"config": saved, "summary": self._summary(), "message": "配置已保存并应用。"})
        except Exception as exc:
            logger.warning(f"[猫娘养成] 拓展页配置保存失败: {exc}")
            return self._error(str(exc))

    async def reset_config(self) -> Dict[str, Any]:
        try:
            saved = self.plugin.runtime_config.reset()
            self.plugin._apply_runtime_config()
            return self._ok({"config": saved, "summary": self._summary(), "message": "已恢复默认运行参数。"})
        except Exception as exc:
            logger.warning(f"[猫娘养成] 拓展页配置重置失败: {exc}")
            return self._error(str(exc))

    async def list_users(self) -> Dict[str, Any]:
        try:
            root = self.plugin.store.get(default={}) or {}
            users = [self._user_row(root, uid) for uid in self._all_user_ids(root)]
            users.sort(key=lambda row: (not row.get("has_catgirl"), -int(row.get("wallet", 0)), row.get("uid", "")))
            return self._ok({"users": users, "summary": self._summary(), "options": self._options()})
        except Exception as exc:
            logger.warning(f"[猫娘养成] 拓展页用户列表读取失败: {exc}")
            return self._error(str(exc))

    async def get_user(self) -> Dict[str, Any]:
        try:
            uid = self._clean_uid(request.args.get("uid", ""))
            if not uid:
                return self._error("缺少用户 ID")
            root = self.plugin.store.get(default={}) or {}
            return self._ok({"user": self._user_detail(root, uid), "options": self._options()})
        except Exception as exc:
            logger.warning(f"[猫娘养成] 拓展页用户详情读取失败: {exc}")
            return self._error(str(exc))

    async def save_user(self) -> Dict[str, Any]:
        try:
            payload = await request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                return self._error("请求内容格式错误")
            uid = self._clean_uid(payload.get("uid", ""))
            if not uid:
                return self._error("缺少用户 ID")

            def op(root):
                wallet = root.setdefault("wallet", {})
                sign = root.setdefault("sign", {})
                cats = root.setdefault("catgirls", {})
                items = root.setdefault("items", {})
                pending = root.setdefault("pending_adoptions", {})

                wallet[uid] = self._int(payload.get("wallet", wallet.get(uid, 0)), 0, 0, 1_000_000_000)

                sign_payload = payload.get("sign")
                if isinstance(sign_payload, dict):
                    sign[uid] = sign_payload

                items_payload = payload.get("items")
                if isinstance(items_payload, dict):
                    items[uid] = items_payload

                if payload.get("pending_adoption_enabled"):
                    pending_payload = payload.get("pending_adoption")
                    if isinstance(pending_payload, dict):
                        pending[uid] = pending_payload
                else:
                    pending.pop(uid, None)

                if bool(payload.get("catgirl_enabled")):
                    cat_payload = payload.get("catgirl")
                    if not isinstance(cat_payload, dict):
                        cat_payload = {}
                    cat_payload = dict(cat_payload)
                    now = int(time.time())
                    cat_payload["user"] = uid
                    cat_payload.setdefault("name", "猫娘")
                    cat_payload.setdefault("created_at", now)
                    cat_payload.setdefault("last_decay", now)
                    if not payload.get("pending_work_enabled"):
                        cat_payload.pop("pending_work", None)
                    cat_payload, _ = normalize_catgirl(cat_payload, uid)
                    cats[uid] = cat_payload
                else:
                    cats.pop(uid, None)

                return self._user_detail(root, uid)

            detail = self.plugin.store.update(op)
            return self._ok({"user": detail, "summary": self._summary(), "message": "用户数据已保存。"})
        except Exception as exc:
            logger.warning(f"[猫娘养成] 拓展页用户数据保存失败: {exc}")
            return self._error(str(exc))

    async def delete_user(self) -> Dict[str, Any]:
        try:
            payload = await request.get_json(silent=True) or {}
            uid = self._clean_uid(payload.get("uid", ""))
            if not uid:
                return self._error("缺少用户 ID")

            def op(root):
                for key in ("wallet", "sign", "catgirls", "items", "pending_adoptions"):
                    section = root.setdefault(key, {})
                    if isinstance(section, dict):
                        section.pop(uid, None)
                return True

            self.plugin.store.update(op)
            return self._ok({"summary": self._summary(), "message": f"用户 {uid} 的养猫插件数据已删除。"})
        except Exception as exc:
            logger.warning(f"[猫娘养成] 拓展页用户数据删除失败: {exc}")
            return self._error(str(exc))

    def _summary(self) -> Dict[str, Any]:
        store = self.plugin.store
        wallet = store.get("wallet", default={}) or {}
        catgirls = store.get("catgirls", default={}) or {}
        pending = store.get("pending_adoptions", default={}) or {}
        config = self.plugin.runtime_config.snapshot()
        jobs = config.get("work", {}).get("jobs", [])
        foods = config.get("feed", {}).get("foods", [])
        effects = config.get("interactions", {}).get("effects", [])
        personalities = config.get("personalities", {}).get("effects", [])
        balances = [self._int(value, 0, 0, 1_000_000_000) for value in wallet.values()]
        return {
            "wallet_users": len(wallet),
            "wallet_total": sum(balances),
            "wallet_max": max(balances) if balances else 0,
            "catgirls": len([cat for cat in catgirls.values() if isinstance(cat, dict) and cat.get("name")]),
            "pending_adoptions": len([row for row in pending.values() if isinstance(row, dict)]),
            "jobs": len(jobs) if isinstance(jobs, list) else 0,
            "enabled_jobs": len([job for job in jobs if isinstance(job, dict) and job.get("enabled", True)]) if isinstance(jobs, list) else 0,
            "foods": len(foods) if isinstance(foods, list) else 0,
            "enabled_foods": len([food for food in foods if isinstance(food, dict) and food.get("enabled", True)]) if isinstance(foods, list) else 0,
            "interactions": len(effects) if isinstance(effects, list) else 0,
            "enabled_interactions": len([row for row in effects if isinstance(row, dict) and row.get("enabled", True)]) if isinstance(effects, list) else 0,
            "personalities": len(personalities) if isinstance(personalities, list) else 0,
            "enabled_personalities": len([row for row in personalities if isinstance(row, dict) and row.get("enabled", True)]) if isinstance(personalities, list) else 0,
        }

    def _options(self) -> Dict[str, Any]:
        return {
            "personalities": list(PERSONALITIES),
            "body_types": list(BODY_TYPES),
        }

    def _all_user_ids(self, root: Dict[str, Any]) -> Iterable[str]:
        users = set()
        for section_name in ("wallet", "sign", "catgirls", "items", "pending_adoptions"):
            section = root.get(section_name)
            if isinstance(section, dict):
                users.update(str(uid) for uid in section.keys() if self._clean_uid(uid))
        return sorted(users)

    def _user_row(self, root: Dict[str, Any], uid: str) -> Dict[str, Any]:
        wallet = root.get("wallet") if isinstance(root.get("wallet"), dict) else {}
        sign = root.get("sign") if isinstance(root.get("sign"), dict) else {}
        cats = root.get("catgirls") if isinstance(root.get("catgirls"), dict) else {}
        pending = root.get("pending_adoptions") if isinstance(root.get("pending_adoptions"), dict) else {}
        cat = cats.get(uid) if isinstance(cats.get(uid), dict) else None
        sign_row = sign.get(uid) if isinstance(sign.get(uid), dict) else {}
        pending_work = cat.get("pending_work") if isinstance(cat, dict) and isinstance(cat.get("pending_work"), dict) else None
        return {
            "uid": uid,
            "wallet": self._int(wallet.get(uid, 0), 0, 0, 1_000_000_000),
            "has_catgirl": bool(cat and cat.get("name")),
            "cat_name": str(cat.get("name", "")) if cat else "",
            "personality": str(cat.get("personality", "")) if cat else "",
            "stage": stage_name(cat.get("stage", 0)) if cat else "-",
            "status": status_tag(cat) if cat else "-",
            "satiety": self._number(cat.get("satiety", 0), 0) if cat else 0,
            "mood": self._number(cat.get("mood", 0), 0) if cat else 0,
            "health": self._number(cat.get("health", 0), 0) if cat else 0,
            "energy": self._number(cat.get("energy", 0), 0) if cat else 0,
            "sign_count": self._int(sign_row.get("count", 0), 0, 0, 1_000_000),
            "last_sign_date": str(sign_row.get("last_sign_date", "")),
            "has_pending_adoption": isinstance(pending.get(uid), dict),
            "pending_work": str(pending_work.get("job", "")) if pending_work else "",
        }

    def _user_detail(self, root: Dict[str, Any], uid: str) -> Dict[str, Any]:
        wallet = root.get("wallet") if isinstance(root.get("wallet"), dict) else {}
        sign = root.get("sign") if isinstance(root.get("sign"), dict) else {}
        cats = root.get("catgirls") if isinstance(root.get("catgirls"), dict) else {}
        items = root.get("items") if isinstance(root.get("items"), dict) else {}
        pending = root.get("pending_adoptions") if isinstance(root.get("pending_adoptions"), dict) else {}
        cat = cats.get(uid) if isinstance(cats.get(uid), dict) else None
        cat_summary = self._cat_summary(cat) if cat else {}
        return {
            "uid": uid,
            "wallet": self._int(wallet.get(uid, 0), 0, 0, 1_000_000_000),
            "sign": sign.get(uid) if isinstance(sign.get(uid), dict) else {},
            "catgirl_enabled": bool(cat),
            "catgirl": cat or None,
            "cat_summary": cat_summary,
            "pending_work_enabled": bool(cat and isinstance(cat.get("pending_work"), dict)),
            "items": items.get(uid) if isinstance(items.get(uid), dict) else {},
            "pending_adoption": pending.get(uid) if isinstance(pending.get(uid), dict) else None,
        }

    def _cat_summary(self, cat: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "stage_name": stage_name(cat.get("stage", 0)),
            "status": status_tag(cat),
            "bond_score": bond_score(cat),
            "companion_days": companion_days(cat),
        }

    def _clean_uid(self, value: Any) -> str:
        return str(value or "").strip()[:128]

    def _int(self, value: Any, default: int = 0, low: int | None = None, high: int | None = None) -> int:
        try:
            result = int(float(value))
        except Exception:
            result = default
        if low is not None:
            result = max(low, result)
        if high is not None:
            result = min(high, result)
        return result

    def _number(self, value: Any, default: float = 0) -> float:
        try:
            return round(float(value), 2)
        except Exception:
            return default

    def _ok(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": True, "data": data}

    def _error(self, message: str) -> Dict[str, Any]:
        return {"success": False, "error": message or "请求失败"}
