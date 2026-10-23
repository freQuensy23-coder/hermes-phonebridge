import json
import socket
import sys


def main():
    addresses = []
    for family, kind, protocol, _, address in socket.getaddrinfo(
        sys.argv[1], int(sys.argv[2]), socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP
    ):
        record = [family, kind, protocol, address]
        if record not in addresses:
            addresses.append(record)
        if len(addresses) == 16:
            break
    print(json.dumps(addresses, separators=(",", ":")))


if __name__ == "__main__":
    main()
