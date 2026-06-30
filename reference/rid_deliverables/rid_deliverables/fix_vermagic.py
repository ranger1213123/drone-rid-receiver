#!/usr/bin/env python2
import sys
with open("/home/root/8812au_drv/8812au.ko", "rb") as f:
    data = bytearray(f.read())
tag = b"vermagic=4.4.19"
idx = data.find(tag)
end = data.find(b"\x00", idx)
old = bytes(data[idx:end])
print "OLD:", old, len(old)
parts = old.decode().strip().split()
new_parts = parts[:3] + ["modversions"] + parts[3:]
new_str = (" ".join(new_parts) + " ").encode()
print "NEW:", new_str, len(new_str)
if len(new_str) <= len(old):
    new_padded = new_str + b"\x00" * (len(old) - len(new_str))
    data[idx:idx+len(old)] = new_padded
    with open("/home/root/8812au_drv/8812au_mod.ko", "wb") as f:
        f.write(data)
    print "Written OK"
else:
    # Shorter version: drop trailing space
    new_parts2 = parts[:3] + ["modversions"] + ["ARMv7", "p2v8"]
    new_str2 = (" ".join(new_parts2)).encode()
    print "ALT:", new_str2, len(new_str2)
    if len(new_str2) <= len(old):
        data[idx:idx+len(old)] = new_str2 + b"\x00" * (len(old) - len(new_str2))
        with open("/home/root/8812au_drv/8812au_mod.ko", "wb") as f:
            f.write(data)
        print "Written OK (alt)"
    else:
        print "STILL too long", len(new_str2), len(old)
