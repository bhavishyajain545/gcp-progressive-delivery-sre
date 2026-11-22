FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir google-auth==2.45.0 requests==2.32.5
COPY postmortem.py .
ENV POSTMORTEM_DIR=/tmp/postmortems
USER 65532
ENTRYPOINT ["python", "/app/postmortem.py"]
