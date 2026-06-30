#!/usr/bin/env python2
import sys
with open("/home/root/8812au_drv/8812au.ko", "rb") as f:
    data = bytearray(f.read())
tag = b"vermagic=4.4.19"
idx = data.find(tag)
end = data.find(b"\x00", idx)
old = bytes(data[idx:end])
print "OLD:", repr(old), len(old)
# vermagic=4.4.19-g5898894eec preempt mod_unload ARMv7 p2v8  (58)
# Target must be 58 or fewer bytes
# Options:
# 1. Abbreviate: "preempt" -> "PREEMPT" is same len, no help
# 2. Replace "preempt" -> "prempt" save 1
# 3. "ARMv7 p2v8 " -> "ARMv7" save 4 (drop p2v8)
# 4. "4.4.19-g5898894eec" -> keep as is (needed)
# Strategy: drop p2v8 (ARMv7 implies it), shorten ARMv7->armv7
# "vermagic=4.4.19-g5898894eec preempt mod_unload modversions ARMv7 "
parts = ["vermagic=4.4.19-g5898894eec", "preempt", "mod_unload", "modversions", "ARMv7"]
new_str = " ".join(parts)
print "NEW:", repr(new_str), len(new_str)
if len(new_str) <= len(old):
    with open("/home/root/8812au_drv/8812au_mod.ko", "wb") as f:
        f.write(data[:idx])
        f.write(new_str.encode())
        f.write(b"\x00" * (len(old) - len(new_str)))
        f.write(data[idx+len(old):])
    print "Written OK"
else:
    print "Still too long:", len(new_str)
    # Try even shorter
    parts2 = ["vermagic=4.4.19-g5898894eec", "preempt", "modvers", "ARMv7"]
    new_str2 = " ".join(parts2)
    print "ALT2:", repr(new_str2), len(new_str2)
    if len(new_str2) <= len(old):
        with open("/home/root/8812au_drv/8812au_mod.ko", "wb") as f:
            f.write(data[:idx])
            f.write(new_str2.encode())
            f.write(b"\x00" * (len(old) - len(new_str2)))
            f.write(data[idx+len(old):])
        print "Written OK (alt2)"
    else:
        print "Still too long:", len(new_str2)
