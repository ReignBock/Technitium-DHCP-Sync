#!/bin/sh
# udhcpc hook: print the lease instead of configuring the interface.
if [ "$1" = "bound" ]; then
    echo "ip=$ip lease=$lease serverid=$serverid"
fi
