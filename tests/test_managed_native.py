"""Semantic preparation is deterministic, owner-relative and fail-closed."""
import base64
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "viewer"))
from managed_native import Adapter, check_read, relative
from activity_api import multipart


def request(path, fields=None, files=None):
    body, content_type = multipart(fields or {}, files or {})
    return {"method": "POST", "path": path, "content_type": content_type,
            "body": base64.b64encode(body).decode()}


def adapter(worker="ash", **capabilities):
    return Adapter({"worker_id": worker, "capabilities": capabilities})


def test_props_preparation_preserves_content_identity_and_resolved_settings():
    native = request("/api/props", {"settings": json.dumps({"names": ["cube"], "atlas": 1024})},
                     {"asset": ("cube.glb", b"GLB bytes")})
    operation = adapter(props={"enabled": True})
    first = operation.prepare(native)
    second = operation.prepare(first["request"])
    assert first["spec"] == second["spec"]
    assert first["spec"]["inputs"] == [{"field": "asset", "filename": "cube.glb", "size": 9,
                                        "sha256": hashlib.sha256(b"GLB bytes").hexdigest()}]
    assert first["spec"]["settings"]["atlas"] == 1024


def test_host_build_identity_is_verified_without_false_cross_host_binary_equality(tmp_path):
    builds = []
    for worker in ("ash", "rowan"):
        binary = tmp_path / worker
        binary.write_bytes(worker.encode())
        builds.append(adapter(worker, props={"enabled": True, "installation": [{
            "id": "native-build", "path": str(binary), "sha256": hashlib.sha256(worker.encode()).hexdigest()}]}))
    native = request("/api/props", files={"asset": ("cube.glb", b"bytes")})
    ash, rowan = [worker.prepare(native) for worker in builds]
    assert ash["spec"] == rowan["spec"]
    assert ash["execution_identity"] != rowan["execution_identity"]
    (tmp_path / "ash").write_bytes(b"changed binary")
    with pytest.raises(ValueError, match="identity changed"):
        builds[0].prepare(native)


def test_unavailable_capability_preserves_operator_reason():
    operation = adapter(props={"enabled": False, "reason": "operator gate"})
    with pytest.raises(ValueError, match="operator gate"):
        operation.prepare(request("/api/props", files={"asset": ("cube.glb", b"bytes")}))


@pytest.mark.parametrize("path", ["../model.glb", "/etc/passwd", "a/../../b", "a\\b", "a//b", ".activity.jsonl"])
def test_references_cannot_escape_worker_output_or_expose_live_state(path):
    with pytest.raises(ValueError):
        relative(path)


@pytest.mark.parametrize("field", ["command", "shell", "executable", "from_latents", "reuse_shape"])
def test_native_settings_cannot_choose_commands_or_request_stage_takeover(field):
    operation = adapter(props={"enabled": True})
    with pytest.raises(ValueError, match="forbidden"):
        operation.prepare(request("/api/props", {"settings": json.dumps({field: "arbitrary"})}))


def test_materializing_export_cannot_bypass_durable_mutation_protocol():
    for path in ("/api/skins/all?asset=a&file=x", "/api/skins/ash__123456789abc/dressed?file=x"):
        with pytest.raises(ValueError, match="durable"):
            check_read(path)


def test_credential_material_never_enters_native_execution():
    payload = {"method": "POST", "path": "/api/hf/sign-in", "content_type": "application/json",
               "body": base64.b64encode(b'{"token":"private"}').decode()}
    with pytest.raises(ValueError, match="credentials"):
        adapter().prepare(payload)


def test_finish_without_picture_binds_the_settings_the_native_job_uses(tmp_path, monkeypatch):
    import finish_api
    monkeypatch.setattr(finish_api, "GENERATED_ROOT", tmp_path / "generated")
    operation = adapter(**{"finish": {"enabled": True}, "finish:mesh": {"enabled": True}})
    prepared = operation.prepare(request("/api/finish", files={"asset": ("cube.glb", b"mesh")}))
    manager = finish_api.FinishJobManager(tmp_path / "finish", tmp_path / "generated")
    job = manager.create("cube.glb", b"mesh", None, prepared["spec"]["settings"])
    assert prepared["spec"]["settings"] == job.settings
    assert operation.prepare(prepared["request"])["spec"] == prepared["spec"]


def test_mesh_finish_authorization_does_not_allow_photo_hero_or_native_replay(tmp_path, monkeypatch):
    import finish_api
    source = tmp_path / "generated"
    source.mkdir()
    monkeypatch.setattr(finish_api, "GENERATED_ROOT", source)
    operation = adapter(**{"finish:mesh": {"enabled": True}})
    with pytest.raises(ValueError, match="finish is unavailable"):
        operation.prepare(request("/api/finish", files={
            "asset": ("cube.glb", b"mesh"), "image": ("front.png", b"image")}))
    with pytest.raises(ValueError, match="Hero needs"):
        operation.prepare(request("/api/finish", {"settings": '{"hero":true}'},
                                  {"asset": ("cube.glb", b"mesh")}))
    (source / "mesh.dump").write_bytes(b"native replay input")
    (source / "mesh.json").write_text(json.dumps({
        "backend": "pixal3d", "output": {"sha256": hashlib.sha256(b"mesh").hexdigest()},
        "post_dump": "mesh.dump"}))
    with pytest.raises(ValueError, match="finish is unavailable"):
        operation.prepare(request("/api/finish", files={"asset": ("cube.glb", b"mesh")}))
