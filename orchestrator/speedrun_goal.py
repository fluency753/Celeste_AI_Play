"""Convert a configured real-number threshold to the native chapter clock."""
from decimal import Decimal, ROUND_CEILING, InvalidOperation


TICKS_PER_SECOND = 10_000_000


def parse_target_seconds(value) -> int:
    try:
        seconds = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('速通目标须为正的有限秒数') from exc
    if not seconds.is_finite() or not 0 < seconds <= Decimal(2**63 - 1) / TICKS_PER_SECOND:
        raise ValueError('速通目标须为原生章节计时范围内的正秒数')
    # For integer elapsed ticks, elapsed < ceil(target) is exactly elapsed < target.
    return int((seconds * TICKS_PER_SECOND).to_integral_value(rounding=ROUND_CEILING))


def target_seconds_text(ticks: int) -> str:
    seconds, fraction = divmod(ticks, TICKS_PER_SECOND)
    return f'{seconds}.{fraction:07d}'.rstrip('0').rstrip('.')


def require_timing_bridge(target_ticks, hello) -> None:
    if target_ticks is not None and hello.get('speedrun_practice_room') is not True:
        raise ValueError('整章速通时间目标需要 Bridge 1.13.0 或更新版本提供原生计时资格。')


def require_capture_bridge(cfg, hello) -> None:
    require_timing_bridge(getattr(cfg, 'speedrun_target_ticks', None), hello)
    if getattr(cfg, 'speedrun_record_on_demand', False) and hello.get('speedrun_recorded_attempt') is not True:
        raise ValueError('录制整章速通需要 Bridge 1.14.0 或更新版本；未发送游戏输入。')
