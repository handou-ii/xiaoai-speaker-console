# 小爱 × OpenClaw 语音桥 + WebUI
#
# 不装 ffmpeg：音频时长用纯 Python 的 mutagen 读，镜像小、构建快。
# 也不装 curl：健康检查用 python 自带 urllib。

FROM python:3.11-slim

# 国内网络默认走镜像源，海外部署可覆盖：
#   docker compose build --build-arg PIP_INDEX=https://pypi.org/simple
ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -i ${PIP_INDEX} -r requirements.txt

COPY main.py mcp_server.py ./
COPY bridge/ ./bridge/
COPY web/ ./web/

# 运行期目录（由 compose 挂载覆盖，这里保证镜像单独跑也不会崩）
RUN mkdir -p /app/conf /app/media /app/logs

EXPOSE 8790

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8790/health', timeout=4).status == 200 else 1)"

CMD ["python", "main.py", "--config", "config.json"]
