"""Typed Graft adapter around the installed AssetFurnace operations.

Only the worker imports this module. It reuses native validators and dispatchers;
there is no executable/path selection supplied by a remote request.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import unquote, urlsplit

import generate_api as native
import finish_api
import props_api
import autorig_api

POST_ROUTES = (
    (r"/api/chains", "chains"), (r"/api/generate", "generate"),
    (r"/api/image", "image"), (r"/api/image/back", "image.back"),
    (r"/api/finish", "finish"), (r"/api/finish/runs/[A-Za-z0-9_.-]+/resume", "finish.resume"),
    (r"/api/props", "props"), (r"/api/props/runs/[A-Za-z0-9_.-]+/turn", "props.turn"),
    (r"/api/rig/rebind", "rig.rebind"), (r"/api/animate/rig", "animate.rig"),
    (r"/api/animate/play", "animate.play"), (r"/api/skins", "skins.create"),
    (r"/api/skins/[A-Za-z0-9_-]+/(try|approve|build|discard)", "skins.action"),
    (r"/api/skins/all", "skins.export"),
    (r"/api/skins/[A-Za-z0-9_-]+/dressed", "skins.dressed"),
    (r"/api/(?:chains|generate|image|finish|props|animate)/[0-9a-f]{32}/cancel", "cancel"),
    (r"/api/(?:rig/rebind|skins/build)/[0-9a-f]{32}/cancel", "cancel"),
    (r"/api/assets/[0-9a-f]{12}/(?:name|hidden|main|delete)", "assets.edit"),
    (r"/api/fix-spot/(?:preview|keep|discard)", "fix-spot"),
    (r"/api/bug-report", "bug-report"), (r"/api/trellis/input-advice", "input-advice"),
    (r"/api/(?:blender|gltfpack)/install", "admin.install"),
    (r"/api/setup/run", "admin.setup"),
    (r"/api/setup/[A-Za-z0-9_-]+/(?:download|rebuild|cancel|remove)", "admin.backend"),
    (r"/api/hf/sign-in", "admin.credentials"),
)
READ_ROUTES = re.compile(r"/api/(?:catalog|backends|recipes|setup(?:/[A-Za-z0-9_./-]+)?|welcome|update-check|hf/status|"
                         r"image(?:/[A-Za-z0-9_./-]+)?|generate/[A-Za-z0-9_./-]+|chains(?:/[0-9a-f]{32})?|"
                         r"activity|thumb|preview|model-info|bug-report|assets(?:/[0-9a-f]{12}/delete-plan)?|"
                         r"finish/[A-Za-z0-9_./-]+|props/[A-Za-z0-9_./-]+|rig/rebind/[A-Za-z0-9_./-]+|"
                         r"animate/[A-Za-z0-9_./-]+|skins(?:/[A-Za-z0-9_./-]+)?)")
PATH_KEYS = {"model", "generated", "picture", "finished", "rigged", "file", "path", "scene", "sidecar"}
FORBIDDEN_KEYS = {"output_dir", "executable", "command", "argv", "shell", "python", "models", "vendor_root", "cli", "from_latents", "reuse_shape"}


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("expected an output-relative reference")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} or (part.startswith(".") and not (index == 0 and part == ".fix-previews"))
                                for index, part in enumerate(value.split("/"))):
        raise ValueError("absolute, hidden and traversing paths are forbidden")
    return value


def digest(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            sha.update(block)
    return sha.hexdigest()


def check_read(path):
    parsed = urlsplit(path)
    decoded = unquote(parsed.path)
    if parsed.scheme or parsed.netloc or "\\" in decoded or any(p in {".", ".."} for p in decoded.split("/")):
        raise ValueError("unsafe native read path")
    if decoded.startswith("/output/"):
        relative(decoded[len("/output/"):])
    elif not READ_ROUTES.fullmatch(decoded):
        raise ValueError("not a native read operation")
    # These legacy GETs produce new files, so they must be durable mutations instead.
    if decoded == "/api/skins/all" or decoded.endswith("/dressed"):
        raise ValueError("skin export materialization requires a durable managed request")


class Adapter:
    def __init__(self, config):
        self.config = config
        self.worker = config["worker_id"]
        self.capabilities = config.get("capabilities", {})
        self.hash_cache = {}

    def capability(self, name):
        entry = self.capabilities.get(name)
        if not entry or entry.get("enabled") is not True:
            reason = (entry or {}).get("reason", "not installed or not authorized")
            raise ValueError(f"{name} is unavailable on {self.worker}: {reason}")
        return entry

    def identities(self, entries):
        result = {}
        for entry in entries:
            path = Path(entry["path"])
            stat = path.stat()
            key = (str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            actual = self.hash_cache.get(key)
            if actual is None:
                actual = digest(path)
                self.hash_cache[key] = actual
            if actual != entry["sha256"]:
                raise ValueError(f"installed content identity changed: {entry['id']}")
            identity = {"sha256": actual, "size": stat.st_size}
            if entry["id"] in result and result[entry["id"]] != identity:
                raise ValueError("conflicting content identity")
            result[entry["id"]] = identity
        return result

    def prepare(self, request):
        if set(request) != {"method", "path", "content_type", "body"} or request["method"] != "POST":
            raise ValueError("expected a typed native mutation")
        parsed = urlsplit(request["path"])
        path = unquote(parsed.path)
        if parsed.query or parsed.fragment or parsed.netloc or parsed.scheme or "\\" in path or ".." in path.split("/"):
            raise ValueError("unsafe native mutation path")
        operation = next((name for pattern, name in POST_ROUTES if re.fullmatch(pattern, path)), None)
        if operation is None:
            raise ValueError("operation is not exposed by the managed native interface")
        body = base64.b64decode(request["body"], validate=True)
        ctype = request["content_type"]
        if len(body) > 768 * 1024 * 1024:
            raise ValueError("managed native request exceeds the native 768 MiB bundle limit")
        multipart = ctype.startswith("multipart/form-data;")
        if operation in {"chains", "generate", "finish", "props", "rig.rebind", "animate.rig", "input-advice"} and not multipart:
            raise ValueError(f"{operation} requires the native multipart input contract")
        files = {}
        if multipart:
            form = native.parse_multipart(ctype, body)
            fields = {key: item["value"] for key, item in form.items() if not item.get("filename")}
            for key, item in form.items():
                if item.get("filename"):
                    filename = item["filename"]
                    if Path(filename).name != filename or "\\" in filename or '"' in filename or "\n" in filename:
                        raise ValueError("uploaded filename must be a plain basename")
                    files[key] = (filename, item["data"])
        else:
            if body and not ctype.startswith("application/json"):
                raise ValueError("native mutation must use JSON or multipart")
            fields = json.loads(body or b"{}")
            if not isinstance(fields, dict):
                raise ValueError("native mutation expects an object")
        if any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key)
               for key in (*fields, *files)):
            raise ValueError("invalid native form field")
        if operation == "admin.credentials":
            raise ValueError("credentials must be provisioned by the worker owner; remote credential persistence is disabled")

        def object_field(key):
            value = fields.get(key, {})
            value = json.loads(value or "{}") if isinstance(value, str) else value
            if not isinstance(value, dict):
                raise ValueError(f"{key} must be an object")
            return value

        def put(key, value):
            fields[key] = json.dumps(value, sort_keys=True) if multipart else value

        def reject_paths(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in FORBIDDEN_KEYS:
                        raise ValueError(f"remote runtime/path override is forbidden: {key}")
                    reject_paths(item)
            elif isinstance(value, list):
                for item in value:
                    reject_paths(item)
        reject_paths(fields)
        settings = object_field("settings")
        reject_paths(settings)
        backend = None
        refs = []
        caps = []
        if operation == "generate":
            backend = settings.pop("backend", "trellis")
            route = native.backend_spec(backend)
            if route is None:
                raise ValueError("unknown requested backend")
            metadata = {k: settings.pop(k) for k in ("output_name", "debug") if k in settings}
            settings = route.validate_settings(settings)
            put("settings", {**settings, **metadata, "backend": backend})
            if settings.get("seed", 0) < 0:
                raise ValueError("managed requests require a resolved nonnegative seed")
            caps = ["generate:" + backend + (":multiview" if backend == "pixal3d" and "back" in files else "")]
        elif operation == "chains":
            chain = native.chain_from_form({**{k: {"value": v} for k, v in fields.items()},
                                            **{k: {"filename": n, "data": d} for k, (n, d) in files.items()}})
            if not chain.steps or any(step not in native.activity_api.KIND for step in chain.steps):
                raise ValueError("chain contains an unavailable step")
            backend = chain.engine or "trellis"
            route = native.backend_spec(backend)
            if route is None:
                raise ValueError("chain backend is unknown")
            chain.model_seed = chain.model_seed if chain.model_seed is not None else route.default_settings.get("seed", 0)
            fields["engine"] = backend
            fields["model_seed"] = str(chain.model_seed)
            put("picture_settings", native.image_api.clean_settings(chain.picture_settings))
            put("finish_settings", finish_api.normalise_settings(chain.finish_settings))
            if chain.model_seed < 0 or object_field("picture_settings")["seed"] < 0:
                raise ValueError("managed chains require resolved nonnegative seeds")
            if "finished" in chain.steps and finish_api.fit_to_machine(object_field("finish_settings")) != object_field("finish_settings"):
                raise ValueError("requested chain repaint is unavailable; no implicit backend substitution")
            if "animated" in chain.steps:
                preset = next((p for p in autorig_api.presets() if p["id"] == chain.first_move), None)
                if preset is None:
                    raise ValueError("requested animation preset is unavailable")
                chain.move_settings = {
                    "arm_spread": autorig_api.clamp_spread(chain.move_settings.get("arm_spread", 0)),
                    "speed": autorig_api.clamp_speed(chain.move_settings.get("speed"), preset)}
                put("move_settings", chain.move_settings)
            settings = {"steps": chain.steps, "engine": backend, "model_seed": chain.model_seed,
                        "picture_settings": object_field("picture_settings"),
                        "finish_settings": object_field("finish_settings"), "move_settings": chain.move_settings}
            refs.extend(relative(v) for v in chain.made.values() if v)
            cap_names = {"picture": "image", "back": "image.back", "model": "generate:" + backend,
                         "finished": "finish", "split": "props", "rigged": "animate.rig", "animated": "animate.play"}
            caps = [cap_names[step] for step in chain.steps]
            if backend == "pixal3d" and "model" in chain.steps and ("back" in chain.steps or chain.made.get("back")):
                caps = ["generate:pixal3d:multiview" if cap == "generate:pixal3d" else cap for cap in caps]
        elif operation == "image":
            settings = native.image_api.clean_settings(settings)
            if settings["seed"] < 0:
                raise ValueError("managed requests require a resolved nonnegative seed")
            put("settings", settings)
            caps = [operation]
        elif operation == "image.back":
            fields["seed"] = int(fields.get("seed", 3))
            settings = {"seed": fields["seed"]}
            caps = [operation]
        elif operation == "finish":
            settings = finish_api.normalise_settings(settings)
            if finish_api.fit_to_machine(settings) != settings:
                raise ValueError("requested repaint is unavailable; no implicit backend substitution")
            asset = files.get("asset")
            if asset is None or not asset[0].lower().endswith(".glb"):
                raise ValueError("Finish requires an uploaded .glb asset")
            if "image" not in files:
                if settings["hero"]:
                    raise ValueError("Hero needs the picture the model was made from.")
                # The native manager makes this same adjustment before writing settings.
                # Bind it before acceptance rather than recording a photo stage that never runs.
                settings.update(skip_photo=True, skip_paint=True)
            dump = finish_api.find_source_dump(asset[1], finish_api.GENERATED_ROOT)
            mesh_only = "image" not in files and dump is None and not settings["hero"]
            if mesh_only:
                backend = "blender"
            put("settings", settings)
            # A mesh-only qualification must not authorize photo, Hero or native replay.
            caps = ["finish:mesh" if mesh_only else "finish"]
        elif operation == "props":
            settings = props_api.normalise_settings(settings)
            put("settings", settings)
            caps = [operation]
        elif operation == "animate.play":
            preset = next((p for p in autorig_api.presets() if p["id"] == fields.get("preset")), None)
            if preset is None:
                raise ValueError("requested animation preset is unavailable")
            fields["arm_spread"] = autorig_api.clamp_spread(fields.get("arm_spread", 0))
            fields["speed"] = autorig_api.clamp_speed(fields.get("speed"), preset)
            settings = {key: fields[key] for key in ("preset", "arm_spread", "speed")}
            caps = [operation]
        elif operation == "props.turn":
            refs.append("props/" + path.split("/")[4])
            settings = {"prop": str(fields.get("prop", "")), "degrees": props_api.wrap_degrees(float(fields.get("degrees", 90)))}
            props_api.normalise_names([settings["prop"]])
            fields.update(settings)
            caps = ["props"]
        elif operation == "finish.resume":
            refs.append("finish/" + path.split("/")[4])
            caps = ["finish"]  # existing owner-local native finish operation, not Graft takeover
        elif operation == "cancel":
            refs.append(path.rsplit("/", 1)[0])
        elif operation == "assets.edit":
            refs.append(path.split("/")[3])
        elif operation == "skins.create":
            refs.append(str(fields.get("asset", "")))
            caps = ["image.back"]
        elif operation == "skins.action":
            refs.append("/api/skins/" + path.split("/")[3])
            action = path.rsplit("/", 1)[1]
            caps = [] if action == "discard" else ["skins.build" if action == "build" else "image.back"]
        elif operation in {"skins.export", "skins.dressed"}:
            if operation == "skins.export":
                refs.append(str(fields.get("asset", "")))
            else:
                refs.append("/api/skins/" + path.split("/")[3])
        elif operation.startswith("admin."):
            caps = [operation]
        elif operation not in {"fix-spot", "bug-report"}:
            caps = [operation]
        for key in PATH_KEYS:
            if fields.get(key) and isinstance(fields[key], str):
                refs.append(relative(fields[key]))
        if operation == "fix-spot":
            for key in ("from", "base", "preview"):
                if fields.get(key):
                    refs.append(relative(fields[key]))
        models, runtimes, installations = [], [], []
        resources = {"accelerator_count": 0, "min_vram_bytes": 0}
        for name in sorted(set(caps)):
            cap = self.capability(name)
            models.extend(cap.get("models", []))
            runtimes.extend(cap.get("runtime", []))
            installations.extend(cap.get("installation", []))
            for key in resources:
                resources[key] = max(resources[key], cap.get("resources", {}).get(key, 0))
        inputs = [{"field": key, "filename": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for key, (name, data) in sorted(files.items())]
        for ref in refs:
            if ref.startswith("/api/") or "/" not in ref:
                continue
            candidate = native.OUTPUT_ROOT / ref
            if candidate.is_file():
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(native.OUTPUT_ROOT.resolve()):
                    raise ValueError("asset reference escapes output root")
                inputs.append({"ref": ref, "size": resolved.stat().st_size, "sha256": digest(resolved)})
        spec = {"operation": operation, "backend": backend, "settings": settings,
                "inputs": inputs, "asset_refs": sorted(set(refs)), "parameters": fields,
                "models": self.identities(models), "runtime": self.identities(runtimes),
                "resources": resources}
        if multipart:
            body, ctype = native.activity_api.multipart(fields, files)
        else:
            body = json.dumps(fields, sort_keys=True, allow_nan=False).encode()
            ctype = "application/json"
        return {"worker": self.worker, "spec": spec, "execution_identity": self.identities(installations), "request": {
            "method": "POST", "path": path, "content_type": ctype, "body": base64.b64encode(body).decode()}}

    def library(self):
        from assetfurnace_completion import MARKER_SUFFIX, load_and_verify
        from completion_api import _state_root
        payload = native.assets_payload(native.OUTPUT_ROOT)
        bindings = []
        verified = {}
        state = _state_root()
        if state.is_dir():
            for marker in state.rglob("*" + MARKER_SUFFIX):
                rel = marker.relative_to(state).as_posix()[:-len(MARKER_SUFFIX)]
                try:
                    seal = load_and_verify(native.OUTPUT_ROOT, state, rel)
                except (OSError, ValueError):
                    continue
                for ref in [rel] + [rel + "/" + name for name in seal["files"]]:
                    verified[ref] = seal
                    bindings.append({"ref": ref, "owner": seal["owner"], "evidence": seal["manifest_sha256"]})
                for meta in seal["files"].values():
                    bindings.append({"ref": "sha256:" + meta["sha256"], "owner": seal["owner"], "evidence": seal["manifest_sha256"]})
        for asset in payload["assets"]:
            files = [asset.get(key) for key in ("picture", "model", "finished", "rigged")]
            files.extend(item.get("file") for key in ("props", "clips", "skins") for item in asset.get(key, []))
            seals = [verified[value] for value in files if isinstance(value, str) and value in verified]
            owners = {seal["owner"] for seal in seals}
            if len(owners) == 1:
                for key in ("id", "family"):
                    bindings.append({"ref": asset[key], "owner": seals[0]["owner"], "evidence": [s["manifest_sha256"] for s in seals]})
                for skin in asset.get("skins", []):
                    seal = verified.get(skin.get("file"))
                    if seal is not None and seal["owner"] == seals[0]["owner"]:
                        bindings.append({"ref": "/api/skins/" + skin["id"], "owner": seal["owner"],
                                         "evidence": seal["manifest_sha256"]})
        return {**payload, "worker": self.worker, "bindings": bindings}
