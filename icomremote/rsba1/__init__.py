"""RS-BA1 network protocol (Icom LAN remote).

The protocol is undocumented. This implementation is a Python port of the
reverse-engineered Go client kappanhang (https://github.com/nonoo/kappanhang,
MIT license, Copyright (c) 2020 Norbert Varga HA2NON, Akos Marton ES1AKOS;
passcode algorithm by W6EL). See THIRD_PARTY_NOTICES.md.

Three UDP streams, each with its own session:
    control  (radio port 50001)  login, auth, stream setup, keepalive
    serial   (radio port 50002)  CI-V frames
    audio    (radio port 50003)  PCM audio, both directions
"""
