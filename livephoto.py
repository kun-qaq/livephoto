#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LivePhoto 制作工具 v2.0
=========================================================================
把一段视频「空间裁剪 + 时间截取(0.5~5s) + 变速/旋转/倒放/静音」后
转成 iPhone 实况图(Live Photo)。支持批量、无损模式、.livp 打包。

输出: <脚本目录>/output/ 下一对同名文件
        NAME.JPG   <- 封面(已写入 Apple MakerNote 0x0011 = ContentIdentifier)
        NAME.MOV   <- 视频(已写入 com.apple.quicktime.content.identifier = 同一 UUID)
        NAME.livp  <- (可选) 两文件打包成单文件, 方便分享

导入 iPhone: 两个文件必须同名、一起传(隔空投送 / 文件 App 放同一文件夹)。
             微信、QQ 会剥掉元数据，传过去就散了，别用它们传。

子命令:
  (无参数)      图形界面(拖拽框选裁剪 / 时间轴选帧 / 批量)
  info          查看视频或实况图信息
  verify        校验一组(或多组)实况图配对是否有效
  unlivp        把 .livp 解开为 JPG + MOV
  pair          已有封面图 + 视频 直接配对(零重编码)

技术说明
-------------------------------------------------------------------------
视频处理(裁剪/截取/变速等)用 ffmpeg; 元数据写入参考开源项目
video2livephoto (https://github.com/969246694/video2livephoto):
  * 图片侧: 在 JPEG 的 Exif MakerNote(0x927C) 写入 Apple MakerNote,
            其中 tag 0x0011 = ContentIdentifier(UUID)
  * 视频侧: 在 MOV 的 moov/meta 写入 com.apple.quicktime.content.identifier
            = 同一 UUID
  仅靠这一配对标识,「照片」App 就能识别为实况图(长按播放 / 设为实况壁纸)。
  无损模式: 不裁剪/不变速/不旋转/全长 0.5~5s 时, 视频数据逐字节搬运零重编码。
  注: 真机拍的实况图还有 still-image-time 运动轨, 本工具不伪造它,
      不影响识别与播放, 仅锁屏动态壁纸的起止位置按默认处理。

ffmpeg 来源: 优先用 PATH 里的 ffmpeg; 否则用 imageio-ffmpeg 自带的二进制
(无需单独安装, 首次运行会自动下载)。
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import uuid as uuidlib
from pathlib import Path

VERSION = "2.2.0"

def _app_dir():
    """程序所在目录: PyInstaller 冻结时为 exe 所在目录, 否则为脚本目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

# ===========================================================================
# 0. ffmpeg 解析
# ===========================================================================

def get_ffmpeg_exe():
    """返回可用的 ffmpeg 可执行文件路径(支持 PyInstaller 冻结环境)。"""
    p = shutil.which("ffmpeg")
    if p:
        return p
    # PyInstaller 冻结环境: 先看 exe 同目录, 再看解包目录里的 imageio_ffmpeg
    if getattr(sys, "frozen", False):
        for base in (Path(sys.executable).parent, getattr(sys, "_MEIPASS", "")):
            if base:
                for cand in (Path(base) / "ffmpeg.exe",
                             Path(base) / "imageio_ffmpeg" / "binaries" / "ffmpeg.exe"):
                    if cand.is_file():
                        return str(cand)
                if Path(base, "imageio_ffmpeg", "binaries").is_dir():
                    for f in Path(base, "imageio_ffmpeg", "binaries").glob("ffmpeg-*.exe"):
                        return str(f)
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    raise RuntimeError(
        "找不到 ffmpeg。请二选一:\n"
        "  1) 把本程序与 ffmpeg.exe 放在同一目录\n"
        "  2) 把 ffmpeg.exe 所在目录加到系统 PATH\n"
        "然后重新运行。"
    )


def _run_ffmpeg(args, log_fn=print):
    exe = get_ffmpeg_exe()
    cmd = [exe] + [str(a) for a in args]
    log_fn("FFMPEG> " + " ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg 执行失败:\n" + (proc.stderr or "")[-2500:])
    return proc


def probe_video(path):
    """用 ffmpeg 读视频信息: 时长 / 分辨率 / 帧率 / 编码 / 有无音频。"""
    exe = get_ffmpeg_exe()
    proc = subprocess.run([exe, "-hide_banner", "-i", str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    txt = proc.stderr
    duration = width = height = fps = codec = None
    has_audio = False
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
    if m:
        h_, m_, s_ = map(float, m.groups())
        duration = h_ * 3600 + m_ * 60 + s_
    for line in txt.splitlines():
        if "Stream" in line and "Video:" in line:
            mw = re.search(r"(\d{2,})x(\d{2,})", line)
            if mw:
                width, height = int(mw.group(1)), int(mw.group(2))
            mf = re.search(r"(\d+(?:\.\d+)?)\s*fps", line)
            if mf:
                fps = float(mf.group(1))
            mc = re.search(r"Video:\s*([A-Za-z0-9_]+)", line)
            if mc:
                codec = mc.group(1)
        if "Stream" in line and "Audio:" in line:
            has_audio = True
    return {"duration": duration, "width": width, "height": height,
            "fps": fps, "codec": codec, "has_audio": has_audio}


# ===========================================================================
# 1. ISOBMFF (MP4 / QuickTime) 盒子解析 / 序列化  —— 用于无损注入配对标识
#    (方案参考 video2livephoto, 纯 Python, 无第三方依赖)
# ===========================================================================

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"dinf", b"edts",
              b"udta", b"mvex", b"moof", b"traf", b"mfra", b"wave", b"gmhd",
              b"ilst", b"ipro", b"sinf", b"schi"}
FULL_CONTAINERS = {b"meta"}
INDEXED_CONTAINERS = {b"ilst"}


def U16(b, o=0):
    return int.from_bytes(b[o:o + 2], "big")


def U32(b, o=0):
    return int.from_bytes(b[o:o + 4], "big")


def U64(b, o=0):
    return int.from_bytes(b[o:o + 8], "big")


class Box:
    """ISOBMFF 盒子。children 非 None 即容器, 否则用 payload。"""
    __slots__ = ("type", "payload", "children", "full", "ver", "flags", "src")

    def __init__(self, btype, payload=b"", children=None, full=False, ver=0, flags=0):
        self.type = btype
        self.payload = payload
        self.children = children
        self.full = full
        self.ver = ver
        self.flags = flags
        self.src = None

    def body(self):
        head = (struct.pack(">B", self.ver) + self.flags.to_bytes(3, "big")) if self.full else b""
        if self.children is not None:
            return head + b"".join(c.serialize() for c in self.children)
        return head + self.payload

    def serialize(self):
        b = self.body()
        n = len(b) + 8
        if n > 0xFFFFFFFF:
            return struct.pack(">I", 1) + self.type + struct.pack(">Q", n + 8) + b
        return struct.pack(">I", n) + self.type + b

    def get(self, btype):
        for c in (self.children or ()):
            if c.type == btype:
                return c
        return None

    def path(self, *types):
        node = self
        for t in types:
            node = node.get(t)
            if node is None:
                return None
        return node

    def all(self, btype):
        return [c for c in (self.children or ()) if c.type == btype]

    def walk(self):
        yield self
        for c in (self.children or ()):
            yield from c.walk()


def _read_header(data, pos, end):
    if pos + 8 > end:
        return None
    size = U32(data, pos)
    btype = data[pos + 4:pos + 8]
    hdr = 8
    if size == 1:
        if pos + 16 > end:
            return None
        size = U64(data, pos + 8)
        hdr = 16
    elif size == 0:
        size = end - pos
    if size < hdr or pos + size > end:
        return None
    return btype, hdr, size


def parse(data, start=0, end=None, depth=0):
    if end is None:
        end = len(data)
    boxes = []
    pos = start
    while True:
        h = _read_header(data, pos, end)
        if h is None:
            break
        btype, hdr, size = h
        pstart, pend = pos + hdr, pos + size
        box = Box(btype)
        box.src = (pos, size, hdr)
        if btype in CONTAINERS or btype in FULL_CONTAINERS:
            full = btype in FULL_CONTAINERS
            off = pstart
            if full:
                if off + 4 <= pend:
                    box.full = True
                    box.ver = data[off]
                    box.flags = U32(b"\0" + data[off + 1:off + 4])
                    off += 4
                else:
                    full = False
            kids = parse(data, off, pend, depth + 1) if depth < 8 else []
            if kids and sum(k.src[1] for k in kids) == pend - off:
                if btype in INDEXED_CONTAINERS:
                    for k in kids:
                        o = k.src[0] + k.src[2]
                        sub = parse(data, o, o + k.src[1] - k.src[2], depth + 2)
                        if sub and sum(s.src[1] for s in sub) == k.src[1] - k.src[2]:
                            k.children = sub
                box.children = kids
            else:
                box.children = None
                box.full = False
                box.payload = data[pstart:pend]
        else:
            box.payload = data[pstart:pend]
        boxes.append(box)
        pos = pend
        if pos >= end:
            break
    return boxes


def load(path):
    with open(path, "rb") as f:
        data = f.read()
    return data, parse(data)


def clone_box(box):
    return parse(box.serialize())[0]


def leaf(btype, payload=b"", **kw):
    return Box(btype, payload=payload, **kw)


def container(btype, children, **kw):
    return Box(btype, children=list(children), **kw)


def full_leaf(btype, payload=b"", ver=0, flags=0):
    return Box(btype, payload=payload, full=True, ver=ver, flags=flags)


# 2. 轨道信息 / 样本表

HANDLER_NAME = {b"vide": "video", b"soun": "audio", b"mdta": "metadata",
                b"meta": "metadata", b"text": "text", b"sbtl": "subtitle"}


class Track:
    def __init__(self, trak):
        self.trak = trak
        self.mdia = trak.path(b"mdia")
        self.minf = trak.path(b"mdia", b"minf")
        self.stbl = trak.path(b"mdia", b"minf", b"stbl")
        hdlr = self.mdia.get(b"hdlr") if self.mdia else None
        self.handler = hdlr.payload[8:12] if hdlr else b"????"
        self.kind = HANDLER_NAME.get(self.handler, self.handler.decode("latin1", "replace"))
        mdhd = self.mdia.get(b"mdhd") if self.mdia else None
        self.timescale = self.duration = 0
        if mdhd:
            p = mdhd.payload
            if mdhd.ver == 1:
                self.timescale, self.duration = U32(p, 20), U64(p, 24)
            else:
                self.timescale, self.duration = U32(p, 12), U32(p, 16)
        self.width = self.height = 0
        tkhd = trak.get(b"tkhd")
        if tkhd:
            p = tkhd.payload
            self.width = U32(p, len(p) - 8) / 65536.0
            self.height = U32(p, len(p) - 4) / 65536.0
        self.codec = "?"
        stsd = self.stbl.get(b"stsd") if self.stbl else None
        if stsd and len(stsd.payload) >= 16:
            self.codec = stsd.payload[12:16].decode("latin1", "replace")
        self.samples = 0
        stsz = self.stbl.get(b"stsz") if self.stbl else None
        if stsz:
            self.samples = U32(stsz.payload, 8)
        self.seconds = self.duration / self.timescale if self.timescale else 0.0
        self.fps = self.samples / self.seconds if self.seconds else 0.0
        self.track_id = U32(tkhd.payload, 12) if tkhd else 0

    def sample_sizes(self):
        stsz = self.stbl.get(b"stsz")
        if stsz is None:
            return []
        p = stsz.payload
        uniform, count = U32(p, 4), U32(p, 8)
        if uniform:
            return [uniform] * count
        return [U32(p, 12 + 4 * i) for i in range(count)]

    def chunk_offsets(self):
        stco = self.stbl.get(b"stco") or self.stbl.get(b"co64")
        if stco is None:
            return [], False
        p = stco.payload
        cnt = U32(p, 4)
        if stco.type == b"co64":
            return [U64(p, 8 + 8 * i) for i in range(cnt)], True
        return [U32(p, 8 + 4 * i) for i in range(cnt)], False

    def sample_ranges(self):
        stsc = self.stbl.get(b"stsc")
        sizes = self.sample_sizes()
        chunks, _ = self.chunk_offsets()
        if stsc is None or not chunks:
            return []
        sp = stsc.payload
        n = U32(sp, 4)
        ent = [(U32(sp, 8 + 12 * i), U32(sp, 12 + 12 * i)) for i in range(n)]
        per_chunk = []
        for i, (first, spc) in enumerate(ent):
            last = (ent[i + 1][0] - 1) if i + 1 < len(ent) else len(chunks)
            per_chunk += [spc] * max(0, last - first + 1)
        while len(per_chunk) < len(chunks):
            per_chunk.append(per_chunk[-1] if per_chunk else 0)
        out, si = [], 0
        for ci, off in enumerate(chunks):
            cur = off
            for _ in range(per_chunk[ci]):
                if si >= len(sizes):
                    return out
                out.append((cur, sizes[si]))
                cur += sizes[si]
                si += 1
        return out

    def __repr__(self):
        return "<%s %s %.2fs %dx%d fps=%.1f>" % (
            self.kind, self.codec, self.seconds, self.width, self.height, self.fps)


def tracks_of(moov):
    return [Track(t) for t in moov.all(b"trak")]


# 3. Live Photo 元数据构造

KEY_CONTENT_ID = "com.apple.quicktime.content.identifier"
DATA_UTF8 = 1
DATA_UNDEFINED = 0


def make_hdlr(handler, name=b"\x00"):
    return full_leaf(b"hdlr", struct.pack(">I", 0) + handler + b"\x00" * 12 + name)


def make_keys(keys):
    body = struct.pack(">I", len(keys))
    for k in keys:
        kb = k.encode("utf-8")
        body += struct.pack(">I", len(kb) + 8) + b"mdta" + kb
    return full_leaf(b"keys", body)


def make_ilst(items):
    kids = []
    for idx, flags, value in items:
        kids.append(container(struct.pack(">I", idx),
                              [full_leaf(b"data", value, flags=flags)]))
    return container(b"ilst", kids)


def make_moov_meta(content_id, extra_keys=()):
    keys = [KEY_CONTENT_ID] + list(extra_keys)
    items = [(1, DATA_UTF8, content_id.encode("ascii"))]
    return container(b"meta", [make_hdlr(b"mdta"), make_keys(keys), make_ilst(items)],
                     full=True)


def make_ftyp():
    return leaf(b"ftyp", b"qt  " + struct.pack(">I", 0x00000200) + b"qt  ")


# --- Apple MakerNote: 图片侧的配对标识 (tag 0x0011) --------------------------

def build_makernote(content_id):
    """构造 Apple MakerNote 数据块(结构同 exiftool 的 Apple 定义)。"""
    s = content_id.encode("ascii") + b"\x00"
    ifd_off = 14
    value_off = ifd_off + 2 + 12 + 4          # = 32
    ifd = struct.pack(">H", 1)                                  # 条目数
    ifd += struct.pack(">HHI", 0x0011, 2, len(s))               # tag/type(ASCII)/count
    ifd += struct.pack(">I", value_off)                         # 值偏移
    ifd += struct.pack(">I", 0)                                 # 下一个 IFD = 无
    return b"Apple iOS\x00\x00\x01MM" + ifd + s


def build_exif_app1(content_id):
    """生成完整的 APP1/Exif 段: IFD0 里只放 MakerNote(0x927C)。

    修正: APP1 长度字段应为 6 + 负载长度 (Exif\\0\\0 占 6 字节, 不含长度字段自身)。
    """
    mn = build_makernote(content_id)
    tiff = b"MM\x00\x2a" + struct.pack(">I", 8)                 # TIFF 头, IFD0 在 +8
    mn_off = 8 + 2 + 12 + 4                                     # = 26
    ifd = struct.pack(">H", 1)
    ifd += struct.pack(">HHI", 0x927C, 7, len(mn))              # UNDEFINED
    ifd += struct.pack(">I", mn_off)
    ifd += struct.pack(">I", 0)
    payload = tiff + ifd + mn
    return (b"\xff\xe1" + struct.pack(">H", 6 + len(payload))
            + b"Exif\x00\x00" + payload)


def read_apple_makernote(jpeg_bytes):
    """把写进去的 ContentIdentifier 读回来(自检用)。"""
    i = jpeg_bytes.find(b"Apple iOS\x00\x00\x01MM")
    if i < 0:
        return None
    p = i + 14
    n = U16(jpeg_bytes, p)
    for k in range(n):
        e = p + 2 + 12 * k
        if U16(jpeg_bytes, e) == 0x0011:
            cnt = U32(jpeg_bytes, e + 4)
            off = U32(jpeg_bytes, e + 8)
            raw = jpeg_bytes[i + off:i + off + cnt]
            return raw.rstrip(b"\x00").decode("ascii", "replace")
    return None


def jpeg_segments(data):
    if data[:2] != b"\xff\xd8":
        raise ValueError("不是 JPEG 文件")
    out = []
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m == 0xDA:
            return out, i
        ln = U16(data, i + 2)
        out.append((m, data[i + 4:i + 2 + ln]))
        i += 2 + ln
    raise ValueError("JPEG 结构异常: 找不到 SOS")


def inject_exif(jpeg_bytes, content_id, keep_existing=False):
    """把实况图 EXIF 注入 JPEG。默认丢弃旧 APP1/Exif(视频抽帧本来就没有)。"""
    segs, sos_at = jpeg_segments(jpeg_bytes)
    body = b""
    lead = b""
    for m, payload in segs:
        if m == 0xE1 and payload[:6] == b"Exif\x00\x00" and not keep_existing:
            continue
        raw = b"\xff" + bytes([m]) + struct.pack(">H", len(payload) + 2) + payload
        if m == 0xE0 and not lead:
            lead = raw
        else:
            body += raw
    return (b"\xff\xd8" + lead + build_exif_app1(content_id) + body
            + jpeg_bytes[sos_at:])


# 4. 无损重封装引擎 (注入配对标识)

class LivePhotoError(Exception):
    pass


def remux(src_path, out_path, content_id, verify=True):
    """把 MP4 变成 Live Photo 用的 .MOV —— 视频采样数据一字节都不改。"""
    data, top = load(src_path)
    ftyp = next((b for b in top if b.type == b"ftyp"), None)
    moov_src = next((b for b in top if b.type == b"moov"), None)
    if moov_src is None:
        raise LivePhotoError("文件里没有 moov, 可能不是 MP4/MOV")
    if moov_src.get(b"mvex") is not None:
        raise LivePhotoError("这是分片 MP4 (fMP4), 请先用 ffmpeg -c copy 转成普通 MP4")

    mdat_boxes = [b for b in top if b.type == b"mdat"]
    if not mdat_boxes:
        raise LivePhotoError("文件里没有 mdat(可能视频数据缺失)")

    moov = clone_box(moov_src)
    if moov.children is not None:
        moov.children = [c for c in moov.children if c.type != b"meta"]
        moov.children.append(make_moov_meta(content_id))

    out_ftyp = make_ftyp()
    moov_size = len(moov.serialize())
    base = len(out_ftyp.serialize()) + moov_size + 8

    ranges = []
    cur = base
    for b in mdat_boxes:
        off, size, hdr = b.src
        plen = size - hdr
        ranges.append((off + hdr, plen, cur))
        cur += plen

    data_off = base
    for box in moov.walk():
        if box.type in (b"stco", b"co64"):
            wide = box.type == b"co64"
            cnt = U32(box.payload, 4)
            body = box.payload[:4] + struct.pack(">I", cnt)
            for i in range(cnt):
                old = U64(box.payload, 8 + 8 * i) if wide else U32(box.payload, 8 + 4 * i)
                new = None
                for o_start, o_len, n_start in ranges:
                    if o_start <= old < o_start + o_len:
                        new = n_start + (old - o_start)
                        break
                if new is None:
                    raise LivePhotoError("样本偏移 0x%X 不在 mdat 内" % old)
                if not wide and new > 0xFFFFFFFF:
                    raise LivePhotoError("超过 4 GiB, 需要 co64(暂不支持)")
                body += struct.pack(">Q", new) if wide else struct.pack(">I", new)
            box.payload = body

    if len(moov.serialize()) != moov_size:
        raise LivePhotoError("内部错误: moov 大小在回填偏移后发生变化")

    with open(out_path, "wb") as f:
        f.write(out_ftyp.serialize())
        f.write(moov.serialize())
        f.write(struct.pack(">I", sum(r[1] for r in ranges) + 8) + b"mdat")
        for o_start, o_len, _n in ranges:
            f.write(data[o_start:o_start + o_len])

    result = {"bytes_in": len(data), "bytes_out": os.path.getsize(out_path)}
    if verify:
        result["verified"] = verify_lossless(src_path, out_path)
        if not result["verified"]:
            raise LivePhotoError("校验失败: 输出文件的采样数据与源文件不一致")
    return result


def verify_lossless(src_path, dst_path):
    _s, stop = load(src_path)
    _d, dtop = load(dst_path)
    smoov = next((b for b in stop if b.type == b"moov"), None)
    dmoov = next((b for b in dtop if b.type == b"moov"), None)
    if not smoov or not dmoov:
        return False
    sd = open(src_path, "rb").read()
    dd = open(dst_path, "rb").read()
    stracks = {t.track_id: t for t in tracks_of(smoov)}
    for dt in tracks_of(dmoov):
        st = stracks.get(dt.track_id)
        if st is None:
            continue
        sr, dr = st.sample_ranges(), dt.sample_ranges()
        if len(sr) != len(dr):
            return False
        for (so, ss), (do, ds) in zip(sr, dr):
            if ss != ds or sd[so:so + ss] != dd[do:do + ds]:
                return False
    return True


# ===========================================================================
# 5. 裁剪 / 截取 预处理 (ffmpeg)
# ===========================================================================

def compute_crop(preset, w, h, custom):
    """根据预设算裁剪框 (x, y, cw, ch), 返回 None 表示不裁剪。"""
    if not preset or preset == "none":
        return None
    if preset == "square":
        s = min(w, h)
        return ((w - s) // 2, (h - s) // 2, s, s)
    if preset == "portrait":  # 9:16 竖屏
        if w / h > 9 / 16:
            nw, nh = int(round(h * 9 / 16)), h
        else:
            nw, nh = w, int(round(w * 16 / 9))
        return ((w - nw) // 2, (h - nh) // 2, nw, nh)
    if preset == "custom":
        if not custom:
            return None
        x, y, cw, ch = custom
        cw = min(cw, w - x)
        ch = min(ch, h - y)
        if cw <= 0 or ch <= 0:
            return None
        return (max(0, x), max(0, y), cw, ch)
    return None


def _even(v):
    return max(2, v & ~1)


def _atempo_chain(speed):
    """把任意速度拆成 atempo 因子链(单个 atempo 只支持 0.5~2.0)。"""
    factors = []
    s = float(speed)
    while s > 2.0:
        factors.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        factors.append("atempo=0.5")
        s /= 0.5
    factors.append(f"atempo={round(s, 4)}")
    return factors


def preprocess(input_path, tmp_mp4, start, duration, crop, speed=1.0, rotate=0,
               mute=False, reverse=False, crf=18, log_fn=print):
    """裁剪 + 截取 + 调整(变速/旋转/倒放/静音), 重编码为 H.264 / yuv420p。

    duration 指输入时间轴上的截取长度(输出时长 = duration / speed)。
    """
    args = ["-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(input_path)]
    filters = []
    if crop:
        x, y, cw, ch = (_even(crop[0]), _even(crop[1]), _even(crop[2]), _even(crop[3]))
        x = min(x, crop[0]); y = min(y, crop[1])
        # 确保不超出画面
        cw = min(cw, crop[0] + crop[2] - x)
        ch = min(ch, crop[1] + crop[3] - y)
        filters.append(f"crop={cw}:{ch}:{x}:{y}")
    if rotate == 90:
        filters.append("transpose=1")
    elif rotate == 180:
        filters.append("transpose=1,transpose=1")
    elif rotate == 270:
        filters.append("transpose=2")
    if reverse:
        filters.append("reverse")
    if speed and abs(speed - 1.0) > 1e-6:
        filters.append(f"setpts=PTS/{speed}")
    if filters:
        args += ["-vf", ",".join(filters)]
    afilters = []
    if not mute:
        if speed and abs(speed - 1.0) > 1e-6:
            afilters.extend(_atempo_chain(speed))
        if reverse:
            afilters.append("areverse")
    if afilters:
        args += ["-af", ",".join(afilters)]
    if mute:
        args += ["-an"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)),
             "-pix_fmt", "yuv420p", "-tag:v", "avc1", "-movflags", "+faststart",
             "-vsync", "cfr", str(tmp_mp4)]
    _run_ffmpeg(args, log_fn)
    return tmp_mp4


def extract_cover(video_path, out_jpg, time_sec, log_fn=print):
    """从视频指定时间点抽一帧做封面(HQ JPEG)。

    -strict unofficial: ffmpeg 7.x 对 limited-range yuv420p -> mjpeg 会报
    "Non full-range YUV is non-standard", 放宽合规等级即可正常编码。
    """
    _run_ffmpeg(["-y", "-ss", f"{time_sec:.3f}", "-i", str(video_path),
                 "-frames:v", "1", "-q:v", "2", "-strict", "unofficial",
                 str(out_jpg)], log_fn)
    return out_jpg


# ===========================================================================
# 6. 主转换流程
# ===========================================================================

def safe_name(path):
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    bad = '<>:"/\\|?*'
    stem = "".join(c for c in stem if c not in bad).strip() or "LIVE"
    return stem


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv",
              ".m4v", ".mpg", ".mpeg"}


def package_livp(jpg_path, mov_path, livp_path):
    """把 JPG+MOV 打包成单个 .livp(本质是 zip), 方便分享。

    iOS「文件」App 能把 .livp 直接识别为实况图; 内部用同名成对存放。
    """
    import zipfile
    base = os.path.splitext(os.path.basename(str(jpg_path)))[0]
    with zipfile.ZipFile(str(livp_path), "w", zipfile.ZIP_DEFLATED) as z:
        z.write(str(jpg_path), f"{base}.JPG")
        z.write(str(mov_path), f"{base}.MOV")
    return str(livp_path)


def _open_dir(path):
    """在系统文件管理器里打开目录。"""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception:
        return False


def convert(input_path, out_dir, name=None, start=0.0, duration=3.0,
            crop_preset="none", crop_custom=None, cover_t=None, cover_rel=0.5,
            speed=1.0, rotate=0, mute=False, reverse=False, crf=18,
            lossless=False, quality=97, log_fn=print, verify=True,
            livp=False, open_folder=False):
    """完整转换。返回 (jpg_path, mov_path, content_id, livp_path)。

    cover_t: 封面帧在源视频时间轴上的绝对时间(秒); None 则用 cover_rel(0~1)。
    speed:   播放速度(输出时长 = duration, 输入截取 = duration*speed)。
    lossless: 无损模式; 条件不满足时自动回退重编码并提示。
    """
    input_path = Path(input_path)
    if not input_path.is_file():
        raise FileNotFoundError(f"找不到输入文件: {input_path}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    speed = max(0.25, min(4.0, float(speed)))
    info = probe_video(input_path)
    vdur = info["duration"] or 0.0
    vw, vh = info["width"] or 0, info["height"] or 0
    log_fn(f"源视频: {input_path.name}  {vw}x{vh}  {vdur:.2f}s  编码 {info.get('codec') or '?'}"
           + ("  (含音频)" if info.get("has_audio") else "  (无音频)"))

    # 时长约束: 输出时长 0.5~5s
    duration = float(duration)
    duration = max(0.5, min(5.0, duration))
    if vdur and start + duration * speed > vdur:
        start = max(0.0, vdur - duration * speed)
        log_fn(f"提示: 截取超出视频长度, 已把起点调整为 {start:.2f}s")
    start = max(0.0, float(start))

    crop = compute_crop(crop_preset, vw, vh, crop_custom)

    # ---- 无损模式判定 ----
    use_lossless = False
    if lossless:
        reasons = []
        if crop:
            reasons.append("需要裁剪")
        if abs(speed - 1.0) > 1e-6:
            reasons.append("需要变速")
        if rotate:
            reasons.append("需要旋转")
        if mute:
            reasons.append("需要静音")
        if reverse:
            reasons.append("需要倒放")
        if start > 0.01:
            reasons.append(f"起点不为0(从 {start:.2f}s 开始)")
        if vdur and vdur > 5.05:
            reasons.append(f"视频全长 {vdur:.1f}s 超过 5s")
        if reasons:
            log_fn("提示: 无损模式不可用(" + "; ".join(reasons) + "), 自动回退重编码")
        else:
            use_lossless = True
            if (info.get("codec") or "").lower() not in ("h264",):
                log_fn(f"提示: 视频编码是 {info.get('codec')}, 无损直通; "
                       f"iOS 对非 H.264 的实况图兼容性可能略差")

    name = name or safe_name(input_path)
    jpg_path = out_dir / f"{name}.JPG"
    mov_path = out_dir / f"{name}.MOV"

    if use_lossless:
        clip = input_path
        clip_dur = vdur
        log_fn("步骤1/3: 无损模式 —— 视频不重编码, 逐字节搬运")
    else:
        tmp_mp4 = out_dir / "_tmp_clip.mp4"
        in_dur = duration * speed if abs(speed - 1.0) > 1e-6 else duration
        adj = []
        if abs(speed - 1.0) > 1e-6:
            adj.append(f"{speed}x")
        if rotate:
            adj.append(f"旋转{rotate}°")
        if reverse:
            adj.append("倒放")
        if mute:
            adj.append("静音")
        log_fn("步骤1/3: ffmpeg 裁剪+截取" + ("(" + "+".join(adj) + ")") * bool(adj)
               + " (重编码 H.264) ...")
        preprocess(input_path, tmp_mp4, start, in_dur, crop, speed=speed,
                   rotate=rotate, mute=mute, reverse=reverse, crf=crf, log_fn=log_fn)
        clip = tmp_mp4
        clip_dur = duration

    # ---- 封面帧时间(换算到 clip 时间轴) ----
    if cover_t is not None:
        if use_lossless:
            out_t = float(cover_t)
        else:
            out_t = (float(cover_t) - start) / speed
        out_t = max(0.0, min(max(0.0, clip_dur - 0.05), out_t))
    else:
        out_t = clip_dur * cover_rel
    tmp_cover = out_dir / "_tmp_cover.jpg"
    log_fn(f"步骤2/3: 抽取封面帧 (t={out_t:.2f}s) ...")
    extract_cover(clip, tmp_cover, out_t, log_fn)

    cid = str(uuidlib.uuid4()).upper()
    with open(tmp_cover, "rb") as f:
        cover_bytes = f.read()
    cover_bytes = inject_exif(cover_bytes, cid, keep_existing=False)
    with open(jpg_path, "wb") as f:
        f.write(cover_bytes)
    if read_apple_makernote(cover_bytes) != cid:
        raise LivePhotoError("内部错误: 封面写入的 ContentIdentifier 读回不一致")
    log_fn(f"封面已写: {jpg_path.name}  (MakerNote 0x0011 = {cid})")

    log_fn("步骤3/3: 视频注入 com.apple.quicktime.content.identifier ...")
    remux(str(clip), str(mov_path), cid, verify=verify)
    log_fn(f"视频已写: {mov_path.name}")

    # 清理临时文件
    tmp_files = (tmp_cover,) if use_lossless else (out_dir / "_tmp_clip.mp4", tmp_cover)
    for t in tmp_files:
        try:
            t.unlink()
        except OSError:
            pass

    log_fn("-" * 60)
    log_fn(f"配对 ID : {cid}")
    log_fn(f"封面图  : {jpg_path}  ({os.path.getsize(jpg_path)/1048576:.2f} MB)")
    log_fn(f"视频    : {mov_path}  ({os.path.getsize(mov_path)/1048576:.2f} MB)")
    if use_lossless:
        log_fn("无损    : 是 —— 视频数据与源文件逐字节一致")
    log_fn("把这两个文件一起导入 iPhone 即可识别为实况图。")
    livp_path = None
    if livp:
        livp_path = out_dir / f"{name}.livp"
        package_livp(jpg_path, mov_path, livp_path)
        log_fn(f"已打包  : {livp_path.name}  ({os.path.getsize(livp_path)/1048576:.2f} MB)")
    if open_folder:
        _open_dir(out_dir)
    return str(jpg_path), str(mov_path), cid, livp_path


# ===========================================================================
# 6b. 实况图读取 / 校验 / 直接配对
# ===========================================================================

def mov_content_id_from_bytes(data):
    """从 MOV/MP4 字节里读 com.apple.quicktime.content.identifier。"""
    top = parse(data)
    moov = next((b for b in top if b.type == b"moov"), None)
    if moov is None:
        return None
    meta = moov.get(b"meta")
    if meta is None:
        return None
    ilst = meta.get(b"ilst")
    if ilst is None or not ilst.children:
        return None
    for it in ilst.children:
        try:
            idx = U32(it.type, 0)
        except Exception:
            continue
        if idx == 1:
            d = it.get(b"data")
            if d is not None:
                return d.payload[4:].decode("ascii", "replace").rstrip("\x00")
    return None


def mov_content_id(path):
    with open(path, "rb") as f:
        return mov_content_id_from_bytes(f.read())


def jpg_content_id(path):
    with open(path, "rb") as f:
        return read_apple_makernote(f.read())


def pair(photo_path, video_path, out_dir, name=None, livp=False,
         log_fn=print, open_folder=False):
    """已有封面图 + 视频 -> 实况图 (全程零重编码)。"""
    photo_path = Path(photo_path)
    video_path = Path(video_path)
    if not photo_path.is_file():
        raise FileNotFoundError(f"找不到图片: {photo_path}")
    if not video_path.is_file():
        raise FileNotFoundError(f"找不到视频: {video_path}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(photo_path, "rb") as f:
        blob = f.read()
    if blob[:2] != b"\xff\xd8":
        from PIL import Image
        img = Image.open(io.BytesIO(blob)).convert("RGB")
        bio = io.BytesIO()
        img.save(bio, "JPEG", quality=97)
        blob = bio.getvalue()
        log_fn("封面已转换为 JPEG")

    info = probe_video(video_path)
    vdur = info["duration"] or 0.0
    if (info.get("codec") or "").lower() not in ("h264",):
        log_fn(f"提示: 视频编码是 {info.get('codec')}, 建议 H.264, "
               f"其他编码可能能导入但兼容性差")
    if vdur > 5.05:
        log_fn(f"提示: 视频 {vdur:.1f}s 超过 5s, 可导入但不太像标准实况图")

    name = name or safe_name(video_path)
    jpg_path = out_dir / f"{name}.JPG"
    mov_path = out_dir / f"{name}.MOV"
    cid = str(uuidlib.uuid4()).upper()

    blob = inject_exif(blob, cid, keep_existing=False)
    with open(jpg_path, "wb") as f:
        f.write(blob)
    if read_apple_makernote(blob) != cid:
        raise LivePhotoError("内部错误: 封面 ContentIdentifier 写入失败")
    log_fn(f"封面已写: {jpg_path.name}  (MakerNote 0x0011 = {cid})")

    remux(str(video_path), str(mov_path), cid)
    log_fn(f"视频已写: {mov_path.name}  (content.identifier = {cid})")
    log_fn(f"配对 ID : {cid}  (全程零重编码)")

    livp_path = None
    if livp:
        livp_path = out_dir / f"{name}.livp"
        package_livp(jpg_path, mov_path, livp_path)
        log_fn(f"已打包  : {livp_path.name}")
    if open_folder:
        _open_dir(out_dir)
    return str(jpg_path), str(mov_path), cid, livp_path


# ===========================================================================
# 7. 命令行
# ===========================================================================

def build_parser():
    p = argparse.ArgumentParser(
        prog="livephoto", description="视频 -> iPhone 实况图 (裁剪 + 截取 0.5~5s)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python livephoto.py 视频.mp4
  python livephoto.py 视频.mp4 -o output -n IMG_0001
  python livephoto.py 视频.mp4 --start 2 --duration 3 --crop portrait
  python livephoto.py 视频.mp4 --crop custom --cx 100 --cy 50 --cw 720 --ch 1280
  python livephoto.py 视频.mp4 --speed 0.5 --reverse --mute --livp
  python livephoto.py 短视频.mp4 --lossless            (不重编码, 需满足条件)
  python livephoto.py --batch 视频文件夹 -p "IMG_{n:04d}"
  python livephoto.py info output/TEST.JPG output/TEST.MOV
  python livephoto.py verify output/
  python livephoto.py unlivp 某实况图.livp
  python livephoto.py pair 封面.jpg 视频.mp4
  (不带参数直接运行会打开图形界面: 拖拽框选裁剪 / 时间轴选封面帧 / 批量)
""")
    p.add_argument("video", nargs="*", help="输入视频(可多个); 不给则打开图形界面")
    p.add_argument("--batch", default=None, help="批量: 处理该目录下的所有视频")
    p.add_argument("--livp", action="store_true", help="额外打包成单个 .livp 方便分享")
    p.add_argument("-o", "--out", default=None, help="输出目录(默认脚本目录/output)")
    p.add_argument("-n", "--name", default=None, help="输出文件名(不含扩展名)")
    p.add_argument("-p", "--pattern", default=None,
                   help="批量命名模板, 如 IMG_{n:04d} (n 从 1 递增)")
    p.add_argument("--start", type=float, default=0.0, help="截取起点(秒), 默认 0")
    p.add_argument("--duration", type=float, default=3.0, help="输出时长(秒), 限 0.5~5, 默认 3")
    p.add_argument("--crop", choices=["none", "square", "portrait", "custom"],
                   default="none", help="裁剪预设: none/正方形/9:16竖屏/自定义")
    p.add_argument("--cx", type=int, default=0, help="自定义裁剪 x")
    p.add_argument("--cy", type=int, default=0, help="自定义裁剪 y")
    p.add_argument("--cw", type=int, default=0, help="自定义裁剪 宽")
    p.add_argument("--ch", type=int, default=0, help="自定义裁剪 高")
    p.add_argument("--cover-rel", type=float, default=0.5,
                   help="封面帧在截取段内的相对位置 0~1, 默认 0.5(中间)")
    p.add_argument("--cover-t", type=float, default=None,
                   help="封面帧时间(源视频时间轴上的秒数), 优先于 --cover-rel")
    p.add_argument("--speed", type=float, default=1.0,
                   help="播放速度 0.25~4 (0.5=慢放, 2=快放), 默认 1")
    p.add_argument("--rotate", type=int, choices=[0, 90, 180, 270], default=0,
                   help="顺时针旋转角度, 默认 0")
    p.add_argument("--mute", action="store_true", help="去掉音频")
    p.add_argument("--reverse", action="store_true", help="倒放")
    p.add_argument("--crf", type=int, default=18,
                   help="视频质量 CRF(越小越清晰文件越大, 14~28), 默认 18")
    p.add_argument("--lossless", action="store_true",
                   help="无损模式: 不重编码(需不裁剪/不变速/不旋转/全长0.5~5s, 否则自动回退)")
    p.add_argument("--open", action="store_true", help="转换完成后打开输出文件夹")
    p.add_argument("--no-verify", action="store_true", help="跳过无损校验")
    p.add_argument("--gui", action="store_true", help="强制打开图形界面")
    return p


def cmd_cli():
    args = build_parser().parse_args()
    if args.gui:
        return launch_gui()
    videos = list(args.video)
    if args.batch:
        d = Path(args.batch)
        if d.is_dir():
            for f in sorted(d.iterdir()):
                if f.suffix.lower() in VIDEO_EXTS:
                    videos.append(str(f))
        else:
            print("错误: --batch 路径不是目录:", args.batch)
            return 2
    if not videos:
        return launch_gui()
    out_dir = args.out or (_app_dir() / "output")
    crop_custom = (args.cx, args.cy, args.cw, args.ch) if args.crop == "custom" else None
    multi = len(videos) > 1
    rc = 0
    for i, v in enumerate(videos, 1):
        if multi:
            print(f"\n===== [{i}/{len(videos)}] {os.path.basename(v)} =====")
        if args.pattern:
            nm = args.pattern.format(n=i)
        elif not multi and args.name:
            nm = args.name
        else:
            nm = None
        try:
            convert(v, out_dir, name=nm, start=args.start, duration=args.duration,
                    crop_preset=args.crop, crop_custom=crop_custom,
                    cover_t=args.cover_t, cover_rel=args.cover_rel,
                    speed=args.speed, rotate=args.rotate, mute=args.mute,
                    reverse=args.reverse, crf=args.crf, lossless=args.lossless,
                    verify=not args.no_verify, livp=args.livp,
                    open_folder=args.open and i == len(videos))
        except (LivePhotoError, RuntimeError, FileNotFoundError) as e:
            print("错误:", e)
            rc = 1
    return rc


# ===========================================================================
# 7b. 子命令: info / verify / unlivp / pair
# ===========================================================================

def _print_file_info(path):
    p = Path(path)
    suf = p.suffix.lower()
    if suf in (".jpg", ".jpeg"):
        cid = jpg_content_id(str(p))
        print("  类型: JPEG" + ("  (实况图封面)" if cid else "  (普通图片)"))
        if cid:
            print("  ContentIdentifier:", cid)
        return
    if suf == ".livp":
        import zipfile
        with zipfile.ZipFile(str(p)) as z:
            names = z.namelist()
            print("  类型: .livp (zip 包装的实况图)")
            for n in names:
                print("   -", n)
            jpgn = next((n for n in names if n.lower().endswith((".jpg", ".jpeg"))), None)
            movn = next((n for n in names if n.lower().endswith(".mov")), None)
            if jpgn:
                print("  JPG ContentIdentifier:", read_apple_makernote(z.read(jpgn)))
            if movn:
                print("  MOV ContentIdentifier:", mov_content_id_from_bytes(z.read(movn)))
        return
    info = probe_video(str(p))
    print("  类型: 视频  %s %dx%d  %.2fs  %sfps  %s  音频: %s" % (
        info["codec"] or "?", info["width"] or 0, info["height"] or 0,
        info["duration"] or 0, info["fps"] or "?", p.suffix,
        "有" if info["has_audio"] else "无"))
    cid = mov_content_id(str(p))
    if cid:
        print("  ContentIdentifier:", cid, " (已是实况图视频)")


def cmd_info(argv):
    p = argparse.ArgumentParser(prog="livephoto info", description="查看视频/实况图信息")
    p.add_argument("files", nargs="+")
    args = p.parse_args(argv)
    for f in args.files:
        print("=" * 60)
        print(f)
        try:
            _print_file_info(f)
        except Exception as e:
            print("  读取失败:", e)
    return 0


def _verify_one(jpg_bytes, mov_bytes, label):
    c1 = read_apple_makernote(jpg_bytes)
    c2 = mov_content_id_from_bytes(mov_bytes)
    if c1 and c2 and c1 == c2:
        print("  [OK]", label, "配对一致:", c1)
        return True
    print("  [FAIL]", label, "配对失败: JPG =", c1, " MOV =", c2)
    return False


def cmd_verify(argv):
    p = argparse.ArgumentParser(prog="livephoto verify", description="校验实况图配对")
    p.add_argument("targets", nargs="+", help=".livp 文件 / .JPG 文件 / 目录")
    args = p.parse_args(argv)
    import zipfile
    ok_all, checked = True, 0
    for t in args.targets:
        pt = Path(t)
        checks = []          # (label, jpg_bytes, mov_bytes)
        if pt.is_dir():
            for f in sorted(pt.iterdir()):
                if f.suffix.lower() == ".livp":
                    with zipfile.ZipFile(str(f)) as z:
                        checks.append((f.name, _zip_jpg(z), _zip_mov(z)))
                elif f.suffix.lower() in (".jpg", ".jpeg"):
                    mov = _sibling_mov(f)
                    if mov:
                        checks.append((f.name, f.read_bytes(), mov.read_bytes()))
                    else:
                        print("  [SKIP]", f.name, ": 找不到同名 .MOV")
        elif pt.suffix.lower() == ".livp":
            with zipfile.ZipFile(str(pt)) as z:
                checks.append((pt.name, _zip_jpg(z), _zip_mov(z)))
        elif pt.suffix.lower() in (".jpg", ".jpeg"):
            mov = _sibling_mov(pt)
            if mov:
                checks.append((pt.name, pt.read_bytes(), mov.read_bytes()))
            else:
                print("  [SKIP]", pt.name, ": 找不到同名 .MOV")
                ok_all = False
        else:
            print("  [SKIP] 忽略:", t)
            continue
        for label, jb, mb in checks:
            if jb is None or mb is None:
                print("  [FAIL]", label, ": 缺少 JPG 或 MOV")
                ok_all = False
                checked += 1
                continue
            checked += 1
            if not _verify_one(jb, mb, label):
                ok_all = False
    print("-" * 60)
    print(f"共检查 {checked} 组, " + ("全部通过 ✅" if ok_all and checked else "存在问题 ❌"))
    return 0 if (ok_all and checked) else 1


def _zip_jpg(z):
    n = next((x for x in z.namelist() if x.lower().endswith((".jpg", ".jpeg"))), None)
    return z.read(n) if n else None


def _zip_mov(z):
    n = next((x for x in z.namelist() if x.lower().endswith(".mov")), None)
    return z.read(n) if n else None


def _sibling_mov(jpg_path):
    for cand in (jpg_path.with_suffix(".MOV"), jpg_path.with_suffix(".mov")):
        if cand.is_file():
            return cand
    return None


def cmd_unlivp(argv):
    p = argparse.ArgumentParser(prog="livephoto unlivp", description="解开 .livp 为 JPG+MOV")
    p.add_argument("livp", nargs="+")
    p.add_argument("-o", "--out", default=None, help="输出目录(默认与 .livp 同目录)")
    args = p.parse_args(argv)
    import zipfile
    for f in args.livp:
        out = Path(args.out) if args.out else Path(f).resolve().parent
        out.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(f) as z:
                z.extractall(str(out))
                print("已解出:", ", ".join(z.namelist()), "->", out)
        except zipfile.BadZipFile:
            print("错误: 不是有效的 .livp(zip) 文件:", f)
            return 1
    return 0


def cmd_pair(argv):
    p = argparse.ArgumentParser(prog="livephoto pair",
                                description="已有封面图+视频 -> 实况图(零重编码)")
    p.add_argument("photo", help="封面图片(jpg/png, 非jpg会自动转)")
    p.add_argument("video", help="视频(mp4/mov, 建议H.264)")
    p.add_argument("-o", "--out", default=None, help="输出目录(默认脚本目录/output)")
    p.add_argument("-n", "--name", default=None, help="输出文件名(不含扩展名)")
    p.add_argument("--livp", action="store_true", help="额外打包成 .livp")
    args = p.parse_args(argv)
    out = args.out or (_app_dir() / "output")
    try:
        pair(args.photo, args.video, out, name=args.name, livp=args.livp)
    except (LivePhotoError, RuntimeError, FileNotFoundError) as e:
        print("错误:", e)
        return 1
    return 0


# ===========================================================================
# 8. 图形界面
#     launch_fluent(): Windows 11 设置页风格 (CustomTkinter + DWM 原生深色标题栏)
#     launch_gui_classic(): 传统 tkinter 界面 (未安装 customtkinter 时的降级)
# ===========================================================================

def launch_gui():
    """GUI 入口: 优先真·Fluent 界面(sv_ttk), 缺依赖则降级到经典界面。"""
    try:
        import sv_ttk  # noqa: F401
        import pywinstyles  # noqa: F401
        from PIL import Image  # noqa: F401
        import fluent_shell  # noqa: F401
    except Exception:
        return launch_gui_classic()
    return launch_fluent()


def launch_fluent(items_carry=None, mode="system"):
    """Windows 11 设置页风格界面(真·Fluent: ttk + Sun Valley 主题)。"""
    import tkinter as tk
    from tkinter import filedialog, messagebox
    import tempfile
    import threading
    from PIL import Image
    from fluent_shell import FluentShell

    OUT_DIR = _app_dir() / "output"
    PREVIEW_MAX = (640, 360)
    _preview_path = os.path.join(tempfile.gettempdir(), "_livephoto_preview.jpg")
    PAGES = [("videos", "视频素材", "添加并预览视频, 拖拽框选裁剪区域"),
             ("edit", "画面调整", "时间截取 / 空间裁剪 / 特效"),
             ("output", "输出设置", "输出选项与批量转换")]

    state = {"items": list(items_carry or []), "sel": -1, "info": None,
             "preview_img": None, "tk_img": None, "scale": 1.0,
             "crop_rect": None, "rect_id": None, "drag": None,
             "running": False, "cover_t": None}

    _ico = _app_dir() / "icon.ico"

    def build(app):
        # ============================ 页面 1: 视频素材 ============================
        pv = app.page("videos")

        c_list = app.card(pv, "素材列表")
        c_list.pack(fill="x", pady=6)
        listbox = app.listbox(c_list, height=6, on_select=lambda lb: _on_list(lb))
        listbox.pack(fill="x", padx=20, pady=(4, 2))
        empty_hint = app.sublabel(c_list, "尚未添加视频 — 点击「添加视频」开始")

        row_btn = app.hbox(c_list)
        row_btn.pack(fill="x", padx=20, pady=(6, 4))
        app.btn(row_btn, "＋ 添加视频", _add_files, primary=True).pack(side="left")
        app.btn(row_btn, "＋ 添加文件夹", _add_folder).pack(side="left", padx=8)
        app.btn(row_btn, "－ 移除选中", _remove).pack(side="left")
        lbl_info = app.label(c_list, "", role="sub", font=app.f_small)
        lbl_info.pack(fill="x", padx=20, pady=(0, 14))

        # --- 预览 ---
        c_prev = app.card(pv, "预览")
        c_prev.pack(fill="both", expand=True, pady=6)
        canvas = tk.Canvas(c_prev, bg=app.P["canvas"], relief="flat", bd=0,
                           highlightthickness=0, height=200)
        canvas.pack(padx=20, pady=(4, 8))
        app.track(canvas, fg=None, bg="canvas")
        lbl_preview = app.sublabel(c_prev, "选中视频后在此显示首帧 — 按住拖拽框选裁剪")

        scrub_row = app.hbox(c_prev)
        scrub_row.pack(fill="x", padx=20, pady=(0, 4))
        app.label(scrub_row, "预览时间", role="sub").pack(side="left")
        lbl_scrub = app.label(scrub_row, "0.00s", role="text")
        lbl_scrub.pack(side="right")
        _scrub_after = {"id": None}

        def _scrub_cmd(_v):
            lbl_scrub.configure(text=f"{float(scl_scrub.get()):.2f}s")
            if _scrub_after["id"]:
                app.root.after_cancel(_scrub_after["id"])
            _scrub_after["id"] = app.root.after(240, _on_scrub)

        scl_scrub = app.slider(scrub_row, 0, 10, command=_scrub_cmd)
        scl_scrub.pack(side="left", fill="x", expand=True, padx=12)

        cover_row = app.hbox(c_prev)
        cover_row.pack(fill="x", padx=20, pady=(0, 14))
        app.btn(cover_row, "设当前帧为封面", _set_cover).pack(side="left")
        lbl_cover = app.label(cover_row, "封面帧: 未指定 (默认取截取段中间)",
                              role="sub", font=app.f_small)
        lbl_cover.pack(side="left", padx=12)

        # ============================ 页面 2: 画面调整 ============================
        pe = app.page("edit")

        c_time = app.card(pe, "时间截取")
        c_time.pack(fill="x", pady=6)
        var_start = tk.DoubleVar(value=0.0)
        var_dur = tk.DoubleVar(value=3.0)

        def _upd_time(*_):
            d = (state["info"]["duration"] if state["info"] else 30) or 30
            st = var_start.get()
            du = max(0.5, min(5.0, var_dur.get()))
            if st + du > d:
                st = max(0.0, d - du)
                var_start.set(round(st, 2))
            lbl_start.configure(text=f"{var_start.get():.2f} s")
            lbl_dur.configure(text=f"{du:.1f} s")
            lbl_end.configure(text=f"结束 ≈ {var_start.get() + du:.2f} s")

        def _time_row(label, var, frm, to):
            r = app.row(c_time, label)
            lbl = app.label(r, "", role="text")
            lbl.pack(side="right")
            app.slider(r, frm, to, variable=var,
                       command=lambda _v: _upd_time()).pack(
                side="right", fill="x", expand=True, padx=12)
            return lbl

        lbl_start = _time_row("起点", var_start, 0, 30)
        lbl_dur = _time_row("时长", var_dur, 0.5, 5)
        lbl_end = app.sublabel(c_time, "结束 ≈ 3.00 s")

        # --- 空间裁剪 ---
        c_crop = app.card(pe, "空间裁剪")
        c_crop.pack(fill="x", pady=6)
        app.sublabel(c_crop, "在「视频素材」页拖拽框选, 会自动切到自定义")
        var_crop_seg = tk.StringVar(value="不裁剪")
        app.segmented(c_crop, ["不裁剪", "正方形", "9:16", "自定义"],
                      var_crop_seg, command=lambda _v: _toggle_crop()).pack(
            fill="x", padx=20, pady=4)

        frm_custom = app.hbox(c_crop)
        frm_custom.pack(fill="x", padx=20, pady=(6, 14))
        var_cx, var_cy, var_cw, var_ch = (tk.IntVar(), tk.IntVar(),
                                          tk.IntVar(), tk.IntVar())
        _c_entries = []
        for lab, vv in (("X", var_cx), ("Y", var_cy), ("宽", var_cw), ("高", var_ch)):
            app.label(frm_custom, lab, role="sub").pack(side="left", padx=(0, 4))
            e = app.entry(frm_custom, width=70, justify="center")
            e.configure(textvariable=vv)
            e.pack(side="left", padx=(0, 12))
            _c_entries.append(e)

        def _toggle_crop():
            on = var_crop_seg.get() == "自定义"
            for e in _c_entries:
                e.configure(state="normal" if on else "disabled")

        # --- 效果 ---
        c_fx = app.card(pe, "效果")
        c_fx.pack(fill="x", pady=6)
        fx_row = app.hbox(c_fx)
        fx_row.pack(fill="x", padx=20, pady=6)
        var_speed = tk.StringVar(value="1.0x")
        var_rotate = tk.StringVar(value="0°")
        var_q = tk.StringVar(value="标准")

        def _opt(label, var, values, width=92):
            app.label(fx_row, label, role="sub").pack(side="left")
            app.optionmenu(fx_row, values, var, width=width).pack(
                side="left", padx=(4, 20))

        _opt("速度", var_speed, ["0.5x", "0.75x", "1.0x", "1.5x", "2.0x"])
        _opt("旋转", var_rotate, ["0°", "90°", "180°", "270°"], 76)
        _opt("画质", var_q, ["高", "标准", "小体积"], 88)

        fx_row2 = app.hbox(c_fx)
        fx_row2.pack(fill="x", padx=20, pady=(6, 4))
        var_mute = tk.BooleanVar(value=False)
        var_rev = tk.BooleanVar(value=False)
        app.toggle(fx_row2, "静音", var_mute).pack(side="left", padx=(0, 28))
        app.toggle(fx_row2, "倒放", var_rev).pack(side="left")
        app.sublabel(c_fx, "变速以输出时长为准 (0.5x 慢放取源片约 2 倍长度)")

        # ============================ 页面 3: 输出设置 ============================
        po = app.page("output")
        c_opt = app.card(po, "输出选项")
        c_opt.pack(fill="x", pady=6)
        var_livp = tk.BooleanVar(value=False)
        var_lossless = tk.BooleanVar(value=False)
        var_open = tk.BooleanVar(value=True)
        app.switch_row(c_opt, "打包成 .livp", "把 JPG+MOV 合成单个文件", var_livp)
        app.switch_row(c_opt, "无损模式",
                       "不重编码(需不裁剪/不变速/不旋转且全长 0.5~5s)", var_lossless)
        app.switch_row(c_opt, "完成后打开输出文件夹", "转换结束自动打开", var_open)
        app.sublabel(c_opt, f"输出目录: {OUT_DIR}  ·  每个视频生成 NAME.JPG + NAME.MOV")
        app.sublabel(c_opt, "批量时裁剪/时间/封面对所有视频统一生效")

        c_run = app.card(po, "转换")
        c_run.pack(fill="both", expand=True, pady=6)
        run_row = app.hbox(c_run)
        run_row.pack(fill="x", padx=20, pady=(10, 4))
        btn_start = app.btn(run_row, "开始转换", _start, primary=True, width=170)
        btn_start.pack(side="left")
        lbl_status = app.label(run_row, "就绪", role="sub")
        lbl_status.pack(side="left", padx=16)
        pg = app.progress(c_run)
        pg.pack(fill="x", padx=20, pady=8)
        pg.set(0)
        txt_log = app.logbox(c_run, height=200)

        # 把常用引用塞进 state, 供嵌套函数使用
        state.update({"listbox": listbox, "canvas": canvas, "lbl_info": lbl_info,
                      "lbl_preview": lbl_preview, "lbl_scrub": lbl_scrub,
                      "scl_scrub": scl_scrub, "lbl_cover": lbl_cover,
                      "var_start": var_start, "var_dur": var_dur,
                      "var_crop_seg": var_crop_seg,
                      "var_cx": var_cx, "var_cy": var_cy,
                      "var_cw": var_cw, "var_ch": var_ch,
                      "var_speed": var_speed, "var_rotate": var_rotate,
                      "var_q": var_q, "var_mute": var_mute, "var_rev": var_rev,
                      "var_livp": var_livp, "var_lossless": var_lossless,
                      "var_open": var_open, "btn_start": btn_start,
                      "lbl_status": lbl_status, "pg": pg, "txt_log": txt_log})

        _refresh_list()
        _toggle_crop()
        _upd_time()

    # ============================ 列表 / 预览 ============================
    def _refresh_list():
        lb = state["listbox"]
        lb.delete(0, "end")
        for i, p in enumerate(state["items"]):
            lb.insert("end", f"  {i+1:>2}.  {os.path.basename(p)}")
        if 0 <= state["sel"] < len(state["items"]):
            lb.selection_set(state["sel"])
            lb.see(state["sel"])

    def _on_list(lb):
        cur = lb.curselection()
        if cur:
            _select(cur[0])

    def _add_files():
        paths = filedialog.askopenfilenames(
            title="选择视频文件",
            filetypes=[("视频", "*.mp4 *.mov *.mkv *.avi *.webm *.flv *.wmv *.m4v"),
                       ("全部", "*.*")])
        for p in paths:
            if p not in state["items"]:
                state["items"].append(p)
        if state["sel"] < 0 and state["items"]:
            _select(0)
        _refresh_list()

    def _add_folder():
        d = filedialog.askdirectory(title="选择包含视频的文件夹")
        if not d:
            return
        for f in sorted(Path(d).iterdir()):
            if f.suffix.lower() in VIDEO_EXTS and str(f) not in state["items"]:
                state["items"].append(str(f))
        if state["sel"] < 0 and state["items"]:
            _select(0)
        _refresh_list()

    def _remove():
        if state["sel"] < 0:
            return
        state["items"].pop(state["sel"])
        state["sel"] = -1
        state["crop_rect"] = None
        state["cover_t"] = None
        _refresh_list()

    def _select(idx):
        state["sel"] = idx
        path = state["items"][idx]
        try:
            info = probe_video(path)
        except Exception as e:
            state["lbl_info"].configure(text=f"读取失败: {e}")
            state["info"] = None
            _refresh_list()
            return
        state["info"] = info
        d = info["duration"] or 0
        state["lbl_info"].configure(
            text=f"{info['width']}×{info['height']}   ·   {d:.2f} 秒   ·   "
                 f"{info['fps'] or '?'} fps")
        _refresh_list()
        if d:
            state["scl_scrub"].configure(to=max(0.1, d))
            state["var_dur"].set(min(3.0, max(0.5, d)))
        _show_preview(path)

    def _load_frame(path, t):
        try:
            extract_cover(path, _preview_path, t, log_fn=lambda *a: None)
            im = Image.open(_preview_path).convert("RGB")
        except Exception as e:
            state["lbl_preview"].configure(text=f"预览失败: {e}")
            return None
        state["preview_img"] = im
        ow, oh = im.size
        scale = min(PREVIEW_MAX[0] / ow, PREVIEW_MAX[1] / oh, 1.0)
        state["scale"] = scale
        disp = im.resize((max(1, int(ow * scale)), max(1, int(oh * scale))))
        from PIL import ImageTk
        state["tk_img"] = ImageTk.PhotoImage(disp)
        canvas = state["canvas"]
        canvas.configure(width=disp.width, height=disp.height)
        canvas.delete("all")
        canvas.create_image(0, 0, image=state["tk_img"], anchor="nw")
        state["rect_id"] = None
        if state["crop_rect"]:
            r = state["crop_rect"]
            state["rect_id"] = canvas.create_rectangle(
                r[0] * scale, r[1] * scale, r[2] * scale, r[3] * scale,
                outline=app.P["accent"], width=2)
        return im

    def _show_preview(path):
        state["crop_rect"] = None
        state["cover_t"] = None
        d = state["info"]["duration"] if state["info"] else 10
        state["scl_scrub"].set(0)
        state["scl_scrub"].configure(to=max(0.1, d or 10))
        state["lbl_scrub"].configure(text="0.00s")
        state["lbl_cover"].configure(text="封面帧: 未指定 (默认取截取段中间)")
        im = _load_frame(path, 0.0)
        if im is None:
            return
        state["lbl_preview"].configure(
            text=f"{im.width}×{im.height}  ·  拖拽框选裁剪 / 拖时间轴换帧")

    def _on_scrub():
        t = float(state["scl_scrub"].get())
        state["lbl_scrub"].configure(text=f"{t:.2f}s")
        if state["sel"] >= 0 and state["items"]:
            _load_frame(state["items"][state["sel"]], t)

    def _set_cover():
        t = round(float(state["scl_scrub"].get()), 3)
        state["cover_t"] = t
        state["lbl_cover"].configure(text=f"封面帧: t = {t:.2f}s (当前时间轴)")

    # ---- 裁剪拖拽 ----
    def _down(e):
        canvas = state["canvas"]
        if state["rect_id"]:
            canvas.delete(state["rect_id"])
        state["drag"] = (e.x, e.y)
        state["rect_id"] = canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline=app.P["accent"], width=2)

    def _move(e):
        if state["drag"] and state["rect_id"]:
            state["canvas"].coords(state["rect_id"], state["drag"][0],
                                   state["drag"][1], e.x, e.y)

    def _up(e):
        if not state["drag"]:
            return
        x0, y0 = state["drag"]
        x1, y1 = e.x, e.y
        state["drag"] = None
        im = state["preview_img"]
        if im is None:
            return
        scale = state["scale"]
        ox0, oy0 = int(min(x0, x1) / scale), int(min(y0, y1) / scale)
        ox1, oy1 = int(max(x0, x1) / scale), int(max(y0, y1) / scale)
        ox0, oy0 = max(0, ox0), max(0, oy0)
        ox1, oy1 = min(im.width, ox1), min(im.height, oy1)
        if ox1 - ox0 < 10 or oy1 - oy0 < 10:
            state["lbl_preview"].configure(text="框选太小已忽略 — 按住左键拖拽")
            return
        state["crop_rect"] = (ox0, oy0, ox1, oy1)
        state["var_crop_seg"].set("自定义")
        state["var_cx"].set(ox0)
        state["var_cy"].set(oy0)
        state["var_cw"].set(ox1 - ox0)
        state["var_ch"].set(oy1 - oy0)
        state["lbl_preview"].configure(
            text=f"框选裁剪: {ox1-ox0}×{oy1-oy0} @ ({ox0},{oy0}) — 已切换到自定义")

    # ============================ 转换 ============================
    def _resolve_crop():
        if state["crop_rect"]:
            r = state["crop_rect"]
            return "custom", (r[0], r[1], r[2] - r[0], r[3] - r[1])
        if state["var_crop_seg"].get() == "自定义":
            return "custom", (state["var_cx"].get(), state["var_cy"].get(),
                              state["var_cw"].get(), state["var_ch"].get())
        return {"不裁剪": "none", "正方形": "square", "9:16": "portrait"}.get(
            state["var_crop_seg"].get(), "none"), None

    def _start():
        if state["running"]:
            return
        if not state["items"]:
            messagebox.showerror("LivePhoto", "请先在「视频素材」页添加至少一个视频")
            return
        crop_preset, crop_custom = _resolve_crop()
        dur = max(0.5, min(5.0, float(state["var_dur"].get())))
        st = float(state["var_start"].get())
        try:
            speed = float(state["var_speed"].get().rstrip("x"))
        except ValueError:
            speed = 1.0
        try:
            rotate = int(state["var_rotate"].get().rstrip("°"))
        except ValueError:
            rotate = 0
        crf = {"高": 16, "标准": 18, "小体积": 23}.get(state["var_q"].get(), 18)
        mute = state["var_mute"].get()
        reverse = state["var_rev"].get()
        lossless = state["var_lossless"].get()
        open_after = state["var_open"].get()
        cover_t = state.get("cover_t")
        items = list(state["items"])
        do_livp = state["var_livp"].get()

        log = lambda m: app.log(m, state["txt_log"])
        ui = app.ui
        btn_start = state["btn_start"]
        lbl_status = state["lbl_status"]
        pg = state["pg"]

        def worker():
            state["running"] = True
            ui(lambda: (btn_start.configure(state="disabled", text="转换中…"),
                        lbl_status.configure(text="转换中"),
                        pg.configure(mode="indeterminate"), pg.start()))
            ok = 0
            for i, v in enumerate(items, 1):
                ui(lambda i=i: lbl_status.configure(text=f"转换中 ({i}/{len(items)})"))
                try:
                    log(f"===== [{i}/{len(items)}] {os.path.basename(v)} =====")
                    convert(v, OUT_DIR, name=None, start=st, duration=dur,
                            crop_preset=crop_preset, crop_custom=crop_custom,
                            cover_t=cover_t, speed=speed, rotate=rotate,
                            mute=mute, reverse=reverse, crf=crf,
                            lossless=lossless, verify=True, log_fn=log,
                            livp=do_livp, open_folder=(open_after and i == len(items)))
                    ok += 1
                except Exception as e:
                    log("❌ 失败: " + str(e))
            ui(lambda: (
                pg.stop(), pg.configure(mode="determinate"),
                pg.set(1.0 if ok == len(items) else 0.3),
                log(f"✅ 完成 {ok}/{len(items)} 个, 结果在 {OUT_DIR}"),
                lbl_status.configure(text="完成"),
                app.toast(f"完成 {ok}/{len(items)} 个 — 已输出到 output 文件夹",
                          ok=(ok == len(items))),
                btn_start.configure(state="normal", text="开始转换")))
            state["running"] = False

        threading.Thread(target=worker, daemon=True).start()

    # 所有嵌套函数都定义好了, 现在才创建窗口(builder 会在构造时立即调用 build)
    app = FluentShell(title=f"LivePhoto 制作工具 v{VERSION}",
                      app_name="LivePhoto", subtitle="视频 → 实况图",
                      pages=PAGES, width=1060, height=760, mode=mode,
                      icon=str(_ico) if _ico.is_file() else None,
                      footer=f"v{VERSION}", builder=lambda a: build(a))
    P = app.P

    # 绑定裁剪拖拽(build 已在 FluentShell 初始化时执行, canvas 已存在)
    c = state["canvas"]
    c.bind("<Button-1>", _down)
    c.bind("<B1-Motion>", _move)
    c.bind("<ButtonRelease-1>", _up)

    app.mainloop()
    return 0


def launch_gui_classic():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    from PIL import Image, ImageTk
    import tempfile

    root = tk.Tk()
    root.title(f"LivePhoto 制作工具 v{VERSION}")
    root.geometry("760x940")
    try:
        style = ttk.Style(root)
        style.theme_use("clam")
    except Exception:
        pass

    OUT_DIR = _app_dir() / "output"
    PREVIEW_MAX = (560, 340)
    _preview_path = os.path.join(tempfile.gettempdir(), "_livephoto_preview.jpg")

    state = {"items": [], "sel": -1, "info": None, "preview_img": None,
             "tk_img": None, "scale": 1.0, "crop_rect": None, "rect_id": None,
             "drag": None, "running": False, "cover_t": None}

    import queue as _queue
    _logq = _queue.Queue()

    def log(msg):
        """线程安全日志: 后台线程只入队, 由主线程定时刷新到界面。"""
        _logq.put(str(msg))

    def _drain_log():
        try:
            while True:
                msg = _logq.get_nowait()
                txt_log.config(state="normal")
                txt_log.insert("end", msg + "\n")
                txt_log.see("end")
                txt_log.config(state="disabled")
        except _queue.Empty:
            pass
        root.after(80, _drain_log)

    # ---------- 1. 视频列表(批量) ----------
    frm_list = ttk.LabelFrame(root, text="1. 视频列表(支持批量)", padding=8)
    frm_list.pack(fill="x", padx=10, pady=6)
    list_frame = ttk.Frame(frm_list)
    list_frame.pack(fill="x")
    lb = tk.Listbox(list_frame, height=4, selectmode="extended")
    lb.pack(side="left", fill="both", expand=True)
    sb = ttk.Scrollbar(list_frame, orient="vertical", command=lb.yview)
    sb.pack(side="right", fill="y")
    lb.config(yscrollcommand=sb.set)
    btn_row = ttk.Frame(frm_list)
    btn_row.pack(fill="x", pady=(6, 0))
    ttk.Button(btn_row, text="添加视频", command=lambda: _add_files()).pack(side="left")
    ttk.Button(btn_row, text="添加文件夹", command=lambda: _add_folder()).pack(side="left")
    ttk.Button(btn_row, text="移除选中", command=lambda: _remove()).pack(side="left")
    ttk.Button(btn_row, text="预览选中", command=lambda: _on_select()).pack(side="left")
    lbl_info = ttk.Label(frm_list, text="视频信息: 尚未选择", foreground="#888")
    lbl_info.pack(anchor="w", pady=(4, 0))

    def _refresh_list():
        lb.delete(0, "end")
        for p in state["items"]:
            lb.insert("end", os.path.basename(p))

    def _add_files():
        paths = filedialog.askopenfilenames(
            title="选择视频文件",
            filetypes=[("视频", "*.mp4 *.mov *.mkv *.avi *.webm *.flv *.wmv *.m4v"), ("全部", "*.*")])
        for p in paths:
            if p not in state["items"]:
                state["items"].append(p)
        _refresh_list()
        if state["sel"] < 0 and state["items"]:
            lb.selection_set(0)
            _on_select()

    def _add_folder():
        d = filedialog.askdirectory(title="选择包含视频的文件夹")
        if not d:
            return
        for f in sorted(Path(d).iterdir()):
            if f.suffix.lower() in VIDEO_EXTS and str(f) not in state["items"]:
                state["items"].append(str(f))
        _refresh_list()
        if state["sel"] < 0 and state["items"]:
            lb.selection_set(0)
            _on_select()

    def _remove():
        for i in sorted(lb.curselection(), reverse=True):
            state["items"].pop(i)
        state["sel"] = -1
        state["crop_rect"] = None
        _refresh_list()

    def _on_select(_=None):
        sel = lb.curselection()
        if not sel:
            return
        idx = sel[0]
        state["sel"] = idx
        path = state["items"][idx]
        try:
            info = probe_video(path)
        except Exception as e:
            lbl_info.config(text=f"读取失败: {e}")
            state["info"] = None
            return
        state["info"] = info
        d = info["duration"] or 0
        lbl_info.config(
            text=f"已选 #{idx+1}: {os.path.basename(path)}  {info['width']}x{info['height']}  "
                 f"{d:.2f}s  {info['fps'] or '?'}fps", foreground="#ccc")
        scl_start.config(to=max(0.1, d))
        var_dur.set(min(3.0, max(0.5, d)))
        _show_preview(path)

    # ---------- 2. 预览 / 拖拽框选裁剪 / 时间轴选帧 ----------
    frm_prev = ttk.LabelFrame(root, text="2. 预览 / 拖拽框选裁剪 / 时间轴选帧", padding=8)
    frm_prev.pack(fill="x", padx=10, pady=6)
    canvas = tk.Canvas(frm_prev, bg="#2b2b2b", relief="flat", bd=0)
    canvas.pack(padx=4, pady=4)
    lbl_preview = ttk.Label(frm_prev, text="先在上方添加视频并选中以加载预览", foreground="#888")
    lbl_preview.pack(anchor="w")

    # 预览时间轴(拖动换帧) + 设为封面
    scrub_row = ttk.Frame(frm_prev)
    scrub_row.pack(fill="x", pady=(4, 0))
    ttk.Label(scrub_row, text="预览时间").pack(side="left")
    scl_scrub = ttk.Scale(scrub_row, from_=0, to=10,
                          command=lambda v: lbl_scrub.config(text=f"{float(v):.2f}s"))
    scl_scrub.pack(side="left", fill="x", expand=True, padx=6)
    lbl_scrub = ttk.Label(scrub_row, text="0.00s", width=7)
    lbl_scrub.pack(side="left")
    scl_scrub.bind("<ButtonRelease-1>", lambda e: _on_scrub())
    cover_row = ttk.Frame(frm_prev)
    cover_row.pack(fill="x", pady=(4, 0))
    ttk.Button(cover_row, text="设当前帧为封面", command=lambda: _set_cover()).pack(side="left")
    lbl_cover = ttk.Label(cover_row, text="封面帧: 未指定(默认取截取段中间)", foreground="#888")
    lbl_cover.pack(side="left", padx=8)

    def _load_frame(path, t):
        """抽取 path 在时间 t 的帧并显示; 返回 PIL Image 或 None。"""
        try:
            extract_cover(path, _preview_path, t, log_fn=lambda *a: None)
            im = Image.open(_preview_path).convert("RGB")
        except Exception as e:
            lbl_preview.config(text=f"预览失败: {e}")
            return None
        state["preview_img"] = im
        ow, oh = im.size
        scale = min(PREVIEW_MAX[0] / ow, PREVIEW_MAX[1] / oh, 1.0)
        state["scale"] = scale
        disp = im.resize((max(1, int(ow * scale)), max(1, int(oh * scale))))
        state["tk_img"] = ImageTk.PhotoImage(disp)
        canvas.config(width=disp.width, height=disp.height)
        canvas.delete("all")
        canvas.create_image(0, 0, image=state["tk_img"], anchor="nw")
        state["rect_id"] = None
        if state["crop_rect"]:
            r = state["crop_rect"]
            state["rect_id"] = canvas.create_rectangle(
                r[0] * scale, r[1] * scale, r[2] * scale, r[3] * scale,
                outline="#ff5555", width=2)
        return im

    def _show_preview(path):
        state["crop_rect"] = None
        state["cover_t"] = None
        d = state["info"]["duration"] if state["info"] else 10
        scl_scrub.set(0)
        scl_scrub.config(to=max(0.1, d or 10))
        lbl_scrub.config(text="0.00s")
        lbl_cover.config(text="封面帧: 未指定(默认取截取段中间)")
        im = _load_frame(path, 0.0)
        if im is None:
            return
        lbl_preview.config(text=f"预览 {im.width}x{im.height}  |  拖拽框选裁剪 / 拖时间轴换帧")

    def _on_scrub():
        t = scl_scrub.get()
        lbl_scrub.config(text=f"{t:.2f}s")
        if state["sel"] >= 0 and state["items"]:
            _load_frame(state["items"][state["sel"]], t)

    def _set_cover():
        t = round(scl_scrub.get(), 3)
        state["cover_t"] = t
        lbl_cover.config(text=f"封面帧: t={t:.2f}s (以当前预览时间轴为准)")

    def _down(e):
        if state["rect_id"]:
            canvas.delete(state["rect_id"])
        state["drag"] = (e.x, e.y)
        state["rect_id"] = canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                  outline="#ff5555", width=2)

    def _move(e):
        if state["drag"]:
            canvas.coords(state["rect_id"], state["drag"][0], state["drag"][1], e.x, e.y)

    def _up(e):
        if not state["drag"]:
            return
        x0, y0 = state["drag"]
        x1, y1 = e.x, e.y
        state["drag"] = None
        im = state["preview_img"]
        if im is None:
            return
        scale = state["scale"]
        ox0, oy0 = int(min(x0, x1) / scale), int(min(y0, y1) / scale)
        ox1, oy1 = int(max(x0, x1) / scale), int(max(y0, y1) / scale)
        ox0, oy0 = max(0, ox0), max(0, oy0)
        ox1, oy1 = min(im.width, ox1), min(im.height, oy1)
        if ox1 - ox0 < 10 or oy1 - oy0 < 10:
            lbl_preview.config(text="框选太小, 已忽略 (需在画面上按住拖拽)")
            return
        state["crop_rect"] = (ox0, oy0, ox1, oy1)
        var_crop.set("custom")
        _toggle_crop()
        var_cx.set(ox0); var_cy.set(oy0)
        var_cw.set(ox1 - ox0); var_ch.set(oy1 - oy0)
        lbl_preview.config(
            text=f"框选裁剪: {ox1-ox0}x{oy1-oy0} @ ({ox0},{oy0})  (已自动填入自定义)")

    canvas.bind("<Button-1>", _down)
    canvas.bind("<B1-Motion>", _move)
    canvas.bind("<ButtonRelease-1>", _up)

    # ---------- 3. 时间截取 ----------
    frm_t = ttk.LabelFrame(root, text="3. 时间截取 (0.5~5 秒)", padding=8)
    frm_t.pack(fill="x", padx=10, pady=6)
    var_start = tk.DoubleVar(value=0.0)
    var_dur = tk.DoubleVar(value=3.0)
    ttk.Label(frm_t, text="起点(秒)").grid(row=0, column=0, sticky="w")
    scl_start = ttk.Scale(frm_t, from_=0, to=30, variable=var_start, command=lambda e: _upd_time())
    scl_start.grid(row=0, column=1, sticky="ew", padx=6)
    lbl_start = ttk.Label(frm_t, text="0.00")
    lbl_start.grid(row=0, column=2, sticky="w")
    ttk.Label(frm_t, text="时长(秒)").grid(row=1, column=0, sticky="w")
    scl_dur = ttk.Scale(frm_t, from_=0.5, to=5, variable=var_dur,
                        command=lambda e: _upd_time())
    scl_dur.grid(row=1, column=1, sticky="ew", padx=6)
    lbl_dur = ttk.Label(frm_t, text="3.0")
    lbl_dur.grid(row=1, column=2, sticky="w")
    frm_t.columnconfigure(1, weight=1)
    lbl_end = ttk.Label(frm_t, text="结束 ≈ 3.00s", foreground="#888")
    lbl_end.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _upd_time(*_):
        d = (state["info"]["duration"] if state["info"] else 30) or 30
        st = var_start.get()
        du = max(0.5, min(5.0, var_dur.get()))
        if st + du > d:
            st = max(0.0, d - du)
            var_start.set(round(st, 2))
        lbl_start.config(text=f"{var_start.get():.2f}")
        lbl_dur.config(text=f"{du:.1f}")
        lbl_end.config(text=f"结束 ≈ {var_start.get()+du:.2f}s")

    # ---------- 4. 空间裁剪 ----------
    frm_c = ttk.LabelFrame(root, text="4. 空间裁剪(也可在预览里拖拽框选)", padding=8)
    frm_c.pack(fill="x", padx=10, pady=6)
    var_crop = tk.StringVar(value="none")
    ttk.Radiobutton(frm_c, text="不裁剪", variable=var_crop, value="none",
                    command=lambda: _toggle_crop()).grid(row=0, column=0, sticky="w")
    ttk.Radiobutton(frm_c, text="居中正方形", variable=var_crop, value="square",
                    command=lambda: _toggle_crop()).grid(row=0, column=1, sticky="w")
    ttk.Radiobutton(frm_c, text="9:16 竖屏", variable=var_crop, value="portrait",
                    command=lambda: _toggle_crop()).grid(row=0, column=2, sticky="w")
    ttk.Radiobutton(frm_c, text="自定义", variable=var_crop, value="custom",
                    command=lambda: _toggle_crop()).grid(row=0, column=3, sticky="w")
    frm_custom = ttk.Frame(frm_c)
    frm_custom.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
    var_cx, var_cy, var_cw, var_ch = (tk.IntVar(), tk.IntVar(), tk.IntVar(), tk.IntVar())
    for i, (lab, vv) in enumerate([("x", var_cx), ("y", var_cy), ("宽", var_cw), ("高", var_ch)]):
        ttk.Label(frm_custom, text=lab).grid(row=0, column=i * 2, padx=(0, 2))
        ttk.Entry(frm_custom, textvariable=vv, width=7).grid(row=0, column=i * 2 + 1, padx=(0, 8))

    def _toggle_crop():
        on = var_crop.get() == "custom"
        for child in frm_custom.winfo_children():
            try:
                child.configure(state="normal" if on else "disabled")
            except Exception:
                pass

    # ---------- 5. 画面调整 ----------
    frm_adj = ttk.LabelFrame(root, text="5. 画面调整", padding=8)
    frm_adj.pack(fill="x", padx=10, pady=6)
    ttk.Label(frm_adj, text="速度").grid(row=0, column=0, sticky="w")
    var_speed = tk.StringVar(value="1.0x")
    ttk.Combobox(frm_adj, textvariable=var_speed, width=6, state="readonly",
                 values=["0.5x", "0.75x", "1.0x", "1.5x", "2.0x"]).grid(row=0, column=1, padx=(2, 12))
    ttk.Label(frm_adj, text="旋转").grid(row=0, column=2, sticky="w")
    var_rotate = tk.StringVar(value="0°")
    ttk.Combobox(frm_adj, textvariable=var_rotate, width=5, state="readonly",
                 values=["0°", "90°", "180°", "270°"]).grid(row=0, column=3, padx=(2, 12))
    ttk.Label(frm_adj, text="画质").grid(row=0, column=4, sticky="w")
    var_q = tk.StringVar(value="标准")
    ttk.Combobox(frm_adj, textvariable=var_q, width=7, state="readonly",
                 values=["高", "标准", "小体积"]).grid(row=0, column=5, padx=(2, 0))
    var_mute = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm_adj, text="静音", variable=var_mute).grid(row=1, column=0, sticky="w", pady=(6, 0))
    var_rev = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm_adj, text="倒放", variable=var_rev).grid(row=1, column=1, sticky="w", pady=(6, 0))
    ttk.Label(frm_adj, text="变速以输出时长为准(0.5x 会取源片 2 倍长度的素材)", foreground="#888").grid(
        row=1, column=2, columnspan=4, sticky="w", pady=(6, 0))

    # ---------- 6. 输出 ----------
    frm_o = ttk.LabelFrame(root, text="6. 输出", padding=8)
    frm_o.pack(fill="x", padx=10, pady=6)
    var_livp = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm_o, text="打包成 .livp (单文件, 方便分享)", variable=var_livp).pack(anchor="w")
    var_lossless = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm_o, text="无损模式 (不重编码; 需不裁剪/不变速/不旋转且全长 0.5~5s, 否则自动回退)",
                    variable=var_lossless).pack(anchor="w")
    var_open = tk.BooleanVar(value=True)
    ttk.Checkbutton(frm_o, text="转换完成后打开输出文件夹", variable=var_open).pack(anchor="w")
    ttk.Label(frm_o, text=f"输出目录: {OUT_DIR}  (每个视频生成 NAME.JPG + NAME.MOV)", foreground="#888").pack(anchor="w", pady=(4, 0))
    ttk.Label(frm_o, text="注: 批量时 裁剪/时间/封面 设置对所有视频统一生效(预览以选中项为准)", foreground="#888").pack(anchor="w")

    # ---------- 6. 日志 ----------
    txt_log = scrolledtext.ScrolledText(root, height=9, state="disabled")
    txt_log.pack(fill="both", expand=True, padx=10, pady=6)

    # ---------- 7. 转换 ----------
    frm_btn = ttk.Frame(root)
    frm_btn.pack(fill="x", padx=10, pady=(0, 10))
    ttk.Button(frm_btn, text="开始转换(批量)", command=lambda: _start()).pack(side="right")

    def _resolve_crop():
        if state["crop_rect"]:
            r = state["crop_rect"]
            return "custom", (r[0], r[1], r[2] - r[0], r[3] - r[1])
        if var_crop.get() == "custom":
            return "custom", (var_cx.get(), var_cy.get(), var_cw.get(), var_ch.get())
        return var_crop.get(), None

    def _start():
        if state["running"]:
            return
        if not state["items"]:
            messagebox.showerror("错误", "请先添加至少一个视频")
            return
        crop_preset, crop_custom = _resolve_crop()
        dur = max(0.5, min(5.0, var_dur.get()))
        st = var_start.get()
        try:
            speed = float(var_speed.get().rstrip("x"))
        except ValueError:
            speed = 1.0
        try:
            rotate = int(var_rotate.get().rstrip("°"))
        except ValueError:
            rotate = 0
        crf = {"高": 16, "标准": 18, "小体积": 23}.get(var_q.get(), 18)
        mute = var_mute.get()
        reverse = var_rev.get()
        lossless = var_lossless.get()
        open_after = var_open.get()
        cover_t = state.get("cover_t")
        items = list(state["items"])
        do_livp = var_livp.get()

        def worker():
            state["running"] = True
            ok = 0
            for i, v in enumerate(items, 1):
                try:
                    log(f"\n===== [{i}/{len(items)}] {os.path.basename(v)} =====")
                    convert(v, OUT_DIR, name=None, start=st, duration=dur,
                            crop_preset=crop_preset, crop_custom=crop_custom,
                            cover_t=cover_t, speed=speed, rotate=rotate,
                            mute=mute, reverse=reverse, crf=crf,
                            lossless=lossless, verify=True, log_fn=log,
                            livp=do_livp, open_folder=(open_after and i == len(items)))
                    ok += 1
                except Exception as e:
                    log("❌ 失败: " + str(e))
            log(f"\n✅ 完成 {ok}/{len(items)} 个。结果在 {OUT_DIR}")
            state["running"] = False

        threading.Thread(target=worker, daemon=True).start()

    _toggle_crop()
    _upd_time()
    root.after(80, _drain_log)
    root.mainloop()


# ===========================================================================
# 9. 入口
# ===========================================================================

def _attach_console():
    """GUI 版 exe(无控制台)里跑命令行时, 附加到父进程控制台让输出可见。"""
    if sys.stdout is not None:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ATTACH_PARENT_PROCESS = 0xFFFFFFFF  # -1
        if kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            try:
                sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
            except Exception:
                pass
    except Exception:
        pass


def main():
    # 排障开关: 设了环境变量 LIVEPHOTO_LOG=<路径> 就把输出来写到文件。
    # GUI 版 exe(无控制台) 在 CI/远程环境里 stdout 是 None, 出问题看不到任何信息。
    _log_path = os.environ.get("LIVEPHOTO_LOG")
    if _log_path:
        try:
            _lf = open(_log_path, "w", encoding="utf-8", errors="replace")
            sys.stdout = _lf
            sys.stderr = _lf
        except Exception:
            pass
    _attach_console()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    argv = list(sys.argv[1:])
    if argv and argv[0] in ("info", "verify", "unlivp", "pair"):
        try:
            return {"info": cmd_info, "verify": cmd_verify,
                    "unlivp": cmd_unlivp, "pair": cmd_pair}[argv[0]](argv[1:])
        except (LivePhotoError, RuntimeError, FileNotFoundError) as e:
            print("错误:", e)
            return 1
    return cmd_cli()


if __name__ == "__main__":
    sys.exit(main())
