"""Local-only device enrollment and revocation. Run python -m mobile.device_admin."""
import argparse
from .auth import Devices


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['enroll', 'list', 'revoke'])
    parser.add_argument('device', nargs='?')
    args = parser.parse_args()
    devices = Devices()
    if args.action == 'enroll':
        print('One-use enrollment code (valid 10 minutes):', devices.code())
    elif args.action == 'list':
        for row in devices.list():
            print(*row, sep='\t')
    elif not args.device:
        parser.error('revoke requires a device ID')
    else:
        print('Revoked:', devices.revoke(args.device))


if __name__ == '__main__':
    main()
