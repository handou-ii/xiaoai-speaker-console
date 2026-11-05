# 小爱音响控制台（fnOS FPK）

飞牛 fnOS 上的小爱音箱控制台：装完即用，自带一个桥接容器，把小米音箱接上本地曲库和 OpenClaw。

- **本地曲库播放**：填 NAS 路径即索引入库，按歌手/专辑/歌单随机或顺序播放，进度条可拖动，音量可调。
- **唤醒词**：本地唤醒词只跑固定指令（如「播放本地」），OpenClaw 触发词整句转给 agent，两套互不干扰。
- **固定指令**：自定义说法 → 动作，支持 iOS 快捷指令与安卓 HTTP Shortcuts 直接 POST 触发。
- **OpenClaw 语音通道**：把音箱当语音入口，网关地址支持自动探测（读 `openclaw.json` 的 `gateway.port`，再探 48670/48671/18789）。
- **登录持久化**：小米登录态存在应用数据目录，serviceToken 12 小时续期一次 + 每 30 分钟探活，续期时收下整包 Set-Cookie（passToken 会被轮换），不必反复扫码。

## 目录结构

```
manifest          # 应用元信息（appname / 版本 / 端口 / 变更记录）
cmd/main          # 启停脚本：hub 进程 + 桥接容器一起启停
config/           # 权限与资源声明
wizard/           # 安装向导
ui/               # 控制台前端（8900），桌面入口从这里加载
server/hub.py     # 控制台后端（宿主 python 进程）
app/bridge/       # 桥接：aiohttp 服务（容器内 8790）
  Dockerfile      # 桥接镜像定义（基础镜像 python:3.11-slim）
  bridge/         # 业务模块：auth / player / music / router / webui ...
  web/            # 桥接页前端（8790）
app/LICENSE       # 许可条款（随代码分发）
app/NOTICE        # 第三方代码归属
tests/            # 授权轮换的桩测试
```

## 打包

需要官方的 `fnpack`：

```bash
fnpack build                 # 产物 fnoshub.fpk
```

桥接镜像不进仓库，打包前先构建并导出：

```bash
docker build -t xiaoai-bridge:latest app/bridge
docker save xiaoai-bridge:latest | gzip -6 > app/bridge/img.tar.gz
```

## 安装

```bash
appcenter-cli install-fpk fnoshub.fpk -v 1
appcenter-cli start fnoshub
```

控制台默认 8900，桥接默认 8790。已装同 appname 的应用时，本地包只能卸载重装。

## 数据位置

- 应用目录：`/vol1/@appcenter/fnoshub`
- 运行数据：`/vol1/@apphome/fnoshub`（登录态 `bridge/conf/.mi.token`、根凭据 `bridge/conf/auth.json`、配置 `bridge/conf/config.json`）
- 这些凭据只留在本机，不随安装包分发。

## 许可

个人非商业使用，详见 `app/LICENSE`；第三方代码归属见 `app/NOTICE`。
