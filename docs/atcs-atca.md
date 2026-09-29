# How the UMA carveout actually gets set

Reverse-engineered from the ACPI tables of an **AMD Ryzen AI Halo (RAH-001),
BIOS 03.03 (2026-06-10)**, Ryzen AI Max+ 395, 128 GB LPDDR5X. Reproduce with
`sudo ./uma-carveout.py probe`.

## The chain

```
userspace                 sysfs uma/carveout  (index)
  -> amdgpu               amdgpu_acpi_set_uma_allocation_size(adev, index, type)
  -> ACPI                 \_SB_.PCI0.GPPA.VGA_.ATCS (0x0A, buffer)
  -> ASL                  ATCA (Arg1)
  -> SMI                  CpmTriggerSmi(cmd, packed_byte, 1 ms)
  -> BIOS SMM handler     writes NVRAM; applied at next POST
```

## The kernel side

`amdgpu` hands the firmware a four-byte packed struct:

```c
struct atcs_set_uma_allocation_size_input {
    u16 size;           /* sizeof(this struct) = 4 */
    u8  uma_size_index;
    u8  uma_size_type;
} __packed;
```

`amdgpu_acpi_set_uma_allocation_size()` performs **no validation** of either
byte -- it only checks that the ATCS function is supported, then passes what it
was given straight to firmware. The bounds check lives one level up, in the
sysfs store function, which rejects any index that is not in the Atom ROM
integrated-info v2.3 option table or that lacks the `AMDGPU_UMA_FLAG_AUTO` /
`AMDGPU_UMA_FLAG_CUSTOM` flag.

That distinction is the whole point of this repo: **the restriction to
advertised sizes is a driver policy, not a firmware one.**

## The firmware side

`ATCS` dispatches function `0x0A` to `ATCA`:

```asl
If ((Arg0 == 0x0A))
{
    M000 (0x0D6A)
    ATCA (Arg1)
    M000 (0x0D7A)
}
```

and `ATCA` is four lines of arithmetic:

```asl
Method (ATCA, 1, Serialized)
{
    CreateWordField (Arg0, Zero, M157)   // bytes 0-1: struct size (ignored)
    CreateByteField (Arg0, 0x02, M23F)   // byte 2:   uma_size_index
    CreateByteField (Arg0, 0x03, M240)   // byte 3:   uma_size_type
    Local0 = ((M240 & 0x0F) << 0x04)     // type  -> high nibble
    Local0 |= (M23F & 0x0F)              // index -> low nibble
    M232 (M23A, Local0, One)             // CpmTriggerSmi(cmd, data, sleep ms)
}
```

`M232` is a generic SMI trigger: it takes a command byte and a data byte,
writes them to a two-byte `SystemIO` region, and sleeps.

```asl
Method (M232, 3, Serialized)
{
    ...
    Acquire (M230, 0xFFFF)
    OperationRegion (VARM, SystemIO, M231, 0x02)
    Field (VARM, ByteAcc, NoLock, Preserve)
    {
        VAR1,   8,
        VAR2,   8
    }
    VAR2 = Local1        // data  = packed nibbles
    VAR1 = Local0        // cmd   = M23A
    If ((Local2 > Zero)) { Sleep (Local2) }
    Release (M230)
}
```

## What this means

1. **No range check in ACPI.** `ATCA` does not compare the index against any
   table, count, or maximum. Anything the driver sends is forwarded.
2. **Four bits each.** `index` and `type` are masked with `0x0F`, so the
   encoding can express **index 0-15 and type 0-15**. A firmware that
   advertises only eight options still has room for eight more in the wire
   format.
3. **The real arbiter is the SMM handler**, which is not in the ACPI tables and
   cannot be read from the OS. Whether an unadvertised index maps to a real
   size, gets clamped, or is ignored is a property of that handler.

## Observed encoding (RAH-001, BIOS 03.03)

`uma-carveout.py trace` walks every advertised index through the supported sysfs
path while a BPF kprobe records what `amdgpu` hands to ACPI:

| sysfs index | size | ATCS index | ATCS type | packed byte |
| --- | --- | --- | --- | --- |
| 0 | 512 MB | 0 | 2 | `0x20` |
| 1 | 1 GiB | 1 | 2 | `0x21` |
| 2 | 2 GiB | 2 | 2 | `0x22` |
| 3 | 4 GiB | 3 | 2 | `0x23` |
| 4 | 8 GiB | 4 | 2 | `0x24` |
| 5 | 16 GiB | 5 | 2 | `0x25` |
| 6 | 32 GiB | 6 | 2 | `0x26` |
| 7 | 64 GiB | 7 | 2 | `0x27` |

Two things fall out of this:

1. **The ATCS index is the sysfs index.** No remapping.
2. **`type` is a platform constant, not a per-entry flag.** Every entry —
   named preset and plain size alike — is sent as type `2`. It corresponds to
   `UMASizeControlOption` from the integrated-info table, not to
   `AMDGPU_UMA_FLAG_AUTO` / `AMDGPU_UMA_FLAG_CUSTOM`; those flags gate whether
   the driver will *accept* an index, and do not reach the wire.

Independent corroboration from EFI variables on the same machine:

```
UmaCarveOutDefault       = 0x02     # matches the type byte
UmaCarveOutIndexDefault  = 0x00     # "Minimum", the documented factory default
```

So an unadvertised size, if the SMM handler has one, is `type 2` with the next
index — packed `0x28` for index 8.

## The 96 GB question

AMD's documentation and retailer material both describe the RAH-001 as
configurable to 96 GB of dedicated VRAM, reached on Windows through AMD
Software: Adrenalin Edition -> Performance -> Tuning -> Variable Graphics
Memory -> **Custom**. The Atom ROM table on BIOS 03.03 advertises nothing above
64 GB, and the Linux sysfs interface is index-only, so Linux cannot ask for it
through the supported path.

Since `ATCA` will pack index 8 without complaint, the experiment is available:

```
sudo ./uma-carveout.py trace                                  # learn the type byte
sudo ./uma-carveout.py set 96 --via atcs --index 8 --type T
```

`trace` kprobes `amdgpu_acpi_set_uma_allocation_size` and walks the advertised
indices through the supported sysfs path, so you read a known-good `type` out
of the driver rather than guessing one.

**Status: unverified.** If you try it, please open an issue with your system,
BIOS version, the index/type you used, and the resulting
`mem_info_vram_total` -- positive or negative.

## Recovery

The carveout is stored in NVRAM and applied at POST. If a value leaves the
machine unable to boot, clear CMOS (unplug, pull the coin cell). Both
`UmaCarveOutDefault` and `UmaCarveOutIndexDefault` exist as EFI variables,
which is consistent with the firmware having its own fallback.
