"""Fail-closed validation for the public portable package (no installed state)."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
APP = "douyindownload/_automation/"
FILES = {
    "DouyinLiveRecorder.exe", APP + "DouyinLiveRecorder.exe",
    APP + "profiles.json", APP + "settings.json", "youtube-dl/ffmpeg.exe",
    "youtube-dl/ffprobe.exe", "README.txt", "LICENSE", "THIRD_PARTY.md",
}
PUBLIC_SETTINGS = {
    "poll_interval_seconds": 60, "ffmpeg_path": "${TOOLS_DIR}\\ffmpeg.exe",
    "quality": "OD", "ytdlp_path": "${TOOLS_DIR}\\yt-dlp.exe",
    "start_hidden_to_tray": False, "start_with_windows": False,
    "not_visible_backoff_seconds": 300, "unsupported_stream_backoff_seconds": 300,
    "error_min_backoff_seconds": 30, "error_max_backoff_seconds": 300,
    "container": "mkv", "language": "zh-CN", "new_profile_poll_interval_seconds": 60,
    "priority_risk_control_backoff_seconds": 60, "standard_risk_control_backoff_seconds": 60,
    "priority_poll_jitter_seconds": 2, "poll_jitter_seconds": 8, "adopt_existing_ffmpeg": True,
    "recording_stall_timeout_seconds": 300, "recording_offline_grace_seconds": 90,
    "recording_segment_max_seconds": 2700, "recording_reconnect_delay_max_seconds": 5,
    "recording_engine_version": 2, "media_poll_interval_seconds": 300,
    "captcha_backoff_seconds": 120,
}


def validate_defaults(profiles, settings):
    if profiles != [] or settings != PUBLIC_SETTINGS:
        raise ValueError("Public profiles/settings differ from approved empty defaults")


def verify_source():
    validate_defaults(json.loads((ROOT / "src/profiles.json").read_text(encoding="utf-8-sig")),
                      json.loads((ROOT / "src/settings.json").read_text(encoding="utf-8-sig")))
    # Only tracked repository material is considered for publication.
    tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    forbidden = re.compile(r"(?i)(^|/)(?:douyin_session\.json|mobile_session\.json|mobile_device\.json|"
                           r"deployed-source\.json|config_backups|logs|\.recording_sessions|"
                           r"browser_profile|\.env(?:\..*)?|id_rsa|id_ed25519)(/|$)|\.(mkv|flv|log|bak|pfx|pem)$")
    token = re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)")
    for name in tracked:
        if forbidden.search(name):
            raise ValueError(f"Private/runtime path is tracked: {name}")
        path = ROOT / name
        if path.is_file() and path.stat().st_size < 2_000_000 and token.search(path.read_bytes()):
            raise ValueError(f"Credential-like content in tracked file: {name}")


def version_value(version):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Version must be X.Y.Z")
    return version


def stage_manifest(directory, version):
    version_value(version)
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}
    if actual != FILES:
        raise ValueError(f"Unexpected staging contents: {sorted(actual ^ FILES)}")
    validate_defaults(json.loads((directory / (APP + "profiles.json")).read_text(encoding="utf-8-sig")),
                      json.loads((directory / (APP + "settings.json")).read_text(encoding="utf-8-sig")))
    manifest = {"version": version, "files": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in sorted(FILES)}}
    (directory / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def verify_zip(path):
    with zipfile.ZipFile(path) as archive:
        names = []
        for member in archive.infolist():
            name = member.filename
            parts = PurePosixPath(name).parts
            if "\\" in name or ":" in name or name.startswith("/") or ".." in parts or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("Unsafe archive path")
            if not member.is_dir():
                names.append(name)
        if len(names) != len(set(names)) or set(names) != FILES | {"release-manifest.json"}:
            raise ValueError("Release contains missing, duplicated, or non-allowlisted files")
        if sum(i.file_size for i in archive.infolist()) > 1_000_000_000:
            raise ValueError("Unexpectedly large release")
        manifest = json.loads(archive.read("release-manifest.json"))
        version_value(manifest["version"])
        if set(manifest["files"]) != FILES:
            raise ValueError("Invalid manifest file list")
        for name in FILES:
            data = archive.read(name)
            if hashlib.sha256(data).hexdigest() != manifest["files"][name]:
                raise ValueError(f"Hash mismatch: {name}")
        validate_defaults(json.loads(archive.read(APP + "profiles.json").decode("utf-8-sig")),
                          json.loads(archive.read(APP + "settings.json").decode("utf-8-sig")))
    return manifest


def smoke_zip(path):
    verify_zip(path)
    with tempfile.TemporaryDirectory(prefix="录制发布验证 ") as temporary:
        directory = Path(temporary)
        with zipfile.ZipFile(path) as archive:
            archive.extractall(directory)
        report = directory / "自检 report.json"
        process = subprocess.run([str(directory / "DouyinLiveRecorder.exe"), "--self-test-report", str(report)],
                                 cwd=directory, timeout=100, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if process.returncode != 0 or not report.is_file():
            raise RuntimeError("Extracted EXE failed its offline self-test")
        result = json.loads(report.read_text(encoding="utf-8"))
        if not result.get("ok"):
            raise RuntimeError(f"EXE self-test failed: {result}")
        print(json.dumps(result, ensure_ascii=False))


def inspect_exe(path):
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(path))
    forbidden = re.compile(r"(?i)(douyin_session|mobile_session|mobile_device|profiles\.json|settings\.json|deployed-source|config_backups|\.log$|^tests[./])")
    for name in archive.toc:
        if forbidden.search(name):
            raise ValueError(f"Private data in frozen executable: {name}")
    for name in archive.toc:
        if name.endswith(".pyz"):
            modules = archive.open_embedded_archive(name).toc
            if any(module.startswith("tests.") or module.startswith("_probe_") for module in modules):
                raise ValueError("Diagnostic/test modules included in executable")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="store_true")
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--zip", type=Path)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--exe", type=Path)
    args = parser.parse_args()
    if args.source:
        verify_source()
    if args.stage:
        stage_manifest(args.stage, args.version)
    if args.exe:
        inspect_exe(args.exe)
    if args.zip:
        verify_zip(args.zip)
        if args.smoke:
            smoke_zip(args.zip)


if __name__ == "__main__":
    main()
