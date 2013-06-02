#!/usr/bin/env python

import os
import sys
import time
import traceback

zeros = "\0" * 2**24

try:
    device = sys.argv[1]
    fd = os.open(device, os.O_RDWR)
    size = os.lseek(fd, 0, os.SEEK_END)
    offz = os.lseek(fd, 0, os.SEEK_SET)
    print >>sys.stderr, "dd: Writing {size} bytes to '{device}'".format(**locals())
    t0 = time.time()
    while offz < size:
        offz += os.write(fd, zeros)
    t1 = time.time()
except:
    traceback.print_exc()
    print >>sys.stderr, "dd: FAILED"
    sys.exit(1)

try:
    speed = size/2**20/(t1-t0)
except:
    speed = "Inf"
print >>sys.stderr, "dd: OK (%.1f MB/s)" % speed
sys.exit(0)
