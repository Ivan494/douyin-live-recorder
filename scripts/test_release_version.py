"""Execute the actual version gate before a tag can publish release assets."""
import os
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest


@pytest.mark.parametrize("ref_type,ref_name,requested,accepted", [
    ("tag", "v1.2.15", "", True),
    ("tag", "v1.2.14", "", False),
    ("branch", "main", "1.2.14", False),
])
def test_workflow_version_gate(tmp_path, ref_type, ref_name, requested, accepted):
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell is required to execute the Windows release gate")
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/release.yml").read_text(encoding="utf-8")
    step = workflow.split("      - name: Validate version\n", 1)[1].split("      - name: Install", 1)[0]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    (tmp_path / "VERSION").write_text("1.2.15\n", encoding="utf-8")
    output = tmp_path / "output.txt"
    result = subprocess.run([shell, "-NoProfile", "-Command", script], cwd=tmp_path,
                            env={**os.environ, "GITHUB_REF_TYPE": ref_type, "GITHUB_REF_NAME": ref_name,
                                 "REQUESTED_VERSION": requested, "GITHUB_OUTPUT": str(output)},
                            capture_output=True, text=True, timeout=20)
    if accepted:
        assert result.returncode == 0, result.stderr
        assert output.read_text(encoding="utf-8").strip() == "version=1.2.15"
    else:
        assert result.returncode != 0
        assert "differs from VERSION" in result.stderr
        assert not output.exists()


def test_local_builder_rejects_other_version_before_touching_output(tmp_path):
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell is required to execute the Windows release builder")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = Path(__file__).with_name("build_release.ps1")
    build = scripts / source.name
    shutil.copy2(source, build)
    (tmp_path / "VERSION").write_text("1.2.15\n", encoding="utf-8")
    output = tmp_path / "dist"
    output.mkdir()
    previous = output / "previous-release.zip"
    previous.write_bytes(b"published release")
    result = subprocess.run([shell, "-NoProfile", "-File", str(build), "-Version", "1.2.14",
                             "-FfmpegDir", str(tmp_path / "missing-tools")],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "Version differs from VERSION" in result.stderr
    assert list(output.iterdir()) == [previous]
    assert previous.read_bytes() == b"published release"
