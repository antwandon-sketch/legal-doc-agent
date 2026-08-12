#!/usr/bin/env python3
"""
Download and verify the CUAD corpus.

Resolves the download location from the TheAtticusProject/cuad GitHub repo
at run time (rather than hardcoding a direct URL, since the repo has moved
before and may move again) and stores the corpus under data/cuad/raw/,
which is gitignored -- the corpus itself is never committed.

Usage:
    python scripts/fetch_cuad.py [--force]
"""
import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import DATA_DIR, RAW_DIR  # noqa: E402

GITHUB_REPO_SLUG = "TheAtticusProject/cuad"
GITHUB_API_ROOT = f"https://api.github.com/repos/{GITHUB_REPO_SLUG}"

# Files we expect inside data.zip once unpacked -- if the upstream repo
# restructures the archive, this check should fail loudly rather than
# silently loading something else.
EXPECTED_MEMBERS = {"CUADv1.json", "train_separate_questions.json", "test.json"}


def resolve_repo(session: requests.Session) -> dict:
    """Follow GitHub's redirect if the repo has been renamed/transferred."""
    resp = session.get(GITHUB_API_ROOT, timeout=30)
    resp.raise_for_status()
    return resp.json()


def find_data_zip(session: requests.Session, full_name: str, default_branch: str) -> dict:
    contents_url = f"https://api.github.com/repos/{full_name}/contents/"
    resp = session.get(contents_url, params={"ref": default_branch}, timeout=30)
    resp.raise_for_status()
    entries = resp.json()
    for entry in entries:
        if entry["type"] == "file" and entry["name"] == "data.zip":
            return entry
    raise RuntimeError(
        f"data.zip not found in {full_name}@{default_branch} root -- "
        "upstream repo structure may have changed, check manually."
    )


def find_category_descriptions(session: requests.Session, full_name: str, default_branch: str) -> dict | None:
    contents_url = f"https://api.github.com/repos/{full_name}/contents/"
    resp = session.get(contents_url, params={"ref": default_branch}, timeout=30)
    resp.raise_for_status()
    for entry in resp.json():
        if entry["type"] == "file" and entry["name"] == "category_descriptions.csv":
            return entry
    return None


def git_blob_sha1(data: bytes) -> str:
    """GitHub's `sha` field on a contents-API entry is the git blob sha1,
    not a plain sha1 of the file bytes -- reproduce that so the check is
    meaningful."""
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def download_file(session: requests.Session, url: str, dest: Path, expected_sha: str | None = None) -> None:
    resp = session.get(url, timeout=120, stream=True)
    resp.raise_for_status()
    data = resp.content
    if expected_sha and git_blob_sha1(data) != expected_sha:
        raise RuntimeError(f"checksum mismatch downloading {url} -- corpus may be corrupted, retry")
    dest.write_bytes(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download even if data/cuad/raw already looks populated")
    args = parser.parse_args()

    if RAW_DIR.exists() and any(RAW_DIR.iterdir()) and not args.force:
        existing = {p.name for p in RAW_DIR.iterdir()}
        if EXPECTED_MEMBERS.issubset(existing):
            print(f"CUAD corpus already present at {RAW_DIR} (use --force to re-download).")
            return
        print(f"{RAW_DIR} exists but is incomplete; re-fetching.")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers["Accept"] = "application/vnd.github+json"

    print(f"Resolving {GITHUB_REPO_SLUG} on GitHub (repo may have been renamed/transferred)...")
    repo = resolve_repo(session)
    full_name = repo["full_name"]
    default_branch = repo["default_branch"]
    print(f"Resolved to {full_name}@{default_branch}")

    zip_entry = find_data_zip(session, full_name, default_branch)
    zip_path = DATA_DIR / "data.zip"
    print(f"Downloading data.zip ({zip_entry['size'] / 1e6:.1f} MB) from {zip_entry['download_url']}")
    download_file(session, zip_entry["download_url"], zip_path, expected_sha=zip_entry.get("sha"))
    print(f"Downloaded and verified: {zip_path}")

    print(f"Extracting to {RAW_DIR}...")
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        missing = EXPECTED_MEMBERS - names
        if missing:
            raise RuntimeError(f"data.zip is missing expected files: {missing} (found: {names})")
        zf.extractall(RAW_DIR)

    cat_entry = find_category_descriptions(session, full_name, default_branch)
    if cat_entry:
        cat_path = RAW_DIR / "category_descriptions.csv"
        download_file(session, cat_entry["download_url"], cat_path, expected_sha=cat_entry.get("sha"))
        print(f"Downloaded {cat_path}")

    print(f"Done. CUAD corpus ready at {RAW_DIR}")
    for member in sorted(EXPECTED_MEMBERS):
        size = (RAW_DIR / member).stat().st_size
        print(f"  {member}: {size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
