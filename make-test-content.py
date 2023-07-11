#!/usr/bin/python3

import hashlib
import json
import os
from pathlib import Path
import shutil
from subprocess import check_call
import tarfile
import tempfile
from textwrap import dedent
from typing import Any, Callable, Dict


# Based on code (with the same author) at:
# https://pagure.io/flatpak-module-tools/blob/master/f/flatpak_module_tools/installer.py


def header(str):
    print(f"\033[1m{str}\033[0m")


def create_oci(workdir: Path,
               ref: str,
               metadata: str,
               add_files: Callable[[Path], None]):
    prefix, name, arch, branch = ref.split("/")
    is_runtime = prefix == "runtime"

    output_dir = workdir / f"oci-{name}-{arch}"
    header(f"Creating single-arch image at {output_dir}")
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)

    builddir = workdir / f"build-{name}-{arch}"
    filesdir = builddir / "files"
    filesdir.mkdir(parents=True)

    repo = workdir / "repo"
    check_call(["ostree", "init", "--mode=archive-z2", "--repo", repo])

    with open(builddir / "metadata", "w") as f:
        f.write(metadata)

    add_files(filesdir)

    check_call([
        "flatpak", "build-finish", builddir
    ])

    with open(os.path.join(builddir, "metadata"), "r") as f:
        metadata = f.read()

    commit_args = ["--repo", repo, "--owner-uid=0",
                   "--owner-gid=0", "--no-xattrs",
                   "--canonical-permissions",
                   "--branch", ref,
                   "-s", "build of " + ref,
                   f"--tree=dir={builddir}",
                   "--add-metadata-string", "xa.metadata=" + metadata]

    check_call(["ostree", "commit"] + commit_args)
    check_call(["ostree", "summary", "-u", "--repo", repo])

    runtime_arg = ["--runtime"] if is_runtime else []
    check_call([
        "flatpak", "build-bundle", repo,
        "--oci"
    ] + runtime_arg + [
        "--arch", arch,
        output_dir, name, branch
    ])

    return output_dir


def create_runtime_oci(workdir: Path, arch: str, contents_tar: Path):
    name = "net.fishsoup.BusyBoxPlatform"
    branch = "2023"
    ref = f"runtime/{name}/{arch}/{branch}"

    metadata = dedent(f"""\
        [Runtime]
        name={name}
        runtime=net.fishsoup.BusyBoxPlatform/{arch}/{branch}
        sdk=net.fishsoup.BusyBoxSdk/{arch}/{branch}
        """)

    def add_files(filesdir: Path):
        # The Flatpak code to create an OCI doesn't handle hard-links efficiently,
        # so we only include /bin/sh and not all the other files in /bin
        # (otherwise we could just use tf.extractall())
        tf = tarfile.open(contents_tar, "r:gz")
        for member in tf:
            if member.name.startswith("bin/") and member.name != "bin/sh":
                continue
            tf.extract(member, filesdir)

    return create_oci(workdir, ref, metadata, add_files)


def create_app_oci(workdir: Path, arch: str):
    name = "net.fishsoup.Hello"
    branch = "stable"
    ref = f"app/{name}/{arch}/{branch}"

    metadata = dedent(f"""\
        [Application]
        name={name}
        runtime=net.fishsoup.BusyBoxPlatform/{arch}/2023
        sdk=net.fishsoup.BusyBoxSdk/{arch}/2023
        command=/app/bin/hello
        """)

    def add_files(filesdir: Path):
        bindir = filesdir / "bin"
        bindir.mkdir()
        hello = bindir / "hello"
        with open(hello, "w") as f:
            f.write(dedent("""
                #!/bin/sh
                echo "Hello World"
            """))
        hello.chmod(0o0755)

    return create_oci(workdir, ref, metadata, add_files)


def load_json(path: Path):
    with open(path, "r") as f:
        return json.load(f)


def blob_path(base: Path, descriptor: Dict[str, Any]):
    assert descriptor["digest"][:7] == "sha256:"
    return base / "blobs/sha256" / descriptor["digest"][7:]


def load_json_blob(base: Path, digest):
    return load_json(blob_path(base, digest))


def make_multiarch_image(output_dir: Path, images: Dict[str, Path]):
    header(f"Creating multi-arch image at {output_dir}")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    blobs_dir = output_dir / "blobs/sha256"
    blobs_dir.mkdir(parents=True)

    image_layout = {
        "imageLayoutVersion": "1.0.0"
    }

    with open(output_dir / "oci-layout", "w") as f:
        json.dump(image_layout, f, indent=4)

    image_index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": []
    }

    for _, input_dir in images.items():
        for blob in (input_dir / "blobs/sha256").iterdir():
            shutil.copyfile(blob, blobs_dir / blob.name)

        input_image_index = load_json(input_dir / "index.json")
        manifest_descriptor = input_image_index["manifests"][0]
        manifest = load_json_blob(input_dir, manifest_descriptor)
        config = load_json_blob(input_dir, manifest["config"])

        output_descriptor = dict(manifest_descriptor)
        output_descriptor["platform"] = {
            "os": config["os"],
            "architecture": config["architecture"]
        }

        image_index["manifests"].append(output_descriptor)

    image_index_contents = json.dumps(image_index, indent=4).encode("utf8")
    image_index_digest = hashlib.sha256(image_index_contents).hexdigest()

    with open(output_dir / "blobs/sha256" / image_index_digest, "wb") as f:
        f.write(image_index_contents)

    archive_index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [{
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "digest": "sha256:" + image_index_digest,
            "size": len(image_index_digest)
        }]
    }

    with open(output_dir / "index.json", "w") as f:
        json.dump(archive_index, f, indent=4)


def main(workdir: Path):
    header("Downloading busybox docker image (all architectures)")

    busybox = workdir / "busybox"
    check_call([
        "skopeo", "copy", "--multi-arch=all",
        "docker://docker.io/library/busybox",
        f"oci:{busybox}"
    ])

    index = load_json(busybox / "index.json")
    image_list = load_json_blob(busybox, index["manifests"][0])

    contents_tars: Dict[str, Path] = {}
    runtimes: Dict[str, Path] = {}
    apps: Dict[str, Path] = {}

    for descriptor in image_list["manifests"]:
        arch = descriptor["platform"]["architecture"]
        if arch == "amd64":
            flatpak_arch = "x86_64"
        elif arch == "arm64":
            flatpak_arch = "aarch64"
        else:
            continue

        manifest = load_json_blob(busybox, descriptor)
        contents_tars[flatpak_arch] = blob_path(busybox, manifest["layers"][0])

    for flatpak_arch, contents_tar in contents_tars.items():
        runtimes[flatpak_arch] = create_runtime_oci(workdir, flatpak_arch, contents_tar)
        apps[flatpak_arch] = create_app_oci(workdir, flatpak_arch)

    make_multiarch_image(Path("oci-net.fishsoup.BusyBoxPlatform"), runtimes)
    make_multiarch_image(Path("oci-net.fishsoup.Hello"), apps)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="flatpak-oci-") as td:
        main(workdir=Path(td))
