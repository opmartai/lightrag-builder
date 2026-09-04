ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.source="https://github.com/opmartai/lightrag-builder"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install --timeout 120 -r /tmp/requirements.txt
COPY controller.py /app/controller.py
EXPOSE 9630
CMD ["python", "-m", "uvicorn", "controller:app", "--host", "0.0.0.0", "--port", "9630"]
