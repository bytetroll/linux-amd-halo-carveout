#!/usr/bin/env python3
"""Read and set the AMD Strix Halo UMA (VRAM) carveout from Linux.

The carveout lives in firmware and is applied at POST, so a change here needs a
reboot but survives OS reinstalls. Two mechanisms:

  sysfs  /sys/class/drm/card*/device/uma/carveout   - supported, index-only,
         limited to the sizes the Atom ROM advertises in carveout_options.
  atcs   ACPI ATCS function 0xA, which is what the sysfs path calls underneath
         and what Adrenalin's "Custom" VGM mode uses to reach sizes the preset
         table omits. Needs acpi-call-dkms. Experimental - see `probe`.

Usage:
  uma-carveout.py list
  uma-carveout.py set 96|64|32|min        (or: set --index N)
  uma-carveout.py probe                   disassemble the firmware's ATCS method
"""

import argparse
import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

# The four steps HP exposes in their BIOS dropdown, in MB.
PRESETS = {"min": 512, "32": 32768, "64": 65536, "96": 98304}

ATCS_SET_UMA = 0xA
CALL = "/proc/acpi/call"
OPT_RE = re.compile(r"^(\d+):\s*(.*?)\s*\((\d+)\s*(MB|GB)\)\s*$")


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def gib(mb):
    return mb / 1024.0


def fmt_size(mb):
    return f"{mb} MB" if mb < 1024 else f"{gib(mb):.0f} GiB"


def find_card():
    for uma in sorted(glob.glob("/sys/class/drm/card*/device/uma")):
        if os.path.isfile(os.path.join(uma, "carveout")):
            return uma
    die("no amdgpu uma/ interface found.\n"
        "       Needs an APU whose BIOS supports ATCS 0xA and a kernel with\n"
        "       'drm/amdgpu: add UMA carveout tuning interfaces' (6.19+).")


def read_options(uma):
    opts = []
    with open(os.path.join(uma, "carveout_options")) as fh:
        for line in fh:
            m = OPT_RE.match(line)
            if not m:
                continue
            idx, name, size, unit = m.groups()
            opts.append((int(idx), name or "-", int(size) * (1024 if unit == "GB" else 1)))
    return opts


def read_current(uma):
    with open(os.path.join(uma, "carveout")) as fh:
        return int(fh.read().strip())


def total_ram_mb(uma):
    """Installed RAM = what firmware carved out + what the OS can see."""
    card = os.path.dirname(uma)
    with open(os.path.join(card, "mem_info_vram_total")) as fh:
        vram_mb = int(fh.read().strip()) // (1024 * 1024)
    with open("/proc/meminfo") as fh:
        kb = int(re.search(r"MemTotal:\s+(\d+)", fh.read()).group(1))
    return vram_mb + kb // 1024


def acpi_gpu_path(uma):
    card = os.path.dirname(uma)
    try:
        with open(os.path.join(card, "firmware_node", "path")) as fh:
            return fh.read().strip()
    except OSError:
        return None


def atcs_candidates(uma):
    """ATCS may sit on the GPU, its bridge, or the host bridge."""
    path = acpi_gpu_path(uma)
    seen, out = set(), []
    if path:
        parts = path.split(".")
        while len(parts) > 1:
            out.append(".".join(parts))
            parts.pop()
    out += ["\\_SB_.PCI0", "\\_SB_"]
    return [f"{p}.ATCS" for p in out if not (p in seen or seen.add(p))]


def cmd_list(args):
    uma = find_card()
    cur = read_current(uma)
    opts = read_options(uma)
    total = total_ram_mb(uma)
    by_mb = {mb: i for i, _, mb in opts}

    print(f"installed memory : ~{gib(total):.1f} GiB")
    print(f"acpi gpu path    : {acpi_gpu_path(uma) or 'unknown'}")
    print(f"sysfs            : {os.path.join(uma, 'carveout')}\n")
    print("  idx  name        carveout        OS sees      preset")
    print("  ---  ----------  --------------  -----------  ------")
    for idx, name, mb in opts:
        label = next((k for k, v in PRESETS.items() if v == mb), "")
        size = fmt_size(mb)
        print(f"  {'*' if idx == cur else ' '}{idx:>2}  {name:<10}  {size:<14}"
              f"  {gib(total - mb):>6.1f} GiB  {label}")
    print("\n  * = active")

    missing = {k: v for k, v in PRESETS.items() if v not in by_mb}
    if missing:
        names = ", ".join(f"{k} ({fmt_size(v)})" for k, v in missing.items())
        print(f"\nnot advertised by this firmware: {names}")
        print("  -> the sysfs path cannot select these; see `probe` and --via atcs")


def cmd_set(args):
    uma = find_card()
    opts = read_options(uma)
    cur = read_current(uma)

    if args.index is not None:
        index = args.index
        match = next((o for o in opts if o[0] == index), None)
        if match is None and args.via != "atcs":
            die(f"index {index} is not in carveout_options (0-{opts[-1][0]})")
        target_mb = match[2] if match else None
    else:
        if args.target is None:
            die("give a target: min, 32, 64, 96, or --index N")
        key = args.target.lower().removesuffix("gb").strip()
        target_mb = PRESETS.get(key)
        if target_mb is None:
            if not key.isdigit():
                die(f"unknown target {args.target!r}; expected one of "
                    f"{', '.join(PRESETS)} or --index N")
            target_mb = int(key) * 1024
        match = next((o for o in opts if o[2] == target_mb), None)
        index = match[0] if match else None

    via = args.via
    if via == "auto":
        if index is None or not match:
            die(f"{fmt_size(target_mb)} is not advertised by this firmware "
                "(see `list`).\n"
                "       To try the undocumented ATCS path instead:\n"
                "         set --via atcs --index N --type T   (run `probe` first)")
        via = "sysfs"

    if via == "sysfs":
        if index is None:
            die(f"{fmt_size(target_mb)} is not in this firmware's option table; "
                "retry with --via atcs")
        if index == cur:
            print(f"already set to index {index} "
                  f"({fmt_size(target_mb)}) - nothing to do")
            return
        node = os.path.join(uma, "carveout")
        print(f"{node}: {cur} -> {index}  ({fmt_size(target_mb)} carveout, "
              f"OS sees ~{gib(total_ram_mb(uma) - target_mb):.1f} GiB)")
        if args.dry_run:
            print("(dry run)")
            return
        require_root()
        try:
            with open(node, "w") as fh:
                fh.write(str(index))
        except OSError as e:
            die(f"write failed: {e}")
        back = read_current(uma)
        if back != index:
            die(f"firmware rejected it (reads back {back})")
        print("ok - reboot to apply")
        return

    # --- ATCS path -------------------------------------------------------
    if args.type is None:
        die("--via atcs needs --type; the encoding is firmware-specific and\n"
            "       AMD has not documented it. Run `probe` first to read how\n"
            "       your BIOS implements ATCS function 0xA.")
    if index is None:
        die("--via atcs needs --index (the firmware's own size index)")
    atcs_call(uma, index, args.type, args.dry_run)


def require_root():
    if os.geteuid() != 0:
        die("must run as root")


def atcs_call(uma, index, type_, dry_run):
    # struct atcs_set_uma_allocation_size_input {
    #     u16 size; u8 uma_size_index; u8 uma_size_type; } __packed;
    buf = f"b0x04,0x00,{index:#04x},{type_:#04x}"
    print(f"ATCS function {ATCS_SET_UMA:#x}  index={index} type={type_}")
    print(f"  buffer: {buf}")
    if dry_run:
        print("(dry run)")
        return
    require_root()
    if not os.path.exists(CALL):
        die(f"{CALL} missing. Install and load acpi_call:\n"
            "       sudo apt install acpi-call-dkms && sudo modprobe acpi_call")

    errors = []
    for path in atcs_candidates(uma):
        try:
            with open(CALL, "w") as fh:
                fh.write(f"{path} {ATCS_SET_UMA:#x} {buf}")
            with open(CALL) as fh:
                res = fh.read().strip("\0\n ")
        except OSError as e:
            errors.append(f"{path}: {e}")
            continue
        if res.lower().startswith("error"):
            errors.append(f"{path}: {res}")
            continue
        print(f"  {path} -> {res}")
        print("ok - reboot, then check mem_info_vram_total")
        return
    print("no ATCS method responded:", file=sys.stderr)
    for e in errors:
        print(f"  {e}", file=sys.stderr)
    sys.exit(1)


NAME_RE = re.compile(r"\b([A-Z_][A-Z0-9_]{3})\b")
# ASL debug/logging helpers and namespace fragments - never worth following.
SKIP = {"M000", "M460", "ATCS", "ARG0", "ARG1", "ZERO", "PCI0", "GPPA", "VGA_",
        "SB__", "TRUE", "ELSE"}


def disassemble_all():
    """iasl -d every DSDT/SSDT, return {table_name: asl_text}."""
    out = {}
    tables = sorted(glob.glob("/sys/firmware/acpi/tables/DSDT") +
                    glob.glob("/sys/firmware/acpi/tables/SSDT*"))
    tmp = tempfile.mkdtemp(prefix="uma-acpi-")
    try:
        for t in tables:
            dst = os.path.join(tmp, os.path.basename(t) + ".dat")
            shutil.copyfile(t, dst)
            subprocess.run(["iasl", "-d", dst], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
            dsl = dst[:-4] + ".dsl"
            if os.path.exists(dsl):
                out[os.path.basename(t)] = open(dsl, errors="replace").read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def extract_blocks(text, kind, name):
    """Pull out `<kind> (NAME ...) { ... }` bodies by brace matching."""
    out = []
    for m in re.finditer(rf"{kind}\s*\(\s*{re.escape(name)}\b", text):
        i = text.find("{", m.end())
        if i < 0:
            continue
        depth, j = 0, i
        while j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(text[m.start():j + 1])
    return out


def referenced_names(block):
    return {n for n in NAME_RE.findall(block) if n not in SKIP}


def show_definitions(tables, name, limit=8):
    """For a name that is not a Method, show where it is declared."""
    pat = re.compile(rf"(Name|OperationRegion|CreateByteField|CreateWordField|"
                     rf"CreateDWordField|CreateField|External)\s*\(\s*{name}\b")
    field = re.compile(rf"^\s*{name}\s*,\s*\d+")
    shown = 0
    for tbl, text in tables.items():
        for line in text.splitlines():
            if pat.search(line) or field.match(line):
                print(f"    {tbl}: {line.strip()}")
                shown += 1
                if shown >= limit:
                    return shown
    return shown


def cmd_probe(args):
    uma = find_card()
    if not shutil.which("iasl"):
        die("iasl not found. sudo apt install acpica-tools")
    if os.geteuid() != 0:
        die("must run as root (ACPI tables are root-only)")

    print("candidate ATCS paths, in the order `set --via atcs` tries them:")
    for path in atcs_candidates(uma):
        print(f"  {path}")
    print()

    tables = disassemble_all()
    if not tables:
        die("could not disassemble any ACPI table")

    wanted = list(args.method) if args.method else ["ATCA"]
    queue = [(n, 0) for n in wanted]
    seen, missing = set(), []
    while queue:
        name, level = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        blocks = [(tbl, b) for tbl, text in tables.items()
                  for b in extract_blocks(text, "Method", name)]
        if not blocks:
            missing.append(name)
            continue
        for tbl, block in blocks:
            print(f"=== {tbl}: Method {name} "
                  f"{'(called by ' + str(level) + ' level up)' if level else ''}===")
            print(block)
            print()
            if level < args.depth:
                for ref in sorted(referenced_names(block) - seen):
                    queue.append((ref, level + 1))

    if missing:
        print("referenced but not methods (data, fields or registers):")
        for name in missing:
            print(f"  {name}")
            if not show_definitions(tables, name):
                print("    (no declaration found in DSDT/SSDT)")
        print()

    print("What to look for: ATCA receives the 4-byte buffer, so byte [0x02] is\n"
          "uma_size_index and byte [0x03] is uma_size_type. Note any comparison\n"
          "that bounds the index, and which NVRAM field the value is written to.")


TRACE = "/sys/kernel/tracing"
KPROBE_SYM = "amdgpu_acpi_set_uma_allocation_size"
KPROBE_NAME = "umaset"
HIT_RE = re.compile(r"index=(\d+)\s+type=(\d+)")
# bpftrace says "Attaching 1 probe..." on older builds, "Attached 1 probe" on newer.
READY_RE = re.compile(r"Attach(?:ing|ed)\b", re.I)


def _w(path, data, mode="w"):
    with open(os.path.join(TRACE, path), mode) as fh:
        fh.write(data)


def _r(path):
    with open(os.path.join(TRACE, path)) as fh:
        return fh.read()


def read_error_log(lines=3):
    try:
        text = _r("error_log").strip()
    except OSError:
        return ""
    if not text:
        return "       (tracefs error_log was empty)"
    return "\n".join(f"       {ln}" for ln in text.splitlines()[-lines:])


def kprobe_target(sym):
    """Both kprobe_events and bpftrace accept `module:symbol`."""
    try:
        with open("/proc/kallsyms") as fh:
            for line in fh:
                f = line.split()
                if len(f) >= 3 and f[2] == sym:
                    if len(f) >= 4 and f[3].startswith("["):
                        return f"{f[3].strip('[]')}:{sym}"
                    return sym
    except OSError:
        pass
    return sym


def notrace_gate_closed():
    """Ubuntu leaves CONFIG_KPROBE_EVENTS_ON_NOTRACE unset, which makes
    kprobe_events answer -EINVAL for any function ftrace cannot see -- and with
    nothing in error_log, since the refusal precedes the argument parser. BPF
    kprobes register by another path and are not subject to that check."""
    try:
        with open(f"/boot/config-{os.uname().release}") as fh:
            return "CONFIG_KPROBE_EVENTS_ON_NOTRACE=y" not in fh.read()
    except OSError:
        return False


def _wait_hits(path, seen, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        hits = HIT_RE.findall(open(path, errors="replace").read())
        if len(hits) > seen:
            return hits
        time.sleep(0.1)
    return HIT_RE.findall(open(path, errors="replace").read())


def _sweep(indices, node, emit, settle=0.1):
    """Write each index through sysfs, pairing it with the captured call."""
    rows, seen = [], 0
    for idx in indices:
        try:
            with open(node, "w") as fh:
                fh.write(str(idx))
        except OSError as e:
            rows.append((idx, None, None, f"write rejected: {e}"))
            continue
        hits = emit(seen)
        if len(hits) > seen:
            aidx, atype = hits[seen]
            rows.append((idx, int(aidx), int(atype), ""))
            seen = len(hits)
        else:
            rows.append((idx, None, None, "no ATCS call observed"))
        time.sleep(settle)
    return rows


def collect_bpftrace(indices, node):
    target = kprobe_target(KPROBE_SYM)
    prog = f'kprobe:{target} {{ printf("index=%d type=%d\\n", arg1, arg2); }}'
    tmp = tempfile.mkdtemp(prefix="uma-trace-")
    out = os.path.join(tmp, "bpftrace.out")
    fh = open(out, "w")
    proc = subprocess.Popen(["bpftrace", "-B", "none", "-e", prog],
                            stdout=fh, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 20
        while True:
            text = open(out, errors="replace").read()
            if READY_RE.search(text):
                break
            if proc.poll() is not None:
                die("bpftrace exited before attaching:\n" + text.strip())
            if time.time() > deadline:
                # Banner wording has changed before; if it is still running,
                # assume it attached rather than giving up on a cosmetic string.
                print("warning: no recognised bpftrace attach banner; proceeding\n"
                      f"         output so far: {text.strip()!r}", file=sys.stderr)
                break
            time.sleep(0.2)
        time.sleep(0.3)
        print(f"backend: bpftrace   probe: kprobe:{target}\n")
        return _sweep(indices, node, lambda seen: _wait_hits(out, seen, 3.0))
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        fh.close()
        shutil.rmtree(tmp, ignore_errors=True)


def probe_candidates():
    tgt = kprobe_target(KPROBE_SYM)
    targets = [tgt] if tgt == KPROBE_SYM else [tgt, KPROBE_SYM]
    argsets = ["index=$arg2:u8 type=$arg3:u8", "index=%si:u8 type=%dx:u8"]
    return [f"p:{KPROBE_NAME} {t} {a}" for a in argsets for t in targets]


def collect_kprobe(indices, node):
    if not os.path.isdir(TRACE):
        die(f"{TRACE} missing (tracefs not mounted?)")
    try:
        _w("kprobe_events", f"-:{KPROBE_NAME}\n", "a")
    except OSError:
        pass

    probe, failures = None, []
    for cand in probe_candidates():
        try:
            _w("kprobe_events", cand + "\n", "a")
            probe = cand
            break
        except OSError as e:
            failures.append((cand, e, read_error_log()))
    if probe is None:
        print("could not install a kprobe. tried:", file=sys.stderr)
        for cand, err, log in failures:
            print(f"  {cand}\n    -> {err}", file=sys.stderr)
            if log:
                print(log, file=sys.stderr)
        if notrace_gate_closed():
            print("\nCONFIG_KPROBE_EVENTS_ON_NOTRACE is not set on this kernel, so\n"
                  "kprobe_events refuses any function ftrace cannot see. Use the\n"
                  "bpftrace backend, which is not subject to that check:\n"
                  "  sudo apt install bpftrace && ... trace --backend bpftrace",
                  file=sys.stderr)
        sys.exit(1)

    print(f"backend: kprobe_events   probe: {probe}\n")
    try:
        _w(f"events/kprobes/{KPROBE_NAME}/enable", "1")
        trace_file = os.path.join(TRACE, "trace")
        return _sweep(indices, node,
                      lambda seen: _wait_hits(trace_file, seen, 1.5))
    finally:
        try:
            _w(f"events/kprobes/{KPROBE_NAME}/enable", "0")
            _w("kprobe_events", f"-:{KPROBE_NAME}\n", "a")
        except OSError:
            pass


def cmd_trace(args):
    """Watch what index/type the driver actually sends for known-good entries.

    Every write goes through the supported sysfs path, so the firmware only ever
    sees values it advertised itself. The original setting is restored at the end.
    """
    uma = find_card()
    require_root()

    opts = read_options(uma)
    orig = read_current(uma)
    indices = args.index if args.index else [i for i, _, _ in opts]
    advertised = {o[0] for o in opts}
    unknown = [i for i in indices if i not in advertised]
    if unknown:
        die(f"index {unknown[0]} is not advertised; trace only uses safe values")

    backend = args.backend
    if backend == "auto":
        backend = "bpftrace" if shutil.which("bpftrace") else "kprobe"
    if backend == "bpftrace" and not shutil.which("bpftrace"):
        die("bpftrace not found. sudo apt install bpftrace")

    node = os.path.join(uma, "carveout")
    sizes = {i: mb for i, _, mb in opts}
    names = {i: n for i, n, _ in opts}
    collect = collect_bpftrace if backend == "bpftrace" else collect_kprobe

    rows = []
    try:
        rows = collect(indices, node)
    finally:
        try:
            with open(node, "w") as fh:
                fh.write(str(orig))
            back = read_current(uma)
            print(f"\nrestored carveout index {back} "
                  f"({fmt_size(sizes.get(back, 0))})\n")
        except OSError as e:
            print(f"WARNING: could not restore index {orig}: {e}", file=sys.stderr)

    print("  sysfs  name        size       -> ATCS index  type  packed byte")
    print("  -----  ----------  ---------     ----------  ----  -----------")
    for idx, aidx, atype, note in rows:
        size = fmt_size(sizes.get(idx, 0))
        if aidx is None:
            print(f"  {idx:>5}  {names.get(idx, '-'):<10}  {size:<9}     {note}")
            continue
        packed = ((atype & 0x0F) << 4) | (aidx & 0x0F)
        print(f"  {idx:>5}  {names.get(idx, '-'):<10}  {size:<9}     "
              f"{aidx:>10}  {atype:>4}  {packed:#04x}")

    seen_types = sorted({t for _, _, t, _ in rows if t is not None})
    if seen_types:
        print(f"\ntype values observed: {seen_types}")
        if len(seen_types) > 1:
            print("  more than one -> the named presets and the plain sizes are")
            print("  sent with different types; that is the AUTO/CUSTOM split.")
        print("\nto try 96 GiB (index 8, which ATCA packs without complaint):")
        for t in seen_types:
            print(f"  sudo {sys.argv[0]} set 96 --via atcs --index 8 --type {t}")


def main():
    ap = argparse.ArgumentParser(
        description="Read and set the AMD Strix Halo UMA (VRAM) carveout.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  uma-carveout.py list\n"
               "  sudo uma-carveout.py set 96\n"
               "  sudo uma-carveout.py set 64 --dry-run\n"
               "  sudo uma-carveout.py probe\n")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="show firmware options and the active one")

    s = sub.add_parser("set", help="change the carveout (applies on next boot)")
    s.add_argument("target", nargs="?", help="min, 32, 64, 96, or NN (GiB)")
    s.add_argument("--index", type=int, help="write a raw option index instead")
    s.add_argument("--via", choices=("auto", "sysfs", "atcs"), default="auto")
    s.add_argument("--type", type=int, help="ATCS uma_size_type (see probe)")
    s.add_argument("--dry-run", action="store_true")

    tr = sub.add_parser("trace", help="observe the index/type the driver sends")
    tr.add_argument("--index", type=int, action="append",
                    help="only trace these advertised indices (repeatable)")
    tr.add_argument("--backend", choices=("auto", "bpftrace", "kprobe"),
                    default="auto",
                    help="how to capture the call (default: auto)")

    pr = sub.add_parser("probe", help="disassemble the firmware's ATCS method")
    pr.add_argument("--method", action="append", metavar="NAME",
                    help="ASL method to dump; repeatable (default: ATCA)")
    pr.add_argument("--depth", type=int, default=1,
                    help="levels of called methods to follow (default 1)")

    args = ap.parse_args()
    {"list": cmd_list, "set": cmd_set, "probe": cmd_probe,
     "trace": cmd_trace}[args.cmd or "list"](args)


if __name__ == "__main__":
    main()
