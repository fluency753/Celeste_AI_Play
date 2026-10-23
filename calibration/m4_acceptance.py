"""M4 验收：第二套按键、预期轨迹对照与偏离停止、自动重算重试。

前提：游戏开着、桥接 0.6.0 已加载，test 存档停在关卡里 Madeline 可操作、站在平地上（头顶与左右留出空间），
智能体容器已创建（用 --agent 指定的标签，默认 dev-haiku）。

用法（仓库根目录）：
    python calibration/m4_acceptance.py --agent dev-haiku

只做竖直方向的动作（原地跳、原地冲刺），所以每一步结束时 Madeline 回到同一个位置，可以反复对照。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config import RunConfig  # noqa: E402
from orchestrator.container import ensure_container  # noqa: E402
from orchestrator.game_session import ActError, GameSession  # noqa: E402
from orchestrator.metrics import Metrics  # noqa: E402
from orchestrator.observation import describe_attempts, describe_step  # noqa: E402

FAILED = 0


def check(ok: bool, name: str, detail: str = "") -> None:
    global FAILED
    if not ok:
        FAILED += 1
    print(f"  [{'通过' if ok else '不通过'}] {name}{('：' + detail) if detail else ''}", flush=True)


def traj_xy(result: dict) -> list[list[float]]:
    return [[row[1], row[2]] for row in result["trajectory"]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", default="dev-haiku")
    ap.add_argument("--save", default="test")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = RunConfig(agent=args.agent, save=args.save, model="-")
    run_dir = Path(tempfile.mkdtemp(prefix="m4_", dir=REPO_ROOT / "runs"))
    metrics = Metrics(run_dir / "metrics.json", args.agent, "-", "-", 3600)
    session = GameSession(cfg, run_dir, metrics)
    session.connect()
    session.container = ensure_container(cfg, lambda m: print(m, flush=True))
    plans = cfg.workspace_dir / "m4_test"
    plans.mkdir(parents=True, exist_ok=True)

    def act(inputs=None, plan=None, stop_on=None, thought="M4 验收"):
        plan_file = None
        if plan is not None:
            (plans / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            plan_file = "m4_test/plan.json"
        return session.act(inputs, thought, stop_on, plan_file)

    try:
        start = session.observe()[0]["player"]
        print(f"起点：{start['pos']}，着地 {start['onGround']}，冲刺 {start['dashes']}", flush=True)

        print("1. 第二套按键", flush=True)
        # 按住 J 起跳并一直按住到落地：落地后不会再跳
        out = act(["1,J", "79,J"])
        ys = [row[2] for row in out.result["trajectory"]]
        check(min(ys) < ys[0] - 10 and out.obs["player"]["onGround"], "J 起跳并落地", f"最高 {ys[0] - min(ys):.1f} 像素")
        # 对照：落地后一直按住 J 不会再跳（最后 20 帧高度不变）
        check(max(ys[-20:]) - min(ys[-20:]) < 0.01, "对照：落地后一直按住 J 不会再跳",
              f"最后 20 帧 y 变化 {max(ys[-20:]) - min(ys[-20:]):.3f}")
        # 仍按住 J，再按下 K：应当立即起跳（同一动作的第二个键）
        out = act(["1,J,K", "40,J,K", "40"])
        ys = [row[2] for row in out.result["trajectory"]]
        check(ys[3] < ys[0] - 1, "按住 J 时按下 K 也会起跳", f"前 4 帧 y：{[round(y, 2) for y in ys[:4]]}")
        # Y 冲刺（向上）
        out = act(["1,U,Y", "3,U", "40"])
        states = {row[5] for row in out.result["trajectory"]}
        check("Dash" in states, "Y 是冲刺键", f"经过的状态 {sorted(states)}")
        act(["60"])
        # V 蹲冲：按下的那一帧冲刺开始并进入下蹲（碰撞箱高 6）；普通冲刺 X 向上时不会下蹲
        session.client.call("step", inputs=["1,U,V"], stop_on=[], image=False, entities=False, tiles=False)
        hb = session.client.call("observe", image=False, entities=False, tiles=False)["obs"]["player"]["hitbox"]
        session.client.call("step", inputs=["3,U", "60"], stop_on=[], image=False, entities=False, tiles=False)
        check(hb[3] == 6, "V 是蹲冲键", f"按下后第 1 帧碰撞箱 {hb}")
        act(["30"])

        print("2. 预期轨迹对照", flush=True)
        jump = ["1,J", "20,J", "50"]
        recorded = act(jump)
        expect = traj_xy(recorded.result)
        out = act(plan={"inputs": jump, "expect": expect, "tolerance": 0.01})
        check(out.result["stop"] == "done" and not out.result.get("deviation"), "与预期一致时整段执行完",
              f"停止原因 {out.result['stop']}，对照 {out.result.get('expectFrames')} 帧")
        bad = [list(r) for r in expect]
        bad[14][1] += 2
        out = act(plan={"inputs": jump, "expect": bad, "tolerance": 0.5})
        dev = out.result.get("deviation") or {}
        check(out.result["stop"] == "deviate" and dev.get("index") == 15 and out.result["consumed"] == 15,
              "第 15 帧位置偏差 2 像素时在第 15 帧停下", describe_step(out.result).splitlines()[2] if dev else "没有偏差")
        act(["60"])
        wrong_state = [list(r) for r in expect]
        wrong_state[4] = wrong_state[4] + ["Dash"]
        out = act(plan={"inputs": jump, "expect": wrong_state, "tolerance": 0.5})
        dev = out.result.get("deviation") or {}
        check(out.result["stop"] == "deviate" and dev.get("index") == 5, "第 5 帧状态不符时停下",
              f"实际状态 {(dev.get('actual') or [None, None, None])[2]}")
        act(["60"])
        try:
            act(plan={"inputs": ["5"], "expect": [[0, 0]] * 6})
            check(False, "预期轨迹比按键长时拒绝执行")
        except ActError as err:
            check("比按键" in str(err), "预期轨迹比按键长时拒绝执行", str(err))

        print("3. 自动重算重试（零网络沙箱）", flush=True)
        # 重算程序：读 retry.json 与最新观测，写一份"原地等落地"的计划（不带预期，所以必定执行完）
        (plans / "replan.py").write_text(
            "import json, os, urllib.request\n"
            "retry = json.load(open('/workspace/game/retry.json'))\n"
            "obs = json.load(open('/workspace/game/latest_obs.json'))['obs']\n"
            "try:\n"
            "    urllib.request.urlopen('https://api.anthropic.com', timeout=3); net = 'reachable'\n"
            "except Exception as exc:\n"
            "    net = 'blocked'\n"
            "plan = {'inputs': ['60'], 'note': [retry['reason'], retry['attempt'], net, obs['player']['pos']]}\n"
            "json.dump(plan, open(os.environ['CELESTE_PLAN_FILE'], 'w'))\n"
            "print('replanned', retry['reason'], retry['attempt'], net)\n", encoding="utf-8")
        out = act(plan={"inputs": jump, "expect": bad, "tolerance": 0.5,
                        "retry": {"command": "python3 m4_test/replan.py", "max": 2, "timeout_s": 20}})
        kinds = [(a["kind"], a.get("stop") or a.get("reason")) for a in out.attempts]
        print(describe_attempts(out.attempts), flush=True)
        written = json.loads((plans / "plan.json").read_text(encoding="utf-8"))
        check(kinds == [("execute", "deviate"), ("replan", "deviate"), ("execute", "done")],
              "偏离后运行重算程序并执行新计划", str(kinds))
        check(written.get("note", [None, None, None])[2] == "blocked", "重算程序连不上网络", str(written.get("note")))
        check(metrics.state.retries == 1 and metrics.state.decisions >= 1, "统计里记了 1 次自动重试",
              f"retries={metrics.state.retries}")
        # 重算程序出错：停止重试并回报
        (plans / "replan_fail.py").write_text("import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n", encoding="utf-8")
        out = act(plan={"inputs": jump, "expect": bad, "tolerance": 0.5,
                        "retry": {"command": "python3 m4_test/replan_fail.py", "max": 3, "timeout_s": 10}})
        last = out.attempts[-1]
        check(last["kind"] == "replan" and last["returncode"] == 3 and "boom" in last.get("error", ""),
              "重算程序出错时停止重试并带回错误输出", describe_attempts(out.attempts).splitlines()[-1].strip())
        act(["60"])
        # 超时
        (plans / "replan_slow.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        out = act(plan={"inputs": jump, "expect": bad, "tolerance": 0.5,
                        "retry": {"command": "python3 m4_test/replan_slow.py", "max": 1, "timeout_s": 2}})
        last = out.attempts[-1]
        check(last["kind"] == "replan" and last["timed_out"] and last["seconds"] < 10, "重算程序超时会被结束",
              f"{last['seconds']:.1f} 秒")
        act(["60"])
    finally:
        session.close()
    print(f"\n结果：{'全部通过' if FAILED == 0 else f'{FAILED} 项不通过'}（日志 {run_dir}）", flush=True)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
