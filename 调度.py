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
CONFIG_FILE  = os.path.join(HERE, "config.json")
CHECK_EVERY  = 10

# 默认配置（首次运行时写入 config.json）
DEFAULT_CONFIG = {
    "state": "disabled",
    "last_switch": "",
    "cycle_seconds": 86400,         # 24 小时（断 24h + 通 24h 循环）
    "adapter": "WLAN",
    "router": {
        "enabled": True,           # 是否启用路由器 MAC 过滤（双重断网）
        "host": "192.168.1.1",
        "username": "user",
        "password": "2n#phm9c",
        "target_macs": [           # 要禁的手机 MAC 列表
            "aa:3f:6c:5b:e4:2c"
        ],
    },
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
        print(f"[本地网卡] 等待网络连接 ...", flush=True)
        max_wait = 30
        waited = 0
        while waited < max_wait:
            cp = subprocess.run(
                "netsh interface show interface",
                shell=True, capture_output=True, text=True,
                creationflags=0x08000000,
            )
            # 调试：打印 netsh 原始输出（前 10 行）
            lines = cp.stdout.splitlines()
            if waited == 0:
                print(f"  [调试] netsh 输出 ({len(lines)} 行):")
                for l in lines[:8]:
                    print(f"    | {l}")

            connected = False
            for line in lines:
                if cfg["adapter"] in line:
                    print(f"  [调试] 匹配到网卡行: {line.strip()}")
                    # 匹配 Connected 或 已连接（中英文系统都兼容）
                    if "Connected" in line or "已连接" in line:
                        connected = True
                    break
            if connected:
                # 再多等 2 秒让路由/ARP 表就绪
                time.sleep(2)
                print("  ✓ 已连接")
                return True
            time.sleep(2)
            waited += 2
            print(f"  ... 等待中 ({waited}s/{max_wait}s)", flush=True)
        print("  ✗ 超时（但可能已联网，继续尝试访问路由器）")
    return True


def _apply_router(cfg: dict, target_state: str) -> bool:
    """操作路由器 MAC 过滤，失败自动重试"""
    router_cfg = cfg.get("router", {})
    if not (router_cfg.get("enabled") and router_cfg.get("target_macs")):
        return True

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


def init_config() -> dict:
    """首次运行：创建默认 config 并执行禁用"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    now = dt.datetime.now()
    cfg["last_switch"] = now.isoformat(timespec="seconds")
    apply_state(cfg, "disabled")
    save_config(cfg)
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

            time.sleep(CHECK_EVERY)
        except KeyboardInterrupt:
            print("\n[退出] Ctrl+C")
            return 0


if __name__ == "__main__":
    sys.exit(main())
