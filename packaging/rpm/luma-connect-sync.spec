# SPDX-License-Identifier: Apache-2.0
# Phase 1 of ADR-054. Not composed into an image until the Settings UI and
# the Hub module are live; it runs as a user-level preview until then.
Name:           luma-connect-sync
Version:        0.2.1
Release:        1.luma.1%{?dist}
Summary:        Luma Connect Sync: end-to-end encrypted sync of apps and app state
License:        Apache-2.0
BuildArch:      noarch
Source0:        luma-connect-sync.tar.gz

BuildRequires:  python3-devel
BuildRequires:  systemd-rpm-macros
# HPKE (RFC 9180) arrived in python-cryptography's hpke module.
BuildRequires:  python3dist(cryptography) >= 50
BuildRequires:  libsodium
BuildRequires:  python3-gobject-base
BuildRequires:  dbus-daemon
Requires:       python3
Requires:       python3dist(cryptography) >= 50
Requires:       libsodium
Requires:       python3-gobject-base
Requires:       libsecret
# Device keys are sealed with systemd-creds --user (systemd 256 and later).
Requires:       systemd >= 256
# An app's publisher is its Flatpak remote's URL and signing keys.
Requires:       gnupg2
Requires:       dconf
Requires:       flatpak
Requires:       polkit
# ADR-052 store apps are rebuilt on the receiving device by its installer.
Recommends:     luma-application-installer

%description
The user service luma-connect-syncd and the luma-connect-sync command
(ADR-054 Phase 1): device keys, join by code or recovery key, the installed
apps of each computer (Flatpak and ADR-052 store records) and the data
folders of closed apps, end-to-end encrypted through the Luma Connect Hub.
It serves org.projectluma.ConnectSync1 on the session bus.

%prep
%autosetup -n luma-connect-sync

%build

%install
install -d %{buildroot}%{python3_sitelib}/luma_connect_sync
install -m 0644 luma_connect_sync/*.py luma_connect_sync/bip39-english.txt %{buildroot}%{python3_sitelib}/luma_connect_sync/
install -D -m 0755 bin/luma-connect-sync %{buildroot}%{_bindir}/luma-connect-sync
install -D -m 0755 bin/luma-connect-syncd %{buildroot}%{_bindir}/luma-connect-syncd
install -D -m 0644 data/luma-connect-syncd.service %{buildroot}%{_userunitdir}/luma-connect-syncd.service
install -D -m 0644 data/org.projectluma.ConnectSync1.service %{buildroot}%{_datadir}/dbus-1/services/org.projectluma.ConnectSync1.service
install -D -m 0644 data/org.projectluma.ConnectSync1.xml %{buildroot}%{_datadir}/dbus-1/interfaces/org.projectluma.ConnectSync1.xml
install -D -m 0644 data/org.projectluma.connect-sync.policy %{buildroot}%{_datadir}/polkit-1/actions/org.projectluma.connect-sync.policy
install -D -m 0644 data/sync/exclude.d/00-luma.conf %{buildroot}%{_datadir}/luma/sync/exclude.d/00-luma.conf
install -D -m 0644 data/sync/deny.d/00-self-syncing.txt %{buildroot}%{_datadir}/luma/sync/deny.d/00-self-syncing.txt
install -D -m 0644 data/sync/deny.d/10-luma-connect-services.txt %{buildroot}%{_datadir}/luma/sync/deny.d/10-luma-connect-services.txt

%check
PYTHONPATH=%{buildroot}%{python3_sitelib} %{python3} -c "import luma_connect_sync.service, luma_connect_sync.cli"
cd tests && PYTHONPATH=$PWD/..:$PWD %{python3} -m unittest discover -p "test_*.py" -v

%files
%license LICENSE.md
%doc README.md
%{_bindir}/luma-connect-sync
%{_bindir}/luma-connect-syncd
%{python3_sitelib}/luma_connect_sync/
%{_userunitdir}/luma-connect-syncd.service
%{_datadir}/dbus-1/services/org.projectluma.ConnectSync1.service
%{_datadir}/dbus-1/interfaces/org.projectluma.ConnectSync1.xml
%{_datadir}/polkit-1/actions/org.projectluma.connect-sync.policy
%{_datadir}/luma/sync/

%changelog
* Mon Nov 23 2026 Project Luma <dev@projectluma.org> - 0.2.1-1.luma.1
- Re-review fixes: approve checks and wraps from one request; capped, visible
  join answers; the joining device waits for the person's confirmation;
  replaced recovery keys are shown; late recovery joins are dropped; the
  zone generation floor covers every zone; signed requests.
* Sun Nov 22 2026 Project Luma <dev@projectluma.org> - 0.2.0-1.luma.1
- Security review fixes, wire format 2: signed membership, approvals bound to
  the joining device's keys, signed bundles with removed devices, zone key
  generation floor, owner-scoped app zones, fixed 1 MiB blocks with Padmé
  padding, sealed tombstones, keys sealed with systemd-creds --user.
* Sun Nov 22 2026 Project Luma <dev@projectluma.org> - 0.1.0-1.luma.1
- Phase 1 of ADR-054: keys, join, installed apps and closed-app state.
