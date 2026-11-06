FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY tg_control ./tg_control
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home gateway
USER gateway
ENTRYPOINT ["tg-control"]
CMD ["demo"]
