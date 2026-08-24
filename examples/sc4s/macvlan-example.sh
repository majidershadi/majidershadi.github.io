#!/usr/bin/env bash
# EXAMPLE ONLY. Do not run unchanged.
# Confirm switch port-security/MAC limits, VLAN design, routing,
# HEC egress, monitoring, and host-to-container communication first.

docker network create -d macvlan \
  --subnet=192.0.2.0/24 \
  --gateway=192.0.2.1 \
  -o parent=ens192 \
  sc4s_l2

# Example attachment:
# docker run --network sc4s_l2 --ip 192.0.2.50 ...
#
# Important: Linux macvlan containers cannot communicate directly
# with the host by default. See Docker's macvlan documentation.
