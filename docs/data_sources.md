# Data sources, terms and credit

Every dataset `./scripts/setup.sh` fetches, where it comes from, under what terms,
and who to credit when it is used. This repo **redistributes none of it**: the
checkout holds accessions, URLs and sha256 anchors, and each user downloads the
bytes from the source at setup time. The one exception is the nanopore pod5 seed,
which is derived from a CC0 source and committed — its provenance is below.

The machine-readable copy of this table is the `license` / `citation` field on
each entry of [config/core_datasets.yaml](../config/core_datasets.yaml).
`tests/test_data_sources_credited.py` fails the build when an entry lacks either
field or has no row here, so adding a dataset means adding its credit.

Terms were verified against the upstream source on 2026-09-23.

## Reference genome

| Dataset | Source | Terms | Credit |
|---|---|---|---|
| chr22 of hg38 (default); chr19 of mm10 and E. coli K-12 MG1655 as alternate builds | UCSC hgdownload (`goldenPath/hg38/chromosomes/chr22.fa.gz`), NCBI RefSeq GCF_000005845.2 | UCSC: raw sequence data "freely available for both public and commercial use"; hg38 README lists no restrictions. GRCh38 itself is a public GRC/NCBI release | Genome Reference Consortium; UCSC Genome Browser (Kent WJ et al., Genome Res 12:996, 2002) |

## Short-read datasets

| Accession | What it is | Source | Terms | Credit |
|---|---|---|---|---|
| SRR1517830 | HG00096 exome, 1000 Genomes | ENA public FTP | INSDC policy: no use or redistribution restrictions. 1000 Genomes: open access, Fort Lauderdale principles | 1000 Genomes Project Consortium, Nature 526:68 (2015); IGSR/EMBL-EBI |
| SRR1039508 | Airway smooth-muscle RNA-seq, single-end | ENA public FTP (GEO GSE52778) | INSDC unrestricted | Himes BE et al., PLoS ONE 9:e99625 (2014) |
| ERR188297 | NA20503 GEUVADIS RNA-seq, paired-end | ENA public FTP (ArrayExpress E-GEUV-1) | INSDC unrestricted; 1000 Genomes sample | Lappalainen T et al., Nature 501:506 (2013); GEUVADIS Consortium |
| SRR1658581 | GM12878 in-situ Hi-C | ENA public FTP (GEO GSE63525) | INSDC unrestricted | Rao SSP et al., Cell 159:1665 (2014) |
| ERR001268 | NA12878 WGS, 1000 Genomes pilot | ENA public FTP | INSDC unrestricted; 1000 Genomes open access | 1000 Genomes Project Consortium, Nature 467:1061 (2010); IGSR/EMBL-EBI |
| SRR4235788 | GM12878 WGBS, ENCODE ENCSR890UQO (GSE86765) | ENA public FTP | ENCODE: "freely download, analyze and publish results based on any ENCODE data without restrictions"; released 2016-02-23, no dbGaP flag. ENCODE asks that the consortium, the producing lab and the accession be acknowledged | ENCODE Project Consortium, Nature 583:699 (2020); dataset ENCSR890UQO, Richard Myers lab, HudsonAlpha Institute for Biotechnology |

## Long-read datasets

| Accession | What it is | Source | Terms | Credit |
|---|---|---|---|---|
| ERR3152364 | NA12878 ONT ultralong reads (PRJEB26791) | ENA public FTP | CC BY 4.0 (nanopore-wgs-consortium LICENSE); INSDC unrestricted | Jain M et al., Nat Biotechnol 36:338 (2018); nanopore-wgs-consortium |
| HG002_CCS_15kb | HG002 PacBio HiFi 15–20 kb CCS reads | NIST GIAB FTP (ReferenceSamples/giab) | NIST public data, no embargo. HG002 is a Personal Genome Project sample whose consent explicitly covers commercial redistribution | Zook JM et al., Sci Data 3:160025 (2016); NIST Genome in a Bottle Consortium |

## Nanopore raw signal (pod5)

| Accession | What it is | Source | Terms | Credit |
|---|---|---|---|---|
| HG002_PAM69552_R1041_5reads | Five HG002 reads, R10.4.1 / FLO-PRO114M / SQK-LSK114, 4 kHz, 400 bps, PromethION run PAM69552 (2022-10-04) | Derived from the HPRC open-data bucket `s3://human-pangenomics` (UCSC HG002 R10.4.1 sheared submission) | Bucket licence: **CC0 1.0 Universal** (AWS Registry of Open Data). HPRC asks that data not be resold as-is and that participants never be re-identified. HG002 carries PGP consent | Human Pangenome Reference Consortium (BioProject PRJNA730823) and its funder NHGRI; Liao W-W et al., Nature 617:312 (2023); sample from NIST Genome in a Bottle |

**Why a derived seed.** Nearly every public pod5 file traces back to Oxford
Nanopore's own releases: dorado's test fixtures sit under the ONT Public License
(research use only), and the `ont-open-data` bucket, including the GIAB 2025.01
HG002 runs that nf-core's test data was cut from, is CC BY-NC 4.0. Neither is
usable in a commercial setting. GIAB's own NIST FTP holds no R10 raw signal, and
HPRC holds it only as multi-hundred-GB fast5 tarballs. Because those tarballs are
uncompressed, one 316 MB member can be fetched by HTTP byte range, converted with
`pod5 convert fast5`, and cut to five reads with `pod5 filter`. That derivation is
`scripts/derive_pod5_seed.py`; the result, 187 KB, is committed at
`config/seed_data/pod5/` with a sidecar recording the source URL, tar member, its
sha256, the run metadata read out of the file, the five read ids and the digest of
their signal. `--check` verifies the committed seed against sidecar and config
without network; `--rederive` rebuilds it from source and refuses to overwrite if
the signal digest changes.

## Phenopackets

| Entry | What it is | Source | Terms | Credit |
|---|---|---|---|---|
| PMID_30315159_Patient_N | Thrombocytopenia 8 (OMIM:620475), ACTB variant, Patient N | monarch-initiative/phenopacket-store (GitHub) | BSD-3-Clause. Content is curated from a published, de-identified case report | Danis D et al., "A corpus of GA4GH phenopackets", HGG Advances (2025); original case: PMID 30315159 |

## Human-subjects note

Every human dataset above is open-access by design: 1000 Genomes and GEUVADIS
donors consented to public release, HG002 is a Personal Genome Project participant,
GM12878 is the NA12878 lymphoblastoid cell line, and the airway cells are
de-identified donor tissue on GEO. None is dbGaP or EGA controlled, and the ENA
read fetcher in `agent/skills/core_test_data.py` only builds public-FTP URLs, which
never serve controlled-access reads. A `source_url` override bypasses that
guarantee, so whoever adds one takes on the licence check for it — and the test
will demand a `license` and `citation` for the entry.

## Acknowledgement text

Ready to paste when publishing results produced from this corpus:

> Test data were drawn from the 1000 Genomes Project / IGSR (EMBL-EBI), the
> GEUVADIS Consortium, the ENCODE Consortium (dataset ENCSR890UQO, Richard Myers
> lab, HudsonAlpha), the NIST Genome in a Bottle Consortium (HG002/NA24385), the
> nanopore-wgs-consortium (NA12878, CC BY 4.0), the Human Pangenome Reference
> Consortium (BioProject PRJNA730823, funded by NHGRI; CC0), and the Monarch
> Initiative phenopacket-store (BSD-3-Clause). Reference sequence from the Genome
> Reference Consortium via the UCSC Genome Browser.
