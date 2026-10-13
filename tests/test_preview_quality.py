"""Live previews must use the main stream and must not be replaced by a smaller one."""

from src.nvr_streams import MAIN_STREAM_SUBTYPE, stream_query
from src.streaming import clearer_jpeg, jpeg_dimensions


def _jpeg(width: int, height: int) -> bytes:
    sof = bytes([
        0xFF, 0xC0, 0x00, 0x0B, 0x08,
        (height >> 8) & 0xFF, height & 0xFF,
        (width >> 8) & 0xFF, width & 0xFF,
        0x01, 0x01, 0x11, 0x00,
    ])
    return b"\xff\xd8" + sof + b"\xff\xd9"


def test_preview_queries_use_the_main_stream():
    assert MAIN_STREAM_SUBTYPE == 0
    direct = stream_query(12, direct_camera=True)
    extra = stream_query(1, direct_camera=True, subtype=1)
    assert direct == "channel=1&subtype=0"
    assert extra == "channel=1&subtype=1"
    assert "subtype=1" not in direct


def test_jpeg_dimensions_read_the_frame_size():
    assert jpeg_dimensions(_jpeg(1920, 1080)) == (1920, 1080)
    assert jpeg_dimensions(b"not a jpeg") is None


def test_clearer_jpeg_keeps_the_main_stream_still():
    main = _jpeg(1920, 1080)
    sub = _jpeg(352, 288)
    assert clearer_jpeg(main, sub) is main
    assert clearer_jpeg(sub, main) is main
    assert clearer_jpeg(main, None) is main
    assert clearer_jpeg(None, sub) is sub
