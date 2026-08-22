"""Content-addressed snapshots, model fingerprints, and signed manifests."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import openstudio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


PROJECT = Path(__file__).resolve().parents[2]


def _var_dir() -> Path:
    # Tests and local project instances can relocate the complete workbench state.
    from workbench import db
    return db.VAR_DIR


def _object_root() -> Path:
    return _var_dir() / "objects"


def _blob_root() -> Path:
    return _object_root() / "sha256"


def _snapshot_root() -> Path:
    return _object_root() / "manifests"


def _key_path() -> Path:
    return _var_dir() / "keys/signing.key"

SHAPEFILE_SUFFIXES = {
    ".shp", ".dbf", ".shx", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".shp.xml",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dataset_components(path: Path) -> list[Path]:
    path = path.resolve()
    if path.suffix.lower() != ".shp":
        return [path]
    prefix = path.stem
    components = []
    for candidate in path.parent.glob(f"{prefix}.*"):
        tail = candidate.name[len(prefix):].lower()
        if candidate.is_file() and tail in SHAPEFILE_SUFFIXES:
            components.append(candidate)
    required = {".shp", ".dbf", ".shx"}
    present = {item.name[len(prefix):].lower() for item in components}
    missing = required - present
    if missing:
        raise ValueError(f"Shapefile is incomplete; missing {', '.join(sorted(missing))}")
    return sorted(components, key=lambda item: item.name.lower())


def snapshot_descriptor(path: Path, *, kind: str) -> dict[str, Any]:
    components = [{
        "name": component.name,
        "sha256": sha256_file(component),
        "size_bytes": component.stat().st_size,
    } for component in dataset_components(path)]
    identity = {"kind": kind, "components": components}
    return {
        "schema_version": 1,
        "snapshot_hash": hashlib.sha256(canonical_json_bytes(identity)).hexdigest(),
        "kind": kind,
        "source_name": path.name,
        "components": components,
    }


def _blob_path(digest: str) -> Path:
    return _blob_root() / digest[:2] / digest


def ensure_snapshot(path: Path, *, kind: str) -> dict[str, Any]:
    descriptor = snapshot_descriptor(path, kind=kind)
    sources = {item.name: item for item in dataset_components(path)}
    for component in descriptor["components"]:
        target = _blob_path(component["sha256"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
            try:
                shutil.copy2(sources[component["name"]], temporary)
                if sha256_file(temporary) != component["sha256"]:
                    raise IOError(f"Snapshot copy hash mismatch: {component['name']}")
                temporary.replace(target)
                target.chmod(0o444)
            finally:
                temporary.unlink(missing_ok=True)
        component["blob"] = str(target.relative_to(_object_root()))

    snapshot_root = _snapshot_root()
    snapshot_root.mkdir(parents=True, exist_ok=True)
    manifest_path = snapshot_root / f"{descriptor['snapshot_hash']}.json"
    if not manifest_path.exists():
        payload = descriptor | {"created_at": utcnow()}
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_bytes(canonical_json_bytes(payload))
        temporary.replace(manifest_path)
        manifest_path.chmod(0o444)
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def load_snapshot(snapshot_hash: str) -> dict[str, Any]:
    path = _snapshot_root() / f"{snapshot_hash}.json"
    if not path.exists():
        raise FileNotFoundError(f"Snapshot manifest not found: {snapshot_hash}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_snapshot_source(path: Path, snapshot_hash: str, *, kind: str) -> dict[str, Any]:
    """Do the files on disk still hold the bytes this snapshot registered?

    The identity hash covers the component *names* as well as their contents,
    which is right for a multi-part dataset - renaming two shapefile sidecars
    past each other changes what the dataset means without changing a byte.

    It is wrong for a single file, and it produced a permanent false alarm:
    uploads were registered while still under their `tmpXXXXXX` upload name and
    then stored under the name the operator gave them, so the recomputed hash
    could never match and health reported "Registered input snapshot no longer
    matches source files" for inputs whose bytes had never changed.  Measured
    2026-08-22 on the live installation: the template and the EPW both
    mismatched, both with byte-identical content.

    So a single-component dataset whose one file still has the registered
    SHA-256 is reported as intact, with `renamed` saying why the hash moved.
    Anything with more than one component, or any content difference at all,
    stays a mismatch: the point of this check is content, and nothing here
    relaxes that.
    """
    actual = snapshot_descriptor(path, kind=kind)
    ok = actual["snapshot_hash"] == snapshot_hash
    renamed = False
    if not ok:
        try:
            registered = load_snapshot(snapshot_hash)["components"]
        except (FileNotFoundError, KeyError, ValueError):
            registered = []
        current = actual["components"]
        if len(registered) == 1 and len(current) == 1:
            renamed = (registered[0]["sha256"] == current[0]["sha256"]
                       and registered[0]["size_bytes"] == current[0]["size_bytes"]
                       and registered[0]["name"] != current[0]["name"])
            ok = renamed
    return {
        "ok": ok,
        "renamed": renamed,
        "expected": snapshot_hash,
        "actual": actual["snapshot_hash"],
        "components": actual["components"],
    }


def snapshot_files(snapshot_hash: str) -> list[tuple[str, Path]]:
    manifest = load_snapshot(snapshot_hash)
    output = []
    for component in manifest["components"]:
        path = _object_root() / component["blob"]
        if not path.exists() or sha256_file(path) != component["sha256"]:
            raise IOError(f"Object-store blob failed verification: {component['name']}")
        output.append((component["name"], path))
    return output


def garbage_collect(referenced_hashes: set[str], *, grace_days: int = 30,
                    now: datetime | None = None) -> dict[str, Any]:
    """Delete only snapshots that stayed unreferenced beyond the grace period."""
    now = now or datetime.now(timezone.utc)
    root = _snapshot_root()
    root.mkdir(parents=True, exist_ok=True)
    state_path = _object_root() / "gc-candidates.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    manifests = {path.stem: path for path in root.glob("*.json")}
    for snapshot_hash in list(state):
        if snapshot_hash in referenced_hashes or snapshot_hash not in manifests:
            state.pop(snapshot_hash, None)
    for snapshot_hash in manifests:
        if snapshot_hash not in referenced_hashes and snapshot_hash not in state:
            state[snapshot_hash] = now.isoformat()

    expired = {
        snapshot_hash for snapshot_hash, first_seen in state.items()
        if now - datetime.fromisoformat(first_seen) >= timedelta(days=grace_days)
    }
    retained_blob_hashes: set[str] = set()
    for snapshot_hash, manifest_path in manifests.items():
        if snapshot_hash in expired:
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        retained_blob_hashes.update(item["sha256"] for item in manifest["components"])

    deleted_snapshots: list[str] = []
    deleted_blobs: list[str] = []
    for snapshot_hash in sorted(expired):
        manifest_path = manifests.get(snapshot_hash)
        if manifest_path is None:
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for component in manifest["components"]:
            digest = component["sha256"]
            if digest not in retained_blob_hashes:
                blob = _blob_path(digest)
                if blob.exists():
                    blob.chmod(0o644)
                    blob.unlink()
                    deleted_blobs.append(digest)
        manifest_path.chmod(0o644)
        manifest_path.unlink()
        state.pop(snapshot_hash, None)
        deleted_snapshots.append(snapshot_hash)

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(canonical_json_bytes(state))
    return {
        "deleted_snapshots": deleted_snapshots,
        "deleted_blobs": deleted_blobs,
        "pending": sorted(state),
    }


def canonical_model_fingerprint(osm_path: Path) -> str:
    translator = openstudio.osversion.VersionTranslator()
    optional = translator.loadModel(openstudio.toPath(str(osm_path)))
    if optional.isNull():
        raise ValueError(f"OpenStudio model cannot be loaded: {osm_path}")
    workspace = openstudio.energyplus.ForwardTranslator().translateModel(optional.get())
    with tempfile.NamedTemporaryFile(suffix=".idf", delete=False) as handle:
        idf_path = Path(handle.name)
    try:
        if not workspace.save(openstudio.toPath(str(idf_path)), True):
            raise IOError("Canonical IDF could not be written")
        workspace_text = idf_path.read_text(encoding="utf-8", errors="replace")
    finally:
        idf_path.unlink(missing_ok=True)
    blocks = []
    for raw in workspace_text.split(";"):
        clean = re.sub(r"!.*", "", raw)
        clean = re.sub(
            r"\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?",
            "<UUID>", clean,
        )
        clean = re.sub(r"\s+", " ", clean).strip(" ,\n\t")
        if clean:
            blocks.append(clean + ";")
    return hashlib.sha256("\n".join(sorted(blocks)).encode("utf-8")).hexdigest()


def environment_manifest() -> dict[str, Any]:
    try:
        packages = subprocess.check_output(
            [sys.executable, "-m", "pip", "freeze", "--all"], text=True, timeout=30,
        ).splitlines()
    except Exception as exc:
        packages = [f"ERROR: {exc}"]
    versions: dict[str, str] = {}
    for module_name in ("geopandas", "pandas", "numpy", "shapely", "pyproj"):
        try:
            module = __import__(module_name)
            versions[module_name] = str(getattr(module, "__version__", "unknown"))
        except Exception as exc:
            versions[module_name] = f"unavailable: {exc}"
    return {
        "schema_version": 1,
        "created_at": utcnow(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
        "openstudio": openstudio.openStudioVersion(),
        "libraries": versions,
        "pip_freeze": sorted(packages),
    }


def _private_key() -> Ed25519PrivateKey:
    key_path = _key_path()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        temporary = key_path.with_suffix(".tmp")
        temporary.write_bytes(raw)
        temporary.chmod(0o600)
        temporary.replace(key_path)
    os.chmod(key_path, 0o600)
    return Ed25519PrivateKey.from_private_bytes(key_path.read_bytes())


def sign_manifest(manifest: dict[str, Any]) -> dict[str, str]:
    key = _private_key()
    payload = canonical_json_bytes(manifest)
    signature = key.sign(payload)
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    return {
        "algorithm": "Ed25519",
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "public_key_base64": base64.b64encode(public).decode("ascii"),
        "signature_base64": base64.b64encode(signature).decode("ascii"),
    }


def verify_signed_manifest(manifest: dict[str, Any], signature: dict[str, str]) -> bool:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    payload = canonical_json_bytes(manifest)
    if hashlib.sha256(payload).hexdigest() != signature["manifest_sha256"]:
        return False
    public = Ed25519PublicKey.from_public_bytes(base64.b64decode(signature["public_key_base64"]))
    try:
        public.verify(base64.b64decode(signature["signature_base64"]), payload)
    except Exception:
        return False
    return True
