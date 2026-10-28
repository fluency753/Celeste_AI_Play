"""CelesteAIBridge 的命令行测试客户端（只用标准库）。

示例：
    python tools/bridge_client.py hello
    python tools/bridge_client.py activate
    python tools/bridge_client.py observe --out shot.png
    python tools/bridge_client.py step "30,R" "1,R,J" "20,R" --stop death,room --out after.png
    python tools/bridge_client.py advance --until controllable --max 600
    python tools/bridge_client.py skip
    python tools/bridge_client.py deactivate
    python tools/bridge_client.py smoke --out-dir smoke
"""
from __future__ import annotations

import argparse
import base64
import json
import socket
import sys
import time
from pathlib import Path

DEFAULT_PORT = 47774
# 只读、重发无副作用的指令：连接在等回复时断开（例如被别的客户端挤掉），重连后可以安全地重发
READ_ONLY = frozenset({"hello", "observe", "areas"})


class BridgeClient:
    """桥接只接受一个连接：新客户端连上时旧连接会被关掉（游戏照样冻结、按键绑定不变）。所以请求发不出去时
    （请求还没到桥接）重连后重发一次；只读指令在等回复时断开也重连重发。step 等会推进游戏的指令发出后才断开时
    不重发，照常报错，免得同一段按键执行两次。"""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 120.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._connect()
        self.next_id = 1
        self.cancel_check = None
        self.step_options = None
        self.after_step = None
        self._cancel_sent = False
        self.reconnects = 0

    def _connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.file = self.sock.makefile("rwb")

    def reconnect(self) -> None:
        try:
            self.close()
        except OSError:
            pass
        self._connect()
        self.reconnects += 1

    def close(self) -> None:
        try:
            self.file.close()
        finally:
            self.sock.close()

    def _send(self, data: bytes, *, allow_reconnect: bool = True) -> None:
        try:
            self.file.write(data)
            self.file.flush()
        except TimeoutError:
            raise
        except OSError:
            if not allow_reconnect:
                raise
            self.reconnect()          # 请求没送到桥接：重连后重发一次
            self.file.write(data)
            self.file.flush()

    def call(self, cmd: str, on_event=None, *, allow_reconnect: bool = True, **args) -> dict:
        """发送一条指令并等待它的回复。执行中桥接推送的事件（{"event": …}，例如 step 的 progress）交给 on_event。"""
        if cmd == "step" and self.cancel_check is not None and self.cancel_check():
            raise InterruptedError("用户停止请求已生效，不再推进游戏")
        if cmd == 'step' and getattr(self, 'before_step', None) is not None:
            self.before_step()
        if cmd == 'step' and self.step_options is not None:
            # Session-owned constraints apply to every caller, including menu
            # helpers and retries, rather than only the public act tool.
            args.update(self.step_options())
        req_id = self.next_id
        self._cancel_sent = False
        if cmd == "step" and self.cancel_check is not None:
            args.setdefault("progress", 6)
        self.next_id += 1
        payload = {"id": req_id, "cmd": cmd, **args}
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._send(data, allow_reconnect=allow_reconnect)
        retried = False
        while True:
            try:
                line = self.file.readline()
            except TimeoutError:
                raise
            except OSError:
                line = b""
            if not line:
                if allow_reconnect and cmd in READ_ONLY and not retried:
                    retried = True
                    self.reconnect()
                    self._send(data)
                    continue
                raise ConnectionError("bridge closed the connection")
            reply = json.loads(line)
            if "event" in reply:
                if on_event is not None:
                    on_event(reply)
                if self.cancel_check is not None and self.cancel_check() and not self._cancel_sent:
                    self.cancel_pending()
                continue
            if reply.get("id") == req_id:
                if not reply.get("ok"):
                    raise RuntimeError(f"{cmd} failed: {reply.get('error')}")
                if isinstance(reply.get('obs'), dict) and ('imageScope' in reply or 'imageError' in reply):
                    reply['obs']['screenshot'] = {'scope': reply.get('imageScope'),
                                                 'format': reply.get('imageFormat'),
                                                 'error': reply.get('imageError')}
                after = getattr(self, "after_step", None)
                if cmd == "step" and after is not None:
                    after(args, reply)     # 例如记下最后真正喂给游戏的那一帧按键（思考僵直时保持它）
                return reply

    def cancel_pending(self) -> None:
        """仅从 on_event 回调使用；共用当前连接，id=0 的确认由读取循环忽略。"""
        self.file.write(b'{"id":0,"cmd":"cancel"}\n')
        self.file.flush()
        self._cancel_sent = True


def save_image(reply: dict, path: str | None) -> None:
    if not path:
        return
    data = reply.get("image")
    if not data:
        print(f"(no image in reply; {path} not written)")
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(base64.b64decode(data))
    print(f"image -> {path}")


def summarize_obs(obs: dict) -> str:
    lines = [f"frame={obs.get('frame')} scene={obs.get('scene')} controllable={obs.get('controllable')}"]
    save = obs.get("save")
    if save:
        lines.append(f"save: {save['name']} (slot {save['slot']})")
    menu = obs.get("menu")
    if menu:
        lines.append(f"menu: current={menu.get('current')} next={menu.get('next')}")
        if "slotIndex" in menu:
            slots = ", ".join(f"{s['index']}:{s['name'] or '-'}" for s in menu.get("slots", []))
            lines.append(f"  slotIndex={menu['slotIndex']} selected={menu['slotSelected']} slots=[{slots}]")
        if "buttons" in menu:
            lines.append(f"  buttons={menu['buttons']} buttonIndex={menu['buttonIndex']}")
    level = obs.get("level")
    if level:
        lines.append(f"level: {level['sid']} side {level['side']} room {level['room']} "
                     f"cutscene={level['inCutscene']} deaths={level['deaths']}")
    p = obs.get("player")
    if p:
        lines.append(f"player: pos={p['pos']} speed={p['speed']} state={p['state']} dashes={p['dashes']} "
                     f"stamina={p['stamina']} onGround={p['onGround']}")
    if "entities" in obs:
        lines.append(f"entities in view: {len(obs['entities'])}")
    tiles = obs.get("tiles")
    if tiles:
        lines.append(f"tiles origin={tiles['origin']}")
        lines.extend("    " + row for row in tiles["rows"])
    return "\n".join(lines)


def print_step(reply: dict) -> None:
    r = reply["result"]
    print(f"stop={r['stop']} consumed={r['consumed']}/{r['planned']} loading={r['loadingFrames']} "
          f"frames {r['startFrame']}->{r['endFrame']}")
    for ev in r.get("events", []):
        print(f"  event @{ev['frame']}: {ev['type']} {ev.get('detail') or ''}")
    traj = r.get("trajectory") or []
    if traj:
        print(f"  trajectory: {len(traj)} rows, first={traj[0]}, last={traj[-1]}")
    print(summarize_obs(reply["obs"]))


def cmd_smoke(client: BridgeClient, out_dir: str) -> int:
    """接管 → 观测 → 推进 → 再观测 → 交还控制，检查帧数是否对得上。"""
    out = Path(out_dir)
    hello = client.call("hello")
    print(f"bridge {hello['bridge']} on Everest {hello['everest']}, scene={hello['scene']}")

    act = client.call("activate", entities=False, tiles=False)
    print("activated:\n" + summarize_obs(act["obs"]))
    if act["obs"].get("scene") != "Level":
        print("请先进入任意关卡（推荐 test 存档的序章）再运行 smoke。")
        client.call("deactivate")
        return 2

    before = client.call("observe")
    save_image(before, str(out / "00_before.png"))
    f0 = before["obs"]["frame"]

    time.sleep(1.0)  # 冻结期间等待 1 秒，帧号不应变化
    frozen = client.call("observe", image=False, entities=False, tiles=False)
    assert frozen["obs"]["frame"] == f0, f"frame advanced while frozen: {f0} -> {frozen['obs']['frame']}"
    print("frozen check: ok")

    one = client.call("step", inputs=["1"], image=False, entities=False, tiles=False)
    assert one["result"]["consumed"] == 1, one["result"]
    print("single-frame step: ok")

    walk = client.call("step", inputs=["30,R", "1,R,J", "20,R"], stop_on=["death", "room"])
    save_image(walk, str(out / "01_after_walk.png"))
    print_step(walk)

    client.call("deactivate")
    print("deactivated; smoke test passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hello")
    sub.add_parser("activate")
    sub.add_parser("deactivate")
    sub.add_parser("skip")
    sub.add_parser("cancel")
    p_obs = sub.add_parser("observe")
    p_obs.add_argument("--out")
    p_obs.add_argument("--json", action="store_true", help="打印完整 JSON")
    p_step = sub.add_parser("step")
    p_step.add_argument("segments", nargs="+", help='如 "30,R" "1,R,J"')
    p_step.add_argument("--stop", default="", help="death,room,land,controllable,uncontrollable,refill,complete")
    p_step.add_argument("--speed", type=int, default=1, help="每个节拍推进几帧，1 为实时")
    p_step.add_argument("--out")
    p_adv = sub.add_parser("advance", help="空输入推进，直到满足条件")
    p_adv.add_argument("--until", default="controllable")
    p_adv.add_argument("--max", type=int, default=600)
    p_adv.add_argument("--speed", type=int, default=1)
    p_smoke = sub.add_parser("smoke")
    p_smoke.add_argument("--out-dir", default="smoke")
    args = ap.parse_args()

    client = BridgeClient(port=args.port)
    try:
        if args.cmd == "hello":
            print(json.dumps(client.call("hello"), ensure_ascii=False, indent=2))
        elif args.cmd == "activate":
            print(summarize_obs(client.call("activate")["obs"]))
        elif args.cmd == "deactivate":
            client.call("deactivate")
            print("deactivated")
        elif args.cmd == "skip":
            print(client.call("skip_cutscene"))
        elif args.cmd == "cancel":
            print(client.call("cancel"))
        elif args.cmd == "observe":
            reply = client.call("observe")
            save_image(reply, args.out)
            if args.json:
                reply.pop("image", None)
                print(json.dumps(reply, ensure_ascii=False, indent=2))
            else:
                print(summarize_obs(reply["obs"]))
        elif args.cmd == "step":
            stop = [s for s in args.stop.split(",") if s]
            reply = client.call("step", inputs=args.segments, stop_on=stop, speed=args.speed)
            save_image(reply, args.out)
            print_step(reply)
        elif args.cmd == "advance":
            reply = client.call("step", inputs=[f"{args.max}"], stop_on=[args.until], speed=args.speed,
                                image=False, trajectory=False)
            print_step(reply)
        elif args.cmd == "smoke":
            return cmd_smoke(client, args.out_dir)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
