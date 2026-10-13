"""Access rules: location, NVR, and camera grants, with denies and role gates."""

from src.access_control import camera_allowed, explain_access, filter_camera_status_payload


def _actor(perms, grants=None, denies=None, *, unrestricted=False, active=True):
    return {
        "isActive": active,
        "role": {
            "unrestricted": unrestricted,
            "permissions": perms,
        },
        "grants": grants or [],
        "denies": denies or [],
    }


def _cam(device_id, location, nvrs):
    return {
        "deviceId": device_id,
        "location": location,
        "nvrGroups": nvrs,
        "networkStatus": "ONLINE",
        "previewStatus": "AVAILABLE",
    }


def test_super_admin_sees_every_camera():
    actor = _actor([], unrestricted=True)
    cam = _cam("c1", "Ahmedabad", ["NVR-01"])
    assert camera_allowed(actor, cam)


def test_operator_without_grants_sees_nothing():
    actor = _actor(["cameras.view", "nvr.access", "locations.access"])
    assert not camera_allowed(actor, _cam("c1", "Ahmedabad", ["NVR-01"]))


def test_location_grant_includes_cameras_at_that_site():
    actor = _actor(
        ["cameras.view", "locations.access"],
        grants=[{"scopeType": "LOCATION", "scopeId": "Ahmedabad"}],
    )
    assert camera_allowed(actor, _cam("c1", "Ahmedabad", ["NVR-01"]))
    assert not camera_allowed(actor, _cam("c2", "Surat", ["NVR-01"]))


def test_location_grant_requires_location_permission():
    actor = _actor(
        ["cameras.view"],
        grants=[{"scopeType": "LOCATION", "scopeId": "Ahmedabad"}],
    )
    assert not camera_allowed(actor, _cam("c1", "Ahmedabad", ["NVR-01"]))


def test_nvr_grant_and_explicit_camera_and_deny():
    actor = _actor(
        ["cameras.view", "nvr.access"],
        grants=[
            {"scopeType": "NVR", "scopeId": "NVR-01"},
            {"scopeType": "CAMERA", "scopeId": "c3"},
        ],
        denies=["c2"],
    )
    assert camera_allowed(actor, _cam("c1", "Ahmedabad", ["NVR-01"]))
    assert not camera_allowed(actor, _cam("c2", "Ahmedabad", ["NVR-01"]))
    assert camera_allowed(actor, _cam("c3", "Surat", ["NVR-09"]))
    assert not camera_allowed(actor, _cam("c4", "Surat", ["NVR-09"]))


def test_inactive_user_is_blocked():
    actor = _actor(["cameras.view"], grants=[{"scopeType": "CAMERA", "scopeId": "c1"}], active=False)
    assert not camera_allowed(actor, _cam("c1", "Ahmedabad", ["NVR-01"]))


def test_status_payload_hides_unauthorized_cameras_and_empty_nvrs():
    actor = _actor(
        ["cameras.view", "nvr.access"],
        grants=[{"scopeType": "NVR", "scopeId": "NVR-01"}],
    )
    payload = {
        "summary": {},
        "cameras": [
            _cam("c1", "Ahmedabad", ["NVR-01"]),
            _cam("c2", "Ahmedabad", ["NVR-02"]),
        ],
        "nvrs": [
            {"nvrId": "NVR-01", "cameras": [_cam("c1", "Ahmedabad", ["NVR-01"])]},
            {"nvrId": "NVR-02", "cameras": [_cam("c2", "Ahmedabad", ["NVR-02"])]},
        ],
    }
    visible = filter_camera_status_payload(payload, actor)
    assert [c["deviceId"] for c in visible["cameras"]] == ["c1"]
    assert [n["nvrId"] for n in visible["nvrs"]] == ["NVR-01"]
    assert visible["summary"]["total"] == 1
    assert payload["cameras"][1]["deviceId"] == "c2"


def test_explain_counts_effective_resources():
    actor = _actor(
        ["cameras.view", "locations.access", "nvr.access"],
        grants=[{"scopeType": "LOCATION", "scopeId": "Ahmedabad"}],
        denies=["c2"],
    )
    summary = explain_access(
        actor,
        [
            _cam("c1", "Ahmedabad", ["NVR-01"]),
            _cam("c2", "Ahmedabad", ["NVR-01"]),
            _cam("c3", "Ahmedabad", ["NVR-02"]),
        ],
    )
    assert summary["cameraCount"] == 2
    assert summary["locationCount"] == 1
    assert summary["nvrCount"] == 2
    assert {p["key"] for p in summary["permissions"]} == {
        "cameras.view",
        "locations.access",
        "nvr.access",
    }


def load_tests(loader, tests, pattern):
    import unittest

    suite = unittest.TestSuite()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            suite.addTest(unittest.FunctionTestCase(fn))
    return suite
