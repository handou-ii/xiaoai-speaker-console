"""音频时长探测（纯 Python，不依赖 ffmpeg）

容器里装 ffmpeg 要拉几百兆、apt 又慢，而我们只需要"这首歌多长"。
mutagen 覆盖常见格式，拿不到就返回 0，由调用方兜底。

坑：曲库里常有「后缀与内容不符」的文件（.flac 实际是 OggS）。
mutagen 默认按后缀选解析器，遇到这种会直接抛异常 → 时长变 0 →
播放器按 200s 兜底，长歌被提前切掉。所以失败后用文件对象再试一次，
让 mutagen 自己嗅探头部；sniff_ext() 则给推流地址用。
"""
import logging

log = logging.getLogger("audio")

try:
    from mutagen import File as _MFile
except ImportError:                      # 极简环境下降级为固定时长
    _MFile = None
    log.warning("未安装 mutagen，音频时长将使用估算值")

# 文件头 → 真实后缀（按顺序匹配，够用即可）
_MAGIC = (
    (b"fLaC", ".flac"),
    (b"OggS", ".ogg"),
    (b"RIFF", ".wav"),
    (b"MAC ", ".ape"),
    (b"ID3", ".mp3"),
    (b"\xff\xfb", ".mp3"),
    (b"\xff\xf3", ".mp3"),
    (b"\xff\xf2", ".mp3"),
)


def sniff_ext(path):
    """按文件头判断真实格式，返回后缀（含点）；认不出来返回空串"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return ""
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    if head[4:8] == b"ftyp":             # MP4 / M4A
        return ".m4a"
    return ""


def _length(f):
    if f is None or getattr(f, "info", None) is None:
        return 0.0
    try:
        length = float(f.info.length)
    except Exception:
        return 0.0
    return length if length > 0 else 0.0


def duration_of(path, default=0.0):
    """返回秒数；失败返回 default"""
    if not _MFile:
        return default
    try:
        dur = _length(_MFile(path))
        if dur:
            return dur
    except Exception as e:
        log.debug("按后缀读取时长失败 %s: %s", path, e)

    # 后缀不可信：交给 mutagen 嗅探文件头再试一次
    try:
        with open(path, "rb") as fh:
            dur = _length(_MFile(fh))
            if dur:
                return dur
    except Exception as e:
        log.debug("嗅探读取时长失败 %s: %s", path, e)
    return default
