FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=10000
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser
COPY --chown=appuser:appuser server ./server
COPY --chown=appuser:appuser scripts/start_http.sh ./scripts/start_http.sh
USER appuser
EXPOSE 10000
CMD ["bash", "scripts/start_http.sh"]
