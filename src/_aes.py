# -*- coding: utf-8 -*-
"""纯 Python AES 解密（CBC / GCM），只用于读取本机客户端凭据。

不依赖 cryptography / pycryptodome，打包体积不变，也不需要额外 DLL。
只实现解密方向；加密核心仅用于 GCM 的 CTR 与标签计算。
"""
import hmac

_R = 0xE1000000000000000000000000000000


def _gf_mul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _rotl8(x, n):
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _build_sbox():
    exp = [0] * 256
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x = _gf_mul(x, 3)
    sbox = [0] * 256
    inv_sbox = [0] * 256
    for a in range(256):
        b = 0 if a == 0 else exp[(255 - log[a]) % 255]
        s = b ^ _rotl8(b, 1) ^ _rotl8(b, 2) ^ _rotl8(b, 3) ^ _rotl8(b, 4) ^ 0x63
        sbox[a] = s
        inv_sbox[s] = a
    return sbox, inv_sbox


_SBOX, _INV_SBOX = _build_sbox()
_RCON = [0x01]
for _ in range(13):
    _RCON.append(_gf_mul(_RCON[-1], 2))


def _expand_key(key):
    nk = len(key) // 4
    if nk not in (4, 6, 8) or len(key) != nk * 4:
        raise ValueError("不支持的 AES 密钥长度：%d" % len(key))
    nr = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = list(words[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[b] for b in t]
        words.append([a ^ b for a, b in zip(words[i - nk], t)])
    return [sum(words[4 * r:4 * r + 4], []) for r in range(nr + 1)]


def _shift_rows(s):
    return [
        s[0], s[5], s[10], s[15],
        s[4], s[9], s[14], s[3],
        s[8], s[13], s[2], s[7],
        s[12], s[1], s[6], s[11],
    ]


def _inv_shift_rows(s):
    return [
        s[0], s[13], s[10], s[7],
        s[4], s[1], s[14], s[11],
        s[8], s[5], s[2], s[15],
        s[12], s[9], s[6], s[3],
    ]


def _mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        out[4 * c] = _gf_mul(a0, 2) ^ _gf_mul(a1, 3) ^ a2 ^ a3
        out[4 * c + 1] = a0 ^ _gf_mul(a1, 2) ^ _gf_mul(a2, 3) ^ a3
        out[4 * c + 2] = a0 ^ a1 ^ _gf_mul(a2, 2) ^ _gf_mul(a3, 3)
        out[4 * c + 3] = _gf_mul(a0, 3) ^ a1 ^ a2 ^ _gf_mul(a3, 2)
    return out


def _inv_mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        out[4 * c] = (
            _gf_mul(a0, 14) ^ _gf_mul(a1, 11) ^ _gf_mul(a2, 13) ^ _gf_mul(a3, 9)
        )
        out[4 * c + 1] = (
            _gf_mul(a0, 9) ^ _gf_mul(a1, 14) ^ _gf_mul(a2, 11) ^ _gf_mul(a3, 13)
        )
        out[4 * c + 2] = (
            _gf_mul(a0, 13) ^ _gf_mul(a1, 9) ^ _gf_mul(a2, 14) ^ _gf_mul(a3, 11)
        )
        out[4 * c + 3] = (
            _gf_mul(a0, 11) ^ _gf_mul(a1, 13) ^ _gf_mul(a2, 9) ^ _gf_mul(a3, 14)
        )
    return out


def _encrypt_block(rk, block):
    nr = len(rk) - 1
    s = [a ^ b for a, b in zip(block, rk[0])]
    for rnd in range(1, nr):
        s = [_SBOX[b] for b in s]
        s = _shift_rows(s)
        s = _mix_columns(s)
        s = [a ^ b for a, b in zip(s, rk[rnd])]
    s = [_SBOX[b] for b in s]
    s = _shift_rows(s)
    s = [a ^ b for a, b in zip(s, rk[nr])]
    return bytes(s)


def _decrypt_block(rk, block):
    nr = len(rk) - 1
    s = [a ^ b for a, b in zip(block, rk[nr])]
    for rnd in range(nr - 1, 0, -1):
        s = _inv_shift_rows(s)
        s = [_INV_SBOX[b] for b in s]
        s = [a ^ b for a, b in zip(s, rk[rnd])]
        s = _inv_mix_columns(s)
    s = _inv_shift_rows(s)
    s = [_INV_SBOX[b] for b in s]
    s = [a ^ b for a, b in zip(s, rk[0])]
    return bytes(s)


def cbc_decrypt(key, iv, data):
    """AES-CBC 解密；不做 padding 处理，原样返回明文块。"""
    if len(iv) != 16:
        raise ValueError("IV 长度应为 16 字节")
    if len(data) % 16:
        raise ValueError("密文长度不是 16 的倍数")
    rk = _expand_key(key)
    prev = iv
    out = bytearray()
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        plain = _decrypt_block(rk, block)
        out += bytes(a ^ b for a, b in zip(plain, prev))
        prev = block
    return bytes(out)


def _gf128_mul(x, y):
    z = 0
    v = y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        if v & 1:
            v = (v >> 1) ^ _R
        else:
            v >>= 1
    return z


def _ghash(h, aad, ct):
    x = 0
    for i in range(0, len(aad), 16):
        block = aad[i:i + 16].ljust(16, b"\0")
        x = _gf128_mul(x ^ int.from_bytes(block, "big"), h)
    for i in range(0, len(ct), 16):
        block = ct[i:i + 16].ljust(16, b"\0")
        x = _gf128_mul(x ^ int.from_bytes(block, "big"), h)
    lengths = (len(aad) * 8).to_bytes(8, "big") + (len(ct) * 8).to_bytes(8, "big")
    return _gf128_mul(x ^ int.from_bytes(lengths, "big"), h)


def _gcm_ctr(rk, j0, data):
    counter = int.from_bytes(j0, "big")
    out = bytearray()
    for i in range(0, len(data), 16):
        counter = (counter + 1) & ((1 << 128) - 1)
        ks = _encrypt_block(rk, counter.to_bytes(16, "big"))
        out += bytes(a ^ b for a, b in zip(data[i:i + 16], ks))
    return bytes(out)


def gcm_decrypt(key, nonce, data):
    """AES-GCM 解密；data 末尾 16 字节为标签，校验失败抛 ValueError。"""
    if len(nonce) != 12:
        raise ValueError("GCM nonce 长度应为 12 字节")
    if len(data) < 16:
        raise ValueError("密文长度不足")
    ct, tag = data[:-16], data[-16:]
    rk = _expand_key(key)
    h = int.from_bytes(_encrypt_block(rk, b"\0" * 16), "big")
    j0 = nonce + b"\0\0\0\1"
    expect = bytes(
        a ^ b
        for a, b in zip(_encrypt_block(rk, j0), _ghash(h, b"", ct).to_bytes(16, "big"))
    )
    if not hmac.compare_digest(expect, tag):
        raise ValueError("GCM 标签校验失败，密钥或数据不匹配")
    return _gcm_ctr(rk, j0, ct)
