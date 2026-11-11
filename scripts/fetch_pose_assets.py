"""Download the pose model that running-form analysis uses in the browser (Google MediaPipe Pose Landmarker, Apache 2.0)
into xcapp/static/vendor/pose, checking each file's SHA-256. The Docker build runs this, so the 55 MB of files never go
into the repository. Run it yourself for local testing:  python scripts/fetch_pose_assets.py
"""
import hashlib
import io
import pathlib
import sys
import tarfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "xcapp" / "static" / "vendor" / "pose"
PACKAGE = ("https://registry.npmjs.org/@mediapipe/tasks-vision/-/tasks-vision-1.2.2.tgz",
           "f45cdb573e76cbd3f289a28d63f5601c632e68d256a3e5b4d82141b013f36b62")
# Two models: "heavy" (more accurate on fast-moving and half-hidden limbs; phones with a GPU use it) and "full" (lighter;
# used when the phone can only run the model on its CPU)
MODELS = {
    "pose_landmarker_heavy.task": ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_heavy/float16/1/pose_landmarker_heavy.task",
                                   "64437af838a65d18e5ba7a0d39b465540069bc8aae8308de3e318aad31fcbc7b"),
    "pose_landmarker_full.task": ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task",
                                  "5134a3aad27a58b93da0088d431f366da362b44e3ccfbe3462b3827a839011b1"),
}
FROM_PACKAGE = {"package/vision_bundle.mjs": "vision_bundle.mjs", "package/wasm/vision_wasm_internal.js": "vision_wasm_internal.js",
                "package/wasm/vision_wasm_internal.wasm": "vision_wasm_internal.wasm", "package/README.md": "README-mediapipe.md"}


def fetch(url: str, sha256: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        sys.exit(f"Checksum mismatch for {url}: {got}")
    return data


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if not all((OUT / name).exists() for name in FROM_PACKAGE.values()):
        with tarfile.open(fileobj=io.BytesIO(fetch(*PACKAGE)), mode="r:gz") as tar:
            for member, name in FROM_PACKAGE.items():
                (OUT / name).write_bytes(tar.extractfile(member).read())
    for name, (url, sha) in MODELS.items():
        if not (OUT / name).exists():
            (OUT / name).write_bytes(fetch(url, sha))
    print(f"Pose files saved in {OUT}")


if __name__ == "__main__":
    main()
