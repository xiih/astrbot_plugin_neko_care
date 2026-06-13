import random
from datetime import datetime
from typing import Callable, List, Dict

from .storage import JsonStore


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class EconomyService:
    def __init__(
        self,
        store: JsonStore,
        coin_name: str = "宝石",
        work_min: int = 50,
        work_max: int = 120,
        runtime_config_provider: Callable[[], Dict] | None = None,
    ):
        self.store = store
        self.coin_name = coin_name
        self.work_min = work_min
        self.work_max = work_max
        self.runtime_config_provider = runtime_config_provider

    def _runtime(self) -> Dict:
        if callable(self.runtime_config_provider):
            try:
                data = self.runtime_config_provider()
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _economy_rules(self) -> Dict:
        rules = self._runtime().get("economy", {})
        return rules if isinstance(rules, dict) else {}

    def _coin_name(self) -> str:
        return str(self._economy_rules().get("coin_name") or self.coin_name or "宝石")

    def get_balance(self, uid: str) -> int:
        return int(self.store.get("wallet", str(uid), default=0))

    def set_balance(self, uid: str, amount: int) -> int:
        amount = max(0, int(amount))
        self.store.set("wallet", str(uid), value=amount)
        return amount

    def add_balance(self, uid: str, delta: int) -> int:
        return self.set_balance(uid, self.get_balance(uid) + int(delta))

    def transfer(self, from_uid: str, to_uid: str, amount: int):
        amount = int(amount)
        if amount <= 0:
            return False, "转账金额要大于 0 喔～"
        if from_uid == to_uid:
            return False, "不能给自己转账啦。"

        def op(data):
            wallet = data.setdefault("wallet", {})
            from_balance = int(wallet.get(from_uid, 0))
            if from_balance < amount:
                return False, f"余额不够喔，转不了 {amount} {self._coin_name()}。"
            wallet[from_uid] = from_balance - amount
            wallet[to_uid] = int(wallet.get(to_uid, 0)) + amount
            return True, f"转账成功～送出了 {amount} {self._coin_name()}。"

        return self.store.update(op)

    def daily_work(self, uid: str):
        today = today_str()
        rules = self._economy_rules()
        work_min = max(0, int(rules.get("daily_work_min_reward", self.work_min)))
        work_max = max(work_min, int(rules.get("daily_work_max_reward", self.work_max)))
        reward = random.randint(work_min, work_max)
        events = rules.get("daily_work_events")
        if not isinstance(events, list) or not events:
            events = [
                "你在猫咖帮忙端了一天甜点。",
                "你帮老板整理仓库，累得耳朵都耷拉下来了。",
                "你接了一个临时外包，顺利完成。",
                "你在便利店值班，遇到了一群买关东煮的猫娘。",
                "你帮别人修好了坏掉的自动贩卖机。",
            ]
        event_text = random.choice([str(x) for x in events if str(x).strip()] or ["你认真完成了今天的打工。"])
        coin_name = self._coin_name()

        def op(root):
            wallet = root.setdefault("wallet", {})
            sign = root.setdefault("sign", {})
            user = sign.setdefault(uid, {})
            if user.get("last_work_date") == today:
                return False, "今天已经打过工啦，休息一下吧喵～"
            wallet[uid] = int(wallet.get(uid, 0)) + reward
            user["last_work_date"] = today
            return True, f"{event_text}\n获得 {reward} {coin_name}！\n当前余额：{wallet[uid]} {coin_name}"

        return self.store.update(op)

    def wallet_rank(self, top_n: int = 10) -> List[Dict]:
        wallet = self.store.get("wallet", default={}) or {}
        rows = [{"uid": uid, "balance": int(balance)} for uid, balance in wallet.items()]
        rows.sort(key=lambda x: x["balance"], reverse=True)
        return rows[:top_n]
