#!/bin/sh
# udhcpc hook for a long-running client. It configures the leased address, so unicast renewals
# work as on a real client, and logs each event to /var/log/dhcp-events.
echo "$1 ip=$ip lease=$lease serverid=$serverid" >> /var/log/dhcp-events
case "$1" in
    deconfig)
        ip -4 addr flush dev eth0 label eth0:dhcp
        ;;
    bound | renew)
        ip -4 addr flush dev eth0 label eth0:dhcp
        ip addr add "$ip/$mask" dev eth0 label eth0:dhcp
        ;;
esac
