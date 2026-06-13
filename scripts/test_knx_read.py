import asyncio
from xknx import XKNX
from xknx.core.value_reader import ValueReader
from xknx.io import ConnectionConfig, ConnectionType
from xknx.telegram.address import parse_device_group_address


async def try_read(host, port, route_back, label):
    print("\n=== %s ===" % label)
    print("  %s:%s route_back=%s" % (host, port, route_back))
    xknx = XKNX(
        connection_config=ConnectionConfig(
            connection_type=ConnectionType.TUNNELING,
            gateway_ip=host,
            gateway_port=port,
            route_back=route_back,
        )
    )
    ga = parse_device_group_address("0/0/2")
    try:
        await xknx.start()
        print("  tunnel: OK")
        r = await ValueReader(xknx, ga, timeout_in_seconds=15).read()
        if r is None:
            print("  group read 0/0/2: TIMEOUT")
            return False
        print("  group read 0/0/2: OK")
        return True
    except Exception as e:
        print("  FAIL:", e)
        return False
    finally:
        try:
            await xknx.stop()
        except Exception:
            pass


async def main():
    a = await try_read("127.0.0.1", 3671, False, "Test A localhost")
    b = await try_read("192.168.8.115", 3672, False, "Test B socat 3672")
    print("\n--- Summary ---")
    print("  A:", "OK" if a else "FAIL")
    print("  B:", "OK" if b else "FAIL")


asyncio.run(main())
