"""Fail-closed validation for the public portable package (no installed state)."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
APP = "douyindownload/_automation/"
INTERNAL = APP + "_internal/"
INTERNAL_ALLOWLIST = Path(__file__).with_name("release_internal_allowlist.txt")
FILES = {
    "DouyinLiveRecorder.exe", APP + "DouyinLiveRecorder.exe",
    APP + "profiles.json", APP + "settings.json", "youtube-dl/ffmpeg.exe",
    "youtube-dl/ffprobe.exe", "README.txt", "LICENSE", "THIRD_PARTY.md",
}
PE_SUFFIXES = {".exe", ".dll", ".pyd", ".ocx", ".sys"}
PRIVATE_BUNDLE_PATH = re.compile(
    r"(?i)(^|/)(?:douyin_session\.json|mobile_session\.json|mobile_device\.json|"
    r"deployed-source\.json|profiles\.json|settings\.json|config_backups|logs|"
    r"\.recording_sessions|browser_profile|\.env(?:\..*)?|id_rsa|id_ed25519)(/|$)|"
    r"\.(?:mkv|flv|mp4|log|bak|pfx|p12|key)$"
)
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
    load_internal_allowlist()
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


def load_internal_allowlist():
    """Names from a reviewed PyInstaller onedir build; update deliberately with dependencies."""
    names = INTERNAL_ALLOWLIST.read_text(encoding="utf-8").splitlines()
    if not names or names != sorted(set(names)) or len({name.casefold() for name in names}) != len(names):
        raise ValueError("Internal bundle allowlist must be nonempty, sorted, and unique")
    if any(
        name.startswith("/") or "\\" in name or ":" in name or
        any(part in ("", ".", "..") or part.endswith((" ", ".")) for part in name.split("/")) or
        PRIVATE_BUNDLE_PATH.search(INTERNAL + name) or
        (name.lower().endswith(".pem") and name != "certifi/cacert.pem")
        for name in names
    ):
        raise ValueError("Unsafe or private name in internal bundle allowlist")
    return {INTERNAL + name for name in names}


def validate_file_set(names):
    allowed_internal = load_internal_allowlist()
    missing = FILES - names
    unexpected = names - FILES - allowed_internal
    internal = {name for name in names if name.startswith(INTERNAL)}
    missing_internal = allowed_internal - internal
    private = {
        name for name in internal
        if PRIVATE_BUNDLE_PATH.search(name) or
        (name.lower().endswith(".pem") and name != INTERNAL + "certifi/cacert.pem")
    }
    if missing or unexpected or missing_internal or private:
        raise ValueError(
            f"Invalid release contents: missing={sorted(missing)}, "
            f"missing_internal={sorted(missing_internal)}, "
            f"unexpected={sorted(unexpected)}, private={sorted(private)}, "
            f"internal_count={len(internal)}"
        )
    if not any(re.fullmatch(r"python\d+\.dll", PurePosixPath(name).name, re.I) for name in internal):
        raise ValueError("Missing bundled Python runtime DLL")


def stage_files(directory):
    if not directory.is_dir():
        raise ValueError(f"Missing staging directory: {directory}")
    entries = list(directory.rglob("*"))
    if any(entry.is_symlink() for entry in entries):
        raise ValueError("Release staging must not contain symlinks")
    names = {entry.relative_to(directory).as_posix() for entry in entries if entry.is_file()}
    validate_file_set(names)
    expected_directories = {
        "/".join(parts[:index])
        for name in names for parts in [name.split("/")]
        for index in range(1, len(parts))
    }
    directories = {entry.relative_to(directory).as_posix() for entry in entries if entry.is_dir()}
    if directories - expected_directories:
        raise ValueError(f"Unexpected empty staging directories: {sorted(directories - expected_directories)}")
    return names


def pe_paths(directory, names):
    result = []
    for name in sorted(names):
        path = directory / name
        with path.open("rb") as stream:
            magic = stream.read(2)
        if path.suffix.lower() in PE_SUFFIXES or magic == b"MZ":
            result.append(path.resolve())
    return result


def verify_signatures(paths):
    paths = list(paths)
    if not paths:
        raise ValueError("Release contains no native executables to verify")
    shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if not shell:
        raise RuntimeError("PowerShell is required to verify Authenticode signatures")
    command = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$paths = @(ConvertFrom-Json -InputObject ([Console]::In.ReadToEnd()))
$failed = $false
foreach ($path in $paths) {
    try {
        $signature = Get-AuthenticodeSignature -LiteralPath $path -ErrorAction Stop
        if ($signature.Status -ne 'Valid') {
            [Console]::Out.WriteLine("$path : $($signature.Status)")
            $failed = $true
        }
    } catch {
        [Console]::Out.WriteLine("$path : $($_.Exception.Message)")
        $failed = $true
    }
}
if ($failed) { exit 1 }
'''
    process = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        input=json.dumps([str(path) for path in paths], ensure_ascii=True),
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    if process.returncode != 0:
        detail = (process.stdout + process.stderr).strip()
        raise ValueError(f"Invalid Authenticode signature(s): {detail}")


def stage_manifest(directory, version, *, bundle=None, require_signatures=False):
    version_value(version)
    actual = stage_files(directory)
    if bundle is not None:
        if not bundle.is_dir():
            raise ValueError(f"Missing PyInstaller bundle: {bundle}")
        bundle_entries = list(bundle.rglob("*"))
        if any(entry.is_symlink() for entry in bundle_entries):
            raise ValueError("PyInstaller bundle must not contain symlinks")
        bundled = {APP + entry.relative_to(bundle).as_posix() for entry in bundle_entries if entry.is_file()}
        staged_bundle = {name for name in actual if name.startswith(INTERNAL) or name == APP + "DouyinLiveRecorder.exe"}
        if bundled != staged_bundle:
            raise ValueError(f"Staged bundle differs from PyInstaller output: {sorted(bundled ^ staged_bundle)}")
    validate_defaults(json.loads((directory / (APP + "profiles.json")).read_text(encoding="utf-8-sig")),
                      json.loads((directory / (APP + "settings.json")).read_text(encoding="utf-8-sig")))
    if require_signatures:
        verify_signatures(pe_paths(directory, actual))
    manifest = {
        "version": version,
        "signatures_required": require_signatures,
        "files": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in sorted(actual)},
    }
    (directory / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def verify_zip(path, *, require_signatures=False):
    with zipfile.ZipFile(path) as archive:
        names = []
        directories = []
        for member in archive.infolist():
            name = member.filename
            if ("\\" in name or ":" in name or name.startswith("/") or
                    any(part in ("", ".", "..") or part.endswith((" ", ".")) for part in name.rstrip("/").split("/")) or
                    stat.S_ISLNK(member.external_attr >> 16)):
                raise ValueError("Unsafe archive path")
            if member.is_dir():
                directories.append(name[:-1])
            else:
                names.append(name)
        file_keys = {name.casefold() for name in names}
        directory_keys = {name.casefold() for name in directories}
        if (len(names) != len(file_keys) or len(directories) != len(directory_keys) or
                "release-manifest.json" not in names):
            raise ValueError("Release contains missing or duplicated files")
        if file_keys & directory_keys:
            raise ValueError("File/directory prefix conflict")
        for name in names + directories:
            parts = name.split("/")
            if any(
                "/".join(parts[:index]).casefold() in file_keys for index in range(1, len(parts))
            ):
                raise ValueError(f"File/directory prefix conflict: {name}")
        if sum(i.file_size for i in archive.infolist()) > 1_000_000_000:
            raise ValueError("Unexpectedly large release")
        manifest = json.loads(archive.read("release-manifest.json"))
        version_value(manifest["version"])
        if not isinstance(manifest.get("signatures_required"), bool):
            raise ValueError("Invalid manifest signing policy")
        if require_signatures and not manifest["signatures_required"]:
            raise ValueError("Release was not built in signing-required mode")
        files = manifest["files"]
        if not isinstance(files, dict) or set(names) != set(files) | {"release-manifest.json"}:
            raise ValueError("Invalid manifest file list")
        expected_directories = {
            "/".join(parts[:index]).casefold()
            for name in names for parts in [name.split("/")]
            for index in range(1, len(parts))
        }
        if directory_keys - expected_directories:
            raise ValueError("Release contains unexpected directory entries")
        validate_file_set(set(files))
        for name in files:
            if not isinstance(files[name], str) or not re.fullmatch(r"[a-f0-9]{64}", files[name]):
                raise ValueError(f"Invalid manifest hash: {name}")
            data = archive.read(name)
            if hashlib.sha256(data).hexdigest() != files[name]:
                raise ValueError(f"Hash mismatch: {name}")
        validate_defaults(json.loads(archive.read(APP + "profiles.json").decode("utf-8-sig")),
                          json.loads(archive.read(APP + "settings.json").decode("utf-8-sig")))
        if manifest["signatures_required"] or require_signatures:
            with tempfile.TemporaryDirectory(prefix="release-signature-check-") as temporary:
                directory = Path(temporary)
                pe_names = []
                for name in files:
                    with archive.open(name) as stream:
                        magic = stream.read(2)
                    if PurePosixPath(name).suffix.lower() in PE_SUFFIXES or magic == b"MZ":
                        pe_names.append(name)
                for name in pe_names:
                    target = directory / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
                verify_signatures(directory / name for name in pe_names)
    return manifest


def smoke_zip(path, *, verified=False, require_signatures=False):
    if not verified:
        verify_zip(path, require_signatures=require_signatures)
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
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--zip", type=Path)
    parser.add_argument("--list-pe", type=Path)
    parser.add_argument("--require-signatures", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--exe", type=Path)
    args = parser.parse_args()
    if args.source:
        verify_source()
    if args.stage:
        stage_manifest(args.stage, args.version, bundle=args.bundle, require_signatures=args.require_signatures)
    if args.list_pe:
        print(json.dumps([str(path) for path in pe_paths(args.list_pe, stage_files(args.list_pe))], ensure_ascii=True))
    if args.exe:
        inspect_exe(args.exe)
    if args.zip:
        verify_zip(args.zip, require_signatures=args.require_signatures)
        if args.smoke:
            smoke_zip(args.zip, verified=True)


if __name__ == "__main__":
    main()
