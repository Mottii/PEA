"""Idempotent offline asset bootstrapper & SHA-256 verifier for PEA v2.

Manages the four offline runtime asset groups under ``local_assets/``:
  1. ``katex``  — Offline KaTeX 0.16.21 CSS, JS, auto-render contrib & .woff2 fonts
  2. ``ocr``    — Pinned RapidOCR PP-OCRv5 Mobile Det + Cyrillic Rec ONNX models
  3. ``ruffle`` — Pinned Ruffle 0.7.1 JS + WASM runtime
  4. ``fonts``  — Noto Sans Regular/Bold TTF + SIL OFL license
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ASSET_GROUPS: tuple[str, ...] = ("katex", "ocr", "ruffle", "fonts")


@dataclass(frozen=True)
class DirectAsset:
    relpath: str
    url: str
    sha256: str


@dataclass(frozen=True)
class TarballSpec:
    version: str
    url: str
    tarball_sha256: str | None
    files: dict[str, tuple[str, str]]  # dest_relpath -> (tar_member_name, expected_sha256)


KATEX_SPEC = TarballSpec(
    version="0.16.21",
    url="https://registry.npmjs.org/katex/-/katex-0.16.21.tgz",
    tarball_sha256="7764a23b6c42d0a260fc1c8dcfe91d73e07d802dfc0624c08997b33c02dac0cd",
    files={
        "katex/LICENSE": (
            "package/LICENSE",
            "766ccc1f306c885aa45542a9846bbd0a505b27a0374f146778171c2254ce18e3",
        ),
        "katex/katex.min.css": (
            "package/dist/katex.min.css",
            "f787891b550d554c214aa8902f39ac46df2dbd48fdec500a2040a5dce1e8ab58",
        ),
        "katex/katex.min.js": (
            "package/dist/katex.min.js",
            "863811e2baa0849c77bc92d26d44f4d0a4843c2a8ab52c462017dea57316e4d8",
        ),
        "katex/contrib/auto-render.min.js": (
            "package/dist/contrib/auto-render.min.js",
            "bb53eb953394531aae36fdd537065c4244eb8542901a3ce914601d932675b8ac",
        ),
        "katex/fonts/KaTeX_AMS-Regular.woff2": (
            "package/dist/fonts/KaTeX_AMS-Regular.woff2",
            "0cdd387c9590a1a9f9794560022dbb59654a7d86f187aa0c81495ad42d3a7308",
        ),
        "katex/fonts/KaTeX_Caligraphic-Bold.woff2": (
            "package/dist/fonts/KaTeX_Caligraphic-Bold.woff2",
            "de7701e42cf1f4cf0b766c03fb27977207eee2f4fd5d76fa82188406da43ea4c",
        ),
        "katex/fonts/KaTeX_Caligraphic-Regular.woff2": (
            "package/dist/fonts/KaTeX_Caligraphic-Regular.woff2",
            "5d53e70ad607c2352162dec9e0923fb54ecdafaccbf604cd8dcf7d00facb989b",
        ),
        "katex/fonts/KaTeX_Fraktur-Bold.woff2": (
            "package/dist/fonts/KaTeX_Fraktur-Bold.woff2",
            "74444efd593c005e3f4573b44524704c0af0a937fe911cca9e94068d0d140d3f",
        ),
        "katex/fonts/KaTeX_Fraktur-Regular.woff2": (
            "package/dist/fonts/KaTeX_Fraktur-Regular.woff2",
            "51814d270d06ff0255dba0799994fa4d8c84d11f09951d47595f4abb1f3602dc",
        ),
        "katex/fonts/KaTeX_Main-Bold.woff2": (
            "package/dist/fonts/KaTeX_Main-Bold.woff2",
            "0f60d1b897938ec918c8ce073092411baf9438f6739465693ff18b0f9d20b021",
        ),
        "katex/fonts/KaTeX_Main-BoldItalic.woff2": (
            "package/dist/fonts/KaTeX_Main-BoldItalic.woff2",
            "99cd42a3c072d918f2f44984a807cf7aa16e13545fd0875fc07c6c65f99e715b",
        ),
        "katex/fonts/KaTeX_Main-Italic.woff2": (
            "package/dist/fonts/KaTeX_Main-Italic.woff2",
            "97479ca6cce906abc961ecac96faa5f9ca2e61b8e7670d475826bcdee9a7c267",
        ),
        "katex/fonts/KaTeX_Main-Regular.woff2": (
            "package/dist/fonts/KaTeX_Main-Regular.woff2",
            "c2342cd8b869e01752a9321dc17213fc40d4d04c79688c1d43f2cf316abd7866",
        ),
        "katex/fonts/KaTeX_Math-BoldItalic.woff2": (
            "package/dist/fonts/KaTeX_Math-BoldItalic.woff2",
            "dc47344dbb6cb5b655c8460d561f4df5f501b90c804ad3c6cec65fe322351ab1",
        ),
        "katex/fonts/KaTeX_Math-Italic.woff2": (
            "package/dist/fonts/KaTeX_Math-Italic.woff2",
            "7af58c5ec8f132a2ddde9027c6d7814decce4d3b822a11192a42a20e2e973264",
        ),
        "katex/fonts/KaTeX_SansSerif-Bold.woff2": (
            "package/dist/fonts/KaTeX_SansSerif-Bold.woff2",
            "e99ae51144bf1232efcc1bfe5add36262c6866b0faab24fa75740e1b98577a62",
        ),
        "katex/fonts/KaTeX_SansSerif-Italic.woff2": (
            "package/dist/fonts/KaTeX_SansSerif-Italic.woff2",
            "00b26ac825e2095056396e0553b8ac26d3f8ad158c3826e28b4c45b385c4714a",
        ),
        "katex/fonts/KaTeX_SansSerif-Regular.woff2": (
            "package/dist/fonts/KaTeX_SansSerif-Regular.woff2",
            "68e8c73ef42afd3ccec58bf0fba302cce448938e7fc020a5e31f8a952eee1342",
        ),
        "katex/fonts/KaTeX_Script-Regular.woff2": (
            "package/dist/fonts/KaTeX_Script-Regular.woff2",
            "036d4e95149b69ff9bcc0cd55771efeb25ffa3947293e69acd78d5ac328c684b",
        ),
        "katex/fonts/KaTeX_Size1-Regular.woff2": (
            "package/dist/fonts/KaTeX_Size1-Regular.woff2",
            "6b47c40166b6dbe21a5dfca7718413f2147fd2399be1ba605d8ad39cedf25dfe",
        ),
        "katex/fonts/KaTeX_Size2-Regular.woff2": (
            "package/dist/fonts/KaTeX_Size2-Regular.woff2",
            "d04c54219f9eaec6d4d4fd42dfb28785975a4794d6b2fc71e566b9cd6db842dd",
        ),
        "katex/fonts/KaTeX_Size3-Regular.woff2": (
            "package/dist/fonts/KaTeX_Size3-Regular.woff2",
            "73d591271b1604960cb10bb90fee021670af7297017e0e98480b332d11f51995",
        ),
        "katex/fonts/KaTeX_Size4-Regular.woff2": (
            "package/dist/fonts/KaTeX_Size4-Regular.woff2",
            "a4af7d414440a1c1790825cfb700cf9cf43b0f2c4b04f0ebc523011ad9853ec0",
        ),
        "katex/fonts/KaTeX_Typewriter-Regular.woff2": (
            "package/dist/fonts/KaTeX_Typewriter-Regular.woff2",
            "71d517d67827787cfabdf186914cc3358eda539e37931941f2b2fd4a21f68c0b",
        ),
    },
)

RUFFLE_SPEC = TarballSpec(
    version="0.7.1",
    url="https://registry.npmjs.org/@ruffle-rs/ruffle/-/ruffle-0.7.1.tgz",
    tarball_sha256=None,
    files={
        "ruffle/package.json": (
            "package/package.json",
            "822dd0fe6f6a84d4ab41351acd2a789aa9ef75b3fe454ab846c0858157c48d89",
        ),
        "ruffle/ruffle.js": (
            "package/ruffle.js",
            "26036a94567088dfb3de88a759e8ed10a4cea07b29500af36f345eef42420098",
        ),
        "ruffle/172de42e6619eac8f371.wasm": (
            "package/172de42e6619eac8f371.wasm",
            "1ed33b9b71a6b01b9aafafed660be343ffeb2d246c5d79e6425a32bf93e38207",
        ),
        "ruffle/878d548199cddef07035.wasm": (
            "package/878d548199cddef07035.wasm",
            "af9b6a13a8f5e8d305bc3ce28a4edf3f6242df1183e8576ad4ca086e9f21eaea",
        ),
        "ruffle/core.ruffle.16d61072ac7fdc4d5013.js": (
            "package/core.ruffle.16d61072ac7fdc4d5013.js",
            "d27c6bdce2a34afb32e8dfa40970bc7e86cbcb912f92db7eb3e1ac70e943bd82",
        ),
        "ruffle/core.ruffle.994eeef0a471a88cd0e3.js": (
            "package/core.ruffle.994eeef0a471a88cd0e3.js",
            "d89e507ee60d550629e9229aa1e4dd4f9e75ec7db271e0135cbc87dab042ea51",
        ),
        "ruffle/LICENSE_APACHE": (
            "package/LICENSE_APACHE",
            "62c7a1e35f56406896d7aa7ca52d0cc0d272ac022b5d2796e7d6905db8a3636a",
        ),
        "ruffle/LICENSE_MIT": (
            "package/LICENSE_MIT",
            "4de9338a7879c68e911742a7d691f0797ff1ef8d8a6fb978b0c711e258fe959c",
        ),
    },
)

OCR_VERSION = "rapidocr-3.10.0-ppocrv5-mobile"
OCR_ASSETS: tuple[DirectAsset, ...] = (
    DirectAsset(
        relpath="ocr/ch_PP-OCRv5_det_mobile.onnx",
        url="https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx",
        sha256="4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
    ),
    DirectAsset(
        relpath="ocr/cyrillic_PP-OCRv5_rec_mobile.onnx",
        url="https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5/rec/cyrillic_PP-OCRv5_rec_mobile.onnx",
        sha256="90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711",
    ),
)

FONTS_VERSION = "noto-sans-2.008-hinted"
FONT_ASSETS: tuple[DirectAsset, ...] = (
    DirectAsset(
        relpath="fonts/NotoSans-Regular.ttf",
        url="https://raw.githubusercontent.com/notofonts/noto-fonts/main/hinted/ttf/NotoSans/NotoSans-Regular.ttf",
        sha256="b85c38ecea8a7cfb39c24e395a4007474fa5a4fc864f6ee33309eb4948d232d5",
    ),
    DirectAsset(
        relpath="fonts/NotoSans-Bold.ttf",
        url="https://raw.githubusercontent.com/notofonts/noto-fonts/main/hinted/ttf/NotoSans/NotoSans-Bold.ttf",
        sha256="c976e4b1b99edc88775377fcc21692ca4bfa46b6d6ca6522bfda505b28ff9d6a",
    ),
    DirectAsset(
        relpath="fonts/LICENSE.txt",
        url="https://raw.githubusercontent.com/notofonts/noto-fonts/main/LICENSE",
        sha256="0dab92d0544f7b233403f14b84a663bdbfa746982eda629e7f4f9ffe1b036feb",
    ),
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65_536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def write_bytes_atomic(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, dest)


def _download_bytes(url: str, timeout_s: float = 120.0) -> bytes:
    import httpx

    with httpx.Client(follow_redirects=True, timeout=timeout_s) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def _check_expected_map(assets_dir: Path, expected: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for relpath, want_sha in expected.items():
        target = assets_dir / relpath
        if not target.is_file():
            errors.append(f"MISSING {relpath}")
            continue
        got_sha = sha256_file(target)
        if got_sha != want_sha:
            errors.append(f"SHA256 MISMATCH {relpath}: got {got_sha}, expected {want_sha}")
    return errors


def _fetch_tarball_group(assets_dir: Path, spec: TarballSpec, group_name: str) -> None:
    expected = {rel: sha for rel, (_, sha) in spec.files.items()}
    if not _check_expected_map(assets_dir, expected):
        print(f"[ok] {group_name} ({spec.version}): already present and verified")
        return

    print(f"[fetch] {group_name} ({spec.version}) from {spec.url}")
    raw = _download_bytes(spec.url)
    if spec.tarball_sha256 is not None:
        got_tb = sha256_bytes(raw)
        if got_tb != spec.tarball_sha256:
            raise RuntimeError(
                f"{group_name} tarball SHA-256 mismatch: "
                f"got {got_tb}, expected {spec.tarball_sha256}"
            )

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        members = {m.name: m for m in archive.getmembers() if m.isfile()}
        for relpath, (member_name, expected_sha) in spec.files.items():
            member = members.get(member_name)
            if member is None:
                raise RuntimeError(f"Expected member {member_name!r} missing from {spec.url}")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RuntimeError(f"Failed to read member {member_name!r} from {spec.url}")
            content = extracted.read()
            got_sha = sha256_bytes(content)
            if got_sha != expected_sha:
                raise RuntimeError(
                    f"Extracted file {relpath} SHA-256 mismatch: "
                    f"got {got_sha}, expected {expected_sha}"
                )
            write_bytes_atomic(assets_dir / relpath, content)
    print(f"[done] {group_name} ({len(spec.files)} files verified)")


def _fetch_direct_group(
    assets_dir: Path,
    assets: tuple[DirectAsset, ...],
    group_name: str,
    version: str,
) -> None:
    for item in assets:
        dest = assets_dir / item.relpath
        if dest.is_file() and sha256_file(dest) == item.sha256:
            print(f"[ok] {group_name}: {item.relpath} already verified")
            continue
        print(f"[fetch] {group_name}: {item.relpath} from {item.url}")
        content = _download_bytes(item.url)
        got_sha = sha256_bytes(content)
        if got_sha != item.sha256:
            raise RuntimeError(
                f"Downloaded {item.relpath} SHA-256 mismatch: got {got_sha}, expected {item.sha256}"
            )
        write_bytes_atomic(dest, content)
        print(f"[done] {group_name}: {item.relpath} ({len(content)} bytes)")


def expected_files_for_group(group: str) -> dict[str, str]:
    if group == "katex":
        return {rel: sha for rel, (_, sha) in KATEX_SPEC.files.items()}
    if group == "ruffle":
        return {rel: sha for rel, (_, sha) in RUFFLE_SPEC.files.items()}
    if group == "ocr":
        return {item.relpath: item.sha256 for item in OCR_ASSETS}
    if group == "fonts":
        return {item.relpath: item.sha256 for item in FONT_ASSETS}
    raise ValueError(f"Unknown asset group: {group!r}")


def check_assets(
    assets_dir: Path,
    groups: tuple[str, ...] = ASSET_GROUPS,
    *,
    require_manifest: bool = True,
) -> list[str]:
    """Verify every pinned file across *groups* offline (zero network calls)."""
    errors: list[str] = []
    for group in groups:
        errors.extend(_check_expected_map(assets_dir, expected_files_for_group(group)))

    if "ruffle" in groups:
        pkg_path = assets_dir / "ruffle" / "package.json"
        if pkg_path.is_file():
            try:
                pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
                if pkg.get("version") != RUFFLE_SPEC.version:
                    errors.append(
                        f"RUFFLE VERSION MISMATCH: got {pkg.get('version')!r}, "
                        f"expected {RUFFLE_SPEC.version!r}"
                    )
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"INVALID ruffle/package.json: {exc}")

    if require_manifest:
        manifest_path = assets_dir / "manifest.json"
        if not manifest_path.is_file():
            errors.append("MISSING manifest.json (run with --fetch to generate)")
        else:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest_groups = manifest.get("groups", {})
                for group in groups:
                    if group not in manifest_groups:
                        errors.append(f"manifest.json missing group {group!r}")
                        continue
                    recorded_files = manifest_groups[group].get("files", {})
                    for relpath, want_sha in expected_files_for_group(group).items():
                        rec = recorded_files.get(relpath)
                        if not isinstance(rec, dict) or rec.get("sha256") != want_sha:
                            errors.append(f"manifest.json mismatch for {relpath}")
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"INVALID manifest.json: {exc}")

    return errors


def build_manifest(assets_dir: Path) -> dict[str, Any]:
    """Construct the ``local_assets/manifest.json`` payload from verified files."""
    group_meta = {
        "katex": {"version": KATEX_SPEC.version, "source": KATEX_SPEC.url},
        "ocr": {
            "version": OCR_VERSION,
            "source": "https://www.modelscope.cn/models/RapidAI/RapidOCR",
        },
        "ruffle": {"version": RUFFLE_SPEC.version, "source": RUFFLE_SPEC.url},
        "fonts": {"version": FONTS_VERSION, "source": "https://github.com/notofonts/noto-fonts"},
    }
    groups_payload: dict[str, Any] = {}
    for group in ASSET_GROUPS:
        files_payload: dict[str, Any] = {}
        for relpath, expected_sha in sorted(expected_files_for_group(group).items()):
            path = assets_dir / relpath
            if not path.is_file():
                continue
            actual_sha = sha256_file(path)
            if actual_sha != expected_sha:
                raise RuntimeError(f"Refusing to write manifest with unverified file: {relpath}")
            files_payload[relpath] = {
                "sha256": actual_sha,
                "size_bytes": path.stat().st_size,
            }
        groups_payload[group] = {
            **group_meta[group],
            "files": files_payload,
        }

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "groups": groups_payload,
    }


def fetch_assets(assets_dir: Path, groups: tuple[str, ...] = ASSET_GROUPS) -> None:
    """Fetch any missing/mismatched assets in *groups*, verify SHA-256s, and write manifest."""
    assets_dir.mkdir(parents=True, exist_ok=True)
    if "katex" in groups:
        _fetch_tarball_group(assets_dir, KATEX_SPEC, "katex")
    if "ocr" in groups:
        _fetch_direct_group(assets_dir, OCR_ASSETS, "ocr", OCR_VERSION)
    if "ruffle" in groups:
        _fetch_tarball_group(assets_dir, RUFFLE_SPEC, "ruffle")
    if "fonts" in groups:
        _fetch_direct_group(assets_dir, FONT_ASSETS, "fonts", FONTS_VERSION)

    manifest = build_manifest(assets_dir)
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_bytes_atomic(assets_dir / "manifest.json", manifest_bytes)
    print(f"[manifest] wrote {(assets_dir / 'manifest.json').name}")


def default_assets_dir() -> Path:
    """Resolve the default offline assets directory via ``PeaConfig``."""
    from pea.config import PeaConfig

    return PeaConfig.from_env().local_assets_dir


def main(
    argv: list[str] | None = None,
    *,
    default_dir: Path | None = None,
) -> int:
    resolved_default = default_dir if default_dir is not None else default_assets_dir()
    parser = argparse.ArgumentParser(
        description="Fetch and verify SHA-256-pinned offline runtime assets for PEA v2."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="Verify all pinned assets and manifest offline (zero network calls).",
    )
    mode.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch missing or mismatched assets, verify SHA-256 hashes, and update manifest.json.",
    )
    parser.add_argument(
        "--assets-dir",
        type=Path,
        default=resolved_default,
        help="Path to local_assets directory.",
    )
    parser.add_argument(
        "--group",
        action="append",
        choices=ASSET_GROUPS,
        help="Restrict operation to one or more asset groups (default: all four).",
    )
    args = parser.parse_args(argv)
    groups = tuple(args.group) if args.group else ASSET_GROUPS

    if args.fetch:
        fetch_assets(args.assets_dir, groups)

    errors = check_assets(args.assets_dir, groups, require_manifest=True)
    if errors:
        print(f"Asset verification FAILED ({len(errors)} issue(s)):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    total_files = sum(len(expected_files_for_group(g)) for g in groups)
    joined_groups = ", ".join(groups)
    print(f"OK: all {total_files} pinned offline assets verified across groups {joined_groups}.")
    return 0


__all__ = [
    "ASSET_GROUPS",
    "FONTS_VERSION",
    "FONT_ASSETS",
    "KATEX_SPEC",
    "OCR_ASSETS",
    "OCR_VERSION",
    "RUFFLE_SPEC",
    "DirectAsset",
    "TarballSpec",
    "build_manifest",
    "check_assets",
    "default_assets_dir",
    "expected_files_for_group",
    "fetch_assets",
    "main",
    "sha256_bytes",
    "sha256_file",
    "write_bytes_atomic",
]


if __name__ == "__main__":
    raise SystemExit(main())
