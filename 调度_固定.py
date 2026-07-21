import ctypes
import datetime as dt
import json
import os
import subprocess
import sys
import time

if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
ROUTER_CONFIG_FILE = os.path.join(HERE, "router_config.json")
CHECK_EVERY = 10

DISABLE_HOUR = 22
DISABLE_MINUTE = 50
ENABLE_HOUR = 10
ENABLE_MINUTE = 50

DEFAULT_CONFIG = {
    "state": "disabled",
    "last_switch": "",
    "target_on_time": "",
    "adapter": "WLAN",
    "mode": "fixed",
    "sports": {
        "upload_folder": "上传运动文件",
        "storage_folder": "运动记录",
        "daily_limit_hours": 3,
        "reduce_per_upload": 3600,
        "today_reduced": 0,
        "last_reduce_date": "",
    },
}

DEFAULT_ROUTER_CONFIG = {
    "enabled": True,
    "host": "192.168.1.1",
    "username": "",
    "password": "",
    "target_macs": [],
}


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def load_config() -> dict | None:
    from config_crypto import load_config as _load_encrypted
    return _load_encrypted(CONFIG_FILE)


def save_config(cfg: dict) -> None:
    from config_crypto import save_config as _save_encrypted
    _save_encrypted(cfg, CONFIG_FILE)


def load_router_config() -> dict:
    if not os.path.exists(ROUTER_CONFIG_FILE):
        with open(ROUTER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_ROUTER_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"[路由器配置] 已创建 {ROUTER_CONFIG_FILE}，请手动填写用户名密码和 MAC 地址")
    with open(ROUTER_CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _netsh_set(adapter: str, enable: bool) -> None:
    action = "enable" if enable else "disable"
    subprocess.run(
        f'netsh interface set interface "{adapter}" admin={action}',
        shell=True, creationflags=0x08000000,
    )


def apply_state(cfg: dict, target_state: str) -> bool:
    if target_state == "disabled":
        ok_router = _apply_router(cfg, target_state)
        ok_local = _apply_local(cfg, target_state)
    else:
        ok_local = _apply_local(cfg, target_state)
        ok_router = _apply_router(cfg, target_state)
    return ok_local and ok_router


def _apply_local(cfg: dict, target_state: str) -> bool:
    action = "禁用" if target_state == "disabled" else "启用"
    print(f"[本地网卡] {action} {cfg['adapter']} ...")
    _netsh_set(cfg["adapter"], enable=(target_state == "enabled"))
    print(f"[本地网卡] 完成")

    if target_state == "enabled":
        print(f"[本地网卡] 等待网络连接 ...", end="", flush=True)
        max_wait = 30
        waited = 0
        while waited < max_wait:
            cp = subprocess.run(
                "netsh interface show interface",
                shell=True, capture_output=True, text=True,
                creationflags=0x08000000,
            )
            connected = False
            for line in cp.stdout.splitlines():
                if cfg["adapter"] in line and ("Connected" in line or "已连接" in line):
                    connected = True
                    break
            if connected:
                time.sleep(2)
                print(" 已连接")
                return True
            time.sleep(2)
            waited += 2
            print(".", end="", flush=True)
        print(" 超时（但可能已联网，继续尝试访问路由器）")
    return True


def _apply_router(_cfg: dict, target_state: str) -> bool:
    router_cfg = load_router_config()
    if not (router_cfg.get("enabled") and router_cfg.get("target_macs")):
        return True

    if not router_cfg.get("username") or not router_cfg.get("password"):
        print("[路由器] 用户名或密码未填写，请编辑 router_config.json")
        return False

    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            from router_mac_filter import RouterMacFilter
            r = RouterMacFilter(
                router_cfg["host"],
                router_cfg["username"],
                router_cfg["password"],
            )
            if not r.login():
                print(f"[路由器] 登录失败（第 {attempt} 次）")
                time.sleep(3)
                continue
            macs = router_cfg["target_macs"]
            if target_state == "disabled":
                print(f"[路由器] 添加 {len(macs)} 个 MAC 到过滤列表 ...")
                ok = True
                for mac in macs:
                    if r.add_mac(mac):
                        print(f"  + {mac}  OK")
                    else:
                        print(f"  + {mac}  FAIL")
                        ok = False
                return ok
            else:
                print(f"[路由器] 从过滤列表移除 {len(macs)} 个 MAC ...")
                ok = True
                for mac in macs:
                    if r.remove_mac(mac):
                        print(f"  - {mac}  OK")
                    else:
                        print(f"  - {mac}  FAIL")
                        ok = False
                return ok
        except Exception as e:
            print(f"[路由器] 异常（第 {attempt}/{max_retries} 次）: {e}")
            if attempt < max_retries:
                time.sleep(3)
            else:
                return False
    return False


def _scan_sports_files(cfg: dict) -> list[str]:
    upload_folder = os.path.join(HERE, cfg["sports"]["upload_folder"])
    if not os.path.exists(upload_folder):
        return []
    new_files = []
    for fname in os.listdir(upload_folder):
        fpath = os.path.join(upload_folder, fname)
        if os.path.isfile(fpath):
            ext = os.path.splitext(fname)[1].lower()
            if ext in (".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mov", ".avi", ".mkv"):
                new_files.append(fpath)
    return new_files


def _process_sports_file(cfg: dict, fpath: str) -> bool:
    storage_folder = os.path.join(HERE, cfg["sports"]["storage_folder"])
    today = dt.datetime.now().strftime("%Y-%m-%d")
    day_folder = os.path.join(storage_folder, today)
    os.makedirs(day_folder, exist_ok=True)
    fname = os.path.basename(fpath)
    new_path = os.path.join(day_folder, fname)
    if os.path.exists(new_path):
        base, ext = os.path.splitext(fname)
        ts = dt.datetime.now().strftime("%H%M%S")
        new_path = os.path.join(day_folder, f"{base}_{ts}{ext}")
    try:
        os.rename(fpath, new_path)
        return True
    except Exception as e:
        print(f"[运动] 移动文件失败: {e}")
        return False


def _reduce_off_duration(cfg: dict) -> None:
    new_files = _scan_sports_files(cfg)
    if not new_files:
        return

    sports_cfg = cfg["sports"]
    today = dt.datetime.now().strftime("%Y-%m-%d")

    if sports_cfg["last_reduce_date"] != today:
        sports_cfg["today_reduced"] = 0
        sports_cfg["last_reduce_date"] = today
        save_config(cfg)

    for fpath in new_files:
        fname = os.path.basename(fpath)
        if cfg["state"] != "disabled":
            print(f"[运动] 当前不是断网状态，跳过: {fname}")
            _process_sports_file(cfg, fpath)
            continue

        remaining_limit = sports_cfg["daily_limit_hours"] * 3600 - sports_cfg["today_reduced"]
        if remaining_limit <= 0:
            print(f"[运动] 今天已减够 {sports_cfg['daily_limit_hours']} 小时，跳过: {fname}")
            _process_sports_file(cfg, fpath)
            continue

        if not cfg["target_on_time"]:
            print(f"[运动] 无目标联网时间，跳过: {fname}")
            _process_sports_file(cfg, fpath)
            continue

        target_on = dt.datetime.fromisoformat(cfg["target_on_time"])
        reduce_sec = min(sports_cfg["reduce_per_upload"], remaining_limit)
        new_target_on = target_on - dt.timedelta(seconds=reduce_sec)
        earliest_on = target_on - dt.timedelta(hours=sports_cfg["daily_limit_hours"])

        if new_target_on < earliest_on:
            print(f"[运动] 已达最大减时上限，跳过: {fname}")
            _process_sports_file(cfg, fpath)
            continue

        cfg["target_on_time"] = new_target_on.isoformat(timespec="seconds")
        sports_cfg["today_reduced"] += reduce_sec

        if _process_sports_file(cfg, fpath):
            print(f"[运动] ✓ 上传成功！减时 {reduce_sec//3600} 小时，今天已减 {sports_cfg['today_reduced']//3600} 小时")
            print(f"[运动]   联网时间提前至: {new_target_on:%Y-%m-%d %H:%M:%S}")
        else:
            print(f"[运动] ✗ 文件移动失败，但减时已生效")

        save_config(cfg)


def _is_in_disable_window(now: dt.datetime) -> bool:
    hour, minute = now.hour, now.minute
    if hour > DISABLE_HOUR or (hour == DISABLE_HOUR and minute >= DISABLE_MINUTE):
        return True
    if hour < ENABLE_HOUR or (hour == ENABLE_HOUR and minute < ENABLE_MINUTE):
        return True
    return False


def _get_next_disable_time(now: dt.datetime) -> dt.datetime:
    disable_time = now.replace(hour=DISABLE_HOUR, minute=DISABLE_MINUTE, second=0, microsecond=0)
    if now >= disable_time:
        disable_time += dt.timedelta(days=1)
    return disable_time


def _get_next_enable_time(now: dt.datetime) -> dt.datetime:
    if _is_in_disable_window(now):
        if now.hour < ENABLE_HOUR or (now.hour == ENABLE_HOUR and now.minute < ENABLE_MINUTE):
            enable_time = now.replace(hour=ENABLE_HOUR, minute=ENABLE_MINUTE, second=0, microsecond=0)
        else:
            enable_time = (now + dt.timedelta(days=1)).replace(hour=ENABLE_HOUR, minute=ENABLE_MINUTE, second=0, microsecond=0)
    else:
        disable_time = _get_next_disable_time(now)
        enable_time = (disable_time + dt.timedelta(days=1)).replace(hour=ENABLE_HOUR, minute=ENABLE_MINUTE, second=0, microsecond=0)
    return enable_time


def _determine_initial_state(cfg: dict) -> tuple[str, str, str]:
    now = dt.datetime.now()

    if _is_in_disable_window(now):
        state = "disabled"
        target_on = _get_next_enable_time(now).isoformat(timespec="seconds")
        print(f"[初始化] 当前时间 {now:%H:%M}，在断网时段（22:50-10:50），状态=disabled")
        print(f"[初始化] 目标联网时间: {_get_next_enable_time(now):%Y-%m-%d %H:%M}")
    else:
        state = "enabled"
        target_on = ""
        print(f"[初始化] 当前时间 {now:%H:%M}，在联网时段（10:50-22:50），状态=enabled")
        print(f"[初始化] 下次断网: {_get_next_disable_time(now):%Y-%m-%d %H:%M}")

    last_switch = now.isoformat(timespec="seconds")
    return state, last_switch, target_on


def init_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    state, last_switch, target_on = _determine_initial_state(cfg)
    cfg["state"] = state
    cfg["last_switch"] = last_switch
    cfg["target_on_time"] = target_on

    router_cfg = load_router_config()

    upload_folder = os.path.join(HERE, cfg["sports"]["upload_folder"])
    storage_folder = os.path.join(HERE, cfg["sports"]["storage_folder"])
    os.makedirs(upload_folder, exist_ok=True)
    os.makedirs(storage_folder, exist_ok=True)

    save_config(cfg)

    if router_cfg.get("enabled") and (not router_cfg.get("username") or not router_cfg.get("password")):
        print(f"[初始化] 已创建 config.json，状态={state}")
        print(f"[初始化] 路由器密码未填写，只操作本地网卡")
        _apply_local(cfg, state)
        print(f"[初始化] 请编辑 router_config.json 填写用户名密码和 MAC，然后重启")
    else:
        apply_state(cfg, state)
        print(f"[初始化] 已创建 config.json，状态={state}")
    return cfg


def main() -> int:
    if not is_admin():
        print("[错误] 必须以管理员身份运行！")
        return 1

    cfg = load_config()
    if cfg is None:
        cfg = init_config()
    else:
        print(f"[启动] 读取 config.json，当前状态={cfg['state']}")
        changed = False
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v if not isinstance(v, (dict, list)) else json.loads(json.dumps(v))
                changed = True
        if changed:
            save_config(cfg)

        if cfg.get("target_on_time"):
            target_on = dt.datetime.fromisoformat(cfg["target_on_time"])
            print(f"[启动] 目标联网时间: {target_on:%Y-%m-%d %H:%M:%S}")

        print(f"[启动同步] 强制应用当前状态: {cfg['state']}")
        apply_state(cfg, cfg["state"])

    while True:
        try:
            now = dt.datetime.now()

            if cfg["state"] == "enabled":
                disable_time = _get_next_disable_time(now)
                remain = (disable_time - now).total_seconds()
                print(f"\r状态={cfg['state']:8s}  距断网 {remain:7.1f}s  ", end="", flush=True)

                if now >= disable_time:
                    new_state = "disabled"
                    target_on_time = _get_next_enable_time(now)
                    cfg["target_on_time"] = target_on_time.isoformat(timespec="seconds")
                    print(f"\n[切换] → {new_state}  {now:%Y-%m-%d %H:%M:%S}")
                    print(f"[切换] 目标联网时间: {target_on_time:%Y-%m-%d %H:%M:%S}")
                    apply_state(cfg, new_state)
                    cfg["state"] = new_state
                    cfg["last_switch"] = now.isoformat(timespec="seconds")
                    save_config(cfg)

            else:
                if cfg.get("target_on_time"):
                    target_on = dt.datetime.fromisoformat(cfg["target_on_time"])
                    remain = (target_on - now).total_seconds()
                    print(f"\r状态={cfg['state']:8s}  距联网 {remain:7.1f}s  ", end="", flush=True)

                    if now >= target_on:
                        new_state = "enabled"
                        print(f"\n[切换] → {new_state}  {now:%Y-%m-%d %H:%M:%S}")
                        apply_state(cfg, new_state)
                        cfg["state"] = new_state
                        cfg["last_switch"] = now.isoformat(timespec="seconds")
                        cfg["target_on_time"] = ""
                        save_config(cfg)

            _reduce_off_duration(cfg)

            time.sleep(CHECK_EVERY)
        except KeyboardInterrupt:
            print("\n[退出] Ctrl+C")
            return 0


if __name__ == "__main__":
    sys.exit(main())
