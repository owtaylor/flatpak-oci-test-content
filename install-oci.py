#!/usr/bin/python3

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from subprocess import check_call
import sys
import tempfile
from typing import Any, Dict


# Based on code (with the same author) at:
# https://pagure.io/flatpak-module-tools/blob/master/f/flatpak_module_tools/flatpak_builder.py


def get_path_from_descriptor(base: Path, descriptor: Dict[str, Any]):
    assert descriptor["digest"].startswith("sha256:")
    return os.path.join(
        base, "blobs", "sha256", descriptor["digest"][len("sha256:"):]
    )


def make_single_arch_copy(source_path: Path, dest_path: Path):
    # skopeo copy --multi-arch=system doesn't preserve the literal
    # OCI manifest with annotations!
    # Flatpak requires the org.opencontainers.image.ref.name even
    # when it doesn't need to!
    check_call([
        "skopeo", "copy", "--multi-arch=system",
        f"oci:{source_path}",
        f"oci:{dest_path}"
    ])

    with open(source_path / "index.json", "rb") as f:
        old_index_json = json.load(f)

    with open(get_path_from_descriptor(source_path, old_index_json['manifests'][0])) as f:
        image_index_json = json.load(f)

    with open(dest_path / "index.json", "rb") as f:
        new_index_json = json.load(f)

    with open(get_path_from_descriptor(dest_path, new_index_json["manifests"][0]), "r") as f:
        new_manifest_json = json.load(f)
    with open(get_path_from_descriptor(dest_path, new_manifest_json["config"]), "r") as f:
        config = json.load(f)
    architecture = config["architecture"]

    for manifest in image_index_json["manifests"]:
        if manifest["platform"]["architecture"] == architecture:
            src = get_path_from_descriptor(source_path, manifest)
            dest = get_path_from_descriptor(dest_path, manifest)
            shutil.copyfile(src, dest)
            new_index_json["manifests"] = [manifest]
            break

    with open(dest_path / "index.json", "w") as f:
        json.dump(new_index_json, f, indent=4)


class Installer:
    def __init__(self, source_path: Path):
        self.source_path = source_path

        data_home = os.environ.get('XDG_DATA_HOME',
                                   os.path.expanduser('~/.local/share'))
        self.repodir = os.path.join(data_home, 'flatpak-module-tools', 'repo')

    def ensure_remote(self):
        if not os.path.exists(self.repodir):
            parent = os.path.dirname(self.repodir)
            if not os.path.exists(parent):
                os.makedirs(parent)

            check_call(['ostree', 'init', '--mode=archive-z2', '--repo', self.repodir])
            check_call(['flatpak', 'build-update-repo', self.repodir])

        output = subprocess.check_output(['flatpak', 'remotes', '--user'], encoding="UTF-8")
        if not re.search(r'^flatpak-module-tools\s', output, re.MULTILINE):
            check_call(['flatpak', 'remote-add',
                        '--user', '--no-gpg-verify',
                        'flatpak-module-tools', self.repodir])

    def _install_from_path(self, source_path: Path):
        with open(os.path.join(source_path, 'index.json')) as f:
            index_json = json.load(f)

        with open(get_path_from_descriptor(source_path, index_json['manifests'][0])) as f:
            manifest_json = json.load(f)

        if manifest_json["mediaType"] == "application/vnd.oci.image.index.v1+json":
            # multi-arch bundle, Flatpak doesn't support this, make a single-arch copy
            with tempfile.TemporaryDirectory(prefix="flatpak-oci-") as td:
                make_single_arch_copy(source_path, Path(td))
                self._install_from_path(Path(td))
                return

        with open(get_path_from_descriptor(source_path, manifest_json["config"])) as f:
            config_json = json.load(f)

            config = config_json.get("config", {})
            labels = config.get("Labels", {})

            ref = labels.get('org.flatpak.ref')

        if ref is None:
            raise RuntimeError(
                "org.flatpak.ref not found in annotations or labels - is this a Flatpak?"
            )

        check_call(['flatpak', 'build-import-bundle',
                    '--update-appstream', '--oci',
                    '--ref', ref,
                    self.repodir, source_path])

        parts = ref.split('/')
        shortref = parts[0] + '/' + parts[1]

        try:
            with open(os.devnull, 'w') as devnull:
                old_origin = subprocess.check_output(['flatpak', 'info', '--user', '-o', shortref],
                                                     stderr=devnull, encoding="UTF-8").strip()
        except subprocess.CalledProcessError:
            old_origin = None

        if old_origin == 'flatpak-module-tools':
            check_call([
                'flatpak', 'update', '-y', '--user', ref
            ])
        else:
            check_call([
                'flatpak', 'install', '-y', '--user', '--reinstall', 'flatpak-module-tools', ref
            ])

    def install(self):
        print('INSTALLING')

        self.ensure_remote()
        self._install_from_path(self.source_path)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("usage: install-oci.py PATH")

    installer = Installer(Path(sys.argv[1]))
    installer.install()
