# Same official Worker and task code; pin Docker exec-stream dependencies.
FROM python:3.10
ENV PYTHONUNBUFFERED=1
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip && \
    python -m pip install agentrl-worker==0.4.0 aiodocker==0.24.0 aiohttp==3.11.18
COPY ./src/server/tasks/os_interaction /app/src/server/tasks/os_interaction
COPY ./configs/tasks/os.yaml /app/configs/tasks/os.yaml
COPY ./data/os_interaction /app/data/os_interaction
ENTRYPOINT ["python", "-m", "agentrl.worker"]
