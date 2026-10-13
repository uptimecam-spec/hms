"""NVR export loading. Screenshot lists are add-ons beside the CSV and TXT files."""

from src.nvr_catalog import load_nvr_cameras

_IMAGE_NVRS = {
    "5.11": 14,
    "5.12": 12,
    "5.13": 21,
    "5.14": 22,
}


def test_existing_exports_still_load():
    rows = load_nvr_cameras()
    sources = {row.nvr_source for row in rows}
    for name in ("5.19", "5.20", "6.19", "6.20", "7.20", "7.21", "7.22", "7.23", "7.24"):
        assert name in sources


def test_screenshot_nvrs_load_every_added_camera():
    by_source: dict[str, set[str]] = {}
    for row in load_nvr_cameras():
        by_source.setdefault(row.nvr_source, set()).add(row.ip)

    for source, count in _IMAGE_NVRS.items():
        assert len(by_source[source]) == count

    assert "192.168.4.75" in by_source["5.11"]
    assert "192.168.6.100" in by_source["5.12"]
    assert "192.168.7.151" in by_source["5.13"]
    assert "192.168.5.21" in by_source["5.14"]
