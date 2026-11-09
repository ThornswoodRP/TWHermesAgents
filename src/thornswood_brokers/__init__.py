"""The Thornswood brokers: tw-policy, tw-git, tw-files and the admission proxy.

Each runs as its own Linux user under systemd (systemd/ in this repo), from a
root-owned copy of the merged commit (install_root in policy.yaml). The Bots
reach them only through Unix sockets whose peers are checked by user on every
connection. The rules each broker enforces are the guard's (thornswood_guard),
so a rule is written once and tested once.

Standard library only: the brokers run on the VM's system python3.
"""
