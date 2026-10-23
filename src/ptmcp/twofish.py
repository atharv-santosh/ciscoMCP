"""Twofish + EAX in pure Python, enough to read and write .pkt files."""
import struct

Q0 = [
    [0x8,0x1,0x7,0xD,0x6,0xF,0x3,0x2,0x0,0xB,0x5,0x9,0xE,0xC,0xA,0x4],
    [0xE,0xC,0xB,0x8,0x1,0x2,0x3,0x5,0xF,0x4,0xA,0x6,0x7,0x0,0x9,0xD],
    [0xB,0xA,0x5,0xE,0x6,0xD,0x9,0x0,0xC,0x8,0xF,0x3,0x2,0x4,0x7,0x1],
    [0xD,0x7,0xF,0x4,0x1,0x2,0x6,0xE,0x9,0xB,0x3,0x0,0x8,0x5,0xC,0xA],
]
Q1 = [
    [0x2,0x8,0xB,0xD,0xF,0x7,0x6,0xE,0x3,0x1,0x9,0x4,0x0,0xA,0xC,0x5],
    [0x1,0xE,0x2,0xB,0x4,0xC,0x3,0x7,0x6,0xD,0xA,0x5,0xF,0x9,0x0,0x8],
    [0x4,0xC,0x7,0x5,0x1,0x6,0x9,0xA,0x0,0xE,0xD,0x8,0x2,0xB,0x3,0xF],
    [0xB,0x9,0x5,0x1,0xC,0x3,0xD,0xE,0x6,0x4,0x7,0xF,0x2,0x0,0x8,0xA],
]


def _ror4(x, n):
    return ((x >> n) | (x << (4 - n))) & 0xF


def _q(t, x):
    a0, b0 = x >> 4, x & 0xF
    a1 = a0 ^ b0
    b1 = (a0 ^ _ror4(b0, 1) ^ (8 * a0)) & 0xF
    a2, b2 = t[0][a1], t[1][b1]
    a3 = a2 ^ b2
    b3 = (a2 ^ _ror4(b2, 1) ^ (8 * a2)) & 0xF
    a4, b4 = t[2][a3], t[3][b3]
    return (b4 << 4) | a4


QA = [_q(Q0, i) for i in range(256)]
QB = [_q(Q1, i) for i in range(256)]


def _gf(a, b, poly):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a <<= 1
        if a & 0x100:
            a ^= poly
        b >>= 1
    return r


MDS = [[0x01, 0xEF, 0x5B, 0x5B], [0x5B, 0xEF, 0xEF, 0x01], [0xEF, 0x5B, 0x01, 0xEF], [0xEF, 0x01, 0xEF, 0x5B]]
RS = [[0x01, 0xA4, 0x55, 0x87, 0x5A, 0x58, 0xDB, 0x9E], [0xA4, 0x56, 0x82, 0xF3, 0x1E, 0xC6, 0x68, 0xE5],
      [0x02, 0xA1, 0xFC, 0xC1, 0x47, 0xAE, 0x3D, 0x19], [0xA4, 0x55, 0x87, 0x5A, 0x58, 0xDB, 0x9E, 0x03]]


def _mds(y):
    out = 0
    for i in range(4):
        v = 0
        for j in range(4):
            v ^= _gf(MDS[i][j], y[j], 0x169)
        out |= v << (8 * i)
    return out


def _rol(x, n):
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _ror(x, n):
    return ((x >> n) | (x << (32 - n))) & 0xFFFFFFFF


def _h(x, L):
    return _mds(_hs(x, L))


def _hs(x, L):
    y = [(x >> (8 * i)) & 0xFF for i in range(4)]
    k = len(L)
    Lb = [[(w >> (8 * i)) & 0xFF for i in range(4)] for w in L]
    if k == 4:
        y = [QB[y[0]] ^ Lb[3][0], QA[y[1]] ^ Lb[3][1], QA[y[2]] ^ Lb[3][2], QB[y[3]] ^ Lb[3][3]]
    if k >= 3:
        y = [QB[y[0]] ^ Lb[2][0], QB[y[1]] ^ Lb[2][1], QA[y[2]] ^ Lb[2][2], QA[y[3]] ^ Lb[2][3]]
    y = [
        QB[QA[QA[y[0]] ^ Lb[1][0]] ^ Lb[0][0]],
        QA[QA[QB[y[1]] ^ Lb[1][1]] ^ Lb[0][1]],
        QB[QB[QA[y[2]] ^ Lb[1][2]] ^ Lb[0][2]],
        QA[QB[QB[y[3]] ^ Lb[1][3]] ^ Lb[0][3]],
    ]
    return y


class Twofish:
    def __init__(self, key: bytes):
        assert len(key) in (16, 24, 32)
        k = len(key) // 8
        words = struct.unpack("<%dI" % (2 * k), key)
        Me = [words[2 * i] for i in range(k)]
        Mo = [words[2 * i + 1] for i in range(k)]
        S = []
        for i in range(k):
            chunk = key[8 * i: 8 * i + 8]
            v = 0
            for r in range(4):
                b = 0
                for c in range(8):
                    b ^= _gf(RS[r][c], chunk[c], 0x14D)
                v |= b << (8 * r)
            S.insert(0, v)
        rho = 0x01010101
        self.K = []
        for i in range(20):
            A = _h((2 * i * rho) & 0xFFFFFFFF, Me)
            B = _rol(_h(((2 * i + 1) * rho) & 0xFFFFFFFF, Mo), 8)
            self.K.append((A + B) & 0xFFFFFFFF)
            self.K.append(_rol((A + 2 * B) & 0xFFFFFFFF, 9))
        # precompute key-dependent S-boxes as full 32-bit tables
        self.T = [[0] * 256 for _ in range(4)]
        for b in range(256):
            for i in range(4):
                y = [0, 0, 0, 0]
                y[i] = _hs(b * 0x01010101, S)[i]
                self.T[i][b] = _mds(y)

    def _g(self, x):
        T = self.T
        return T[0][x & 0xFF] ^ T[1][(x >> 8) & 0xFF] ^ T[2][(x >> 16) & 0xFF] ^ T[3][x >> 24]

    def encrypt_block(self, block: bytes) -> bytes:
        K = self.K
        r = list(struct.unpack("<4I", block))
        r = [r[i] ^ K[i] for i in range(4)]
        for rnd in range(16):
            t0 = self._g(r[0])
            t1 = self._g(_rol(r[1], 8))
            f0 = (t0 + t1 + K[2 * rnd + 8]) & 0xFFFFFFFF
            f1 = (t0 + 2 * t1 + K[2 * rnd + 9]) & 0xFFFFFFFF
            r2 = _ror(r[2] ^ f0, 1)
            r3 = _rol(r[3], 1) ^ f1
            r = [r2, r3, r[0], r[1]]
        out = [r[2] ^ K[4], r[3] ^ K[5], r[0] ^ K[6], r[1] ^ K[7]]
        return struct.pack("<4I", *out)


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def _dbl(b):
    n = int.from_bytes(b, "big") << 1
    if n >> 128:
        n ^= 0x87
    return (n & ((1 << 128) - 1)).to_bytes(16, "big")


class EAX:
    def __init__(self, key):
        self.c = Twofish(key)
        L = self.c.encrypt_block(bytes(16))
        self.B = _dbl(L)
        self.P = _dbl(self.B)

    def _cmac(self, data):
        blocks = [data[i:i + 16] for i in range(0, len(data), 16)] or [b""]
        last = blocks[-1]
        if len(last) == 16:
            last = _xor(last, self.B)
        else:
            last = _xor(last + b"\x80" + bytes(15 - len(last)), self.P)
        x = bytes(16)
        for b in blocks[:-1]:
            x = self.c.encrypt_block(_xor(x, b))
        return self.c.encrypt_block(_xor(x, last))

    def _omac(self, t, data):
        return self._cmac(bytes(15) + bytes([t]) + data)

    def _ctr(self, nonce_mac, data):
        ctr = int.from_bytes(nonce_mac, "big")
        out = bytearray()
        for i in range(0, len(data), 16):
            ks = self.c.encrypt_block(ctr.to_bytes(16, "big"))
            chunk = data[i:i + 16]
            out += _xor(chunk, ks[:len(chunk)])
            ctr = (ctr + 1) & ((1 << 128) - 1)
        return bytes(out)

    def encrypt(self, nonce, plaintext, header=b""):
        n = self._omac(0, nonce)
        h = self._omac(1, header)
        c = self._ctr(n, plaintext)
        tag = _xor(_xor(n, h), self._omac(2, c))
        return c + tag

    def decrypt(self, nonce, data, header=b"", verify=True):
        c, tag = data[:-16], data[-16:]
        n = self._omac(0, nonce)
        if verify:
            h = self._omac(1, header)
            exp = _xor(_xor(n, h), self._omac(2, c))
            if exp != tag:
                raise ValueError("EAX tag mismatch")
        return self._ctr(n, c)
