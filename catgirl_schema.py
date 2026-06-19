import math
import random
import time
from typing import Dict, Tuple


CATGIRL_SCHEMA_VERSION = 2
WEIGHT_UNIT = "斤"
DEFAULT_WEIGHT_JIN = 60.0
DEFAULT_IDEAL_WEIGHT_JIN = 60.0
WEIGHT_JIN_MIN = 40.0
WEIGHT_JIN_MAX = 90.0
LEGACY_DEFAULT_JIN = 5.0
LEGACY_WEIGHT_EPS = 0.11

PERSONALITIES = ("害羞", "活泼", "傲娇", "温柔", "贪吃", "慵懒", "认真", "黏人")
BODY_TYPES = ("娇小", "匀称", "轻盈", "丰满", "高挑")
IDEAL_WEIGHT_BY_BODY_JIN = {
    "娇小": 46.0,
    "轻盈": 52.0,
    "匀称": 62.0,
    "高挑": 72.0,
    "丰满": 80.0,
}
WEIGHT_RANGE_BY_BODY_JIN = {
    "娇小": (40.0, 52.0),
    "轻盈": (45.0, 58.0),
    "匀称": (55.0, 70.0),
    "高挑": (65.0, 82.0),
    "丰满": (70.0, 90.0),
}

STAGES: Tuple[Tuple[str, int, int, str], ...] = (
    ("初遇", 0, 0, "她还不太敢看你的眼睛，只是悄悄拉住你的衣角。"),
    ("熟悉", 100, 30, "她已经记住了你的声音，听见你回来时会轻轻抬头。"),
    ("亲近", 300, 100, "她会主动蹭到你身边，像是在确认你有没有好好休息。"),
    ("信赖", 700, 250, "她把最柔软的一面交给了你，开始完全信赖你的陪伴。"),
    ("羁绊", 1500, 600, "你们之间的默契已经不需要太多语言。"),
    ("灵契", 3000, 1200, "她的心意与你相连，像星光一样温柔而坚定。"),
    ("永伴", 6000, 2500, "无论经过多少个清晨与夜晚，她都会陪在你身边。"),
)

INTIMACY_LEVEL_THRESHOLDS: Tuple[int, ...] = (
    0,
    30,
    100,
    250,
    600,
    1200,
    2500,
    4300,
    6800,
    10300,
    15100,
    21600,
    30300,
    41900,
    57300,
    77700,
    104600,
    140000,
    186500,
    247500,
)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def clamp_int(value, low: int = 0, high: int = 100) -> int:
    try:
        value = int(value)
    except Exception:
        value = low
    return max(low, min(high, value))


def random_body_profile() -> Tuple[str, float, float]:
    body_type = random.choice(BODY_TYPES)
    low, high = WEIGHT_RANGE_BY_BODY_JIN[body_type]
    weight = round(random.uniform(low, high), 2)
    ideal = round(clamp(IDEAL_WEIGHT_BY_BODY_JIN.get(body_type, DEFAULT_IDEAL_WEIGHT_JIN) + random.uniform(-2, 2), WEIGHT_JIN_MIN, WEIGHT_JIN_MAX), 2)
    return body_type, weight, ideal


def is_legacy_catgirl(cat: Dict) -> bool:
    if not isinstance(cat, dict):
        return False
    if int(cat.get("schema_version", 1) or 1) < CATGIRL_SCHEMA_VERSION:
        return True
    return cat.get("weight_unit") != WEIGHT_UNIT


def normalize_weight_to_jin(weight, weight_unit: str = "", legacy: bool = False) -> float:
    try:
        old = float(weight)
    except Exception:
        return DEFAULT_WEIGHT_JIN

    unit = str(weight_unit or "").lower()
    if unit == "kg":
        return clamp(old * 2, WEIGHT_JIN_MIN, WEIGHT_JIN_MAX)
    if unit in ("斤", "jin"):
        return clamp(old, WEIGHT_JIN_MIN, WEIGHT_JIN_MAX)

    if legacy and abs(old - LEGACY_DEFAULT_JIN) < LEGACY_WEIGHT_EPS:
        return DEFAULT_WEIGHT_JIN
    if legacy and 0 < old < 30:
        return clamp(DEFAULT_WEIGHT_JIN + (old - LEGACY_DEFAULT_JIN), WEIGHT_JIN_MIN, WEIGHT_JIN_MAX)
    return clamp(old, WEIGHT_JIN_MIN, WEIGHT_JIN_MAX)


def calc_stage(growth, intimacy) -> int:
    growth = int(float(growth or 0))
    intimacy = int(float(intimacy or 0))
    stage = 0
    for idx, (_, need_growth, need_intimacy, _) in enumerate(STAGES):
        if growth >= need_growth and intimacy >= need_intimacy:
            stage = idx
    return stage


def stage_name(stage) -> str:
    try:
        idx = int(stage)
    except Exception:
        idx = 0
    idx = max(0, min(len(STAGES) - 1, idx))
    return STAGES[idx][0]


def stage_description(stage) -> str:
    try:
        idx = int(stage)
    except Exception:
        idx = 0
    idx = max(0, min(len(STAGES) - 1, idx))
    return STAGES[idx][3]


def next_stage_need(stage):
    try:
        idx = int(stage) + 1
    except Exception:
        idx = 1
    if idx >= len(STAGES):
        return None
    name, growth, intimacy, _ = STAGES[idx]
    return name, growth, intimacy


def _intimacy_thresholds_for(points: int):
    thresholds = list(INTIMACY_LEVEL_THRESHOLDS)
    if points < thresholds[-1]:
        return thresholds

    gap = thresholds[-1] - thresholds[-2]
    while points >= thresholds[-1]:
        gap = max(gap + 1, int(gap * 1.32))
        thresholds.append(thresholds[-1] + gap)
    return thresholds


def intimacy_level(intimacy) -> int:
    try:
        points = max(0, int(float(intimacy or 0)))
    except Exception:
        points = 0

    level = 1
    for idx, threshold in enumerate(_intimacy_thresholds_for(points)):
        if points >= threshold:
            level = idx + 1
        else:
            break
    return level


def format_intimacy_level(intimacy) -> str:
    return f"Lv.{intimacy_level(intimacy)}"


def stage_growth_progress(growth, stage) -> float:
    try:
        points = max(0, int(float(growth or 0)))
    except Exception:
        points = 0
    try:
        idx = int(stage)
    except Exception:
        idx = 0
    idx = max(0, min(len(STAGES) - 1, idx))
    if idx >= len(STAGES) - 1:
        return 100.0

    current_need = STAGES[idx][1]
    next_need = STAGES[idx + 1][1]
    span = max(1, next_need - current_need)
    return clamp((points - current_need) / span * 100, 0, 100)


def format_stage_growth_progress(growth, stage) -> str:
    return f"{int(round(stage_growth_progress(growth, stage)))}%"


def companion_days(cat: Dict) -> int:
    created_at = int(cat.get("created_at", int(time.time())) or int(time.time()))
    return max(1, int((time.time() - created_at) // 86400) + 1)


def bond_score(cat: Dict) -> int:
    intimacy = int(cat.get("intimacy", 0) or 0)
    growth = int(cat.get("growth", 0) or 0)
    stage = int(cat.get("stage", 0) or 0)
    mood = clamp_int(cat.get("mood", 0))
    health = clamp_int(cat.get("health", 0))
    days = companion_days(cat)

    # 照顾活跃度：总喂食+互动+打工次数
    stats = cat.get("care_stats", {}) or {}
    total_acts = (
        int(stats.get("total_feeds", 0))
        + int(stats.get("total_interacts", 0))
        + int(stats.get("total_works", 0))
    )

    # 体重健康度：偏离理想体重越少越好
    weight = float(cat.get("weight", 60))
    ideal = float(cat.get("ideal_weight", 60))
    weight_deviation = abs(weight - ideal)
    weight_health = max(0, int(50 - weight_deviation * 2))

    # —— 1. 核心基础分 ——
    # 亲密度（intimacy 更难涨，权重更高）和成长值都用开平方减缓后期膨胀
    intimacy_term = int(math.isqrt(intimacy) * 100)
    growth_term = int(math.isqrt(growth) * 25)
    # 阶段作为系数放大基础分（stage 0→1.00, 3→1.24, 6→1.48）
    base = int((intimacy_term + growth_term) * (1.0 + stage * 0.08))

    # —— 2. 实时状态加成 ——
    state_bonus = mood * 5 + health * 5

    # —— 3. 陪伴加成（每天+3） ——
    days_bonus = days * 3

    # —— 4. 照顾活跃加成（开平方降噪，上限 2000） ——
    acts_bonus = min(int(math.isqrt(total_acts) * 20), 2000)

    # —— 5. 体重管理加成 ——
    weight_bonus = weight_health

    # —— 6. 身心协同加成（心情≥60 且 健康≥60 时，二者兼顾的额外奖励） ——
    synergy = int((mood * health) / 30) if mood >= 60 and health >= 60 else 0

    return max(0, base + state_bonus + days_bonus + acts_bonus + weight_bonus + synergy)


def status_tag(cat: Dict) -> str:
    satiety = float(cat.get("satiety", 0) or 0)
    mood = int(cat.get("mood", 0) or 0)
    health = int(cat.get("health", 0) or 0)
    energy = int(cat.get("energy", 0) or 0)
    stage = int(cat.get("stage", 0) or 0)
    intimacy = int(cat.get("intimacy", 0) or 0)
    if health < 40:
        return "虚弱"
    if satiety < 30:
        return "饿肚子"
    if energy < 25:
        return "疲惫"
    if mood < 30:
        return "失落"
    if stage >= 5:
        return "心意相通"
    if intimacy >= 600:
        return "依赖你"
    if satiety >= 80 and mood >= 80:
        return "元气满满"
    if mood >= 90:
        return "开心"
    return "安心"


def normalize_catgirl(cat: Dict, uid: str = "") -> Tuple[Dict, bool]:
    if not isinstance(cat, dict):
        return cat, False

    before = dict(cat)
    cat = dict(cat)
    legacy = is_legacy_catgirl(cat)
    original_weight_unit = cat.get("weight_unit", "")

    body_type = cat.get("body_type")
    if body_type not in BODY_TYPES:
        body_type, generated_weight, generated_ideal = random_body_profile()
    else:
        generated_weight = None
        generated_ideal = IDEAL_WEIGHT_BY_BODY_JIN.get(body_type, DEFAULT_IDEAL_WEIGHT_JIN)

    if legacy:
        cat["weight"] = round(normalize_weight_to_jin(cat.get("weight", LEGACY_DEFAULT_JIN), original_weight_unit, True), 2)
    elif "weight" not in cat:
        cat["weight"] = generated_weight if generated_weight is not None else DEFAULT_WEIGHT_JIN
    else:
        cat["weight"] = round(normalize_weight_to_jin(cat.get("weight", DEFAULT_WEIGHT_JIN), cat.get("weight_unit", WEIGHT_UNIT)), 2)

    cat["schema_version"] = CATGIRL_SCHEMA_VERSION
    cat["weight_unit"] = WEIGHT_UNIT
    cat.setdefault("user", uid or cat.get("user", ""))
    cat["body_type"] = body_type
    cat.setdefault("ideal_weight", generated_ideal)
    cat["ideal_weight"] = round(normalize_weight_to_jin(cat.get("ideal_weight", generated_ideal), original_weight_unit or WEIGHT_UNIT), 2)
    cat.setdefault("personality", random.choice(PERSONALITIES))
    if cat.get("personality") not in PERSONALITIES:
        cat["personality"] = random.choice(PERSONALITIES)

    cat["satiety"] = round(clamp(float(cat.get("satiety", 80)), 0, 100), 4)
    cat["mood"] = round(clamp(float(cat.get("mood", 85)), 0, 100), 4)
    cat["health"] = round(clamp(float(cat.get("health", 90)), 0, 100), 4)
    cat["energy"] = round(clamp(float(cat.get("energy", 80)), 0, 100), 4)

    if "growth" not in cat:
        cat["growth"] = max(0, int((companion_days(cat) - 1) * 2))
    else:
        cat["growth"] = max(0, int(float(cat.get("growth", 0) or 0)))

    if "intimacy" not in cat:
        interactions = cat.get("interactions", {}) or {}
        total_interactions = 0
        if isinstance(interactions, dict):
            for count in interactions.values():
                try:
                    total_interactions += int(count)
                except Exception:
                    pass
        cat["intimacy"] = max(0, int(cat.get("mood", 80)) // 2 + total_interactions * 3)
    else:
        cat["intimacy"] = max(0, int(float(cat.get("intimacy", 0) or 0)))

    cat["stage"] = calc_stage(cat.get("growth", 0), cat.get("intimacy", 0))
    cat.setdefault("care_stats", {})
    if not isinstance(cat.get("care_stats"), dict):
        cat["care_stats"] = {}
    unlocks = cat.get("unlocks")
    if isinstance(unlocks, dict):
        unlocks.setdefault("work_jobs", [])
    elif isinstance(unlocks, list):
        unlocks = {"work_jobs": [], "legacy": [str(x) for x in unlocks]}
    else:
        unlocks = {"work_jobs": []}
    if not isinstance(unlocks.get("work_jobs"), list):
        unlocks["work_jobs"] = []
    unlocks["work_jobs"] = [str(x) for x in unlocks.get("work_jobs", []) if str(x).strip()]
    cat["unlocks"] = unlocks
    for key in ("buffs", "care_cooldowns", "gift_stats"):
        if not isinstance(cat.get(key), dict):
            cat[key] = {}

    return cat, cat != before
