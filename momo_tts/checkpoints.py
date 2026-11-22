"""Download the exact inference files used by IndexTTS 2.5, without runtime fetches."""

import hashlib
import os
from pathlib import Path
import subprocess
from urllib.parse import urlencode

INDEX_REPO = "IndexTeam/IndexTTS-2.5"
INDEX_WEIGHTS = "432be07b5fd9514d76a936f8c6e96007647bd61b"
W2V_REPO = "AI-ModelScope/w2v-bert-2.0"
W2V_REVISION = "fc76caf6922661691d672f6494d821585975bc98"
FILES = [
    ("LICENSE", INDEX_REPO, "5dcd5b08da3dd2170c3f42457e5c40671e6dca22", "cc7da9ea0f8a97ef15ab3bf0389e636ce79ffca1aef5489520796ac87d87a87b"),
    ("config.yaml", INDEX_REPO, "0ab0ff3290a89ea4a9473647c3cd4982fdca5bbb", "18adf417be3e8f5e2e48e30f7420c719170a6870619436250f360d626877870e"),
    ("codec.pth", INDEX_REPO, INDEX_WEIGHTS, "d15cbed16a40f478438c961fb043f68dfa6353bf56c966761315db3433e9722c"),
    ("feat1.pt", INDEX_REPO, INDEX_WEIGHTS, "f219cb447d80216ba615666da2ff8d63ac544eee26657f3a7b278692bf7a67c4"),
    ("feat2.pt", INDEX_REPO, INDEX_WEIGHTS, "9c4292e96dee535aea9a6206e9a0c856dd578dde9212acdb16dd3ada4d12bf80"),
    ("gpt.pth", INDEX_REPO, INDEX_WEIGHTS, "43a8f4c30eccdf201958d3b9713511482c19d56dc20b0b1c4ee1e6b080b19d85"),
    ("multilingual_zh_ja_yue_char_del.tiktoken", INDEX_REPO, INDEX_WEIGHTS, "747979631e813193436aabcff7c1c235d37de8097b71c563ec8b63b7a515c718"),
    ("s2mel.pth", INDEX_REPO, INDEX_WEIGHTS, "9b1b0003fc189c94cc349758d7ebc25f903b7eb2de4602879959cc64ce816456"),
    ("wav2vec2bert_stats.pt", INDEX_REPO, INDEX_WEIGHTS, "c9c176c2b8850ab2e3ba828bbfa969deaf4566ce55db5f2687b8430b87526ad2"),
    ("hf_cache/w2v-bert-2.0/config.json", W2V_REPO, W2V_REVISION, "f5572bd5998b68182e9c328a43127ed21fed687f6910497136b91a4e3b0e3675"),
    ("hf_cache/w2v-bert-2.0/model.safetensors", W2V_REPO, W2V_REVISION, "eb890c9660ed6e3414b6812e27257b8ce5454365d5490d3ad581ea60b93be043"),
    ("hf_cache/w2v-bert-2.0/preprocessor_config.json", W2V_REPO, W2V_REVISION, "8e6281aad64f97e40534135a59dcc5d33571efae376f2a25adf5551951897ab4"),
    ("hf_cache/campplus_cn_common.bin", "iic/speech_campplus_sv_zh-cn_16k-common", "013c18884dffb82846cf472dd06dd4a08d97e2cd", "3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8"),
]
BIGVGAN = "nvidia/bigvgan_v2_22khz_80band_256x"
BIGVGAN_REVISION = "fede455c2532b4f611fcef720825137b405af776"
FILES += [
    ("hf_cache/bigvgan/config.json", BIGVGAN, BIGVGAN_REVISION, "88a1f47acf747db0b21e97a389d838566147f7a5464583ff5c8d819d870f03ee"),
    ("hf_cache/bigvgan/bigvgan_generator.pt", BIGVGAN, BIGVGAN_REVISION, "ee7862b70ad758a26e61f9901f3d3cbd187c26ea52d8adf8bded12bfe0603edb"),
]


def sha256(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def download(path, url, expected):
    if path.is_file() and sha256(path) == expected:
        print(f"Verified {path.name}", flush=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    # A complete partial from a previous interrupted run can be promoted directly.
    if not partial.is_file() or sha256(partial) != expected:
        subprocess.run(
            ["curl", "-fsSL", "--retry", "4", "--retry-all-errors", "--connect-timeout", "15",
             "--max-time", "900", "-C", "-", "-o", str(partial), url],
            check=True,
        )
        if sha256(partial) != expected:
            partial.unlink()
            raise RuntimeError(f"Checksum mismatch: {path.name}")
    partial.replace(path)
    print(f"Verified {path.name}", flush=True)


def main():
    models = Path(os.environ["MOMO_INDEX_MODELS"])
    hf = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    for relative, repo, revision, digest in FILES:
        path = models / relative
        if repo == BIGVGAN:
            url = f"{hf}/{repo}/resolve/{revision}/{path.name}"
        else:
            query = urlencode({"Revision": revision, "FilePath": path.name})
            url = f"https://www.modelscope.cn/api/v1/models/{repo}/repo?{query}"
        download(path, url, digest)


if __name__ == "__main__":
    main()
