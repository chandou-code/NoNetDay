"""
测试脚本：移除手机 MAC 让手机能联网
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from router_mac_filter import RouterMacFilter, PLACEHOLDER_MAC

PHONE_MAC = "aa:3f:6c:5b:e4:2c"

r = RouterMacFilter("192.168.1.1", "user", "2n#phm9c")

print("=== 登录路由器 ===")
if not r.login():
    print("登录失败")
    sys.exit(1)
print("登录成功")

print("\n=== 当前列表 ===")
for d, m in r.list_macs():
    tag = " [占位]" if m == PLACEHOLDER_MAC else ""
    print(f"  {m}{tag}  ({d})")

print(f"\n=== 移除手机 MAC: {PHONE_MAC} ===")
ok = r.remove_mac(PHONE_MAC)
print(f"移除结果: {'成功' if ok else '失败'}")

print("\n=== 移除后列表 ===")
for d, m in r.list_macs():
    tag = " [占位]" if m == PLACEHOLDER_MAC else ""
    print(f"  {m}{tag}  ({d})")

print(f"\n手机 MAC 是否还在过滤列表: {r.mac_exists(PHONE_MAC)}")
print("如果显示 False，手机应该能上网了，等几秒试试")
