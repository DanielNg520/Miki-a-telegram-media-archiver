FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY miki_sorter_bot ./miki_sorter_bot

RUN pip install --no-cache-dir .

CMD ["miki-sorter"]
