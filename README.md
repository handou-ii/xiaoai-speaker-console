# 小爱音响控制台（fnOS FPK）

飞牛 fnOS 上的小爱音箱控制台：装完即用，自带一个桥接容器，把小米音箱接上本地曲库和 OpenClaw。

- **本地曲库播放**：填 NAS 路径即索引入库，按歌手/专辑/歌单随机或顺序播放，音量可调。
- **唤醒词**：本地唤醒词只跑固定指令（如「播放本地」）完全由控制台执行，OpenClaw 触发词整句转给 agent，两套互不干扰。
- **固定指令**：自定义说法 → 动作，控制识别操作执行，
- **局域网快捷指令**支持 iOS 快捷指令与安卓 HTTP Shortcuts 直接 POST 触发。
- **OpenClaw 语音通道**：把音箱当语音入口，网关地址支持自动探测（读 `openclaw.json` 的 `gateway.port`，再探 48670/48671/18789）。
## 目录结构

```
manifest          # 应用元信息（appname / 版本 / 端口 / 变更记录）
cmd/main          # 启停脚本：hub 进程 + 桥接容器一起启停
docker-compose.yml # 非飞牛环境用：只起桥接（8790）
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
fnpack build                 # 产物 xiaoai-console.fpk（对外分发时改名带版本号）
```

桥接镜像不进仓库，打包前先构建并导出：

```bash
docker build -t xiaoai-bridge:latest app/bridge
docker save xiaoai-bridge:latest | gzip -6 > app/bridge/img.tar.gz
```

## 安装

```bash
appcenter-cli install-fpk xiaoai-console.fpk -v 1
appcenter-cli start xiaoai-console
```

控制台默认 8900，桥接默认 8790。

## 非飞牛环境（Docker Compose）

群晖、威联通、绿联、极空间、普通 Linux 等也可以用，但**只有桥接（8790）**：曲库索引、播放控制、固定指令、唤醒词、OpenClaw 转发都在里面。8900 控制台是飞牛套件的一部分（宿主 python 进程 + 应用框架），不包含在这份 compose 里，访问保护也只是 8900 的功能。

```bash
mkdir -p data
cp app/bridge/config.example.json data/config.json
# 改 docker-compose.yml 里的 XIAOAI_PUBLIC_BASE、XIAOAI_HOST_IP 和曲库目录
docker compose up -d --build
```

打开 `http://<这台机器的局域网地址>:8790`。

三个容易踩的点：

- `XIAOAI_PUBLIC_BASE` 必须写宿主机的局域网地址，不能写 `127.0.0.1`——音箱是按这个地址回来拉音频的，容器里的回环地址它访问不到。
- 曲库目录要**容器内外同路径**挂载（例如 `/mnt/music:/mnt/music:ro`），因为这个路径会原样填进「音乐路径」。
- `data/config.json` 必须在 `up` 之前存在，否则 Docker 会把它建成一个目录，容器起不来。

桥接本身不做访问校验，只适合放在局域网，不要映射到公网。

## 更新

装好之后不要再手工装包，用控制台右上角的「检查更新」按钮：

1. 点「检查更新」，弹窗会显示当前版本、可更新版本与安装包大小；没有新版时按钮显示「已是最新」。
2. 如果本机监视目录里已经有新包，按钮是「立即更新」，点了就装。
3. 如果只查到远端有新版本、本机没有包，按钮是「下载并更新」——这台 NAS 会自己去发布页把 fpk 取回来（弹窗里显示下载百分比），校验 appname 与版本号之后再装。
4. 安装过程自动完成停止 → 卸载 → 安装 → 启动，约 20–60 秒，页面随后自动刷新。

要手动放包也可以：把新版本 fpk 放进控制台监视的目录（默认 `<应用配置目录>/update`，额外目录写在同级的 `update.dirs`，一行一个路径），再点检查更新。

登录态、曲库索引与配置都在数据目录里，升级不会清掉。

### 从 1.5.5 及更早版本升上来（只需手动一次）

「下载并更新」是 1.5.6 才有的能力。旧版本点检查更新时，只会提示「本机没有对应安装包」，因为那份代码里没有下载逻辑——这是版本本身的限制，不是网络问题。

旧版本仍然支持「本机包一键升级」（停止 → 卸载 → 安装 → 启动 这套流程一直都在），所以只差把包放到监视目录这一步。在 NAS 上执行一条命令即可：

```bash
cd /vol1/@appconf/xiaoai-console/update   # 改过 update.dirs 的话换成你配的目录
curl -fL -o xiaoai-speaker-console-1.5.6.fpk \
  https://github.com/handou-ii/xiaoai-speaker-console/releases/download/v1.5.6/xiaoai-speaker-console-1.5.6.fpk
```

下完回控制台点「检查更新」，按钮会变成「立即更新」，点一下就走完整个升级。也可以直接下载这个 fpk，通过应用中心的「手动安装」装上。

装到 1.5.6 之后，后续版本就能一直在控制台里点按钮完成，不再需要这条命令。

**监视目录要选在只有管理员能写的位置。** 一键更新的最后一步是 `appcenter-cli install-fpk`，它会以 root 执行包内 `cmd/` 下的脚本；控制台只校验「文件在受监视目录内 / 外层 manifest 的 appname 一致 / 版本号更高」，这三条只防装错包，不防恶意包。所以真正的边界是目录权限：把 `update.dirs` 指向共享盘或多人都能写的位置，等于把 root 执行权限交给所有能写那个目录的人。控制台检测到监视目录对同组或其他用户可写时，会在更新弹窗里给出提示。

## 安全与隐私

**两个端口都不要映射到公网。** 8900（控制台）与 8790（桥接）能直接控制音箱播放、读曲库路径、读写 OpenClaw 配置，只适合放在局域网。

**访问保护**：控制台「概览 → 访问保护」可以设一个口令，设置后打开页面需要先输入。口令只存加盐哈希，登录后下发的是另一枚随机会话 token；控制台与桥接共用同一枚 token，登录一次两个端口都通。音箱取音频的三个地址（`/media/`、`/m/`、`/audio/`）不做校验，否则设备带不上 cookie 就拉不到音频——也就是说，同网段知道这些地址的人仍能取到音频。

**会离开本机的数据**：

| 内容 | 去向 |
|---|---|
| 小米账号登录态 | 小米云（MiNA 接口），同时存在本机 |
| 播报文本（走云端合成通道时） | 微软 edge-tts 接口 |
| 播报文本与语音内容（接了 OpenClaw 时） | 你配置的大模型服务 |
| 曲库与音频文件 | 不出本机，只在 8790 端口提供 |

**容器可见范围**：桥接容器只读挂载各卷下的普通一级目录（如 `/vol1/1000`），`@appdata`、`@appcenter` 这类应用数据目录不挂进来。因此加曲库路径立即生效，不用重启；只有新建的一级目录才需要重启应用一次。

## 分发与校验

- 发出去时附上安装包的 **md5**，接收方用 `md5sum` 核对，避免下载损坏或被替换。
- x86 与 ARM 是两个包，`appname` 与版本号相同，**同一台机器只能装一个**；换架构要先卸载再装。
- 跨版本升级走控制台「检查更新」，不要手工覆盖安装：同 appname 已安装时 `install-fpk` 只打印「已安装」就返回 0，不会覆盖文件。

## 数据位置

- 应用目录：`/vol1/@appcenter/xiaoai-console`
- 运行数据：`/vol1/@appdata/xiaoai-console/bridge`（登录态 `conf/.mi.token`、根凭据 `conf/auth.json`、配置 `conf/config.json`）
- 配置目录：`/vol1/@appconf/xiaoai-console`（`update.dirs`、访问口令 `access.json`）
- 上面两类目录里的凭据是**明文**的，只留在本机，不随安装包分发；卸载应用不会删除它们，要彻底清干净需手工删这两个目录。

## 免责

- 小米 MiNA / ubus 与 edge-tts 都不是公开接口，协议随时可能变更，失效后不保证及时修复。
- 用账号自动化控制音箱可能与服务条款冲突，风险由使用者自行承担。
- 本项目与小米、微软均无隶属关系。

## 许可

个人非商业使用，详见 `app/LICENSE`；第三方代码归属见 `app/NOTICE`。
