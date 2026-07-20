import ctypes
import datetime as dt
import json
import os
import subprocess
import sys
import time

# 兼容 PyInstaller 打包：exe 模式下用 exe 所在目录，脚本模式用 .py 所在目录
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)   # exe 所在目录
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE       = os.path.join(HERE, "config.json")
ROUTER_CONFIG_FILE = os.path.join(HERE, "router_config.json")
CHECK_EVERY       = 10

# 默认配置（首次运行时写入 config.json）
DEFAULT_CONFIG = {
    "state": "disabled",
    "last_switch": "",
    "cycle_seconds": 86400,         # 24 小时（断 24h + 通 24h 循环）
    "adapter": "WLAN",
    "sports": {
        "upload_folder": "上传运动文件",   # 相对路径，运行时解析为 HERE 的子目录
        "storage_folder": "运动记录",      # 相对路径
        "daily_limit_hours": 3,    # 每天最多减 3 小时
        "reduce_per_upload": 3600, # 每次上传减 1 小时（秒）
        "today_reduced": 0,        # 今天已减时长（秒）
        "last_reduce_date": "",    # 上次减时的日期（用于判断是否跨天）
    },
}

# 路由器配置模板（首次运行时写入 router_config.json，明文存储）
DEFAULT_ROUTER_CONFIG = {
    "enabled": True,           # 是否启用路由器 MAC 过滤（双重断网）
    "host": "192.168.1.1",
    "username": "",
    "password": "",
    "target_macs": [],         # 要禁的手机 MAC 列表
}


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def load_config() -> dict | None:
    """读取加密 config（自动兼容旧版明文并迁移）"""
    from config_crypto import load_config as _load_encrypted
    return _load_encrypted(CONFIG_FILE)


def save_config(cfg: dict) -> None:
    """加密写入 config（DPAPI 加密，密钥由 Windows 管理）"""
    from config_crypto import save_config as _save_encrypted
    _save_encrypted(cfg, CONFIG_FILE)


def load_router_config() -> dict:
    """读取路由器配置（明文存储），不存在则创建默认模板"""
    if not os.path.exists(ROUTER_CONFIG_FILE):
        # 创建默认模板（空密码，需要用户自己填）
        with open(ROUTER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_ROUTER_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"[路由器配置] 已创建 {ROUTER_CONFIG_FILE}，请手动填写用户名密码和 MAC 地址")
    with open(ROUTER_CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _netsh_set(adapter: str, enable: bool) -> None:
    """直接调用 netsh 启用/禁用网卡，不依赖外部 bat 文件"""
    action = "enable" if enable else "disable"
    subprocess.run(
        f'netsh interface set interface "{adapter}" admin={action}',
        shell=True, creationflags=0x08000000,
    )


def apply_state(cfg: dict, target_state: str) -> bool:
    """
    应用目标状态：disabled / enabled
    顺序（关键）：
      断网（disabled）：先操作路由器，再禁用本地网卡
      联网（enabled）：先启用本地网卡，再操作路由器
    原因：本地网卡是访问路由器的通道，禁用后再访问路由器会失败。
    """
    # 1. 断网：先路由器后网卡
    if target_state == "disabled":
        ok_router = _apply_router(cfg, target_state)
        ok_local  = _apply_local(cfg, target_state)
    # 2. 联网：先网卡后路由器
    else:
        ok_local  = _apply_local(cfg, target_state)
        ok_router = _apply_router(cfg, target_state)
    return ok_local and ok_router


def _apply_local(cfg: dict, target_state: str) -> bool:
    """操作本地网卡，返回是否成功"""
    action = "禁用" if target_state == "disabled" else "启用"
    print(f"[本地网卡] {action} {cfg['adapter']} ...")
    _netsh_set(cfg["adapter"], enable=(target_state == "enabled"))
    print(f"[本地网卡] 完成")

    # 启用网卡后，等几秒让网络就绪（DHCP 分配 IP 需要时间）
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
    """操作路由器 MAC 过滤，失败自动重试"""
    router_cfg = load_router_config()
    if not (router_cfg.get("enabled") and router_cfg.get("target_macs")):
        return True

    # 检查密码是否已填写
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
    """扫描上传文件夹，返回未处理的文件列表"""
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
    """把运动文件移到按日期分类的存储目录"""
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


def _reduce_time(cfg: dict) -> None:
    """扫描运动文件并执行减时逻辑"""
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

        last = dt.datetime.fromisoformat(cfg["last_switch"])
        reduce_sec = min(sports_cfg["reduce_per_upload"], remaining_limit)
        new_last = last + dt.timedelta(seconds=reduce_sec)
        cfg["last_switch"] = new_last.isoformat(timespec="seconds")
        sports_cfg["today_reduced"] += reduce_sec

        if _process_sports_file(cfg, fpath):
            print(f"[运动] ✓ 上传成功！减时 {reduce_sec//3600} 小时，今天已减 {sports_cfg['today_reduced']//3600} 小时")
        else:
            print(f"[运动] ✗ 文件移动失败，但减时已生效")

        save_config(cfg)


def init_config() -> dict:
    """首次运行：创建默认 config 并执行禁用"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    now = dt.datetime.now()
    cfg["last_switch"] = now.isoformat(timespec="seconds")

    router_cfg = load_router_config()

    upload_folder = os.path.join(HERE, cfg["sports"]["upload_folder"])
    storage_folder = os.path.join(HERE, cfg["sports"]["storage_folder"])
    os.makedirs(upload_folder, exist_ok=True)
    os.makedirs(storage_folder, exist_ok=True)

    save_config(cfg)

    if router_cfg.get("enabled") and (not router_cfg.get("username") or not router_cfg.get("password")):
        print(f"[初始化] 已创建 config.json，状态=disabled")
        print(f"[初始化] 路由器密码未填写，只禁用本地网卡")
        _apply_local(cfg, "disabled")
        print(f"[初始化] 请编辑 router_config.json 填写用户名密码和 MAC，然后重启")
    else:
        apply_state(cfg, "disabled")
        print(f"[初始化] 已创建 config.json，状态=disabled, 周期={cfg['cycle_seconds']}s")
    return cfg


def main() -> int:
    if not is_admin():
        print("[错误] 必须以管理员身份运行！")
        return 1

    cfg = load_config()
    if cfg is None:
        cfg = init_config()
    else:
        print(f"[启动] 读取 config.json，当前状态={cfg['state']}, 周期={cfg['cycle_seconds']}s")
        # 补全缺失字段（兼容旧版 config）
        changed = False
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v if not isinstance(v, (dict, list)) else json.loads(json.dumps(v))
                changed = True
        if changed:
            save_config(cfg)
        # 启动时同步一次状态，保证实际状态跟 config 一致
        # （防止之前手动测试改了路由器 / 网卡状态，启动后状态错乱）
        print(f"[启动同步] 强制应用当前状态: {cfg['state']}")
        apply_state(cfg, cfg["state"])

    while True:
        try:
            now = dt.datetime.now()
            last = dt.datetime.fromisoformat(cfg["last_switch"])
            elapsed = (now - last).total_seconds()
            remain = cfg["cycle_seconds"] - elapsed

            print(f"\r状态={cfg['state']:8s}  剩余 {remain:7.1f}s / {cfg['cycle_seconds']}s  ",
                  end="", flush=True)

            if elapsed >= cfg["cycle_seconds"]:
                new_state = "enabled" if cfg["state"] == "disabled" else "disabled"
                print(f"\n[切换] → {new_state}  {now:%Y-%m-%d %H:%M:%S}")
                apply_state(cfg, new_state)
                cfg["state"] = new_state
                cfg["last_switch"] = now.isoformat(timespec="seconds")
                save_config(cfg)

            _reduce_time(cfg)

            time.sleep(CHECK_EVERY)
        except KeyboardInterrupt:
            print("\n[退出] Ctrl+C")
            return 0


if __name__ == "__main__":
    sys.exit(main())
