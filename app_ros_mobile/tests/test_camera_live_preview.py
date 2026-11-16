from fastapi import FastAPI
from fastapi.testclient import TestClient
from lelab import ros_mobile


def test_latest_preview_requires_advancing_source_identity(monkeypatch):
    ros_mobile.reset_preview_frame_state()
    frames = [(b"jpeg", {"X-LeLab-Frame-Seq": str(seq),
                         "X-LeLab-Source-Stamp-Ns": str(stamp)})
              for seq, stamp in [(10, 100), (10, 100), (11, 101)]]
    monkeypatch.setattr(ros_mobile, "fetch_preview_frame", lambda **_: frames.pop(0))
    app = FastAPI(); app.include_router(ros_mobile.router)
    client = TestClient(app)
    headers = {"X-LeLab-Viewer": "single-page"}
    assert client.get("/ros-mobile/preview.jpg?fresh=true", headers=headers).status_code == 200
    refused = client.get("/ros-mobile/preview.jpg?fresh=true", headers=headers)
    assert refused.status_code == 409 and "stale_frame_seq" in refused.text
    recovered = client.get("/ros-mobile/preview.jpg?fresh=true", headers=headers)
    assert recovered.status_code == 200
    assert recovered.headers["X-LeLab-Frame-Seq"] == "11"
    ros_mobile.reset_preview_frame_state()


def test_fresh_polling_without_viewer_identity_is_refused(monkeypatch):
    ros_mobile.reset_preview_frame_state()
    monkeypatch.setattr(ros_mobile, "fetch_preview_frame",
                        lambda **_: (b"jpeg", {"X-LeLab-Frame-Seq": "10",
                                               "X-LeLab-Source-Stamp-Ns": "100"}))
    app = FastAPI(); app.include_router(ros_mobile.router)
    client = TestClient(app)
    refused = client.get("/ros-mobile/preview.jpg?fresh=true",
                         headers={"X-LeLab-Client": "shared-browser-id"})
    assert refused.status_code == 400 and "viewer_required" in refused.text
    # The legacy non-fresh thumbnail path still works without a viewer identity.
    assert client.get("/ros-mobile/preview.jpg").status_code == 200
    ros_mobile.reset_preview_frame_state()


def test_two_viewers_track_frame_progress_independently(monkeypatch):
    ros_mobile.reset_preview_frame_state()
    frames = [(b"jpeg-10", {"X-LeLab-Frame-Seq": "10",
                           "X-LeLab-Source-Stamp-Ns": "100"}),
              (b"jpeg-11", {"X-LeLab-Frame-Seq": "11",
                           "X-LeLab-Source-Stamp-Ns": "101"}),
              (b"jpeg-10-duplicate", {"X-LeLab-Frame-Seq": "10",
                                     "X-LeLab-Source-Stamp-Ns": "100"}),
              (b"jpeg-11-duplicate", {"X-LeLab-Frame-Seq": "11",
                                     "X-LeLab-Source-Stamp-Ns": "101"})]
    monkeypatch.setattr(ros_mobile, "fetch_preview_frame", lambda **_: frames.pop(0))
    app = FastAPI()
    app.include_router(ros_mobile.router)
    client = TestClient(app)

    first = client.get("/ros-mobile/preview.jpg?fresh=true",
                       headers={"X-LeLab-Viewer": "page-a"})
    second = client.get("/ros-mobile/preview.jpg?fresh=true",
                        headers={"X-LeLab-Viewer": "page-b"})

    assert first.status_code == 200
    assert first.content == b"jpeg-10"
    assert second.status_code == 200
    assert second.content == b"jpeg-11"
    assert client.get("/ros-mobile/preview.jpg?fresh=true",
                      headers={"X-LeLab-Viewer": "page-a"}).status_code == 409
    assert client.get("/ros-mobile/preview.jpg?fresh=true",
                      headers={"X-LeLab-Viewer": "page-b"}).status_code == 409
    ros_mobile.reset_preview_frame_state()
