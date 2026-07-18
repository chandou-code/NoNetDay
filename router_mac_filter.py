"""
华为光猫 MAC 过滤控制模块
适用机型：电信/移动 E8-C 光猫（页面含 macfiltere8c.asp）

功能：
    - 登录路由器
    - 查询当前 MAC 过滤列表
    - 添加 MAC 到过滤列表
    - 从过滤列表删除 MAC（用置空占位法）
    - 启用 / 禁用 MAC 过滤总开关
    - 查询 / 修改过滤模式（黑名单 / 白名单）

实现说明：
    路由器删除接口（del.cgi）参数复杂不易逆向，采用"占位条目"方案：
    - 添加：优先复用占位条目（00:00:00:00:00:00），不够再用 add.cgi 新增
    - 删除：用 set.cgi 把目标条目 MAC 改成 00:00:00:00:00:00（占位）
    实际效果一样，占位 MAC 不会匹配任何设备。
"""

import re
import base64
import requests

PLACEHOLDER_MAC = "00:00:00:00:00:00"


class RouterMacFilter:
    MAC_FILTER_PAGE = "/html/bbsp/macfilter/macfiltere8c.asp"

    def __init__(self, host: str, username: str, password: str):
        self.host = host if host.startswith("http") else f"http://{host}"
        self.username = username
        self.password = password
        self.sess = requests.Session()
        self.sess.verify = False
        self.sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
        })
        self._token = ""
        self._logged_in = False

    # ---------- 登录 ----------
    def login(self) -> bool:
        self.sess.get(f"{self.host}/", timeout=10)
        r = self.sess.post(f"{self.host}/asp/GetRandCount.asp", timeout=10)
        cnt = r.text.strip().lstrip("\ufeff")
        if not cnt:
            raise RuntimeError("获取随机 token 失败")
        self.sess.cookies.set("body", "Language:chinese:id=-1", path="/")
        data = {
            "UserName": self.username,
            "PassWord": base64.b64encode(self.password.encode()).decode(),
            "x.X_HW_Token": cnt,
        }
        self.sess.post(
            f"{self.host}/login.cgi",
            data=data, timeout=10, allow_redirects=True,
            headers={"Referer": f"{self.host}/"},
        )
        cookie_str = ""
        for k, v in self.sess.cookies.get_dict().items():
            cookie_str += f"{k}={v}; "
        self._logged_in = "sid=" in cookie_str
        if self._logged_in:
            self._refresh_token()
        return self._logged_in

    def _refresh_token(self) -> str:
        r = self.sess.get(f"{self.host}{self.MAC_FILTER_PAGE}", timeout=10)
        if r.status_code != 200 or len(r.text) < 1000:
            raise RuntimeError(f"访问 MAC 过滤页失败：HTTP {r.status_code}")
        m = re.search(r'name="onttoken"[^>]*value="([^"]+)"', r.text)
        if not m:
            raise RuntimeError("未找到 onttoken")
        self._token = m.group(1)
        return self._token

    def _get_page(self) -> str:
        r = self.sess.get(f"{self.host}{self.MAC_FILTER_PAGE}", timeout=10)
        self._token = re.search(r'name="onttoken"[^>]*value="([^"]+)"', r.text).group(1)
        return r.text

    # ---------- 查询 ----------
    def list_macs(self) -> list[tuple[str, str]]:
        """返回 [(domain, mac), ...]，不含空占位条目"""
        html = self._get_page()
        matches = re.findall(
            r'new\s+stMacFilter\("([^"]+)","([^"]+)"\)', html,
        )
        result = []
        for domain, mac in matches:
            if not mac:
                continue
            mac = mac.replace(r"\x3a", ":").replace(r"\x3A", ":").upper()
            result.append((domain, mac))
        return result

    def list_all(self) -> list[tuple[str, str]]:
        """返回所有条目（含占位），用于找空位"""
        return self.list_macs()

    def mac_exists(self, mac: str) -> bool:
        mac = mac.upper().replace("-", ":")
        return any(m == mac for _, m in self.list_macs() if m != PLACEHOLDER_MAC)

    # ---------- 添加 ----------
    def add_mac(self, mac: str) -> bool:
        mac = mac.upper().replace("-", ":")
        if self.mac_exists(mac):
            return True

        # 先找占位条目复用
        all_macs = self.list_macs()
        for domain, m in all_macs:
            if m == PLACEHOLDER_MAC:
                return self._set_domain_mac(domain, mac)

        # 没有空位，新增
        url = (f"{self.host}/html/bbsp/macfilter/add.cgi"
               f"?x=InternetGatewayDevice.X_HW_Security.MacFilter"
               f"&RequestFile=html/bbsp/macfilter/macfiltere8c.asp")
        data = {
            "x.SourceMACAddress": mac,
            "x.X_HW_Token": self._token,
        }
        self.sess.post(
            url, data=data, timeout=10,
            headers={"Referer": f"{self.host}{self.MAC_FILTER_PAGE}"},
        )
        self._refresh_token()
        return self.mac_exists(mac)

    # ---------- 删除 ----------
    def remove_mac(self, mac: str) -> bool:
        """把目标 MAC 改成占位值，等效删除"""
        mac = mac.upper().replace("-", ":")
        all_macs = self.list_macs()
        target_domain = None
        for domain, m in all_macs:
            if m == mac:
                target_domain = domain
                break
        if target_domain is None:
            return True
        return self._set_domain_mac(target_domain, PLACEHOLDER_MAC)

    def _set_domain_mac(self, domain: str, mac: str) -> bool:
        """修改指定 domain 条目的 MAC 地址"""
        url = (f"{self.host}/html/bbsp/macfilter/set.cgi"
               f"?x={domain}"
               f"&RequestFile=html/bbsp/macfilter/macfiltere8c.asp")
        data = {
            "x.SourceMACAddress": mac,
            "x.X_HW_Token": self._token,
        }
        self.sess.post(
            url, data=data, timeout=10,
            headers={"Referer": f"{self.host}{self.MAC_FILTER_PAGE}"},
        )
        self._refresh_token()
        # 验证
        all_macs = self.list_macs()
        for d, m in all_macs:
            if d == domain:
                return m.upper() == mac.upper()
        return False

    # ---------- 总开关 ----------
    def is_filter_enabled(self) -> bool:
        html = self._get_page()
        m = re.search(r"var enableFilter\s*=\s*'(\d+)'", html)
        if m:
            return m.group(1) == "1"
        return bool(re.search(r'id="MAC_filter_enable_checkbox"[^>]*checked', html))

    def get_filter_mode(self) -> str:
        """返回 'black'（黑名单） / 'white'（白名单） / 'unknown'"""
        html = self._get_page()
        black = re.search(r"var BlackEnable\s*=\s*'(\d+)'", html)
        white = re.search(r"var WhiteEnable\s*=\s*'(\d+)'", html)
        if black and black.group(1) == "1":
            return "black"
        if white and white.group(1) == "1":
            return "white"
        return "unknown"

    def set_filter_enabled(self, enable: bool) -> bool:
        """启用 / 禁用 MAC 过滤总开关"""
        if self.is_filter_enabled() == enable:
            return True
        html = self._get_page()
        m = re.search(r"StrHomeMacAddr\s*=\s*'([0-9A-Fa-f]+)'", html)
        if not m:
            raise RuntimeError("未找到路由器自身 MAC")
        home_mac = m.group(1)

        url = (f"{self.host}/html/bbsp/macfilter/set.cgi"
               f"?x=InternetGatewayDevice.X_HW_Security"
               f"&RequestFile=html/bbsp/macfilter/macfiltere8c.asp")

        # 从 JS OnSaveFilterModeCommon 看参数
        data = {
            "x.MacFilterRight": "1" if enable else "0",
            "x.ClientMAC": home_mac,
            "x.X_HW_Token": self._token,
        }
        self.sess.post(
            url, data=data, timeout=10,
            headers={"Referer": f"{self.host}{self.MAC_FILTER_PAGE}"},
        )
        self._refresh_token()
        return self.is_filter_enabled() == enable
