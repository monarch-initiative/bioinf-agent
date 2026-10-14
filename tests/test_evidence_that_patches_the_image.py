"""Evidence that modifies the image is a patch, not evidence.

The shipped image must run the tool as it is. A command that writes into the image's own
tree (a redirect, tee or copy into /usr, /opt, /etc, a site-packages) or installs a package
before invoking the tool proves the patched container, not the image, and the contract names
the two honest ways out: build the fix in and freeze that, or say the host cannot run the tool
as shipped. A tool's own output under /tmp or a relative path, and a system path read as an
input, are not writes into the image.
"""
from __future__ import annotations

import pytest

from agent.skills import env_honesty as EH

SHIM = ("cat > /usr/lib/python3.10/sitecustomize.py << 'PYEOF'\nimport sys, types\n"
        "sys.meta_path.insert(0, object())\nPYEOF\n"
        "QEMU_CPU=SandyBridge /opt/deepvariant/bin/run_deepvariant --help")


@pytest.mark.parametrize("evidence, fragment", [
    (SHIM, "> /usr/lib/python3.10/sitecustomize.py"),
    ("echo x | tee /opt/tool/conf.ini && tool --version", "tee /opt/tool/conf.ini"),
    ("cp fix.py /usr/local/lib/python3.10/site-packages/fix.py && tool -h", "cp fix.py /usr/local/lib/python3.10/site-packages/fix.py"),
    ("ln -sf /x/tool /usr/bin/tool; tool -h", "ln -sf /x/tool /usr/bin/tool"),
    ("pip install numpy && tool -h", "pip install"),
    ("apt-get install -y zlib1g && tool -h", "apt-get install"),
    ("conda install -y -c bioconda samtools && samtools --version", "conda install"),
    ("uv pip install pyarrow; tool --version", "uv pip install"),
    ("tool --version >> /etc/tool.log", ">> /etc/tool.log"),
])
def test_a_write_into_the_image_or_an_install_is_refused_with_the_fragment(evidence, fragment):
    assert EH.image_mutation(evidence) == fragment
    reason = EH.evidence_shape_violation(evidence, "tool")
    assert reason and "modifies the image before running the tool" in reason and fragment in reason
    assert "build it in" in reason and "locus that can run it" in reason


@pytest.mark.parametrize("evidence", [
    "tool --version > /tmp/out.txt 2>&1; grep -q 1.0 /tmp/out.txt",
    "cd /tmp && tool CreateSequenceDictionary -R r.fa -O r.dict && test -s r.dict",
    "cp /usr/share/doc/tool/example.fa /tmp/x.fa && tool stats /tmp/x.fa",
    "tool -i /usr/share/data/ref.fa --version",
    "tool --help 2>/dev/null",
    "tool run > ./out.txt && test -s out.txt",
    "/opt/tool/bin/tool --version",
    "python -c 'import tool' && tool --version",
])
def test_ordinary_output_and_system_paths_read_as_inputs_are_not_patches(evidence):
    assert EH.image_mutation(evidence) == ""
    assert EH.evidence_shape_violation(evidence, "tool") is None
