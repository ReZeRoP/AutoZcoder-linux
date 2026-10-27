Name:           autozcoder
Version:        1.2.2
Release:        1%{?dist}
Summary:        Native desktop dashboard for the local ZCode API gateway
# Local bundled RPM, not an official Fedora package. See THIRD_PARTY_NOTICES.md.
License:        MIT AND CC-BY-NC-SA-4.0 AND LicenseRef-Bundled-Dependencies
URL:            https://github.com/RiZiRo/AutoZcoder-linux
Source0:        autozcoder-%{version}-fedora.tar.gz
BuildArch:      x86_64
BuildRequires:  desktop-file-utils
Requires:       python3 >= 3.14
Requires:       python3 < 3.15
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       gtk3
Requires:       webkit2gtk4.1
Requires:       ca-certificates
Requires:       nodejs(engine) >= 22
Requires:       /usr/bin/node
Requires:       /usr/bin/npm
Requires:       alsa-lib
Requires:       nss
Requires:       libXcomposite
Requires:       libXdamage
Requires:       libXfixes
Requires:       libXrandr
Requires:       libxkbcommon
Requires:       mesa-libgbm
Requires:       at-spi2-atk
Requires:       cups-libs
Requires:       libatomic
Requires:       libxcrypt-compat
Requires:       libglvnd-glx
Requires:       libICE
Requires:       libSM
Requires:       libX11
Requires:       libXext
Requires:       libXft
Requires:       libXrender
Requires:       libxcb
Requires:       libstdc++
Requires:       libgcc
Requires:       glib2
Requires:       fontconfig
Requires:       freetype
Requires:       harfbuzz
Requires:       bzip2-libs
Requires:       libuuid
Requires:       libxml2
Requires:       libpng
Requires:       libbrotli
Requires:       libtommath
Requires:       pcre2
Requires:       tcl
Requires:       tk
Requires:       xz-libs
Requires:       zlib-ng-compat

%global debug_package %{nil}
# Preserve prebuilt relocatable ELF binaries; GNU strip corrupts this CPython build.
%global __brp_strip %{nil}
%global __brp_strip_comment_note %{nil}
%global __brp_strip_lto %{nil}
# Python libraries are private to this application, not system Python providers.
%global __provides_exclude_from ^%{_libdir}/autozcoder/.*$
%global __requires_exclude ^python3\.(12|14)dist\(.*\).*$
%global __requires_exclude_from ^%{_libdir}/autozcoder/.*$
%global _python_bytecompile_extra 0

%description
AutoZcoder's FastAPI gateway and dashboard in a native GTK/WebKit window.
This separate desktop package uses port 8789 and per-user XDG configuration
and data. It does not replace or start the existing zcode-gateway service.
Python application dependencies are bundled privately; Fedora provides the
interpreter and native GTK/WebKit runtime. Update this app using RPMs, not
its upstream Windows executable updater. The upstream CAPTCHA minter source
is retained with its CC BY-NC-SA 4.0 license, but no minter executable is built
or shipped for Linux. This is an unofficial fork, not a ZCode or Fedora product.

%prep
%setup -q -n autozcoder-%{version}-fedora

%build
# Locked dependencies were staged by packaging/fedora/build-rpm.sh.

%install
install -d %{buildroot}%{_datadir}/autozcoder %{buildroot}%{_libdir}/autozcoder
cp -a zcode_gateway generator mail-worker assets tools docs config.example.yaml RUNTIME-MANIFEST.json SOURCE-FILES.txt %{buildroot}%{_datadir}/autozcoder/
cp -a python generator-packages cpython browsers node-tools generator-python wrangler-run %{buildroot}%{_libdir}/autozcoder/
install -Dm0755 packaging/fedora/autozcoder %{buildroot}%{_bindir}/autozcoder
install -Dm0644 packaging/fedora/autozcoder.desktop %{buildroot}%{_datadir}/applications/autozcoder.desktop
install -Dm0644 packaging/fedora/autozcoder.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/autozcoder.svg
find %{buildroot} -type d -name __pycache__ -exec rm -rf {} +

%check
export HOME="$PWD/.check-home"
export XDG_CONFIG_HOME="$HOME/config" XDG_CACHE_HOME="$HOME/cache" XDG_DATA_HOME="$HOME/data"
export PYTHONDONTWRITEBYTECODE=1 WRANGLER_SEND_METRICS=false
mkdir -p "$HOME"
desktop-file-validate %{buildroot}%{_datadir}/applications/autozcoder.desktop
%{buildroot}%{_libdir}/autozcoder/generator-python -c 'import sys,playwright,ddddocr,cv2,numpy,PIL,requests,socks; print(sys.version)'
%{buildroot}%{_libdir}/autozcoder/wrangler-run --version

%files
%license LICENSE THIRD_PARTY_NOTICES.md tools/captcha-mint/LICENSE.vendor BUNDLED-LICENSES.txt
%doc README.md packaging/fedora/requirements.lock packaging/fedora/generator-requirements.lock packaging/fedora/runtime-versions.json
%doc packaging/fedora/node-tools/package.json packaging/fedora/node-tools/package-lock.json
%{_bindir}/autozcoder
%{_datadir}/autozcoder
%{_libdir}/autozcoder
%{_datadir}/applications/autozcoder.desktop
%{_datadir}/icons/hicolor/scalable/apps/autozcoder.svg

%changelog
* Tue Oct 27 2026 Hassan <hassan@localhost> - 1.2.2-1
- Rebase the separate Fedora 44 desktop port onto upstream 1.2.2.
- Add locked cache bootstrap, source filtering, manifests and license notices.
- Exclude Windows executables; retain but do not build CAPTCHA minter source.

* Thu Oct 22 2026 Hassan <hassan@localhost> - 1.1.0-4
- Initial local Fedora 44 desktop integration with private optional runtime.
