#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Helper script to erase device.
"""


import os
import sys
import time
import traceback


ZEROS = "\0" * 2**24


def main():
    """Main entry point."""
    try:
        device = sys.argv[1]
        fdesc = os.open(device, os.O_RDWR)
        size = os.lseek(fdesc, 0, os.SEEK_END)
        offz = os.lseek(fdesc, 0, os.SEEK_SET)
        print >>sys.stderr, "dd: Writing {size} bytes to '{device}'".format(**locals())
        t_0 = time.time()
        while offz < size:
            offz += os.write(fdesc, ZEROS)
        t_1 = time.time()
    except:  # pylint: disable=bare-except
        traceback.print_exc()
        print >>sys.stderr, "dd: FAILED"
        sys.exit(1)

    try:
        speed = size/2**20/(t_1-t_0)
    except:  # pylint: disable=bare-except
        speed = "Inf"
    print >>sys.stderr, "dd: OK (%.1f MB/s)" % speed
    sys.exit(0)


if __name__ == '__main__':
    main()
