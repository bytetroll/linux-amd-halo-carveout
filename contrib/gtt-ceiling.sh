#!/usr/bin/env bash
# Raise the amdgpu GTT ceiling, so the iGPU can address system RAM on top of
# whatever the firmware carved out as dedicated VRAM.
#
# GTT is lent on demand rather than pinned: pages the GPU is not using stay
# available to the OS. On Linux this is often a better deal than a large
# carveout, and it needs no firmware change at all -- just two kernel params.
#
#   sudo ./gtt-ceiling.sh 32      # 32 GiB GTT ceiling
#   sudo ./gtt-ceiling.sh 96
#
# Debian/Ubuntu (GRUB). On other distros apply the same two parameters with
# your own bootloader tooling.
set -euo pipefail

GTT_GB=${1:?usage: sudo $0 <GiB>}
[[ $GTT_GB =~ ^[0-9]+$ ]] || { echo "GiB must be an integer" >&2; exit 1; }
GTT_MB=$(( GTT_GB * 1024 ))
PAGES=$(( GTT_MB * 256 ))          # 4 KiB pages
GRUB=/etc/default/grub

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
[ -f "$GRUB" ] || { echo "$GRUB not found -- not a GRUB system?" >&2; exit 1; }

cp -a "$GRUB" "${GRUB}.bak.$(date +%Y%m%d-%H%M%S)"
echo "backed up $GRUB"

python3 - "$GRUB" "amdgpu.gttsize=${GTT_MB} ttm.pages_limit=${PAGES}" <<'PY'
import re, sys
path, params = sys.argv[1], sys.argv[2]
src = open(path).read()
key = "GRUB_CMDLINE_LINUX_DEFAULT"
m = re.search(rf'^{key}="(.*)"$', src, re.M)
if not m:
    sys.exit(f"{key} not found in {path}")
kept = [w for w in m.group(1).split()
        if not w.startswith(("amdgpu.gttsize=", "ttm.pages_limit="))]
new = " ".join(kept + params.split())
open(path, "w").write(src[:m.start(1)] + new + src[m.end(1):])
print(f"  old: {m.group(1)}\n  new: {new}")
PY

update-grub
echo
echo "Reboot, then verify:"
echo "  cat /sys/class/drm/card*/device/mem_info_gtt_total"
