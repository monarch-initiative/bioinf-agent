#!/usr/bin/env python3
"""
freeze_tiers — the canonical definition of the freeze TIERS, imported by the
generator, the render, and the ratchet test so all three share ONE source of
truth (the sibling of seaworthy_scope.py for the outcomes dashboard).

WHY THIS EXISTS. The headline promise is "call once for ANY tool, get a
trustworthy artifact." That promise is only as true as the number of install
tiers a REAL container-native docker build has actually baked AND passed
`env_honesty.check_build` on. The P2 tier-breadth recon measured that number at
**1 of 11** (only conda, via L15/pigz) — every other tier was implemented and
wired but never baked. This module enumerates every tier so the grid can measure
that number honestly, and can never let it be confused with resolver-decision
coverage (the intent grid) or terminal coverage (the outcomes dashboard) — three
orthogonal maps, each blind to the others' half.

TWO KINDS OF ROW:
  - install tiers  — the InstallMethod.type members the container-native build
                     bakes (conda + the 9 non-conda). Proven by build_env_image.
  - build methods  — the ADOPT/AUTHORS routes that ship an image WITHOUT a
                     container-native reconstruction: adopt a biocontainer / the
                     author's own image / build the author's Dockerfile.

DRIFT GUARD. `assert_tiers_match_model()` checks the install-tier set equals the
InstallMethod.type Literal. A tier added to the union with no grid row (or a grid
row for a tier that left the union) fails the hermetic test — the same ledger-vs-
source posture extract_outcomes takes on the outcome enum. Adding a tier to the
model is therefore not optional bookkeeping here; it is what keeps the breadth
meter honest.

FLOORS are a REVIEWED ratchet, kept in CODE (not auto-written by the generator),
exactly like the intent corpus's `expect`: the generator re-measures
`passed/attempts`; a human raises a floor only after a tier's real build lands.
The ratchet test asserts `floor <= measured` for every measured tier and
`floor == 0` for every unmeasured one — so a floor can only ever be earned, and a
regressed measurement reddens.
"""
from __future__ import annotations

import hashlib
import json
import sys
import typing
from pathlib import Path

# Make `agent` importable whether this module is run as a script or imported by
# the generator / hermetic test (pytest already has the repo root on the path).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── the tiers ────────────────────────────────────────────────────────────────
# Each install tier with `builder == "container_native"` carries a `build`
# recipe the generator feeds to env_freeze.build_env_image. A tier with
# `builder is None` is DECLARED but not yet wired to a real probe — its row shows
# "unmeasured" (floor 0), to be promoted in a later slice. Probes PIN a
# tag/commit so a rebuild is reproducible.

FREEZE_TIERS: list[dict] = [
    {
        "tier": "conda", "kind": "install", "builder": "container_native",
        "probe_tool": "pigz",
        "build": {"conda_deps": ["pigz"], "primary_tools": ["pigz"]},
        "note": "pixi engine lock (URL + sha256). The proven seed "
                "(also L15/test_real_container_build.py).",
    },
    {
        "tier": "source", "kind": "install", "builder": "container_native",
        "probe_tool": "seqtk",
        "build": {
            "install_method": {
                "type": "source",
                "source": "https://github.com/lh3/seqtk",
                "commit_sha": "ae7defa8bead3ef77d241f12194dc66acdd40fca",  # v1.4
                "build_command": "make",
                "bin_path": "seqtk",
            },
            "primary_tools": ["seqtk"],
        },
        "note": "the SHARED long-tail ContainerBuild.run executor that binary / "
                "jar / cargo / go / perl / synthesized also ride — highest "
                "leverage single build. Proving it is necessary-but-not-"
                "sufficient for those siblings (each has its own spec/toolchain).",
    },
    {
        "tier": "pip", "kind": "install", "builder": "container_native",
        "probe_tool": "pyfaidx",
        "build": {
            "install_method": {"type": "pip", "name": "pyfaidx"},
            "primary_tools": ["pyfaidx"],
        },
        "note": "flagless pip → declared THROUGH the pixi engine (`pixi add --pypi` "
                "→ uv, in-lock); reproducible via the engine lock. In-image "
                "evidence = importlib.metadata.version (the pixi/uv env ships no "
                "`pip` binary). Proven 2026-07-20 (pyfaidx). Flag-bearing pip is a "
                "distinct engine-coupled longtail RUN, not yet separately baked.",
    },
    {
        "tier": "r_install", "kind": "install", "builder": "container_native",
        "probe_tool": "BiocGenerics",
        "build": {
            "install_method": {
                "type": "r_install", "name": "BiocGenerics",
                # `source` is the R expression install_r_package records; the freeze
                # reads 'BiocManager' → the bioconductor branch (BioC_mirror pin +
                # BiocManager bootstrap + BiocManager::install). BiocGenerics is the
                # FOUNDATIONAL, pure-R Bioconductor package (no compiled code,
                # minimal deps) → a reliable emulated build that still exercises the
                # real BiocManager path, not a toy.
                "source": 'BiocManager::install("BiocGenerics", ask=FALSE, update=FALSE)',
            },
            "primary_tools": ["BiocGenerics"],
        },
        "note": "cran / bioconductor / github → Rscript install + "
                "requireNamespace||stop() load-or-die. Rides the SAME shared "
                "ContainerBuild long-tail executor as source/binary, but as an "
                "ENGINE-COUPLED tool (like cargo/go/perl — the command runs with the "
                "conda r-base active) via a DISTINCT install-command generator "
                "(ic.r_package → BiocManager), so it adds genuinely new coverage over "
                "the source tier. Proven 2026-07-20 via BiocGenerics (BIOCONDUCTOR "
                "branch: BioC_mirror pin + BiocManager bootstrap). VALIDATED_IN_IMAGE "
                "evidence is the DEFAULT IMPORT-LEVEL check — library(BiocGenerics) "
                "(namespace load, executes R in the shipped image) + packageVersion() "
                "version capture — NOT a functional_evidence run-on-data smoke; a "
                "functional_check would strengthen it to validated==ran (future "
                "hardening). The r-base + c/cxx/fortran-compiler + zlib toolchain is "
                "auto-injected (plan_conda) and IS engine-lock-anchored, but the R "
                "PACKAGE itself is Rscript-installed at build time — NOT in the pixi "
                "lock and NOT sha-pinned, so its point-version floats with the current "
                "Bioc release (weaker reproducibility than conda/pip/source/binary; a "
                "snapshot-repo / explicit-version pin is future hardening). The cran + "
                "github sub-sources ride the SAME generator's other branches, not "
                "separately baked this slice.",
    },
    {
        "tier": "binary", "kind": "install", "builder": "container_native",
        "probe_tool": "mosdepth",
        "build": {
            "install_method": {
                "type": "binary", "name": "mosdepth",
                # a GitHub release-download URL → resolve_linux_asset picks the
                # linux/amd64 asset of THAT tag; the freeze re-fetches + sha256-
                # anchors it. v0.3.8's `mosdepth` asset is a static x86_64 linux
                # binary. asset_sha256 is the REAL install-time hash of that asset,
                # so the freeze exercises the INSTALL→SHIP integrity firewall (F2)
                # for real (freeze re-fetch == install sha ⇒ chain_intact,
                # disclosed `pinned_tofu`) — not just the sha-anchoring of shipped
                # bytes. A mutated asset would REFUSE (build.binary_integrity_mismatch).
                "binary_url": "https://github.com/brentp/mosdepth/releases/download/v0.3.8/mosdepth",
                "asset_sha256": "107eb3cdb4d4b48be6df4c7f5cc7c077134fdeca8c7c06f713b63f68549dd86c",
            },
            "primary_tools": ["mosdepth"],
        },
        "note": "re-fetch + sha256 firewall (F2) + PATH wrapper. Proven 2026-07-20 "
                "(mosdepth v0.3.8). A darwin→linux asset is a CORRECT "
                "unanchored_cross_platform disclosure, not a fail.",
    },
    {
        "tier": "jar", "kind": "install", "builder": "container_native",
        "probe_tool": "picard",
        "build": {
            "install_method": {
                "type": "jar", "name": "picard",
                # Picard 3.4.0 — the canonical Java bioinformatics tool (Broad
                # Institute), shipped as a single fat jar. Arch-independent bytecode,
                # so the jar bytes are identical cross-platform; container_build adds
                # default-jre-headless (OpenJDK 17 on bookworm — Picard 3.x needs 17+)
                # to the RUNTIME stage when a (java jar) longtail step exists.
                "source": "https://github.com/broadinstitute/picard/releases/download/3.4.0/picard.jar",
                # FUNCTIONAL evidence (validated==ran, NOT presence): actually RUN
                # Picard on an inline-generated fasta and assert it produced a .dict.
                # Leads with `cd` (not printf) so env_honesty's anti-echo-cheat shape
                # rule sees a real `picard …` invocation; the `>` redirect classifies
                # it 'functional'. This exercises the _map_install evidence-threading
                # (env_freeze), without which a jar's VALIDATED_IN_IMAGE is presence-
                # only (`command -v picard && java -version`).
                "evidence": ("cd /tmp && printf '>chrTEST\\n"
                             "ACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT\\n' "
                             "> picard_ref.fa && picard CreateSequenceDictionary "
                             "-R picard_ref.fa -O picard_ref.dict && test -s picard_ref.dict"),
            },
            "primary_tools": ["picard"],
        },
        "note": "JRE-ensure + jar download + `java -jar` wrapper; the runtime stage "
                "carries default-jre-headless. Proven 2026-07-21 via Picard 3.4.0 "
                "(Broad) with FUNCTIONAL evidence — CreateSequenceDictionary RUNS on "
                "an inline fasta and writes a .dict (validated==ran, not the presence "
                "default). Version-PINNED asset URL, sha256-anchored best-effort at "
                "freeze (jars rarely publish a checksum → pinned_tofu; the bytecode is "
                "arch-independent). Depends on the _map_install jar branch now threading "
                "a recorded smoke as evidence (parity with source/synthesized/r_install).",
    },
    {
        "tier": "cargo", "kind": "install", "builder": "container_native",
        "probe_tool": "nanoq",
        "build": {
            "install_method": {
                "type": "cargo", "name": "nanoq",
                # nanoq 0.10.0 (esteinig/nanoq) — a real, small, pure-Rust Nanopore
                # read-QC tool. `cargo install nanoq --version 0.10.0 --root /usr/local
                # --locked`: the crate ships its own Cargo.lock (every bin crate does) so
                # --locked pins the FULL dep tree, the --version pins the tool, and the
                # rust toolchain is engine-lock-anchored (conda `rust`, auto-injected by
                # plan_conda's _TOOLCHAIN_SPECS['cargo']) → the STRONGEST reproducibility
                # shape of any tier (tool-version + crate-lock + toolchain-lock all pinned).
                "crate": "nanoq", "version": "0.10.0", "binary_name": "nanoq",
                # FUNCTIONAL evidence (validated==ran, NOT the `--version` default): RUN the
                # built binary on an inline fastq and assert it wrote filtered reads. Leads
                # with `cd` (not printf) so env_honesty's anti-echo-cheat shape rule sees a
                # real `nanoq …` invocation; the `/tmp/*.fq` operands + `-i`/`-o` classify it
                # 'functional'. `-i`/`-o` are nanoq's core flags (stable across releases).
                # Exercises the _map_install cargo evidence-threading (env_freeze) — without
                # which a cargo tool's VALIDATED_IN_IMAGE is the `--version` default.
                "evidence": ("cd /tmp && printf '@r1\\n"
                             "ACGTACGTACGTACGTACGT\\n+\\n"
                             "IIIIIIIIIIIIIIIIIIII\\n' > /tmp/nanoq_in.fq && "
                             "nanoq -i /tmp/nanoq_in.fq -o /tmp/nanoq_out.fq && "
                             "test -s /tmp/nanoq_out.fq"),
            },
            "primary_tools": ["nanoq"],
        },
        "note": "cargo install --root /usr/local --locked, engine rust toolchain "
                "(conda `rust`, auto-injected). ENGINE-COUPLED like go/perl/r_install — "
                "the build runs with the conda env active; the OUTPUT binary is self-"
                "contained and COPYed to the slim runtime. Proven via nanoq 0.10.0 with "
                "FUNCTIONAL evidence — filters an inline fastq and writes output "
                "(validated==ran, not the `--version` default). STRONGEST reproducibility "
                "shape: tool-version pinned (--version 0.10.0) + crate Cargo.lock (--locked) "
                "+ engine-lock-anchored rust toolchain.",
    },
    {
        "tier": "go", "kind": "install", "builder": "container_native",
        "probe_tool": "gofasta",
        "build": {
            "install_method": {
                "type": "go", "name": "gofasta",
                # gofasta v1.2.3 (virus-evolution/gofasta) — a real SARS-CoV-2 genomics
                # tool (alignment/mutation calling, used in the pangolin/civet ecosystem).
                # `GOBIN=/usr/local/bin go install github.com/virus-evolution/gofasta@v1.2.3`:
                # the module version pins the tool, the module's go.sum pins the dep tree, and
                # the go toolchain is engine-lock-anchored (conda `go`, _TOOLCHAIN_SPECS['go']
                # → 1.26.5, > gofasta's `go 1.23` floor). Pure Go → a static binary, so the
                # runtime stage needs no extra libs; main.go is at the module root → binary
                # `gofasta`.
                #
                # WHY NOT seqkit (the obvious first pick): `go install pkg@version` REFUSES a
                # module whose go.mod carries `replace` directives ("must not contain directives
                # that would cause it to be interpreted differently than if it were the main
                # module"). shenwei356 tools (seqkit/csvtk) vendor their own libs via `replace`,
                # so they are `git clone && go build`-only, NOT `go install`-able — the go tier
                # requires a clean (replace-free) go.mod, which gofasta has.
                "package": "github.com/virus-evolution/gofasta", "version": "v1.2.3",
                "binary_name": "gofasta",
                # FUNCTIONAL evidence (validated==ran): RUN gofasta on inline data and assert
                # it produced output. `gofasta snps -r <ref> -q <query>` calls SNPs of a query
                # against a same-length reference (both pre-aligned); the query here differs at
                # the last base → one SNP → a non-empty CSV. The `/tmp/*.fa`/`.csv` operands +
                # `-r`/`-q`/`-o` classify it 'functional'; it leads with `cd` (shape-clean).
                # Exercises the _map_install go evidence-threading (env_freeze), sibling of cargo.
                "evidence": ("cd /tmp && printf '>ref\\n"
                             "ACGTACGTACGTACGTACGT\\n' > /tmp/gofasta_ref.fa && "
                             "printf '>q1\\nACGTACGTACGTACGTACGA\\n' > /tmp/gofasta_q.fa && "
                             "gofasta snps -r /tmp/gofasta_ref.fa -q /tmp/gofasta_q.fa "
                             "-o /tmp/gofasta_out.csv && test -s /tmp/gofasta_out.csv"),
            },
            "primary_tools": ["gofasta"],
        },
        "note": "GOBIN=/usr/local/bin go install pkg@version, engine go toolchain "
                "(conda `go` 1.26.5, auto-injected). ENGINE-COUPLED like cargo/perl/r_install. "
                "Proven via gofasta v1.2.3 (virus-evolution) with FUNCTIONAL evidence — "
                "`gofasta snps` calls a SNP between two inline aligned fastas and writes a CSV "
                "(validated==ran). Reproducibility: module version pinned (@v1.2.3) + module "
                "go.sum + engine-lock-anchored go toolchain; pure-Go static binary. NOTE: the "
                "go tier requires a replace-directive-FREE go.mod — `go install pkg@v` refuses "
                "a module with `replace` directives, so seqkit/csvtk (shenwei356, which vendor "
                "libs via replace) are clone-and-build-only, NOT go-install-able.",
    },
    {
        "tier": "perl", "kind": "install", "builder": "container_native",
        "probe_tool": "Set::IntervalTree",
        "build": {
            "install_method": {
                "type": "perl", "name": "Set::IntervalTree",
                # Set::IntervalTree 0.12 — a real Ensembl VEP dependency (VEP's
                # INSTALL.pl cpanm-installs it) AND a genomic interval tree, so a genuine
                # bioinformatics XS module, not a toy. It's C++ (a template-based interval-
                # tree header + a .xs), so the build EXERCISES the hard part of this tier:
                # compiling XS against the conda perl (5.32) with the conda cxx-compiler
                # AND the xlocale.h shim (conda perl still #includes <xlocale.h>, which
                # modern glibc folded into <locale.h>) — auto-injected via
                # _TOOLCHAIN_SPECS['perl'] = perl + perl-app-cpanminus + c-/cxx-compiler.
                # A pure-perl module would skip all of that and prove nothing about the
                # tier's real mechanism.
                "module": "Set::IntervalTree",
                # Version-pinned via cpanm's Module@version syntax → a reproducible rebuild.
                # (The tier's replay assurance stays `cpan_tofu`: CPAN records no content
                # hash in the recipe, so a same-version re-upload is undetectable — TOFU is
                # the honest ceiling, even though PAUSE makes (author,version) tarballs
                # immutable in practice.)
                "distribution": "Set::IntervalTree@0.12",
                # FUNCTIONAL evidence (RUNS the compiled XS, not just loads the .so): build
                # an interval tree, insert two disjoint intervals, fetch a point inside the
                # first, and assert the fetch returns that value AND excludes the far one —
                # i.e. the C++ query methods work and DISCRIMINATE. q()/qq() string literals
                # keep the shell command double-quote-free; leads with `cd` so env_honesty's
                # anti-echo-cheat shape rule sees a real `perl -MSet::IntervalTree …`. NOTE
                # the depth CLASSIFIER discloses this as 'import' (not 'functional') because
                # perl's `-Mmodule` glue is structurally indistinguishable from an import-
                # only load — that UNDER-discloses the real depth (conservative; never over-
                # claims). Exercises the _map_install perl evidence-threading (env_freeze) —
                # without which VALIDATED_IN_IMAGE is the `perl -M{module} -e1` import default.
                "evidence": ("cd /tmp && perl -MSet::IntervalTree -e "
                             "'my $t=Set::IntervalTree->new; "
                             "$t->insert(q(geneA),10,20); $t->insert(q(geneB),100,200); "
                             "my $r=$t->fetch(12,13); "
                             "die unless grep {$_ eq q(geneA)} @$r; "
                             "die if grep {$_ eq q(geneB)} @$r; "
                             "print scalar(@$r).qq(\\n);' "
                             "> /tmp/sit_out.txt && test -s /tmp/sit_out.txt"),
            },
            "primary_tools": ["Set::IntervalTree"],
        },
        "note": "cpanm --notest into the conda perl + xlocale shim for XS. Proven "
                "2026-07-21 via Set::IntervalTree 0.12 (a real Ensembl VEP dep; C++ XS) "
                "with a run-on-data smoke — builds an interval tree and asserts fetch() "
                "discriminates an overlapping from a non-overlapping interval (exercises "
                "the compiled XS query methods, not just XSLoader). Version-pinned via "
                "cpanm Module@version; replay assurance is cpan_tofu (no CPAN content-hash "
                "pin in the recipe). The c-/cxx-compiler + xlocale shim ARE the tier's real "
                "mechanism (a pure-perl module would skip them). Depth CLASSIFIER reads "
                "'import' (perl `-M` glue is indistinguishable from an import-only load) — "
                "an honest UNDER-disclosure of a genuine run, never an over-claim. Depends "
                "on _map_install now threading the recorded evidence for perl (parity with "
                "source/cargo/go/jar/r_install/synthesized — the last non-conda tier to "
                "close its evidence-threading gap).",
    },
    {"tier": "synthesized", "kind": "install", "builder": None, "probe_tool": "",
     "note": "provenance-gated command sequence via the shared longtail executor. "
             "A 2026-07-20 real bake BUILT the image but check_build correctly "
             "REFUSED it (PROVENANCE_CLEAN.untagged_command) — its honest probe "
             "must drive the real synth_fetch + validate_submission machinery so "
             "every command is EXTRACTED (repo file + sha256) or grounded "
             "AGENT_AUTHORED, NOT hand-tagged. Deferred to its own slice."},
    # ── build methods (adopt / authors — no container-native reconstruction) ──
    {"tier": "adopt-biocontainer", "kind": "build_method", "builder": None,
     "probe_tool": "",
     "note": "pull a BioContainer by manifest digest + in-image evidence. The "
             "DEFAULT production path, yet 0 real-bytes coverage (all tests mock "
             "the digest-pull)."},
    {"tier": "adopt-image", "kind": "build_method", "builder": None,
     "probe_tool": "",
     "note": "freeze_from_image on the author's own image. L15 proves the method "
             "wrapper on a toy debian shell tool — NOT a real package install."},
    {"tier": "authors-dockerfile", "kind": "build_method", "builder": None,
     "probe_tool": "",
     "note": "build_env_from_authors_recipe (docker build -f + build-args). L15 "
             "proves it on a toy tool — NOT a real tool's Dockerfile."},
]

INSTALL_TIERS: list[str] = [t["tier"] for t in FREEZE_TIERS if t["kind"] == "install"]
BUILD_METHODS: list[str] = [t["tier"] for t in FREEZE_TIERS if t["kind"] == "build_method"]
ALL_TIERS: list[str] = [t["tier"] for t in FREEZE_TIERS]


# ── reviewed floors (the ratchet — raised by a human, never by the generator) ─
FLOORS: dict[str, float] = {
    "conda":  1.0,   # L15/pigz + the generator
    "source": 1.0,   # seqtk v1.4 — proven 2026-07-20 (P2 slice 2)
    "pip":    1.0,   # pyfaidx  — proven 2026-07-20 (P2 slice 3, pixi --pypi)
    "binary": 1.0,   # mosdepth v0.3.8 — proven 2026-07-20 (P2 slice 3, F2 firewall)
    "r_install": 1.0,   # BiocGenerics — proven 2026-07-20 (P2 slice 4, BiocManager path)
    "jar":    1.0,   # Picard 3.4.0 — proven 2026-07-21 (P2-C, FUNCTIONAL evidence)
    "cargo":  1.0,   # nanoq 0.10.0 — proven 2026-07-21 (P2-C, FUNCTIONAL evidence)
    "go":     1.0,   # gofasta v1.2.3 — proven 2026-07-21 (P2-C, FUNCTIONAL evidence)
    "perl":   1.0,   # Set::IntervalTree 0.12 — proven 2026-07-21 (P2-C, XS run-on-data smoke)
    "synthesized":        0.0,
    "adopt-biocontainer": 0.0,
    "adopt-image":        0.0,
    "authors-dockerfile": 0.0,
}


def recipe_fingerprint(spec: dict) -> str:
    """A stable content hash of a tier's BUILD RECIPE — the tier-specific input that
    determines WHAT gets built and HOW it's validated (the `build` dict: install_method
    + evidence + primary_tools, or conda_deps + primary_tools). Deterministic (sorted
    keys), pure.

    This is the load-bearing anchor of INCREMENTAL measurement. When the generator
    measures one tier and carries the others forward (rather than re-baking all N every
    run — the cost that used to force a full emulated sweep to add a single tier), a
    carried-forward record is only honest if that tier's recipe is byte-identical to
    when it was measured. The fingerprint makes that checkable WITHOUT a rebuild: a
    changed recipe → a changed fingerprint → the carried measurement is auto-invalidated
    (re-measure required). This is precisely what makes 'retroactively fix a tier we got
    wrong' safe — editing its recipe reddens it until a fresh bake, instead of silently
    keeping the stale green. A tier with no `build` recipe (builder None / a build_method
    row) fingerprints to a constant; it carries no measurement anyway.

    Deliberately scoped to the RECIPE, not the shared build code (env_freeze /
    container_build / install_commands): a shared-code change affects every tier and is
    surfaced instead by the per-tier `measured_sha` staleness the render shows — the human
    re-measures when they touch the build path, the same posture as the reviewed FLOORS."""
    return hashlib.sha256(
        json.dumps(spec.get("build") or {}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def tier(name: str) -> dict:
    """The FREEZE_TIERS row for `name` (raises if unknown — a typo shouldn't
    silently measure nothing)."""
    for t in FREEZE_TIERS:
        if t["tier"] == name:
            return t
    raise KeyError(f"unknown freeze tier {name!r}; known: {ALL_TIERS}")


def buildable_install_types() -> list[str]:
    """The InstallMethod.type Literal members — the tiers the container-native
    build can bake. Read from the model so the grid can't drift from it."""
    from agent.models.core_data import InstallMethod
    ann = InstallMethod.model_fields["type"].annotation
    args = list(typing.get_args(ann))
    if not args:
        raise AssertionError(
            "could not read the InstallMethod.type Literal — the model shape "
            "changed; the tier-grid drift guard needs updating")
    return args


def assert_tiers_match_model() -> None:
    """The drift guard. The grid's install-tier set MUST equal the
    InstallMethod.type union — no more, no less. A new tier in the union with no
    grid row is a breadth hole the meter would silently omit; a grid row for a
    tier no longer in the union is a ghost."""
    model = set(buildable_install_types())
    grid = set(INSTALL_TIERS)
    missing = model - grid   # in the model, no grid row
    extra = grid - model     # grid row, not in the model
    assert not missing, (
        f"{sorted(missing)} are InstallMethod.type members with NO freeze-tier "
        f"grid row — add them to FREEZE_TIERS so the breadth meter counts them.")
    assert not extra, (
        f"{sorted(extra)} are freeze-tier grid rows that are NOT InstallMethod.type "
        f"members — they left the model; remove the ghost rows.")
    # floors must cover exactly every tier (install + build method)
    fl = set(FLOORS)
    all_t = set(ALL_TIERS)
    assert fl == all_t, (
        f"FLOORS must cover exactly every tier. missing={sorted(all_t - fl)} "
        f"extra={sorted(fl - all_t)}")


if __name__ == "__main__":
    assert_tiers_match_model()
    print(f"freeze tiers OK — {len(INSTALL_TIERS)} install tiers "
          f"+ {len(BUILD_METHODS)} build methods; model set matches.")
    for t in FREEZE_TIERS:
        wired = "real-probe" if t["builder"] else "unmeasured"
        print(f"  {t['tier']:20s} {t['kind']:13s} {wired:11s} floor={FLOORS[t['tier']]}")
