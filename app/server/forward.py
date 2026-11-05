"""把 docker 网桥网关上的端口，转发到宿主机回环地址。

为什么需要它：
  小爱桥接容器跑在独立网络里（这样飞牛 Docker 页才会显示 8790 端口入口），
  但 OpenClaw 网关只监听 127.0.0.1:48670，容器内连不到宿主机回环。
  于是由应用（root）在 docker0 网关地址上监听同一端口，转发到 127.0.0.1，
  容器就能用 http://host.docker.internal:48670 访问到网关。

用法：
  python3 forward.py --listen 172.17.0.1 --port 48670 --target 127.0.0.1 --target-port 48670
"""
import argparse
import asyncio
import logging
import sys


async def _pump(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _handle(client_reader, client_writer, target_host, target_port):
    try:
        remote_reader, remote_writer = await asyncio.open_connection(target_host, target_port)
    except Exception as e:
        log.warning("目标不可达 %s:%s (%s)", target_host, target_port, e)
        try:
            client_writer.close()
        except Exception:
            pass
        return

    await asyncio.gather(
        _pump(client_reader, remote_writer),
        _pump(remote_reader, client_writer),
        return_exceptions=True,
    )


async def main_async(args):
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, args.target, args.target_port),
        host=args.listen,
        port=args.port,
    )
    log.info("转发已启动 %s:%s -> %s:%s", args.listen, args.port, args.target, args.target_port)
    async with server:
        await server.serve_forever()


def main():
    global log
    p = argparse.ArgumentParser(description="TCP 端口转发")
    p.add_argument("--listen", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--target", default="127.0.0.1")
    p.add_argument("--target-port", type=int, default=None)
    args = p.parse_args()
    if args.target_port is None:
        args.target_port = args.port

    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s forward  %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("forward")

    try:
        asyncio.run(main_async(args))
    except OSError as e:
        # 端口被占（比如已经有转发器在跑）就让出，不拖垮应用启动
        log.warning("监听 %s:%s 失败：%s", args.listen, args.port, e)
        sys.exit(0)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
