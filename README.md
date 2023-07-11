# flatpak-oci-test-content

These are some scripts to create test Flatpak content in OCI format

* **oci-net.fishsoup.BusyBoxPlatform**: A platform based on the BusyBox docker
  image that just has /bin/sh
* **oci-net.fishsoup.Hello**: An application that is just a script that
  prints "Hello, World"

They are created as multi-arch (amd64 and arm64).

## Usage

``` sh
./make-test-content.py
./install-oci.py oci-net.fishsoup.BusyBoxPlatform
./install-oci.py oci-net.fishsoup.Hello
flatpak run oci-net.fishsoup.Hello
```

## Authors and License

The scripts are written by Owen Taylor <otaylor@redhat.com> and licensed under the terms of the [MIT LICENSE](LICENSE).
