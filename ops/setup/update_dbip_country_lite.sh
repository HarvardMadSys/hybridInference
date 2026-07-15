#!/usr/bin/env bash
# Download and atomically install the current DB-IP Country Lite MMDB database.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

release="${DBIP_COUNTRY_RELEASE:-$(date -u +%Y-%m)}"
destination="${DBIP_COUNTRY_DESTINATION:-${REPO_ROOT}/var/data/geoip/dbip-country-lite.mmdb}"
download_url="${DBIP_COUNTRY_URL:-https://download.db-ip.com/free/dbip-country-lite-${release}.mmdb.gz}"
release_marker="${destination}.release"

if [[ ! "$release" =~ ^[0-9]{4}-(0[1-9]|1[0-2])$ ]]; then
  printf '[dbip-country] Invalid release %q; expected UTC YYYY-MM.\n' "$release" >&2
  exit 1
fi

if [[ -f "$destination" && -f "$release_marker" ]] &&
  [[ "$(<"$release_marker")" == "$release" ]]; then
  printf '[dbip-country] Release %s is already installed at %s.\n' "$release" "$destination"
  exit 0
fi

destination_dir="$(dirname "$destination")"
mkdir -p "$destination_dir"

archive_tmp=""
database_tmp=""
marker_tmp=""

cleanup() {
  [[ -z "$archive_tmp" ]] || rm -f "$archive_tmp"
  [[ -z "$database_tmp" ]] || rm -f "$database_tmp"
  [[ -z "$marker_tmp" ]] || rm -f "$marker_tmp"
}
trap cleanup EXIT

archive_tmp="$(mktemp "${destination_dir}/.dbip-country.XXXXXX.mmdb.gz")"
database_tmp="$(mktemp "${destination_dir}/.dbip-country.XXXXXX.mmdb")"
marker_tmp="$(mktemp "${destination_dir}/.dbip-country.XXXXXX.release")"

printf '[dbip-country] Downloading release %s from %s.\n' "$release" "$download_url"
curl --fail --location --silent --show-error --retry 3 --output "$archive_tmp" "$download_url"
gzip --test "$archive_tmp"
gzip --decompress --stdout "$archive_tmp" >"$database_tmp"

database_size="$(wc -c <"$database_tmp")"
if ((database_size < 1048576)); then
  printf '[dbip-country] Refusing implausibly small MMDB file (%s bytes).\n' "$database_size" >&2
  exit 1
fi
if ! LC_ALL=C grep --text --quiet 'MaxMind\.com' "$database_tmp"; then
  printf '[dbip-country] Refusing file without an MMDB metadata marker.\n' >&2
  exit 1
fi

printf '%s\n' "$release" >"$marker_tmp"
chmod 0644 "$database_tmp" "$marker_tmp"

# Both temporary files live beside their targets, so each rename is atomic.
# Install the database first: a marker must never claim a release that failed
# before the usable database reached its final path.
mv -f "$database_tmp" "$destination"
mv -f "$marker_tmp" "$release_marker"

printf '[dbip-country] Installed release %s at %s.\n' "$release" "$destination"
