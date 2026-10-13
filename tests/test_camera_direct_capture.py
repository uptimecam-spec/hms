"""Images must come from the camera IP, never from an NVR login path."""

from src.nvr_streams import build_rtsp_url, stream_query


def test_preview_rtsp_is_direct_camera_channel():
    query = stream_query(12, direct_camera=True)
    assert query == "channel=1&subtype=0"


def test_build_rtsp_url_refuses_nvr_channel_style():
    assert build_rtsp_url("192.168.7.24", 12, direct_camera=False) is None


def test_camera_jpeg_path_does_not_use_nvr_host(monkeypatch):
    from src import streaming

    called: list[tuple] = []

    def fake_snapshot(host, channel=1):
        called.append(("snapshot", host, channel))
        return None

    def fake_mjpeg(host, channel=1, *, subtype=0):
        called.append(("mjpeg", host, channel, subtype))
        return None

    def fake_rtsp(host, channel, *, direct_camera, timeout_sec):
        called.append(("rtsp", host, channel, direct_camera))
        return None

    monkeypatch.setattr(streaming, "http_snapshot", fake_snapshot)
    monkeypatch.setattr(streaming, "http_mjpeg_frame", fake_mjpeg)
    monkeypatch.setattr(streaming, "_rtsp_still", fake_rtsp)
    monkeypatch.setattr(streaming, "_ffmpeg_path", lambda: None)
    monkeypatch.setattr(streaming, "_custom_rtsp_jpeg", lambda _device: None)

    streaming._camera_jpeg(
        {
            "ipAddress": "192.168.5.21",
            "nvrHost": "192.168.7.24",
            "nvrChannel": 12,
            "rtspUrl": "",
        }
    )

    hosts = {item[1] for item in called}
    assert hosts == {"192.168.5.21"}
    assert "192.168.7.24" not in hosts
