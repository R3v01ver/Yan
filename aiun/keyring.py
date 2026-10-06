"""本地 Key 保险库：Key 不硬编码、不明文落盘。

三条规矩：
1. 环境变量最优先（高级用户 / CI 用）。
2. 没有环境变量时，Key 用 Windows DPAPI 加密后存进用户目录
   （%APPDATA%\\Yan\\keys.dat，绑定当前 Windows 账户，换电脑/换账户都解不开）。
   非 Windows 退化为 0600 权限的明文 JSON，并明确告知。
3. council.yaml 里永远不写明文 Key —— 谁想把整个项目目录传上 git 都不会泄露。
"""

import base64
import json
import os
import sys

# 厂商 -> 环境变量名（GUI 里保存时如果环境变量已有值，以环境变量为准）
ENV_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "zhipu": "ZHIPUAI_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def _store_path():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Yan", "keys.dat")
    return os.path.join(os.path.expanduser("~"), ".aiun", "keys.json")


# ── Windows DPAPI ────────────────────────────────────────
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data):
        """DATA_BLOB 要持有缓冲区本体，调用期间不能被回收。"""
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        return (_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf)

    _crypt32 = ctypes.windll.crypt32
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_BLOB)]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_BLOB), ctypes.POINTER(wintypes.LPCWSTR),
        ctypes.POINTER(_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_BLOB)]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL

    def _protect(raw: bytes) -> bytes:
        cin, keep = _blob(raw)
        cout = _BLOB()
        if not _crypt32.CryptProtectData(
                ctypes.byref(cin), None, None, None, None, 0, ctypes.byref(cout)):
            raise OSError("DPAPI 加密失败")
        try:
            return ctypes.string_at(cout.pbData, cout.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(
                ctypes.cast(cout.pbData, ctypes.c_void_p))

    def _unprotect(raw: bytes) -> bytes:
        cin, keep = _blob(raw)
        cout = _BLOB()
        if not _crypt32.CryptUnprotectData(
                ctypes.byref(cin), None, None, None, None, 0, ctypes.byref(cout)):
            raise OSError("DPAPI 解密失败（换账户或换机器后密文不可解）")
        try:
            return ctypes.string_at(cout.pbData, cout.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(
                ctypes.cast(cout.pbData, ctypes.c_void_p))

    _PLAIN = False
else:
    _PLAIN = True   # 非 Windows：明文 + 收紧权限

    def _protect(raw: bytes) -> bytes:
        return raw

    def _unprotect(raw: bytes) -> bytes:
        return raw


def _legacy_path():
    """应用还叫 AIUN 时的存储位置。首次启动自动搬过来。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "AIUN", "keys.dat")
    return os.path.join(os.path.expanduser("~"), ".aiun", "keys.json")


def _read_all():
    p = _store_path()
    if not os.path.isfile(p):
        legacy = _legacy_path()
        if os.path.isfile(legacy):          # 从旧目录迁移（改名不丢 Key）
            try:
                with open(legacy, "rb") as f:
                    data = _unprotect(base64.b64decode(f.read()))
                store = json.loads(data.decode("utf-8"))
                if store:
                    _write_all(store)
                return store
            except Exception:
                return {}
        return {}
    try:
        with open(p, "rb") as f:
            data = _unprotect(base64.b64decode(f.read()))
        return json.loads(data.decode("utf-8"))
    except Exception:
        # 密文坏了（换系统重装等）就当没有，别让程序起不来
        return {}


def _write_all(store):
    p = _store_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    payload = base64.b64encode(_protect(json.dumps(store).encode("utf-8")))
    with open(p, "wb") as f:
        f.write(payload)
    if _PLAIN:
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def save(vendor, key):
    """保存/更新一把 Key。key 为空等于删除。"""
    store = _read_all()
    if key:
        store[vendor] = key
    else:
        store.pop(vendor, None)
    try:
        _write_all(store)
    except OSError:
        pass   # 保存失败不致命：这次会话内存里还有


def load(vendor):
    return _read_all().get(vendor, "")


def delete(vendor):
    save(vendor, "")


def has(vendor):
    return bool(load(vendor))


def get_key(vendor, env_hint=None):
    """取 Key：环境变量 > 保险库。env_hint 允许配置里自定义变量名。"""
    for env in (env_hint, ENV_KEYS.get(vendor)):
        if env:
            v = os.environ.get(env, "").strip()
            if v:
                return v
    return load(vendor)


def masked(key):
    """sk-abcd...wxyz 形式的掩码，日志和界面显示用。"""
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 10:
        return k[0] + "***" + k[-1]
    return k[:6] + "***" + k[-4:]


def storage_desc():
    if sys.platform == "win32":
        return "Key 用 Windows 加密（DPAPI）存在你自己的用户目录里，" \
               "不写进项目文件，换电脑也带不走——泄露不了。"
    return "Key 存在 ~/.aiun/keys.json（权限 0600，仅当前用户可读）。"
