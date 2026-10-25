"""可调参数：主控里原本写死的数值（规划回放的倍速、解说计时、单次 act 的帧数上限、程序时长等）。

命令行用 `--tune 名称=值` 修改（可以写多个），控制台的"可调参数"一栏也是生成这个参数。默认值就是一直以来的数值，
不改时行为与提示词都与之前完全相同；改了之后，提示词与工具说明里提到的数字跟着变（见 prompting.fill）。
续跑沿用原运行改过的参数（记在 run_state.json 的 tuning 里），续跑时再写 --tune 可以覆盖。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

HOST_CPUS = os.cpu_count() or 1     # 本机的 CPU 核数（控制台与主控在同一台电脑上）


@dataclass(frozen=True)
class Tunable:
    key: str            # 同时是 RunConfig 的属性名
    label: str
    group: str
    kind: type          # int 或 float
    default: float | int | None
    low: float
    high: float
    unit: str = ""
    note: str = ""
    modes: tuple[str, ...] = ("speedrun", "explore")   # 哪些游玩模式用到它（控制台据此显示）
    optional: bool = False                               # 允许空值（不限）


GROUPS = ("执行与解说", "规划回放", "速通：程序与规划", "探索：规划程序", "探索：限额预测")

TUNABLES: tuple[Tunable, ...] = (
    Tunable("max_frames_per_act", "单次 act 最多推进的帧数", "执行与解说", int, 600, 60, 3600, "帧",
            "提示词与 act 的说明里的上限跟着变（探索模式用\"探索：规划程序\"里的那一项）", ("speedrun",)),
    Tunable("thought_max_chars", "思考语句最长字数", "执行与解说", int, 120, 20, 400, "字",
            "act 的 thought 与 narrate 的长度上限"),
    Tunable("thought_max_hanzi", "思考语句最多汉字数", "执行与解说", int, 30, 5, 400, "个",
            "只对 Claude Code 执行器：act 的 thought 与 narrate 里的汉字数（英文术语、数字与标点不计），超过就退回让智能体压缩；"
            "提示词与工具说明里的数字跟着变"),
    Tunable("thought_lead_s", "动作前等 HUD 换上新思考", "执行与解说", float, 0.4, 0, 3, "秒",
            "游戏冻结着等，录像里不占时间"),
    Tunable("remind_s", "解说提醒间隔", "执行与解说", float, 30, 5, 600, "秒",
            "思考累计这么久没有 narrate，就在下一次调用工具时提醒（不开直播）"),
    Tunable("remind_live_s", "解说提醒间隔（直播）", "执行与解说", float, 10, 3, 600, "秒", "直播模式用这个"),
    Tunable("playback_speed", "回放倍速", "规划回放", float, 3, 0.5, 20, "倍", "每个画面帧推进几帧推演"),
    Tunable("playback_recolor", "变色传导倍速", "规划回放", float, 8, 1, 50, "倍",
            "线走到头后死亡变红、最快变金，从终点往起点传导"),
    Tunable("playback_round_s", "每轮最长时长", "规划回放", float, 1.5, 0.1, 60, "秒",
            "以一屏大的房间为准，按房间面积等比放大；超过时整轮按比例加速"),
    Tunable("playback_ratio", "每多少条推演画一条", "规划回放", int, 100, 1, 100000, "条"),
    Tunable("playback_min_tracks", "每轮至少画几条", "规划回放", int, 40, 1, 2000, "条"),
    Tunable("playback_stagger_s", "一轮里各条线陆续出场的时长", "规划回放", float, 0.8, 0, 10, "秒"),
    Tunable("bash_default_s", "Bash 默认时长上限", "速通：程序与规划", int, 300, 10, 3600, "秒",
            "Claude Code 的 Bash 不写 timeout 时的上限", ("speedrun",)),
    Tunable("bash_max_s", "Bash 最长时长", "速通：程序与规划", int, 600, 10, 3600, "秒",
            "Bash 写了 timeout 时最多能到多久", ("speedrun",)),
    Tunable("retry_max", "自动重试最多几次", "速通：程序与规划", int, 5, 1, 50, "次",
            "计划文件的 retry.max 不能超过它", ("speedrun",)),
    Tunable("retry_timeout_max_s", "自动重试单次最长", "速通：程序与规划", float, 60, 1, 600, "秒",
            "计划文件的 retry.timeout_s 不能超过它", ("speedrun",)),
    Tunable("death_bonus", "死亡补的规划轮次", "速通：程序与规划", int, 1, 0, 20, "轮",
            "只在限制规划轮次时用", ("speedrun",)),
    Tunable("explore_program_s", "单次规划程序最长", "探索：规划程序", int, 15, 1, 600, "秒",
            "超时就被结束；提示词里的上限跟着变。空着 = 不限时（仍受整次运行的现实时间上限约束）", ("explore",), optional=True),
    Tunable("explore_program_runs", "两次 act 之间最多运行几次", "探索：规划程序", int, None, 1, 100, "次",
            "空着 = 不限", ("explore",), optional=True),
    Tunable("agent_cpus", "自写程序最多用几个 CPU 核", "探索：规划程序", int, HOST_CPUS, 1, HOST_CPUS, "核",
            f"默认是本机的核数（{HOST_CPUS}）；探索模式的提示词写明最多可以用几个核，容器运行方式同时按它限制智能体容器能用的核",
            ("explore",)),
    Tunable("explore_act_frames", "单次 act 最多推进的帧数", "探索：规划程序", int, 300, 30, 3600, "帧",
            "探索模式用这一项；提示词与 act 的说明里的上限跟着变", ("explore",)),
    Tunable("explore_search_frames", "自写模拟器与搜索程序每次最多推演", "探索：规划程序", int, 300, 1, 36000, "帧",
            "写进提示词，由智能体遵守（主控检查不了它自己程序里的推演帧数）；空着 = 不限", ("explore",), optional=True),
    Tunable("assist_capacity", "体力上限", "探索：限额预测", int, 100, 1, 10000, "点", modes=("explore",)),
    Tunable("assist_initial", "初始体力", "探索：限额预测", int, 60, 0, 10000, "点", modes=("explore",)),
    Tunable("assist_regen_frames", "每多少个移动帧恢复 1 点", "探索：限额预测", int, 60, 1, 3600, "帧", modes=("explore",)),
    Tunable("assist_regen_per_min", "每分钟最多恢复", "探索：限额预测", int, 20, 0, 10000, "点", modes=("explore",)),
    Tunable("assist_room_bonus", "首次进入新房间奖励", "探索：限额预测", int, 15, 0, 10000, "点", modes=("explore",)),
    Tunable("assist_cost_base", "每次预测的基础花费", "探索：限额预测", int, 2, 0, 1000, "点", modes=("explore",)),
    Tunable("assist_cost_frames", "每多少帧预测加 1 点", "探索：限额预测", int, 60, 1, 600, "帧", modes=("explore",)),
    Tunable("assist_cost_extra", "每条额外候选加", "探索：限额预测", int, 2, 0, 1000, "点", modes=("explore",)),
    Tunable("assist_max_candidates", "每次最多几条候选", "探索：限额预测", int, 6, 1, 50, "条", modes=("explore",)),
    Tunable("assist_max_frames", "每条最多预测", "探索：限额预测", int, 360, 1, 3600, "帧", modes=("explore",)),
    Tunable("assist_max_total", "每次合计最多预测", "探索：限额预测", int, 600, 1, 36000, "帧", modes=("explore",)),
)

BY_KEY = {t.key: t for t in TUNABLES}
DEFAULTS = {t.key: t.default for t in TUNABLES}


def coerce(key: str, value) -> int | float | None:
    """校验并转换一个参数值；不合法时抛 ValueError（中文说明）。"""
    item = BY_KEY.get(key)
    if item is None:
        raise ValueError(f"没有名为 {key} 的可调参数")
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "none", "不限")):
        if item.optional:
            return None
        raise ValueError(f"{item.label}（{key}）不能为空")
    if isinstance(value, bool):
        raise ValueError(f"{item.label}（{key}）须为数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{item.label}（{key}）须为数字") from None
    if item.kind is int:
        if number != int(number):
            raise ValueError(f"{item.label}（{key}）须为整数")
        number = int(number)
    if not item.low <= number <= item.high:
        raise ValueError(f"{item.label}（{key}）须在 {item.low:g}～{item.high:g} 之间")
    return number


def parse_pairs(pairs: list[str] | None) -> dict:
    """命令行的 --tune 名称=值 列表 → {名称: 值}。"""
    out = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep:
            raise ValueError(f"--tune 须写成 名称=值：{pair}")
        out[key.strip()] = coerce(key.strip(), value.strip())
    return out


def check_relations(values: dict) -> None:
    """几项之间的约束。"""
    merged = {**DEFAULTS, **values}
    if merged["bash_default_s"] > merged["bash_max_s"]:
        raise ValueError("Bash 默认时长上限不能大于 Bash 最长时长")
    if merged["assist_initial"] > merged["assist_capacity"]:
        raise ValueError("初始体力不能大于体力上限")
    if merged["assist_max_frames"] > merged["assist_max_total"]:
        raise ValueError("每条最多预测的帧数不能大于每次合计最多预测的帧数")


def apply(cfg, values: dict) -> None:
    """把参数写进 RunConfig（没写的用默认值）。"""
    check_relations(values)
    for item in TUNABLES:
        setattr(cfg, item.key, values.get(item.key, item.default))
    cfg.tuning = {k: v for k, v in values.items() if v != BY_KEY[k].default}


def overrides(cfg) -> dict:
    """这次运行改过的参数（与默认值不同的），记进 run_state 供续跑沿用。"""
    return {t.key: getattr(cfg, t.key, t.default) for t in TUNABLES if getattr(cfg, t.key, t.default) != t.default}


def argv(values: dict) -> list[str]:
    """{名称: 值} → --tune 参数（只写与默认值不同的）。"""
    out = []
    for key, value in values.items():
        value = coerce(key, value)
        if value != BY_KEY[key].default:
            out += ["--tune", f"{key}={'' if value is None else format(value, 'g') if isinstance(value, float) else value}"]
    return out


def catalog() -> list[dict]:
    """给控制台的参数表。"""
    return [{"key": t.key, "label": t.label, "group": t.group, "kind": t.kind.__name__, "default": t.default,
             "low": t.low, "high": t.high, "unit": t.unit, "note": t.note, "modes": list(t.modes),
             "optional": t.optional} for t in TUNABLES]


def seconds_text(seconds: float) -> str:
    """提示词里的时长：整分钟写"N 分钟"，否则写"N 秒"。"""
    return f"{seconds / 60:g} 分钟" if seconds >= 60 and seconds % 60 == 0 else f"{seconds:g} 秒"
